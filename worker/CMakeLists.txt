cmake_minimum_required(VERSION 3.25)
project(k2websdr_worker LANGUAGES CXX)

# Acquisition worker. M0: replay source only; no hardware drivers are linked.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

option(K2_SANITIZE "Build with AddressSanitizer and UndefinedBehaviorSanitizer" OFF)

add_executable(ksdr-worker
  src/main.cpp
  src/args.cpp
  src/device_lock.cpp
  src/io.cpp
  src/replay_source.cpp
  src/worker.cpp
)
target_include_directories(ksdr-worker PRIVATE src ${CMAKE_CURRENT_SOURCE_DIR}/../protocol)
target_compile_options(ksdr-worker PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror)

if(K2_SANITIZE)
  target_compile_options(ksdr-worker PRIVATE -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer)
  target_link_options(ksdr-worker PRIVATE -fsanitize=address,undefined)
endif()
