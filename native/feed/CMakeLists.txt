# SPDX-License-Identifier: Apache-2.0
cmake_minimum_required(VERSION 3.20)
project(pineforge_feed VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
find_package(CURL 8.14.1 REQUIRED)
find_package(OpenSSL REQUIRED COMPONENTS Crypto)
find_package(Threads REQUIRED)
include(CheckCXXSourceRuns)
get_target_property(CURL_CHECK_TARGET CURL::libcurl ALIASED_TARGET)
if(NOT CURL_CHECK_TARGET)
  set(CURL_CHECK_TARGET CURL::libcurl)
endif()
set(CMAKE_REQUIRED_LIBRARIES ${CURL_CHECK_TARGET})
check_cxx_source_runs("#include <curl/curl.h>
#include <cstring>
int main() {
  const auto* info = curl_version_info(CURLVERSION_NOW);
  if (!info || info->version_num < 0x080e01) return 1;
  bool ws = false, wss = false;
  for (const char* const* protocol = info->protocols; protocol && *protocol; ++protocol) {
    ws |= std::strcmp(*protocol, \"ws\") == 0;
    wss |= std::strcmp(*protocol, \"wss\") == 0;
  }
  return ws && wss ? 0 : 1;
}" PINEFORGE_CURL_WEBSOCKETS)
unset(CMAKE_REQUIRED_LIBRARIES)
if(NOT PINEFORGE_CURL_WEBSOCKETS)
  message(FATAL_ERROR "pineforge-feed requires libcurl >= 8.14.1 with both ws and wss")
endif()

file(SHA256 "${CMAKE_CURRENT_SOURCE_DIR}/vendor/json.hpp" JSON_SHA256)
if(NOT JSON_SHA256 STREQUAL "9431c7c311d74be1cea0e8d1a3942a33528ac11f6e67ee67d000cf4a8af7f830")
  message(FATAL_ERROR "The attributed runner/json.hpp pin does not match")
endif()

set(PINEFORGE_FEED_SANITIZER "" CACHE STRING "Sanitizer: address,undefined or thread")
if(PINEFORGE_FEED_SANITIZER)
  if(NOT PINEFORGE_FEED_SANITIZER MATCHES "^(address,undefined|thread)$")
    message(FATAL_ERROR "Unsupported sanitizer profile")
  endif()
  add_compile_options(-fsanitize=${PINEFORGE_FEED_SANITIZER} -fno-omit-frame-pointer -g)
  add_link_options(-fsanitize=${PINEFORGE_FEED_SANITIZER})
endif()

add_library(pineforge_feed_core STATIC src/core.cpp src/state.cpp src/transport.cpp src/binance.cpp src/okx.cpp
  src/bybit.cpp src/session.cpp)
target_include_directories(pineforge_feed_core PUBLIC src vendor)
target_link_libraries(pineforge_feed_core PUBLIC CURL::libcurl OpenSSL::Crypto Threads::Threads)
target_compile_options(pineforge_feed_core PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_executable(pineforge-feed src/main.cpp)
target_link_libraries(pineforge-feed PRIVATE pineforge_feed_core)
target_compile_options(pineforge-feed PRIVATE -Wall -Wextra -Wpedantic -Werror)
install(TARGETS pineforge-feed RUNTIME DESTINATION bin)

# serve: the only code path that links an HTTP/WebSocket server. CivetWeb 1.16 (MIT) is fetched by release
# archive and pinned by SHA-256; only its core is compiled: no TLS, CGI, Lua, Duktape, WebDAV, SSI or files.
option(PINEFORGE_FEED_SERVE "Build the serve subcommand with the pinned CivetWeb release" ON)
if(PINEFORGE_FEED_SERVE)
  enable_language(C)
  include(FetchContent)
  set(PINEFORGE_FEED_CIVETWEB_ARCHIVE "https://github.com/civetweb/civetweb/archive/refs/tags/v1.16.tar.gz"
      CACHE STRING "CivetWeb 1.16 release archive, a URL or a local path; its SHA-256 is pinned")
  FetchContent_Declare(civetweb URL "${PINEFORGE_FEED_CIVETWEB_ARCHIVE}"
    URL_HASH SHA256=f0e471c1bf4e7804a6cfb41ea9d13e7d623b2bcc7bc1e2a4dd54951a24d60285
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE SOURCE_SUBDIR do-not-add-civetweb-cmake)
  FetchContent_MakeAvailable(civetweb)
  add_library(pineforge_feed_civetweb STATIC "${civetweb_SOURCE_DIR}/src/civetweb.c")
  target_include_directories(pineforge_feed_civetweb SYSTEM PUBLIC "${civetweb_SOURCE_DIR}/include")
  target_compile_definitions(pineforge_feed_civetweb PRIVATE NO_SSL NO_CGI NO_FILES NO_CACHING
    PUBLIC USE_WEBSOCKET)
  set_target_properties(pineforge_feed_civetweb PROPERTIES C_STANDARD 99)
  target_compile_options(pineforge_feed_civetweb PRIVATE -w)
  target_link_libraries(pineforge_feed_civetweb PUBLIC Threads::Threads ${CMAKE_DL_LIBS})
  add_library(pineforge_feed_serve STATIC src/serve.cpp)
  target_link_libraries(pineforge_feed_serve PUBLIC pineforge_feed_core PRIVATE pineforge_feed_civetweb)
  target_compile_options(pineforge_feed_serve PRIVATE -Wall -Wextra -Wpedantic -Werror)
  target_link_libraries(pineforge-feed PRIVATE pineforge_feed_serve)
  target_compile_definitions(pineforge-feed PRIVATE PINEFORGE_FEED_SERVE=1)
endif()

include(CTest)
if(BUILD_TESTING)
  add_executable(feed-unit tests/unit.cpp)
  target_link_libraries(feed-unit PRIVATE pineforge_feed_core)
  if(PINEFORGE_FEED_SERVE)
    target_link_libraries(feed-unit PRIVATE pineforge_feed_serve)
    target_compile_definitions(feed-unit PRIVATE PINEFORGE_FEED_SERVE=1)
  endif()
  target_compile_options(feed-unit PRIVATE -Wall -Wextra -Wpedantic -Werror -UNDEBUG)
  add_test(NAME feed-unit COMMAND feed-unit)
  find_package(Python3 REQUIRED COMPONENTS Interpreter)
  add_test(NAME feed-mock COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/mock_venue.py $<TARGET_FILE:pineforge-feed>)
  set_tests_properties(feed-mock PROPERTIES TIMEOUT 420)
endif()
