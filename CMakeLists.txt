cmake_minimum_required(VERSION 3.20)
if(POLICY CMP0167)
  cmake_policy(SET CMP0167 NEW)
endif()
project(ips CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(Boost REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(nlohmann_json REQUIRED)
find_package(SQLite3 REQUIRED)

add_library(ipscore STATIC
  hub/src/gesture.cpp
  hub/src/storage.cpp
  hub/src/hub.cpp
  hub/src/ai.cpp)
target_include_directories(ipscore PUBLIC hub/src)
target_link_libraries(ipscore PUBLIC Boost::headers OpenSSL::SSL OpenSSL::Crypto
                      nlohmann_json::nlohmann_json SQLite::SQLite3)
if(WIN32)
  target_compile_definitions(ipscore PUBLIC _WIN32_WINNT=0x0A00)
  target_link_libraries(ipscore PUBLIC ws2_32 mswsock)
endif()

add_executable(ipsd hub/src/main.cpp hub/src/server.cpp)
target_link_libraries(ipsd PRIVATE ipscore)

enable_testing()
add_executable(ips_tests hub/tests/test_main.cpp)
target_link_libraries(ips_tests PRIVATE ipscore)
add_test(NAME ips_tests COMMAND ips_tests)
