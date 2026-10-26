// Stable line-oriented adapter over SimAI's actual ns-3 RDMA network model.
// ns-3 owns time. Python schedules external work by asking it to advance at
// most to a deadline; any RDMA completion interrupts that advance first.
#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/rdma-driver.h"
#include "ns3/rdma-hw.h"
#include "ns3/switch-node.h"
#include "ns3/qbb-helper.h"
#include "ns3/qbb-channel.h"
#include "ns3/int-header.h"
#include <algorithm>
#include <cstdint>
#include <cmath>
#include <fstream>
#include <iostream>
#include <limits>
#include <map>
#include <queue>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include <tuple>

using namespace ns3;

namespace {
struct Edge { uint32_t peer; uint32_t port; uint64_t rate; uint64_t delay; };
std::vector<Ptr<Node>> nodes;
std::vector<std::vector<Edge>> graph;
std::vector<Ipv4Address> addresses;
std::vector<uint64_t> completed;
std::set<uint64_t> activeIds;
uint32_t hostCount = 0;
uint64_t baseRtt = 100000;
uint32_t packetBytes = 1000;
uint32_t ccMode = 1;
uint32_t bufferBytes = 16 * 1024 * 1024;
std::map<std::pair<uint32_t, uint32_t>, uint32_t> portCursor;
std::map<std::pair<uint32_t, uint32_t>, std::map<uint16_t, uint64_t>> portAvailable;
std::map<uint32_t, uint64_t> localAvailable;
std::map<uint64_t, std::tuple<uint32_t, uint32_t, uint16_t>> flowEndpoints;

void NoOp() {}
void StopAtBarrier() { Simulator::Stop(); }
void TimerComplete(uint64_t id) {
  completed.push_back(id); activeIds.erase(id); Simulator::Stop();
}

void FlowComplete(uint64_t id) {
  auto endpoints = flowEndpoints.at(id);
  auto src = std::get<0>(endpoints);
  auto dst = std::get<1>(endpoints);
  auto port = std::get<2>(endpoints);
  flowEndpoints.erase(id);
  completed.push_back(id);
  activeIds.erase(id);
  // Delay re-use to avoid mapping stale duplicate ACKs onto a new QP.
  portAvailable[{src, dst}][port] = Simulator::Now().GetNanoSeconds() + 100000000;
  Simulator::Stop();
}

void QpComplete(Ptr<RdmaQueuePair> qp) {
  nodes.at(qp->m_dest)->GetObject<RdmaDriver>()->m_rdma->DeleteRxQp(qp->sip.Get(), qp->m_pg, qp->sport);
}
void Sent(Ptr<RdmaQueuePair>) {}

uint16_t AllocatePort(uint32_t src, uint32_t dst) {
  auto key = std::make_pair(src, dst);
  auto& cursor = portCursor[key];
  auto& available = portAvailable[key];
  for (uint32_t attempt = 0; attempt < 64512; ++attempt) {
    uint16_t port = static_cast<uint16_t>(1024 + (cursor++ % 64512));
    auto found = available.find(port);
    if (found == available.end() || found->second <= static_cast<uint64_t>(Simulator::Now().GetNanoSeconds())) {
      available[port] = std::numeric_limits<uint64_t>::max();
      return port;
    }
  }
  throw std::runtime_error("endpoint pair exhausted its active/cooldown RDMA port namespace");
}

void InstallRoutes() {
  for (uint32_t dst = 0; dst < hostCount; ++dst) {
    std::vector<int> distance(nodes.size(), -1);
    std::queue<uint32_t> queue;
    distance[dst] = 0;
    queue.push(dst);
    while (!queue.empty()) {
      auto current = queue.front(); queue.pop();
      for (const auto& edge : graph[current]) {
        if (distance[edge.peer] < 0) {
          distance[edge.peer] = distance[current] + 1;
          if (edge.peer >= hostCount) queue.push(edge.peer);
        }
      }
    }
    for (uint32_t src = 0; src < nodes.size(); ++src) {
      if (src == dst) continue;
      if (distance[src] < 0) throw std::runtime_error("disconnected topology");
      for (const auto& edge : graph[src]) {
        if (distance[edge.peer] == distance[src] - 1) {
          if (src < hostCount) {
            nodes[src]->GetObject<RdmaDriver>()->m_rdma->AddTableEntry(addresses[dst], edge.port, false);
          } else {
            DynamicCast<SwitchNode>(nodes[src])->AddTableEntry(addresses[dst], edge.port);
          }
        }
      }
    }
  }
}

void LoadTopology(const std::string& file) {
  std::ifstream input(file);
  uint32_t count, gpusPerEndpoint, nvCount, switchCount, linkCount;
  std::string gpuType;
  if (!(input >> count >> gpusPerEndpoint >> nvCount >> switchCount >> linkCount >> gpuType))
    throw std::runtime_error("cannot parse topology header");
  if (nvCount != 0 || gpusPerEndpoint != 1 || count <= switchCount || count > 65000)
    throw std::runtime_error("adapter expects a host-aggregated topology without explicit NVSwitches");
  hostCount = count - switchCount;
  std::set<uint32_t> switchIds;
  for (uint32_t i = 0; i < switchCount; ++i) {
    uint32_t id; input >> id; switchIds.insert(id);
  }
  NodeContainer all;
  for (uint32_t id = 0; id < count; ++id) {
    if ((id >= hostCount) != (switchIds.count(id) != 0))
      throw std::runtime_error("hosts must precede switches in topology");
    Ptr<Node> node = id < hostCount ? CreateObject<Node>() : StaticCast<Node>(CreateObject<SwitchNode>());
    nodes.push_back(node); all.Add(node);
  }
  InternetStackHelper stack;
  stack.Install(all);
  graph.resize(count);
  addresses.resize(hostCount);
  for (uint32_t id = 0; id < hostCount; ++id) addresses[id] = Ipv4Address(0x0b000001 + id * 256);
  uint64_t maxDelay = 0;
  uint64_t minRate = std::numeric_limits<uint64_t>::max();
  for (uint32_t i = 0; i < linkCount; ++i) {
    uint32_t src, dst;
    std::string rate, delay;
    double errorRate;
    if (!(input >> src >> dst >> rate >> delay >> errorRate) || src >= count || dst >= count || src == dst)
      throw std::runtime_error("invalid topology link");
    if (errorRate != 0) throw std::runtime_error("lossy links are not supported by this reliable baseline");
    QbbHelper helper;
    helper.SetDeviceAttribute("DataRate", StringValue(rate));
    helper.SetChannelAttribute("Delay", StringValue(delay));
    auto devices = helper.Install(nodes[src], nodes[dst]);
    for (uint32_t side = 0; side < 2; ++side) {
      uint32_t current = side == 0 ? src : dst;
      uint32_t peer = side == 0 ? dst : src;
      auto dev = DynamicCast<QbbNetDevice>(devices.Get(side));
      auto ipv4 = nodes[current]->GetObject<Ipv4>();
      int interface = ipv4->AddInterface(dev);
      if (current < hostCount)
        ipv4->AddAddress(interface, Ipv4InterfaceAddress(addresses[current], Ipv4Mask(0xff000000)));
      ipv4->SetUp(interface);
      auto ns = DynamicCast<QbbChannel>(dev->GetChannel())->GetDelay().GetNanoSeconds();
      auto bps = dev->GetDataRate().GetBitRate();
      graph[current].push_back({peer, dev->GetIfIndex(), bps, static_cast<uint64_t>(ns)});
      maxDelay = std::max(maxDelay, static_cast<uint64_t>(ns));
      minRate = std::min(minRate, bps);
    }
  }
  std::string extra;
  if (input >> extra) throw std::runtime_error("unexpected extra topology fields");
  baseRtt = std::max<uint64_t>(1000, maxDelay * 12 + packetBytes * 8ULL * 1000000000ULL / minRate * 6);
  if (baseRtt > std::numeric_limits<uint32_t>::max()) throw std::runtime_error("path RTT exceeds the pinned backend range");
  for (uint32_t id = 0; id < count; ++id) {
    if (id < hostCount) {
      if (graph[id].size() != 1) throw std::runtime_error("each server must have exactly one shared network uplink");
      auto hw = CreateObject<RdmaHw>();
      hw->SetAttribute("GPUsPerServer", UintegerValue(1));
      hw->SetAttribute("CcMode", UintegerValue(ccMode));
      hw->SetAttribute("Mtu", UintegerValue(packetBytes));
      hw->SetAttribute("L2AckInterval", UintegerValue(1));
      hw->SetAttribute("MinRate", DataRateValue(DataRate(std::max<uint64_t>(1, minRate / 100))));
      auto driver = CreateObject<RdmaDriver>();
      driver->SetNode(nodes[id]); driver->SetRdmaHw(hw);
      nodes[id]->AggregateObject(driver); driver->Init();
      driver->TraceConnectWithoutContext("QpComplete", MakeCallback(&QpComplete));
      driver->TraceConnectWithoutContext("SendComplete", MakeCallback(&Sent));
    } else {
      auto sw = DynamicCast<SwitchNode>(nodes[id]);
      sw->SetAttribute("EcnEnabled", BooleanValue(ccMode != 0));
      sw->SetAttribute("CcMode", UintegerValue(ccMode));
      sw->SetAttribute("AckHighPrio", UintegerValue(1));
      sw->SetAttribute("MaxRtt", UintegerValue(static_cast<uint32_t>(baseRtt)));
      if (graph[id].size() >= SwitchMmu::pCnt) throw std::runtime_error("too many switch ports");
      for (const auto& edge : graph[id]) {
        sw->m_mmu->ConfigEcn(edge.port, 64 * 1024, 256 * 1024, 1.0);
        sw->m_mmu->ConfigHdrm(edge.port, std::max<uint64_t>(65536, edge.rate * edge.delay / 8000000000ULL * 3));
        sw->m_mmu->pfc_a_shift[edge.port] = 3;
      }
      sw->m_mmu->ConfigNPort(graph[id].size());
      if (sw->m_mmu->total_hdrm + sw->m_mmu->total_rsrv >= bufferBytes)
        throw std::runtime_error("switch buffer cannot cover configured per-port headroom");
      sw->m_mmu->ConfigBufferSize(bufferBytes);
      sw->m_mmu->node_id = id;
    }
  }
  RdmaEgressQueue::ack_q_idx = 0;
  InstallRoutes();
}
} // namespace

int main(int argc, char** argv) {
  try {
    if (argc == 2 && std::string(argv[1]) == "--protocol-version") {
      std::cout << "JSB_PROTOCOL 1" << std::endl;
      return 0;
    }
    if (argc != 6) throw std::runtime_error("usage: driver topology seed cc_mode packet_bytes buffer_bytes");
    Time::SetResolution(Time::NS);
    RngSeedManager::SetSeed(std::stoul(argv[2]));
    ccMode = std::stoul(argv[3]); packetBytes = std::stoul(argv[4]); bufferBytes = std::stoul(argv[5]);
    if ((ccMode != 0 && ccMode != 1) || packetBytes < 64 || bufferBytes < 1048576)
      throw std::runtime_error("invalid backend parameters");
    Config::SetDefault("ns3::QbbNetDevice::QcnEnabled", BooleanValue(true));
    Config::SetDefault("ns3::QbbNetDevice::DynamicThreshold", BooleanValue(true));
    Config::SetDefault("ns3::QbbNetDevice::PauseTime", UintegerValue(5));
    IntHeader::mode = IntHeader::NONE;
    LoadTopology(argv[1]);
    std::cout << "JSB READY 1" << std::endl;
    std::string line;
    while (std::getline(std::cin, line)) {
      std::istringstream command(line);
      std::string verb; command >> verb;
      if (verb == "CLOSE") break;
      if (verb == "SEND") {
        uint64_t id, bytes;
        uint32_t src, dst;
        if (!(command >> id >> src >> dst >> bytes) || src >= hostCount || dst >= hostCount || src == dst || bytes == 0 || bytes > 0xffffffffULL || activeIds.count(id))
          throw std::runtime_error("invalid SEND command");
        auto port = AllocatePort(src, dst);
        flowEndpoints[id] = std::make_tuple(src, dst, port);
        activeIds.insert(id);
        nodes[src]->GetObject<RdmaDriver>()->AddQueuePair(src, dst, id, bytes, 3,
            addresses[src], addresses[dst], port, port, 0, baseRtt,
            MakeBoundCallback(&FlowComplete, id), MakeCallback(&NoOp));
        std::cout << "JSB OK" << std::endl;
      } else if (verb == "TIMER") {
        uint64_t id, delay;
        if (!(command >> id >> delay) || activeIds.count(id)) throw std::runtime_error("invalid TIMER command");
        activeIds.insert(id);
        Simulator::Schedule(NanoSeconds(delay), &TimerComplete, id);
        std::cout << "JSB OK" << std::endl;
      } else if (verb == "LOCAL") {
        uint64_t id, bytes, rate, latency;
        uint32_t host;
        if (!(command >> id >> host >> bytes >> rate >> latency) || host >= hostCount || !bytes || !rate || activeIds.count(id))
          throw std::runtime_error("invalid LOCAL command");
        activeIds.insert(id);
        auto now = static_cast<uint64_t>(Simulator::Now().GetNanoSeconds());
        auto service = static_cast<uint64_t>(std::ceil(static_cast<long double>(bytes) * 8000000000.0L / rate));
        auto end = std::max(now, localAvailable[host]) + service + latency;
        localAvailable[host] = end;
        Simulator::Schedule(NanoSeconds(end - now), &TimerComplete, id);
        std::cout << "JSB OK" << std::endl;
      } else if (verb == "ADVANCE") {
        int64_t deadline;
        if (!(command >> deadline) || deadline < Simulator::Now().GetNanoSeconds())
          throw std::runtime_error("invalid ADVANCE deadline");
        completed.clear();
        auto barrier = Simulator::Schedule(NanoSeconds(deadline - Simulator::Now().GetNanoSeconds()), &StopAtBarrier);
        Simulator::Run();
        Simulator::Cancel(barrier);
        std::cout << "JSB TIME " << Simulator::Now().GetNanoSeconds() << " " << completed.size();
        for (auto id : completed) std::cout << " " << id;
        std::cout << std::endl;
      } else {
        throw std::runtime_error("unknown command");
      }
    }
    Simulator::Destroy();
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "joint-scheduling backend: " << error.what() << std::endl;
    std::cout << "JSB ERROR " << error.what() << std::endl;
    return 2;
  }
}
