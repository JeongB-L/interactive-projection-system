# CMakeLists.txt
# -----------------------------------------------------------------------------
# WHAT THIS FILE DOES
#   Build recipe for the C++ hub. It produces three things:
#     ipscore    a library with all the hub logic (gestures, storage, hub, AI client)
#     ipsd       the hub program itself (the library + main.cpp + the network server)
#     ips_tests  the unit tests (the library + tests)
#   Build with:  python ips.py build   (or run cmake directly)
# -----------------------------------------------------------------------------
cmake_minimum_required(VERSION 3.20)
# Use the modern way of finding Boost (only on CMake versions that know this policy).
if(POLICY CMP0167)
  cmake_policy(SET CMP0167 NEW)
endif()
project(ips CXX)

# The code uses C++20 features.
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# Default to an optimized build.
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# Libraries the hub needs: Boost (networking/WebSocket, headers only), OpenSSL (HTTPS to the AI
# provider), nlohmann_json (JSON), SQLite3 (the catalog database).
find_package(Boost REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(nlohmann_json REQUIRED)
find_package(SQLite3 REQUIRED)

# The core library: everything except main() and the server, so tests can use it too.
add_library(ipscore STATIC
  hub/src/gesture.cpp
  hub/src/storage.cpp
  hub/src/hub.cpp
  hub/src/ai.cpp)
target_include_directories(ipscore PUBLIC hub/src)
target_link_libraries(ipscore PUBLIC Boost::headers OpenSSL::SSL OpenSSL::Crypto
                      nlohmann_json::nlohmann_json SQLite::SQLite3)
# Windows needs a target-version define and the socket libraries.
if(WIN32)
  target_compile_definitions(ipscore PUBLIC _WIN32_WINNT=0x0A00)
  target_link_libraries(ipscore PUBLIC ws2_32 mswsock)
endif()

# The hub executable.
add_executable(ipsd hub/src/main.cpp hub/src/server.cpp)
target_link_libraries(ipsd PRIVATE ipscore)

# The unit-test executable, registered with CTest.
enable_testing()
add_executable(ips_tests hub/tests/test_main.cpp)
target_link_libraries(ips_tests PRIVATE ipscore)
add_test(NAME ips_tests COMMAND ips_tests)
