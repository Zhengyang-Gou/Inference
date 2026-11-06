// Line-oriented adapter over the vendored flowSim fluid network model.
// flowSim owns simulated time: it holds flow-completion events and the
// compute/sampling timers submitted by Python. Python advances this process to
// a deadline at most, and every completion epoch interrupts that advance first,
// mirroring the ns-3 adapter's protocol (version 4).
#include "Topology.h"
#include "Type.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <list>
#include <map>
#include <memory>
#include <queue>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

constexpr double kBytesPerGigabyte = 1e9;

struct Edge {
  uint32_t peer;
  double bandwidth_gbps;  // bytes per nanosecond
  double latency_ns;
};

struct Flow {
  uint64_t id = 0;
  uint32_t source = 0;
  uint32_t destination = 0;
  std::string kind;
  uint32_t priority = 2;
  uint64_t size = 0;
  uint64_t accounted = 0;
  Chunk* chunk = nullptr;
  std::vector<uint32_t> path;
  bool done = false;
};

struct LinkCounter {
  std::string resource_id;
  uint32_t source = 0;
  uint32_t destination = 0;
  double bandwidth_gbps = 0;
  double latency_ns = 0;
  uint64_t tx = 0;
  uint64_t kv = 0;
  uint64_t collective = 0;
  uint64_t priority[4] = {0, 0, 0, 0};
};

struct LocalJob {
  uint64_t id = 0;
  std::string kind;
  uint64_t bytes = 0;
  uint64_t start = 0;
  uint64_t service_end = 0;
  uint64_t end = 0;
  uint64_t accounted = 0;
  bool done = false;
};

struct LocalResource {
  std::string resource_id;
  uint32_t node_id = 0;
  uint64_t capacity_bps = 0;
  uint64_t tx = 0;
  uint64_t kv = 0;
  uint64_t collective = 0;
  std::vector<LocalJob*> jobs;
  uint64_t available = 0;
};

std::shared_ptr<EventQueue> queue;
std::shared_ptr<Topology> topology;
uint32_t device_count = 0;
uint32_t host_count = 0;
uint32_t gpus_per_node = 1;
uint64_t seed = 0;
bool priority_enabled = false;

std::vector<std::vector<Edge>> graph;
std::map<std::pair<uint32_t, uint32_t>, Edge> edge_lookup;
std::map<std::pair<uint32_t, uint32_t>, LinkCounter> links;
std::vector<std::vector<uint32_t>> distance_to;

std::map<uint64_t, std::unique_ptr<Flow>> flows;
std::list<Flow*> active_flows;
std::set<uint64_t> active_ids;
std::vector<uint64_t> completed;

std::map<std::string, LocalResource> local_resources;
std::map<uint64_t, std::unique_ptr<LocalJob>> local_jobs;

uint64_t flow_starts = 0;
uint64_t flow_completions = 0;
uint64_t timer_events = 0;
uint64_t pending_sends = 0;
uint64_t advance_epochs = 0;

void FlushPending();

void MixHash(uint64_t value, uint64_t& hash) {
  hash ^= value + 0x9e3779b97f4a7c15ULL + (hash << 6) + (hash >> 2);
}

uint64_t RouteHash(uint32_t source, uint32_t destination, uint64_t flow_id, uint32_t device) {
  uint64_t hash = 0xcbf29ce484222325ULL;
  MixHash(seed, hash);
  MixHash(source, hash);
  MixHash(destination, hash);
  MixHash(flow_id, hash);
  MixHash(device, hash);
  return hash;
}

void BuildDistances() {
  distance_to.assign(device_count, std::vector<uint32_t>(device_count, UINT32_MAX));
  for (uint32_t destination = 0; destination < host_count; ++destination) {
    auto& distance = distance_to[destination];
    distance[destination] = 0;
    std::queue<uint32_t> pending;
    pending.push(destination);
    while (!pending.empty()) {
      uint32_t current = pending.front();
      pending.pop();
      for (const auto& edge : graph[current]) {
        if (distance[edge.peer] == UINT32_MAX) {
          distance[edge.peer] = distance[current] + 1;
          pending.push(edge.peer);
        }
      }
    }
  }
}

std::vector<uint32_t> BuildRoute(uint32_t source, uint32_t destination, uint64_t flow_id, double& latency_ns) {
  const auto& distance = distance_to[destination];
  if (distance[source] == UINT32_MAX) throw std::runtime_error("disconnected topology");
  std::vector<uint32_t> path{source};
  uint32_t current = source;
  latency_ns = 0;
  while (current != destination) {
    std::vector<uint32_t> candidates;
    for (const auto& edge : graph[current])
      if (distance[edge.peer] == distance[current] - 1) candidates.push_back(edge.peer);
    if (candidates.empty()) throw std::runtime_error("no shortest-path next hop");
    std::sort(candidates.begin(), candidates.end());
    uint32_t next = candidates[RouteHash(source, destination, flow_id, current) % candidates.size()];
    auto link = edge_lookup.find({current, next});
    if (link == edge_lookup.end()) throw std::runtime_error("route uses a missing directed link");
    latency_ns += link->second.latency_ns;
    path.push_back(next);
    current = next;
  }
  return path;
}

LinkCounter& LinkFor(uint32_t source, uint32_t destination) {
  auto key = std::make_pair(source, destination);
  auto found = links.find(key);
  if (found == links.end()) throw std::runtime_error("flow route uses an unregistered link");
  return found->second;
}

void AccountFlow(Flow& flow, uint64_t now) {
  uint64_t target = flow.size;
  if (!flow.done) {
    if (flow.chunk == nullptr) return;
    double elapsed = static_cast<double>(now) - static_cast<double>(flow.chunk->get_transmission_start_time());
    double produced = elapsed > 0 ? elapsed * flow.chunk->get_rate() : 0.0;
    target = produced >= static_cast<double>(flow.size) ? flow.size : static_cast<uint64_t>(produced);
  }
  if (target <= flow.accounted) return;
  uint64_t delta = target - flow.accounted;
  for (size_t index = 0; index + 1 < flow.path.size(); ++index) {
    LinkCounter& link = LinkFor(flow.path[index], flow.path[index + 1]);
    link.tx += delta;
    if (flow.kind == "kv") link.kv += delta;
    else link.collective += delta;
    link.priority[flow.priority] += delta;
  }
  flow.accounted = target;
}

void AccountAll(uint64_t now) {
  for (Flow* flow : active_flows) AccountFlow(*flow, now);
}

void AccountLocal(uint64_t now) {
  for (auto& entry : local_resources) {
    LocalResource& resource = entry.second;
    for (LocalJob* job : resource.jobs) {
      if (job == nullptr || job->done) continue;
      uint64_t sent = 0;
      if (now >= job->service_end) sent = job->bytes;
      else if (now > job->start && job->service_end > job->start)
        sent = static_cast<uint64_t>(static_cast<long double>(job->bytes) * (now - job->start) /
                                     (job->service_end - job->start));
      if (sent <= job->accounted) continue;
      uint64_t delta = sent - job->accounted;
      job->accounted = sent;
      resource.tx += delta;
      if (job->kind == "kv") resource.kv += delta;
      else resource.collective += delta;
    }
  }
}

void FinishLocalJob(LocalJob* job) {
  if (job == nullptr || job->done) return;
  for (auto& entry : local_resources) {
    LocalResource& resource = entry.second;
    auto found = std::find(resource.jobs.begin(), resource.jobs.end(), job);
    if (found == resource.jobs.end()) continue;
    if (job->accounted < job->bytes) {
      uint64_t delta = job->bytes - job->accounted;
      resource.tx += delta;
      if (job->kind == "kv") resource.kv += delta;
      else resource.collective += delta;
      job->accounted = job->bytes;
    }
    // Retire the pointer so later accounting and snapshots stay O(active jobs).
    resource.jobs.erase(found);
  }
  job->done = true;
}

void FlowComplete(void* arg) {
  Flow* flow = static_cast<Flow*>(arg);
  if (flow == nullptr || flow->done) return;
  const uint64_t id = flow->id;
  flow->done = true;
  AccountFlow(*flow, queue->get_current_time());
  flow->chunk = nullptr;
  active_ids.erase(id);
  active_flows.remove(flow);
  ++flow_completions;
  completed.push_back(id);
  // Release the object; a completed flow is never revisited.
  flows.erase(id);
}

void TimerComplete(void* arg) {
  uint64_t id = *static_cast<uint64_t*>(arg);
  delete static_cast<uint64_t*>(arg);
  active_ids.erase(id);
  ++timer_events;
  completed.push_back(id);
}

void LocalComplete(void* arg) {
  LocalJob* job = static_cast<LocalJob*>(arg);
  const uint64_t id = job->id;
  FinishLocalJob(job);
  active_ids.erase(id);
  ++flow_completions;
  completed.push_back(id);
  local_jobs.erase(id);
}

void LoadTopology(const std::string& path) {
  std::ifstream input(path);
  uint32_t switch_count = 0, link_count = 0, nv_count = 0;
  uint32_t endpoints = 0;
  std::string endpoint_type;
  if (!(input >> device_count >> endpoints >> nv_count >> switch_count >> link_count >> endpoint_type))
    throw std::runtime_error("cannot parse topology header");
  if (device_count <= switch_count || device_count > 60000)
    throw std::runtime_error("invalid topology device count");
  if (nv_count != 0 || endpoints != 1)
    throw std::runtime_error("adapter expects host-aggregated endpoints without explicit NVSwitches");
  host_count = device_count - switch_count;
  std::set<uint32_t> switch_ids;
  for (uint32_t index = 0; index < switch_count; ++index) {
    uint32_t id = 0;
    if (!(input >> id) || id >= device_count) throw std::runtime_error("invalid switch id list");
    switch_ids.insert(id);
  }
  for (uint32_t id = 0; id < device_count; ++id)
    if ((id >= host_count) != (switch_ids.count(id) != 0))
      throw std::runtime_error("hosts must precede switches in topology");
  graph.assign(device_count, {});
  topology = std::make_shared<Topology>(static_cast<int>(device_count), static_cast<int>(host_count));
  for (uint32_t index = 0; index < link_count; ++index) {
    uint32_t source = 0, destination = 0;
    std::string rate, delay;
    double error_rate = 0;
    if (!(input >> source >> destination >> rate >> delay >> error_rate) || source >= device_count ||
        destination >= device_count || source == destination)
      throw std::runtime_error("invalid topology link");
    if (error_rate != 0) throw std::runtime_error("lossy links are not supported by the fluid model");
    uint64_t bits_per_second = std::stoull(rate);
    if (bits_per_second < 8) throw std::runtime_error("link bandwidth must be at least 1 byte/second");
    double latency_ns = std::stod(delay);
    double bandwidth_gbps = static_cast<double>(bits_per_second) / 8.0 / kBytesPerGigabyte;
    for (uint32_t direction = 0; direction < 2; ++direction) {
      uint32_t current = direction == 0 ? source : destination;
      uint32_t peer = direction == 0 ? destination : source;
      graph[current].push_back(Edge{peer, bandwidth_gbps, latency_ns});
      edge_lookup[{current, peer}] = Edge{peer, bandwidth_gbps, latency_ns};
      LinkCounter counter;
      counter.resource_id = "link:" + std::to_string(current) + "->" + std::to_string(peer);
      counter.source = current;
      counter.destination = peer;
      counter.bandwidth_gbps = bandwidth_gbps;
      counter.latency_ns = latency_ns;
      links[{current, peer}] = counter;
    }
    topology->connect(source, destination, bandwidth_gbps, latency_ns, true);
  }
  std::string extra;
  if (input >> extra) throw std::runtime_error("unexpected extra topology fields");
  BuildDistances();
}

void WriteRouteTable(const std::string& path) {
  std::ofstream out(path);
  out << "{\n  \"format\": \"joint-scheduling-flowsim-routing-v1\",\n";
  out << "  \"routing\": \"deterministic_ecmp_hash_v1\",\n";
  out << "  \"hash\": \"mix(seed,src,dst,flow_id,device) over sorted shortest-path next hops\",\n";
  out << "  \"seed\": " << seed << ",\n  \"host_count\": " << host_count << ",\n";
  out << "  \"devices\": [";
  for (uint32_t id = 0; id < device_count; ++id) {
    if (id != 0) out << ',';
    out << "{\"id\":" << id << ",\"kind\":\"" << (id < host_count ? "endpoint" : "switch") << "\"}";
  }
  out << "],\n  \"links\": [";
  bool first = true;
  for (const auto& entry : links) {
    if (!first) out << ',';
    first = false;
    out << "{\"src\":" << entry.second.source << ",\"dst\":" << entry.second.destination
        << ",\"bandwidth_gbps\":" << entry.second.bandwidth_gbps
        << ",\"latency_ns\":" << entry.second.latency_ns << '}';
  }
  out << "],\n  \"ecmp_groups\": [";
  first = true;
  for (uint32_t destination = 0; destination < host_count; ++destination) {
    const auto& distance = distance_to[destination];
    for (uint32_t device = 0; device < device_count; ++device) {
      if (device == destination || distance[device] == UINT32_MAX) continue;
      std::vector<uint32_t> candidates;
      for (const auto& edge : graph[device])
        if (distance[edge.peer] == distance[device] - 1) candidates.push_back(edge.peer);
      if (candidates.size() < 2) continue;
      std::sort(candidates.begin(), candidates.end());
      if (!first) out << ',';
      first = false;
      out << "{\"destination\":" << destination << ",\"device\":" << device << ",\"next_hops\":[";
      for (size_t index = 0; index < candidates.size(); ++index) {
        if (index) out << ',';
        out << candidates[index];
      }
      out << "]}";
    }
  }
  out << "]\n}\n";
}

void WriteStats() {
  FlushPending();
  uint64_t now = queue->get_current_time();
  AccountAll(now);
  AccountLocal(now);
  std::cout << "JSB STATS {\"time_ns\":" << now << ",\"model\":\"flowsim_fluid\",\"resources\":[";
  bool first = true;
  for (const auto& entry : links) {
    const LinkCounter& link = entry.second;
    if (!first) std::cout << ',';
    first = false;
    uint64_t other = link.tx - link.kv - link.collective;
    std::cout << "{\"resource_id\":\"" << link.resource_id << "\",\"node_id\":" << link.source
              << ",\"peer_id\":" << link.destination << ",\"capacity_bps\":"
              << static_cast<uint64_t>(std::llround(link.bandwidth_gbps * 8 * kBytesPerGigabyte))
              << ",\"tx_bytes\":" << link.tx << ",\"kv_bytes\":" << link.kv
              << ",\"collective_bytes\":" << link.collective << ",\"other_bytes\":" << other
              << ",\"data_priority_bytes\":[" << link.priority[0] << ',' << link.priority[1] << ','
              << link.priority[2] << ',' << link.priority[3] << ']'
              << ",\"queue_bytes\":0,\"pfc_pause_count\":0,\"latency_ns\":" << link.latency_ns << '}';
  }
  for (const auto& entry : local_resources) {
    const LocalResource& resource = entry.second;
    // Report a local port only once traffic has defined its rate. The observer
    // already knows every local capacity from the topology; an unused port with
    // capacity 0 would break its utilization computation.
    if (resource.capacity_bps == 0 && resource.jobs.empty()) continue;
    if (!first) std::cout << ',';
    first = false;
    uint64_t queued = 0;
    uint64_t active = 0;
    for (LocalJob* job : resource.jobs)
      if (job != nullptr && !job->done && job->end > now) {
        queued += job->bytes - job->accounted;
        ++active;
      }
    std::cout << "{\"resource_id\":\"" << resource.resource_id << "\",\"node_id\":" << resource.node_id
              << ",\"peer_id\":" << resource.node_id << ",\"capacity_bps\":" << resource.capacity_bps
              << ",\"tx_bytes\":" << resource.tx << ",\"kv_bytes\":" << resource.kv
              << ",\"collective_bytes\":" << resource.collective
              << ",\"other_bytes\":" << resource.tx - resource.kv - resource.collective
              << ",\"queue_bytes\":" << queued << ",\"pfc_pause_count\":0,\"active_jobs\":" << active << '}';
  }
  std::cout << "],\"flows\":[";
  first = true;
  // Only active flows: completed ones are erased, so a snapshot is O(active).
  for (const Flow* entry : active_flows) {
    const Flow& flow = *entry;
    if (flow.done) continue;
    if (!first) std::cout << ',';
    first = false;
    std::cout << "{\"id\":" << flow.id << ",\"acked_bytes\":" << flow.accounted
              << ",\"size_bytes\":" << flow.size << ",\"kind\":\"" << flow.kind << "\",\"priority\":"
              << flow.priority << ",\"rate_bytes_per_second\":"
              << (flow.chunk != nullptr ? static_cast<uint64_t>(std::llround(flow.chunk->get_rate() * 1e9)) : 0)
              << ",\"path\":[";
    for (size_t index = 0; index < flow.path.size(); ++index) {
      if (index) std::cout << ',';
      std::cout << flow.path[index];
    }
    std::cout << "]}";
  }
  std::cout << "],\"diagnostics\":{\"active_ids\":" << active_ids.size() << ",\"flow_starts\":" << flow_starts
            << ",\"flow_completions\":" << flow_completions << ",\"timers\":" << timer_events
            << ",\"priority_enabled\":" << (priority_enabled ? "true" : "false") << "}}" << std::endl;
}

void WriteInfo() {
  std::cout << "JSB INFO {\"backend\":\"simai_flowsim_fluid\",\"protocol_version\":4"
               ",\"completion_semantics\":\"all_bytes_serialized_at_destination_no_ack\""
               ",\"link_model\":\"per_direction_fluid_max_min_fair_share\""
               ",\"latency_model\":\"path_propagation_sum\""
               ",\"queue_model\":\"none\""
               ",\"congestion_control\":\"not_modeled\""
               ",\"packet_model\":\"not_modeled\""
               ",\"pfc\":\"not_modeled\""
               ",\"ecmp\":\"deterministic_hash_per_flow\""
               ",\"priority_model\":\""
            << (priority_enabled ? "metadata_only_equal_share" : "metadata_only_equal_share")
            << "\",\"seed\":" << seed << ",\"host_count\":" << host_count
            << ",\"device_count\":" << device_count << "}" << std::endl;
}

// Flows submitted between two advances share one bandwidth re-allocation. The
// fluid engine's send() re-plans every active flow per arrival; doing that once
// per batch keeps an online trace tractable without changing the arithmetic.
void FlushPending() {
  if (pending_sends == 0) return;
  uint64_t now = queue->get_current_time();
  AccountAll(now);
  AccountLocal(now);
  topology->settle();
  pending_sends = 0;
}

void CommandSend(std::istringstream& command) {
  uint64_t id = 0, bytes = 0;
  uint32_t source = 0, destination = 0, priority = 0;
  std::string kind;
  if (!(command >> id >> source >> destination >> bytes >> kind >> priority) || id == 0 || bytes == 0 ||
      (kind != "kv" && kind != "collective") || priority > 3 || source >= host_count ||
      destination >= host_count || source == destination || active_ids.count(id))
    throw std::runtime_error("invalid SEND command");
  auto flow = std::make_unique<Flow>();
  flow->id = id;
  flow->source = source;
  flow->destination = destination;
  flow->kind = kind;
  flow->priority = priority;
  flow->size = bytes;
  double latency_ns = 0;
  flow->path = BuildRoute(source, destination, id, latency_ns);
  Route route;
  for (uint32_t device : flow->path) route.push_back(topology->get_device(static_cast<int>(device)));
  Flow* pointer = flow.get();
  auto chunk = std::make_unique<Chunk>(bytes, route, &FlowComplete, static_cast<void*>(pointer));
  chunk->set_latency(latency_ns);
  pointer->chunk = chunk.get();
  flows[id] = std::move(flow);
  active_flows.push_back(pointer);
  active_ids.insert(id);
  ++flow_starts;
  topology->enqueue(std::move(chunk));
  ++pending_sends;
  std::cout << "JSB OK" << std::endl;
}

void CommandTimer(std::istringstream& command) {
  uint64_t id = 0, delay = 0;
  if (!(command >> id >> delay) || id == 0 || active_ids.count(id))
    throw std::runtime_error("invalid TIMER command");
  active_ids.insert(id);
  queue->schedule_event(queue->get_current_time() + delay, &TimerComplete, new uint64_t(id));
  std::cout << "JSB OK" << std::endl;
}

LocalResource& Local(std::string resource_id, uint32_t node) {
  auto found = local_resources.find(resource_id);
  if (found == local_resources.end()) {
    LocalResource resource;
    resource.resource_id = resource_id;
    resource.node_id = node;
    found = local_resources.emplace(resource_id, resource).first;
  }
  return found->second;
}

void CommandLocal(std::istringstream& command, bool per_port) {
  uint64_t id = 0, bytes = 0, rate = 0, latency = 0;
  uint32_t host = 0, source = 0, destination = 0;
  std::string kind;
  bool read = per_port
                  ? static_cast<bool>(command >> id >> host >> source >> destination >> bytes >> rate >> latency >> kind)
                  : static_cast<bool>(command >> id >> host >> bytes >> rate >> latency >> kind);
  if (!read || id == 0 || bytes == 0 || rate == 0 || (kind != "kv" && kind != "collective") ||
      host >= host_count || active_ids.count(id))
    throw std::runtime_error("invalid local command");
  uint64_t now = queue->get_current_time();
  AccountLocal(now);
  uint64_t service = static_cast<uint64_t>(
      std::ceil(static_cast<long double>(bytes) * 8e9L / static_cast<long double>(rate)));
  if (service == 0) service = 1;
  std::vector<LocalResource*> ports;
  if (per_port) {
    if (source >= host_count || destination >= host_count) throw std::runtime_error("invalid local GPU id");
    ports.push_back(&Local(std::string("local:gpu-") + std::to_string(source) + ":tx", source));
    ports.push_back(&Local(std::string("local:gpu-") + std::to_string(destination) + ":rx", destination));
  } else {
    ports.push_back(&Local(std::string("local:") + std::to_string(host), host));
  }
  uint64_t start = now;
  for (LocalResource* port : ports) start = std::max(start, port->available);
  uint64_t service_end = start + service;
  uint64_t end = service_end + latency;
  auto job = std::make_unique<LocalJob>();
  job->id = id;
  job->kind = kind;
  job->bytes = bytes;
  job->start = start;
  job->service_end = service_end;
  job->end = end;
  LocalJob* pointer = job.get();
  local_jobs[pointer->id] = std::move(job);
  for (LocalResource* port : ports) {
    port->jobs.push_back(pointer);
    port->capacity_bps = rate;
    port->available = per_port ? service_end : end;
  }
  active_ids.insert(id);
  queue->schedule_event(end, &LocalComplete, static_cast<void*>(pointer));
  std::cout << "JSB OK" << std::endl;
}

void CommandAdvance(std::istringstream& command) {
  uint64_t deadline = 0;
  if (!(command >> deadline) || deadline < queue->get_current_time())
    throw std::runtime_error("invalid ADVANCE deadline");
  FlushPending();
  completed.clear();
  bool processed = false;
  if (!queue->finished()) {
    uint64_t next = queue->next_event_time();
    if (next <= deadline) {
      AccountAll(next);
      AccountLocal(next);
      queue->proceed();
      ++advance_epochs;
      processed = true;
    }
  }
  // Only skip ahead when this advance processed no event: a completion epoch is
  // reported at its own timestamp, exactly like the ns-3 adapter.
  if (!processed && queue->get_current_time() < deadline &&
      (queue->finished() || queue->next_event_time() > deadline))
    queue->advance_to(deadline);
  uint64_t now = queue->get_current_time();
  std::cout << "JSB TIME " << now << " " << completed.size();
  for (uint64_t id : completed) std::cout << " " << id;
  std::cout << std::endl;
}

void RegisterLocalResources(const std::string& mode) {
  for (uint32_t gpu = 0; gpu < host_count; ++gpu) {
    if (mode == "gpu_ports") {
      for (const char* direction : {"tx", "rx"}) {
        LocalResource resource;
        resource.resource_id = "local:gpu-" + std::to_string(gpu) + ":" + direction;
        resource.node_id = gpu / gpus_per_node;
        local_resources[resource.resource_id] = resource;
      }
    } else {
      LocalResource resource;
      resource.resource_id = "local:" + std::to_string(gpu);
      resource.node_id = gpu;
      local_resources[resource.resource_id] = resource;
    }
  }
}

// Write the run-level counters next to the route table so a run can report how
// many logical flows and completion epochs the fluid engine actually executed.
void WriteSummary(const std::string& route_table) {
  std::ofstream out(route_table + ".summary.json");
  out << "{\n  \"backend\": \"simai_flowsim_fluid\",\n";
  out << "  \"protocol_version\": 4,\n  \"model\": \"flow_level_fluid_max_min_fair_share\",\n";
  out << "  \"completion_semantics\": \"all_bytes_serialized_at_destination_no_ack\",\n";
  out << "  \"link_model\": \"per_direction_fluid_max_min_fair_share\",\n";
  out << "  \"latency_model\": \"path_propagation_sum\",\n  \"queue_model\": \"none\",\n";
  out << "  \"congestion_control\": \"not_modeled\",\n  \"pfc\": \"not_modeled\",\n";
  out << "  \"ecmp\": \"deterministic_hash_per_flow\",\n  \"priority_model\": \"metadata_only_equal_share\",\n";
  out << "  \"seed\": " << seed << ",\n  \"host_count\": " << host_count << ",\n";
  out << "  \"device_count\": " << device_count << ",\n  \"directed_links\": " << links.size() << ",\n";
  out << "  \"flow_starts\": " << flow_starts << ",\n  \"flow_completions\": " << flow_completions << ",\n";
  out << "  \"timer_events\": " << timer_events << ",\n  \"completion_epochs\": " << advance_epochs << ",\n";
  out << "  \"flows_active_at_close\": " << active_flows.size() << ",\n";
  out << "  \"simulated_end_ns\": " << queue->get_current_time() << "\n}\n";
}

}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc == 2 && std::string(argv[1]) == "--protocol-version") {
      std::cout << "JSB_PROTOCOL 4" << std::endl;
      return 0;
    }
    if (argc != 7)
      throw std::runtime_error("usage: flow-driver topology seed priority_enabled local_mode gpus_per_node route_table");
    seed = std::stoull(argv[2]);
    if (std::string(argv[3]) != "0" && std::string(argv[3]) != "1")
      throw std::runtime_error("invalid priority mode");
    priority_enabled = std::string(argv[3]) == "1";
    std::string local_mode = argv[4];
    if (local_mode != "gpu_ports" && local_mode != "shared_fifo")
      throw std::runtime_error("invalid local resource mode");
    gpus_per_node = static_cast<uint32_t>(std::stoul(argv[5]));
    if (gpus_per_node == 0) throw std::runtime_error("invalid gpus_per_node");
    queue = std::make_shared<EventQueue>();
    LoadTopology(argv[1]);
    topology->set_event_queue(queue);
    RegisterLocalResources(local_mode);
    WriteRouteTable(argv[6]);
    std::cout << "JSB READY 4" << std::endl;
    std::string line;
    while (std::getline(std::cin, line)) {
      std::istringstream command(line);
      std::string verb;
      command >> verb;
      if (verb == "CLOSE") break;
      if (verb == "SEND") CommandSend(command);
      else if (verb == "TIMER") CommandTimer(command);
      else if (verb == "LOCAL") CommandLocal(command, false);
      else if (verb == "LOCAL_PORTS") CommandLocal(command, true);
      else if (verb == "STATS") WriteStats();
      else if (verb == "INFO") WriteInfo();
      else if (verb == "ADVANCE") CommandAdvance(command);
      else throw std::runtime_error("unknown command");
    }
    WriteSummary(argv[6]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "joint-scheduling flowsim backend: " << error.what() << std::endl;
    std::cout << "JSB ERROR " << error.what() << std::endl;
    return 2;
  }
}
