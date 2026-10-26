#ifndef SIMCCL_WORKLOAD_PARSER_H
#define SIMCCL_WORKLOAD_PARSER_H

#include <string>
#include <vector>
#include <cstdint>
#include "astra-sim/system/Common.hh"

namespace SimCCL {

// Simplified layer description (mirrors astra-sim Workload layer fields)
struct LayerDesc {
  int layer_id;
  // Forward pass
  uint64_t fwd_pass_comm_size = 0;
  AstraSim::ComType fwd_pass_comm_type = AstraSim::ComType::None;
  int fwd_pass_group_type = 0; // ParallelStrategy as int (TP=0, DP=1, PP=2, EP=3, DP_EP=4, NONE=5)
  // Weight gradient
  uint64_t weight_grad_comm_size = 0;
  AstraSim::ComType weight_grad_comm_type = AstraSim::ComType::None;
  int weight_grad_group_type = 0;
  // Input gradient
  uint64_t input_grad_comm_size = 0;
  AstraSim::ComType input_grad_comm_type = AstraSim::ComType::None;
  int input_grad_group_type = 0;
};

// Parse a SimAI workload file (same format as microAllReduce.txt etc.)
// Returns vector of LayerDesc for all layers.
std::vector<LayerDesc> parse_workload(const std::string& filepath);

} // namespace SimCCL

#endif // SIMCCL_WORKLOAD_PARSER_H
