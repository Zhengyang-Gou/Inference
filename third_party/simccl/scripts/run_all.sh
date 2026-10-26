#!/bin/bash
# SimCCL Standalone Full-Feature Test Script
# Runs all valid + invalid configuration combinations, records results.
# Usage: bash run_all.sh [path-to-simccl-standalone]
set -o pipefail

BINARY="${1:-$(dirname "$0")/../src/build/simccl-standalone}"
BINARY="$(cd "$(dirname "$BINARY")" && pwd)/$(basename "$BINARY")"
RESULTS_DIR="$(cd "$(dirname "$0")" && pwd)/../results"
mkdir -p "$RESULTS_DIR"
SUMMARY="$RESULTS_DIR/standalone_test_summary.csv"
WORKDIR=$(mktemp -d /tmp/simccl_test_XXXXX)
CSV_OUT="$WORKDIR/ncclFlowModel_detailed_flows.csv"

# Header
echo "test_id,category,op,size,nRanks,nNodes,gpus_per_node,gpu_type,mock_version,exit_code,csv_lines,algo_field,proto_field,status" > "$SUMMARY"

PASS=0; FAIL=0; TOTAL=0

run_case() {
  local TID="$1" CAT="$2" OP="$3" SIZE="$4" NR="$5" NN="$6" GPN="$7" GT="$8" MV="$9" EXPECT="${10}"
  TOTAL=$((TOTAL+1))
  rm -f "$CSV_OUT"

  local CMD="cd $WORKDIR && $BINARY --op $OP --size $SIZE --nRanks $NR --nNodes $NN --gpus_per_node $GPN --gpu_type $GT"
  OUTPUT=$(bash -c "$CMD" 2>&1)
  EC=$?

  CSV_LINES=0; ALGO=""; PROTO=""
  if [ -f "$CSV_OUT" ]; then
    CSV_LINES=$(wc -l < "$CSV_OUT")
    ALGO=$(tail -1 "$CSV_OUT" | cut -d',' -f4 | head -1)
    PROTO=$(tail -1 "$CSV_OUT" | cut -d',' -f5 | head -1)
  fi

  STATUS="PASS"
  if [ "$EXPECT" = "ok" ] && [ "$EC" -ne 0 ]; then STATUS="FAIL"; fi
  if [ "$EXPECT" = "fail" ] && [ "$EC" -eq 0 ]; then STATUS="UNEXPECTED_PASS"; fi
  if [ "$EXPECT" = "fail" ] && [ "$EC" -ne 0 ]; then STATUS="EXPECTED_FAIL"; fi

  if [ "$STATUS" = "PASS" ] || [ "$STATUS" = "EXPECTED_FAIL" ]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi

  echo "$TID,$CAT,$OP,$SIZE,$NR,$NN,$GPN,$GT,$MV,$EC,$CSV_LINES,$ALGO,$PROTO,$STATUS" >> "$SUMMARY"
  printf "[%03d] %-15s %-5s | op=%-14s size=%-10s nR=%-3s nN=%-2s gpn=%-2s | exit=%d csv=%d %s\n" \
    "$TOTAL" "$STATUS" "$CAT" "$OP" "$SIZE" "$NR" "$NN" "$GPN" "$EC" "$CSV_LINES" "$ALGO"
}

echo "======================================"
echo "SimCCL Standalone Full-Feature Tests"
echo "Binary: $BINARY"
echo "======================================"

# ---- VALID CASES (positive tests) ----
ID=0

# Single node: 8 GPU
for OP in AllReduce AllGather ReduceScatter AlltoAll; do
  for SIZE in 524288 1048576 4194304 16777216 67108864; do
    ID=$((ID+1)); run_case "$ID" "valid-1n8g" "$OP" "$SIZE" 8 1 8 H20 v2.30 ok
  done
done

# Two nodes: 16 GPU (8 per node)
for OP in AllReduce AllGather ReduceScatter AlltoAll; do
  for SIZE in 524288 4194304 67108864; do
    ID=$((ID+1)); run_case "$ID" "valid-2n8g" "$OP" "$SIZE" 16 2 8 H20 v2.30 ok
  done
done

# Two nodes: 2 GPU (1 per node) — PAT trigger for AllGather/ReduceScatter
for OP in AllReduce AllGather ReduceScatter AlltoAll; do
  for SIZE in 524288 4194304 67108864; do
    ID=$((ID+1)); run_case "$ID" "valid-2n1g-PAT" "$OP" "$SIZE" 2 2 1 H20 v2.30 ok
  done
done

# Single node: 4 GPU
for OP in AllReduce AllGather; do
  for SIZE in 1048576 16777216; do
    ID=$((ID+1)); run_case "$ID" "valid-1n4g" "$OP" "$SIZE" 4 1 4 H20 v2.30 ok
  done
done

# Two nodes: 4 GPU (2 per node)
for OP in AllGather ReduceScatter; do
  for SIZE in 4194304 67108864; do
    ID=$((ID+1)); run_case "$ID" "valid-2n2g" "$OP" "$SIZE" 4 2 2 H20 v2.30 ok
  done
done

# ---- INVALID CASES (negative tests) ----

# nRanks != nNodes * gpus_per_node
ID=$((ID+1)); run_case "$ID" "invalid-mismatch" "AllGather" "4194304" 3 2 1 H20 v2.30 fail
ID=$((ID+1)); run_case "$ID" "invalid-mismatch" "AllReduce" "4194304" 5 1 8 H20 v2.30 fail

# gpus_per_node > nRanks
ID=$((ID+1)); run_case "$ID" "invalid-gpn>nr" "AllGather" "4194304" 2 1 4 H20 v2.30 fail

# Missing required params (no --op)
ID=$((ID+1))
TOTAL=$((TOTAL+1))
OUTPUT=$(bash -c "cd $WORKDIR && $BINARY --size 4194304 --nRanks 8 --nNodes 1 --gpus_per_node 8" 2>&1)
EC=$?
STATUS="EXPECTED_FAIL"; [ "$EC" -eq 0 ] && STATUS="UNEXPECTED_PASS"
[ "$STATUS" = "EXPECTED_FAIL" ] && PASS=$((PASS+1)) || FAIL=$((FAIL+1))
echo "$ID,invalid-no-op,-,4194304,8,1,8,H20,v2.30,$EC,0,,,${STATUS}" >> "$SUMMARY"
printf "[%03d] %-15s %-5s | (no --op specified) | exit=%d\n" "$TOTAL" "$STATUS" "neg"  "$EC"

echo ""
echo "======================================"
echo "Results: PASS=$PASS FAIL=$FAIL TOTAL=$TOTAL"
echo "Summary: $SUMMARY"
echo "======================================"

rm -rf "$WORKDIR"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
