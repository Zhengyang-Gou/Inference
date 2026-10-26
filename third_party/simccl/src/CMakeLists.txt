cmake_minimum_required(VERSION 3.14)
project(simccl-standalone CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# NCCL mock version selection (default: v2.30)
set(MOCK_VERSION "v2.30" CACHE STRING "Mock NCCL version to compile (v2.20 or v2.30)")

# Paths
set(MOCK_DIR ${CMAKE_SOURCE_DIR}/mock/${MOCK_VERSION})
# Inference repository layout adaptation: vendored siblings.
set(ASTRASIM_ROOT ${CMAKE_SOURCE_DIR}/../../astra_sim)

# Validate mock version exists
if(NOT EXISTS ${MOCK_DIR})
  message(FATAL_ERROR "Mock version '${MOCK_VERSION}' not found at: ${MOCK_DIR}\nSupported: v2.20, v2.30")
endif()

# Collect mock source files
file(GLOB MOCK_SRCS ${MOCK_DIR}/*.cc)

# Build standalone executable
add_executable(simccl-standalone
  main.cc
  workload_parser.cc
  ${MOCK_SRCS}
)

target_include_directories(simccl-standalone PRIVATE
  ${MOCK_DIR}
  ${ASTRASIM_ROOT}
  ${ASTRASIM_ROOT}/astra-sim/system
)

# Compiler flags
target_compile_options(simccl-standalone PRIVATE -O2 -Wall)

message(STATUS "SimCCL Standalone: mock version = ${MOCK_VERSION}")
message(STATUS "SimCCL Standalone: mock dir = ${MOCK_DIR}")
message(STATUS "SimCCL Standalone: astra-sim include = ${ASTRASIM_ROOT}")
