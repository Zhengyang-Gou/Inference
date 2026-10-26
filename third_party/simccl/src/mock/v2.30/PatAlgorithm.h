/**
 * PAT Algorithm State Machines (Extracted from NCCL 2.30)
 *
 * Contains PatAGAlgorithm (AllGather) and PatRSAlgorithm (ReduceScatter)
 * for generating accurate PAT FlowModels in SimCCL.
 *
 * Source: nccl-2.30/src/include/collectives.h
 *   - PatAGAlgorithm: L701-870
 *   - PatRSAlgorithm: L442-698
 *
 * Modifications from NCCL original:
 *   - Removed __device__ / __host__ qualifiers
 *   - Removed CUDA atomics (ps->flags assignment)
 *   - Removed __forceinline__
 *   - Added host-only bit manipulation stubs
 */
#ifndef PAT_ALGORITHM_H
#define PAT_ALGORITHM_H

#include <cstdint>
#include <cstdlib>
#include <algorithm>

// === Bit manipulation stubs (host-only) ===
#ifndef COMPILER_FFS
#define COMPILER_FFS(x) __builtin_ffs(x)
#endif
#ifndef COMPILER_CLZ
#define COMPILER_CLZ(x) __builtin_clz(x)
#endif
#ifndef COMPILER_CLZL
#define COMPILER_CLZL(x) __builtin_clzl(x)
#endif
#ifndef COMPILER_CLZLL
#define COMPILER_CLZLL(x) __builtin_clzll(x)
#endif
#ifndef COMPILER_POPCOUNT32
#define COMPILER_POPCOUNT32(x) __builtin_popcount(x)
#endif

// log2Up from NCCL bitops.h
template <typename Int>
static int log2Up(Int x) {
  int w, n;
  if (x != 0) x -= 1;
  if (x == 0) return 0;
  if (sizeof(Int) <= sizeof(unsigned int)) {
    w = 8 * sizeof(unsigned int);
    n = COMPILER_CLZ((unsigned int)x);
  } else if (sizeof(Int) <= sizeof(unsigned long)) {
    w = 8 * sizeof(unsigned long);
    n = COMPILER_CLZL((unsigned long)x);
  } else {
    w = 8 * sizeof(unsigned long long);
    n = COMPILER_CLZLL((unsigned long long)x);
  }
  return w - n;
}

// ncclPatStep struct from collectives.h
struct ncclPatStep {
  int recvDim, sendDim, recvOffset, sendOffset, stepOffset, postRecv, postSend, nelem, last, flags;
  size_t inpIx, outIx;
};

static constexpr int PatUsed = 0x1, PatSkipped = 0x2;

// ============================================================
// PatAGAlgorithm (AllGather PAT)
// Source: nccl-2.30/src/include/collectives.h L701-870
// ============================================================
template <typename T>
class PatAGAlgorithm {
  size_t offset;
  size_t end;
  size_t count;
  int chunkCount;
  int nelem;
  int rank;
  int nranks;
  int nrPow2;
  int postFreq;
  int lastA;
  int parallelFactor;
  int aggFactor;
  int as;
  int a;
  int aggDelta;
  int scale;
  int phase;

  int asDim;
  int v;
  int bitCount[32];
  int bitZeroStep[32];

  ssize_t min(ssize_t a, ssize_t b) { return (a < b) ? a : b; }
  int getNelem() { return min(chunkCount, (ssize_t)(end - offset)); }

  int mirror(int i, int max) {
    int ret = 0;
    for (int mask = 1, imask = max / 2; mask < max; mask <<= 1, imask >>= 1) {
      if ((i & mask)) ret += imask;
    }
    return ret;
  }

  int firstBitSet(int i, int max) {
    int ffs = COMPILER_FFS(i);
    return ffs ? ffs - 1 : max;
  }

  void resetA() {
    a = 0;
    lastA = aggFactor;
    if (phase >= 2) lastA /= 2 * scale;
  }

  void reset() {
    nelem = getNelem();
    scale = aggFactor / 2;
    phase = scale ? 2 : 1;
    v = 0;
    for (int i = 0; i < asDim; i++) {
      bitCount[i] = asDim - i;
      bitZeroStep[i] = 1;
    }
    as = nextAs();
    resetA();
  }

  int nextAs() {
    for (int d = 0; d < asDim; d++) {
      int p = 1 << d;
      bitCount[d]--;
      if (bitCount[d] == 0) {
        v ^= p;
        bitCount[d] = p;
        if ((v & p) == 0) {
          bitCount[d] += firstBitSet(bitZeroStep[d], asDim) - 1;
          if (bitCount[d] == 0) {
            v ^= p;
            bitCount[d] = p;
          }
          bitZeroStep[d]++;
        }
      }
    }
    return v;
  }

public:
  PatAGAlgorithm(int stepSize, int stepDepth, int maxParallelFactor, size_t offset, size_t end,
                 size_t count, int chunkCount, int rank, int nranks)
    : offset(offset), end(end), count(count), chunkCount(chunkCount), rank(rank), nranks(nranks) {
    parallelFactor = maxParallelFactor;
    aggDelta = nrPow2 = (1 << log2Up(nranks));

    aggFactor = 1;
    size_t channelSize = end - offset;
    while (stepSize / (channelSize * sizeof(T) * aggFactor) >= 2 && aggFactor < nranks / 2) {
      aggFactor *= 2;
      aggDelta /= 2;
    }
    postFreq = aggFactor;
    if (postFreq < parallelFactor) parallelFactor = postFreq;
    int d = stepDepth;
    while (d > 1 && aggFactor < nranks / 2) {
      d /= 2;
      aggFactor *= 2;
      aggDelta /= 2;
    }
    asDim = log2Up(aggDelta);
    reset();
  }

  int getParallelFactor() { return parallelFactor; }

  void getNextOp(struct ncclPatStep* ps) {
    ps->last = 0;
    ps->nelem = nelem;
    ps->inpIx = offset;
    int skip = 0;
    if (a >= lastA) {
      skip = 1;
    } else if (phase == 0) {
      int s = a * aggDelta + as;
      if (s >= nranks) skip = 1;
      int recvDataRank = (rank + s) % nranks;
      ps->outIx = recvDataRank * count + offset;
      ps->sendDim = -1;
      ps->recvDim = 0;
      ps->inpIx = 0;
      ps->sendOffset = -1;
      ps->recvOffset = (a % postFreq) * nelem;
      ps->stepOffset = 0;
      ps->postRecv = (a % postFreq == postFreq - 1) || ((a + 1) * aggDelta + as >= nranks) ? 1 : 0;
      ps->postSend = 0;
    } else if (phase == 1) {
      int s = a * aggDelta + as;
      if (s >= nranks) skip = 1;
      ps->sendDim = firstBitSet(s, nrPow2);
      int sMinusDim = s - (1 << ps->sendDim);
      int sendDataRank = (rank + nranks + sMinusDim) % nranks;
      ps->outIx = sendDataRank * count + offset;
      ps->recvDim = sMinusDim ? firstBitSet(sMinusDim, nrPow2) : -1;
      ps->sendOffset = ps->recvOffset = (a % postFreq) * nelem;
      ps->postSend = (a % postFreq == postFreq - 1) || ((a + 1) * aggDelta + as >= nranks) ? 1 : 0;
      ps->postRecv =
        (ps->sendDim == 0) && ((a % postFreq == postFreq - 1) || ((a + 1) * aggDelta + as - 1 >= nranks)) ? 1 : 0;
      ps->stepOffset = (ps->sendDim == 0) ? 0 : a / postFreq;
      if (ps->recvDim == -1) {
        ps->recvOffset = -1;
        ps->postRecv = 0;
      } else if (as - (1 << ps->sendDim) == 0) {
        int foffset = (a * aggDelta) >> (ps->recvDim + 1);
        ps->recvOffset = (foffset % postFreq) * nelem;
        ps->postRecv = (ps->sendDim == 0) && ((foffset % postFreq == postFreq - 1) ||
                                              ((((foffset + 1) * 2) + 1) << ps->recvDim) >= nranks) ? 1 : 0;
        ps->stepOffset = (ps->sendDim == 0) ? 0 : foffset / postFreq;
      }
      if (sMinusDim < nranks && ps->sendDim == 0 && skip) {
        ps->sendDim = -1;
        ps->sendOffset = -1;
        ps->postSend = 0;
        skip = 0;
      }
    } else { // phase >= 2
      int s = (2 * a + 1) * scale * aggDelta + as; // Corrected from mirror form
      if (s >= nranks) skip = 1;
      ps->sendDim = firstBitSet(s, nrPow2);
      int sMinusDim = s - (1 << ps->sendDim);
      int sendDataRank = (rank + nranks + sMinusDim) % nranks;
      ps->outIx = sendDataRank * count + offset;
      ps->recvDim = sMinusDim ? firstBitSet(sMinusDim, nrPow2) : -1;
      ps->sendOffset = ps->recvOffset = (a % postFreq) * nelem;
      ps->postSend = (a % postFreq == postFreq - 1) || ((a + 1) * aggDelta + as >= nranks / (2 * scale)) ? 1 : 0;
      ps->postRecv = 0;
      ps->stepOffset = (ps->sendDim == 0) ? 0 : a / postFreq;
      if (ps->recvDim == -1) {
        ps->recvOffset = -1;
      } else if (as - (1 << (ps->sendDim - log2Up(2 * scale))) == 0) {
        int foffset = (a * aggDelta) >> (ps->recvDim + 1 - log2Up(2 * scale));
        ps->recvOffset = (foffset % postFreq) * nelem;
        ps->stepOffset = (ps->sendDim == 0) ? 0 : foffset / postFreq;
      }
    }

    // Advance state
    a++;
    if (a >= lastA) {
      if (phase == 0) {
        ps->last = skip ? 1 : 2;
      } else if (phase == 1) {
        phase = 0;
        resetA();
      } else {
        scale /= 2;
        if (scale == 0) {
          phase = 1;
        }
        resetA();
      }
      if (!skip && phase > 0) {
        as = nextAs();
        resetA();
      }
    }

    ps->flags = PatUsed | (skip ? PatSkipped : 0);
  }
};

// ============================================================
// PatRSAlgorithm (ReduceScatter PAT)
// Source: nccl-2.30/src/include/collectives.h L442-698
// ============================================================
template <typename T>
class PatRSAlgorithm {
  size_t offset;
  size_t end;
  size_t count;
  int chunkCount;
  int nelem;
  int rank;
  int nranks;
  int nrPow2;
  int postFreq;
  int lastA;
  int parallelFactor;
  int aggFactor;
  int as;
  int a;
  int sendSkipped;
  int stepOffset;
  int aggDelta;
  int scale;
  int phase;

  ssize_t min(ssize_t a, ssize_t b) { return (a < b) ? a : b; }
  int getNelem() { return min(chunkCount, (ssize_t)(end - offset)); }

  int mirrorInvert(int i, int max) {
    int ret = 0;
    for (int mask = 1, imask = max / 2; mask < max; mask <<= 1, imask >>= 1) {
      if ((i & mask) == 0) ret += imask;
    }
    return ret;
  }

  int firstBitSet(int i, int max) {
    int ffs = COMPILER_FFS(i);
    return ffs ? ffs - 1 : max;
  }

  int nBitsSet(int i) {
    return COMPILER_POPCOUNT32(i);
  }

  int newPeer(int i, int pow2) {
    return nBitsSet((i ^ (pow2 - 1)) + 1) == 1 ? 1 : 0;
  }

  void resetA() {
    a = 0;
    sendSkipped = stepOffset = 0;
    lastA = aggFactor;
    if (phase >= 2) lastA /= 2 * scale;
    if (phase == 4) lastA = 1;
  }

  void reset() {
    nelem = getNelem();
    phase = 0;
    scale = 1;
    as = aggDelta - 1;
    resetA();
  }

public:
  PatRSAlgorithm(int stepSize, int stepDepth, int maxParallelFactor, size_t offset, size_t end,
                 size_t count, int chunkCount, int rank, int nranks)
    : offset(offset), end(end), count(count), chunkCount(chunkCount), rank(rank), nranks(nranks) {
    parallelFactor = maxParallelFactor;
    aggDelta = nrPow2 = (1 << log2Up(nranks));

    aggFactor = 1;
    size_t channelSize = end - offset;
    while (stepSize / (channelSize * sizeof(T) * aggFactor) >= 2 && aggFactor < nranks / 2) {
      aggFactor *= 2;
      aggDelta /= 2;
    }
    postFreq = aggFactor;
    if (postFreq < parallelFactor) parallelFactor = postFreq;
    int d = stepDepth;
    while (d > 1 && aggFactor < nranks / 2) {
      d /= 2;
      aggFactor *= 2;
      aggDelta /= 2;
    }

    reset();
  }

  int getParallelFactor() { return parallelFactor; }

  void getNextOp(struct ncclPatStep* ps) {
    ps->last = 0;
    ps->nelem = nelem;
    ps->outIx = offset;
    ps->stepOffset = stepOffset;
    int skip = 0;
    if (a >= lastA) {
      skip = 1;
    } else if (phase == 0) {
      int s = mirrorInvert(a, lastA) * aggDelta + as;
      if (s >= nranks) skip = 1;
      int sendDataRank = (rank + s) % nranks;
      ps->inpIx = sendDataRank * count + offset;
      ps->recvDim = -1;
      ps->sendDim = 0;
      ps->outIx = 0;
      ps->recvOffset = -1;
      ps->sendOffset = (a % postFreq) * nelem;
      if (((a % postFreq) + 1 >= postFreq) || (a == lastA - 1)) {
        ps->postSend = 1;
      } else {
        ps->postSend = 0;
      }
      ps->postRecv = 0;
    } else if (phase == 1) {
      int s = mirrorInvert(a, lastA) * aggDelta + as;
      if (s >= nranks) skip = 1;
      ps->recvDim = firstBitSet(s, nrPow2);
      ps->sendOffset = (a % postFreq) * nelem;
      ps->recvOffset = (a % postFreq) * nelem;
      ps->postSend = 0;
      if (ps->recvDim == 0 && (((a % postFreq) + 1 >= postFreq) || (a == lastA - 1))) ps->postSend = 1;
      if (((a % postFreq) + 1 >= postFreq) || (a == lastA - 1)) {
        ps->postRecv = 1;
      } else {
        ps->postRecv = 0;
      }
      s -= (1 << ps->recvDim);
      int recvDataRank = (rank + nranks + s) % nranks;
      ps->inpIx = recvDataRank * count + offset;
      ps->sendDim = s ? firstBitSet(s, nrPow2) : -1;
      if (ps->sendDim == -1) {
        ps->sendOffset = -1;
      } else if (as - (1 << ps->recvDim) == 0) {
        if (newPeer(a, aggFactor)) {
          sendSkipped = a;
          ps->stepOffset = stepOffset = 0;
        }
        int foffset = a - sendSkipped;
        ps->sendOffset = (foffset % postFreq) * nelem;
      }
      int recvDim = ps->recvDim;
      if (s < nranks && skip) {
        ps->recvDim = -1;
        ps->recvOffset = -1;
        ps->postRecv = 0;
        skip = 0;
      }
      if (recvDim > 0 && (((a - sendSkipped) % postFreq) + 1 >= postFreq) && skip == 0) stepOffset++;
    } else if (phase == 2) {
      int s = (2 * mirrorInvert(a, lastA) + 1) * scale * aggDelta + 1;
      ps->postRecv = 0;
      if (s >= nranks) skip = 1;
      ps->recvDim = 0;
      ps->postSend = a == lastA - 1 ? 1 : 0;
      s -= 1;
      if (s < nranks && skip) {
        ps->recvDim = -1;
        ps->recvOffset = -1;
        skip = 0;
      } else if (!skip) {
        int foffset = a + aggFactor - aggFactor / scale;
        ps->postRecv |= ((foffset + 1) % postFreq) == 0 ? 1 : 0;
        ps->recvOffset = (foffset % postFreq) * nelem;
      }
      int recvDataRank = (rank + nranks + s) % nranks;
      ps->inpIx = recvDataRank * count + offset;
      ps->sendDim = s ? firstBitSet(s, nrPow2) : -1;
      int foffset = a;
      ps->postSend |= ((foffset + 1) % postFreq) == 0 ? 1 : 0;
      ps->sendOffset = (foffset % postFreq) * nelem;
    } else if (phase == 3) {
      int s = (2 * mirrorInvert(a, lastA) + 1) * scale * aggDelta;
      ps->postRecv = a == lastA - 1 ? 1 : 0;
      if (s >= nranks) skip = 1;
      ps->recvDim = firstBitSet(s, nrPow2);
      ps->postSend = 0;
      s -= (1 << ps->recvDim);
      int foffset = a;
      ps->postRecv |= (foffset + 1) % postFreq == 0 ? 1 : 0;
      ps->recvOffset = (foffset % postFreq) * nelem;
      int recvDataRank = (rank + nranks + s) % nranks;
      ps->inpIx = recvDataRank * count + offset;
      ps->sendDim = s ? firstBitSet(s, nrPow2) : -1;
      if (s < nranks && skip) {
        ps->recvDim = -1;
        ps->recvOffset = -1;
        ps->postRecv = 0;
        skip = 0;
      }
      if (newPeer(a, aggFactor / (2 * scale))) {
        sendSkipped = a;
        ps->stepOffset = stepOffset = 0;
      }
      foffset = a - sendSkipped;
      if ((foffset % postFreq) + 1 >= postFreq && skip == 0) stepOffset++;
      ps->sendOffset = ps->sendDim >= 0 ? (foffset % postFreq) * nelem : -1;
    } else if (phase == 4) {
      ps->recvDim = 0;
      ps->sendDim = -1;
      ps->inpIx = rank * count + offset;
      ps->recvOffset = ((aggFactor - 1) % postFreq) * nelem;
      ps->sendOffset = -1;
      ps->postRecv = 1;
      ps->postSend = 0;
      offset += chunkCount;
    }

    // Advance state
    a++;
    if (a >= lastA && a >= parallelFactor) {
      int p = phase;
      if (p == 1) as--;
      if (p == 3) scale *= 2;
      phase = p == 0 ? as == 1 ? (aggFactor > 1 ? 2 : 4) : 1 :
              p == 1 ? as % 2 == 1 ? 0 : 1 :
              p == 2 ? 3 :
              p == 3 ? scale < aggFactor ? 2 : 4 :
                       5;
      if (p == 4) {
        if (offset >= end) {
          ps->last = 2;
        } else {
          reset();
        }
      } else {
        resetA();
      }
    } else if (phase == 4 && offset >= end) {
      ps->last = 1;
    }

    ps->flags = PatUsed | (skip ? PatSkipped : 0);
  }
};

#endif // PAT_ALGORITHM_H
