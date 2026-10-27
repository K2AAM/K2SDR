cmake_minimum_required(VERSION 3.25)
project(k2websdr_worker LANGUAGES C CXX)

# Acquisition worker: replay (M0) and, when libairspyhf is installed, the Airspy HF+ source (M1).

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

option(K2_SANITIZE "Build with AddressSanitizer and UndefinedBehaviorSanitizer" OFF)

# liquid-dsp: only the pinned, project-built static archive (tools/build_liquid.sh, no FFTW).
# Imported by absolute path; there is deliberately no find_package/find_library fallback, so
# Debian's libliquid (FFTW-linked) can never be picked up.
set(K2_LIQUID_PREFIX "${CMAKE_CURRENT_SOURCE_DIR}/../build/third_party/liquid-dsp" CACHE PATH
    "Prefix of the project-built liquid-dsp")
if(NOT EXISTS "${K2_LIQUID_PREFIX}/lib/libliquid.a" OR NOT EXISTS "${K2_LIQUID_PREFIX}/include/liquid/liquid.h")
  message(FATAL_ERROR "liquid-dsp not built at ${K2_LIQUID_PREFIX}: run `make liquid` (tools/build_liquid.sh)")
endif()
add_library(k2_liquid STATIC IMPORTED)
set_target_properties(k2_liquid PROPERTIES
  IMPORTED_LOCATION "${K2_LIQUID_PREFIX}/lib/libliquid.a"
  INTERFACE_INCLUDE_DIRECTORIES "${K2_LIQUID_PREFIX}/include"       # imported => -isystem, ahead of /usr/include
  INTERFACE_LINK_LIBRARIES m)

# libairspyhf (M1 live source + Window 1 probe). Built with it when installed (Debian:
# libairspyhf-dev); K2_REQUIRE_AIRSPYHF=ON makes it mandatory (CI, the Optiplex).
option(K2_REQUIRE_AIRSPYHF "Fail the configure step if libairspyhf is missing" OFF)
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(AIRSPYHF libairspyhf)
endif()
if(AIRSPYHF_FOUND)
  find_package(Threads REQUIRED)
  # Debian's libairspyhf.pc adds -I/usr/include/libusb-1.0 without depending on
  # libusb-1.0-0-dev; airspyhf.h needs only <stdint.h>, so drop paths that do not exist.
  set(K2_AIRSPYHF_INCLUDES "")
  foreach(dir IN LISTS AIRSPYHF_INCLUDE_DIRS)
    if(EXISTS "${dir}")
      list(APPEND K2_AIRSPYHF_INCLUDES "${dir}")
    endif()
  endforeach()
  message(STATUS "libairspyhf ${AIRSPYHF_VERSION}: ksdr-worker gets --source airspyhf; building the probe")
elseif(K2_REQUIRE_AIRSPYHF)
  message(FATAL_ERROR "libairspyhf not found (install libairspyhf-dev)")
else()
  message(STATUS "libairspyhf not found: ksdr-worker is replay-only; skipping the probe")
endif()

set(K2_WORKER_SOURCES src/main.cpp src/args.cpp src/device_lock.cpp src/io.cpp src/replay_source.cpp src/worker.cpp)
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
target_link_libraries(ksdr-worker PRIVATE k2dsp k2_liquid)
# Link map: every input file the linker used (archives and members), checked by tools/check_dsp_deps.py.
target_link_options(ksdr-worker PRIVATE "-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/ksdr-worker.map")
if(AIRSPYHF_FOUND)
  target_sources(ksdr-worker PRIVATE src/airspyhf_source.cpp)
  target_compile_definitions(ksdr-worker PRIVATE K2_HAVE_AIRSPYHF)
  target_include_directories(ksdr-worker PRIVATE ${K2_AIRSPYHF_INCLUDES})
  target_link_libraries(ksdr-worker PRIVATE ${AIRSPYHF_LINK_LIBRARIES} Threads::Threads)
  # Test-only twin: the same worker linked against our mock libairspyhf (synthetic device,
  # scripted faults), so the live path is tested without hardware. Never shipped.
  add_executable(ksdr-worker-airspyhf-mock ${K2_WORKER_SOURCES} src/airspyhf_source.cpp probe/mock_airspyhf.c)
  target_compile_definitions(ksdr-worker-airspyhf-mock PRIVATE K2_HAVE_AIRSPYHF)
  target_include_directories(ksdr-worker-airspyhf-mock PRIVATE src ${CMAKE_CURRENT_SOURCE_DIR}/../protocol
                             ${K2_AIRSPYHF_INCLUDES})
  target_compile_options(ksdr-worker-airspyhf-mock PRIVATE $<$<COMPILE_LANGUAGE:CXX>:-Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror>)
  target_link_libraries(ksdr-worker-airspyhf-mock PRIVATE k2dsp k2_liquid Threads::Threads m)
  if(K2_SANITIZE)
    target_compile_options(ksdr-worker-airspyhf-mock PRIVATE -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer)
    target_link_options(ksdr-worker-airspyhf-mock PRIVATE -fsanitize=address,undefined)
  endif()
endif()

# Worker DSP (M1 §3.2): spectrum, channels, AGC. Same strict warnings as the worker.
add_library(k2dsp STATIC src/dsp/convert.cpp src/dsp/agc.cpp src/dsp/spectrum.cpp src/dsp/channel.cpp)
target_include_directories(k2dsp PUBLIC src)
target_compile_options(k2dsp PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror)
target_link_libraries(k2dsp PUBLIC k2_liquid)
add_executable(ksdr-dsp-run dsp/dsp_run.cpp)
target_compile_options(ksdr-dsp-run PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror)
target_link_libraries(ksdr-dsp-run PRIVATE k2dsp)
target_link_options(ksdr-dsp-run PRIVATE "-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/ksdr-dsp-run.map")
if(K2_SANITIZE)
  foreach(t k2dsp ksdr-dsp-run)
    target_compile_options(${t} PRIVATE -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer)
  endforeach()
  target_link_options(ksdr-dsp-run PRIVATE -fsanitize=address,undefined)
endif()

# liquid-dsp verification and benchmark harness (tests only; not shipped).
add_executable(ksdr-dsp-check dsp/dsp_check.cpp)
target_include_directories(ksdr-dsp-check PRIVATE src)
target_compile_options(ksdr-dsp-check PRIVATE -Wall -Wextra -Werror -O2)
target_link_libraries(ksdr-dsp-check PRIVATE k2_liquid)
target_link_options(ksdr-dsp-check PRIVATE "-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/ksdr-dsp-check.map")

if(K2_SANITIZE)
  target_compile_options(ksdr-worker PRIVATE -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer)
  target_link_options(ksdr-worker PRIVATE -fsanitize=address,undefined)
endif()

# ---------------------------------------------------------------------------
# Airspy HF+ measurement probe (M1 Window 1), and its mock twin for tests.
if(AIRSPYHF_FOUND)
  add_executable(ksdr-airspyhf-probe probe/airspyhf_probe.cpp)
  target_include_directories(ksdr-airspyhf-probe PRIVATE ${K2_AIRSPYHF_INCLUDES})
  target_link_libraries(ksdr-airspyhf-probe PRIVATE ${AIRSPYHF_LINK_LIBRARIES} Threads::Threads)
  target_compile_options(ksdr-airspyhf-probe PRIVATE -Wall -Wextra -Wpedantic -Wshadow -Werror)
  add_executable(ksdr-airspyhf-probe-mock probe/airspyhf_probe.cpp probe/mock_airspyhf.c)
  target_include_directories(ksdr-airspyhf-probe-mock PRIVATE ${K2_AIRSPYHF_INCLUDES})
  target_link_libraries(ksdr-airspyhf-probe-mock PRIVATE Threads::Threads m)
  target_compile_options(ksdr-airspyhf-probe-mock PRIVATE -Wall -Wextra -Werror)
endif()
