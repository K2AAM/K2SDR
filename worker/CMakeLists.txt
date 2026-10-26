cmake_minimum_required(VERSION 3.25)
project(k2websdr_worker LANGUAGES C CXX)

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

# ---------------------------------------------------------------------------
# Airspy HF+ measurement probe (M1 Window 1). Built only when libairspyhf is
# installed (Debian: libairspyhf-dev). K2_REQUIRE_AIRSPYHF=ON makes it mandatory (CI).
option(K2_REQUIRE_AIRSPYHF "Fail the configure step if libairspyhf is missing" OFF)
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(AIRSPYHF IMPORTED_TARGET libairspyhf)
endif()
if(AIRSPYHF_FOUND)
  find_package(Threads REQUIRED)
  add_executable(ksdr-airspyhf-probe probe/airspyhf_probe.cpp)
  target_link_libraries(ksdr-airspyhf-probe PRIVATE PkgConfig::AIRSPYHF Threads::Threads)
  target_compile_options(ksdr-airspyhf-probe PRIVATE -Wall -Wextra -Wpedantic -Wshadow -Werror)
  message(STATUS "libairspyhf ${AIRSPYHF_VERSION}: building ksdr-airspyhf-probe")
  # Test-only twin linked against a mock library (synthetic signal, scripted faults).
  add_executable(ksdr-airspyhf-probe-mock probe/airspyhf_probe.cpp probe/mock_airspyhf.c)
  target_include_directories(ksdr-airspyhf-probe-mock PRIVATE ${AIRSPYHF_INCLUDE_DIRS})
  target_link_libraries(ksdr-airspyhf-probe-mock PRIVATE Threads::Threads m)
  target_compile_options(ksdr-airspyhf-probe-mock PRIVATE -Wall -Wextra -Werror)
elseif(K2_REQUIRE_AIRSPYHF)
  message(FATAL_ERROR "libairspyhf not found (install libairspyhf-dev)")
else()
  message(STATUS "libairspyhf not found: skipping ksdr-airspyhf-probe")
endif()
