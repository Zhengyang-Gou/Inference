/**
 * SimCCL Standalone - NCCL FlowModel Translation Layer (No ns3 dependency)
 *
 * This binary generates ncclFlowModel_detailed_flows.csv from either:
 *   Mode 1: Command-line arguments (single collective operation)
 *   Mode 2: Workload file (batch, all layers enumerated)
 *
 * It links only MockNccl translation code + minimal AstraSim headers.
 * No ns3, no network simulation, no event scheduler.
 */
#include <iostream>
#include <string>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <stdexcept>
#include "MockNcclGroup.h"
#include "MockNcclChannel.h"
#include "workload_parser.h"

using namespace MockNccl;

static void print_usage(const char* prog) {
  std::cerr << "Usage:\n"
    << "  Mode 1 (single op):\n"
    << "    " << prog << " --op <AllReduce|AllGather|ReduceScatter|AlltoAll|Broadcast>\n"
    << "                --size <bytes> --nRanks <N> --nNodes <N>\n"
    << "                --gpus_per_node <N> [--gpu_type <H20|H100|A100>]\n"
    << "  Mode 2 (workload file):\n"
    << "    " << prog << " -w <workload.txt> --nRanks <N> --nNodes <N>\n"
    << "                --gpus_per_node <N> [--gpu_type <H20|H100|A100>]\n"
    << "\n  Options:\n"
    << "    --gpu_type <H20|H100|H800|A100|A800>  GPU type (default: H20)\n"
    << "    --help                                 Show this message\n"
    << "\n  Note: Mock NCCL version (v2.20/v2.30) is selected at compile time\n"
    << "        via CMake -DMOCK_VERSION=v2.30 (default).\n"
    << "\n  Constraints:\n"
    << "    - nRanks must equal nNodes * gpus_per_node\n"
    << "    - nRanks must be divisible by gpus_per_node\n"
    << "    - gpus_per_node must be <= nRanks\n"
    << "    - PAT triggers when nNodes>1 && gpus_per_node==1 (AllGather/ReduceScatter only)\n";
}

static AstraSim::ComType parse_op(const std::string& s) {
  if (s == "AllReduce") return AstraSim::ComType::All_Reduce;
  if (s == "AllGather") return AstraSim::ComType::All_Gather;
  if (s == "ReduceScatter") return AstraSim::ComType::Reduce_Scatter;
  if (s == "AlltoAll") return AstraSim::ComType::All_to_All;
  if (s == "Broadcast") return AstraSim::ComType::Broadcast;
  std::cerr << "[ERROR] Unknown op: " << s << "\n";
  exit(1);
}

static GPUType parse_gpu_type(const std::string& s) {
  if (s == "H20") return GPUType::H20;
  if (s == "H100") return GPUType::H100;
  if (s == "H800") return GPUType::H800;
  if (s == "A100") return GPUType::A100;
  if (s == "A800") return GPUType::A800;
  std::cerr << "[ERROR] Unknown --gpu_type: " << s
            << " (valid: H20|H100|H800|A100|A800)\n";
  exit(1);
}

int main(int argc, char* argv[]) {
  // Parse arguments
  std::string workload_file;
  std::string op_str;
  uint64_t data_size = 0;
  int nRanks = 0, nNodes = 0, gpus_per_node = 0;
  std::string gpu_type_str = "H20";

  for (int i = 1; i < argc; i++) {
    std::string arg = argv[i];
    if (arg == "--help" || arg == "-h") { print_usage(argv[0]); return 0; }
    else if (arg == "-w" && i+1 < argc) workload_file = argv[++i];
    else if (arg == "--op" && i+1 < argc) op_str = argv[++i];
    else if (arg == "--size" && i+1 < argc) data_size = strtoull(argv[++i], nullptr, 10);
    else if (arg == "--nRanks" && i+1 < argc) nRanks = std::stoi(argv[++i]);
    else if (arg == "--nNodes" && i+1 < argc) nNodes = std::stoi(argv[++i]);
    else if (arg == "--gpus_per_node" && i+1 < argc) gpus_per_node = std::stoi(argv[++i]);
    else if (arg == "--gpu_type" && i+1 < argc) gpu_type_str = argv[++i];
  }

  // Validate required params
  if (nRanks <= 0 || nNodes <= 0 || gpus_per_node <= 0) {
    std::cerr << "[ERROR] --nRanks, --nNodes, --gpus_per_node are required and must be > 0\n";
    print_usage(argv[0]);
    return 1;
  }
  if (workload_file.empty() && op_str.empty()) {
    std::cerr << "[ERROR] Must specify either -w <workload> or --op <op> --size <bytes>\n";
    print_usage(argv[0]);
    return 1;
  }
  if (workload_file.empty() && !op_str.empty() && data_size == 0) {
    std::cerr << "[ERROR] --size is required and must be > 0 in single-op mode\n";
    print_usage(argv[0]);
    return 1;
  }

  // Validate topology constraints
  if (nRanks != nNodes * gpus_per_node) {
    std::cerr << "[ERROR] nRanks(" << nRanks << ") must equal nNodes(" << nNodes
              << ") * gpus_per_node(" << gpus_per_node << ") = " << nNodes * gpus_per_node << "\n";
    return 1;
  }
  if (nRanks % gpus_per_node != 0) {
    std::cerr << "[ERROR] nRanks(" << nRanks << ") must be divisible by gpus_per_node(" << gpus_per_node << ")\n";
    return 1;
  }
  if (gpus_per_node > nRanks) {
    std::cerr << "[ERROR] gpus_per_node(" << gpus_per_node << ") cannot exceed nRanks(" << nRanks << ")\n";
    return 1;
  }

  GPUType gpu_type = parse_gpu_type(gpu_type_str);

  // Derive group sizes from nRanks/nNodes/gpus_per_node
  int TP_size = gpus_per_node;
  int DP_size = nRanks / TP_size;
  if (DP_size <= 0) DP_size = 1;
  int PP_size = 1, EP_size = 1, DP_EP_size = DP_size;
  // NVSwitchs: for H20/H100 with >4 GPUs per node, simulate NVSwitch nodes.
  // The vector contains NVSwitch node IDs (appended after GPU IDs).
  std::vector<int> NVSwitchs;
  if (gpus_per_node > 4) {
    // One NVSwitch per node, placed after all GPU indices
    for (int n = 0; n < nNodes; n++) {
      NVSwitchs.push_back(nRanks + n); // NVSwitch ID = nRanks + node_index
    }
  }

  // Initialize MockNcclGroup
  MockNcclGroup group(nRanks, gpus_per_node, TP_size, DP_size, PP_size, EP_size, DP_EP_size, NVSwitchs, gpu_type);

  // Pre-generate ring channels for all groups (required before getFlowModels).
  // The constructor only populates GroupIndex/AllGroups; ring channel topology
  // must be explicitly generated via genringchannels() before any flow model
  // generation, otherwise Allringchannels[gp_idx] is empty causing div-by-zero.
  for (auto& kv : group.GroupIndex) {
    int rank_key = kv.first.first;
    GroupType type_key = kv.first.second;
    int gp_idx = kv.second;
    if (group.Allringchannels.find(gp_idx) == group.Allringchannels.end()) {
      group.genringchannels(rank_key, type_key);
    }
  }

  std::cout << "[SimCCL-Standalone] Initialized: nRanks=" << nRanks
            << " nNodes=" << nNodes << " gpus_per_node=" << gpus_per_node
            << " gpu_type=" << gpu_type_str << "\n";

  if (!workload_file.empty()) {
    // Mode 2: batch workload
    std::cout << "[SimCCL-Standalone] Mode: workload file '" << workload_file << "'\n";
    std::vector<SimCCL::LayerDesc> layers;
    try {
      layers = SimCCL::parse_workload(workload_file);
    } catch (const std::exception& e) {
      std::cerr << "[ERROR] Failed to parse workload: " << e.what() << "\n";
      return 1;
    }
    std::cout << "[SimCCL-Standalone] Parsed " << layers.size() << " layers\n";

    for (const auto& l : layers) {
      auto process_op = [&](AstraSim::ComType ct, uint64_t sz, int grp) {
        if (sz > 0 && ct != AstraSim::ComType::None) {
          GroupType gt = static_cast<GroupType>(grp);
          // Call getFlowModels for rank 0 to trigger dump
          group.getFlowModels(gt, 0, ct, sz, l.layer_id, MockNccl::State::Forward_Pass);
        }
      };
      process_op(l.fwd_pass_comm_type, l.fwd_pass_comm_size, l.fwd_pass_group_type);
      process_op(l.weight_grad_comm_type, l.weight_grad_comm_size, l.weight_grad_group_type);
      process_op(l.input_grad_comm_type, l.input_grad_comm_size, l.input_grad_group_type);
    }
  } else {
    // Mode 1: single op
    AstraSim::ComType op = parse_op(op_str);
    std::cout << "[SimCCL-Standalone] Mode: single op=" << op_str
              << " size=" << data_size << "\n";
    // Select group type: for PAT (gpus_per_node=1, inter-node), use DP group.
    // For intra-node (gpus_per_node > 1), use TP group.
    GroupType gt = (gpus_per_node == 1 && nNodes > 1) ? GroupType::DP : GroupType::TP;
    group.getFlowModels(gt, 0, op, data_size, 0, MockNccl::State::Forward_Pass);
  }

  std::cout << "[SimCCL-Standalone] Done. Check ncclFlowModel_detailed_flows.csv\n";
  return 0;
}
