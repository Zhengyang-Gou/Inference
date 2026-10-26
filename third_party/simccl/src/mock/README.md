# SimCCL Mock — Versioned Translation Layer

The `mock/` directory contains the MockNccl translation layer that converts
NCCL collective decisions (algorithm/protocol/channel/chunk) into FlowModels
consumed by the ns3 network simulator.

## Directory Layout

```
mock/
├── v2.20/    NCCL v2.20.5 semantics (legacy baseline)
├── v2.30/    NCCL v2.30.7 semantics (PAT algorithm, protocol-aware, default)
└── README.md (this file)
```

## Build Switch

The ns3 build script (`build/astra_ns3/build.sh`) selects which version to
compile via the `SIMAI_NCCL_VERSION` environment variable:

```bash
# Default: v2.30 (protocol-aware, PAT support)
./build.sh -c ns3

# Select v2.20 mock (legacy)
SIMAI_NCCL_VERSION=v2.20 ./build.sh -c ns3
```

The selected version's files are flat-copied into the ns3 application tree
at build time. CMakeLists.txt globs `SimCCL/mock/*.cc` and is unaffected
by this version switch.

## Version Differences

| Feature | v2.20 | v2.30 |
|---|---|---|
| Algorithms | Ring/Tree/NVLS/NVLS_TREE | + PAT (Parallel Aggregated Trees) |
| Protocol awareness | Default OFF (UNDEF) | Default ON (LL/LL128/Simple) |
| PAT FlowModel branch | N/A | AllGather/ReduceScatter multi-node |
