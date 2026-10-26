#include "workload_parser.h"
#include <fstream>
#include <sstream>
#include <iostream>
#include <stdexcept>

namespace SimCCL {

static AstraSim::ComType parse_comm_type(const std::string& s) {
  if (s == "ALLREDUCE") return AstraSim::ComType::All_Reduce;
  if (s == "ALLGATHER") return AstraSim::ComType::All_Gather;
  if (s == "REDUCESCATTER") return AstraSim::ComType::Reduce_Scatter;
  if (s == "ALLTOALL") return AstraSim::ComType::All_to_All;
  if (s == "NONE") return AstraSim::ComType::None;
  throw std::runtime_error("Unknown communication type: '" + s + "'");
}

std::vector<LayerDesc> parse_workload(const std::string& filepath) {
  std::ifstream ifs(filepath);
  if (!ifs.is_open()) {
    throw std::runtime_error("Cannot open workload file: " + filepath);
  }

  std::vector<LayerDesc> layers;
  std::string line;

  // Line 1: header (model info)
  std::getline(ifs, line);

  // Line 2: number of layers
  std::getline(ifs, line);
  int num_layers = std::stoi(line);

  // Remaining lines: one per layer
  // Format: name comp_time comp_size fwd_comm_type fwd_comm_size fwd_group
  //         wg_comm_type wg_comm_size wg_group ig_comm_type ig_comm_size ig_group
  for (int i = 0; i < num_layers; i++) {
    if (!std::getline(ifs, line)) break;
    std::istringstream iss(line);

    LayerDesc ld;
    ld.layer_id = i;

    std::string name, fwd_type_str, wg_type_str, ig_type_str;
    int comp_time;
    uint64_t comp_size;

    // Parse: name comp_time comp_size fwd_type fwd_size fwd_group wg_type wg_size wg_group ig_type ig_size ig_group
    iss >> name >> comp_time >> comp_size
        >> fwd_type_str >> ld.fwd_pass_comm_size >> ld.fwd_pass_group_type
        >> wg_type_str >> ld.weight_grad_comm_size >> ld.weight_grad_group_type
        >> ig_type_str >> ld.input_grad_comm_size >> ld.input_grad_group_type;

    if (iss.fail()) {
      throw std::runtime_error(
        "Malformed workload line " + std::to_string(i + 3) +
        " (expected 12 tokens): '" + line + "'");
    }

    ld.fwd_pass_comm_type = parse_comm_type(fwd_type_str);
    ld.weight_grad_comm_type = parse_comm_type(wg_type_str);
    ld.input_grad_comm_type = parse_comm_type(ig_type_str);

    layers.push_back(ld);
  }

  return layers;
}

} // namespace SimCCL
