// main.cpp
// ---------------------------------------------------------------------------
// WHAT THIS FILE DOES
//   Program entry point for the hub (`ipsd`). It only wires things together:
//     1. read the optional --config argument,
//     2. load secrets from .env,
//     3. build the Hub from config.json,
//     4. start the server (which runs until the program is stopped).
// ---------------------------------------------------------------------------
#include <cstdio>
#include <cstring>

#include "server.hpp"

int main(int argc, char** argv) {
  // Command-line options: --config <file> chooses the settings file; --help prints usage.
  std::string cfg_path = "config.json";
  for (int i = 1; i < argc; ++i)
    if (!std::strcmp(argv[i], "--config") && i + 1 < argc) cfg_path = argv[++i];
    else if (!std::strcmp(argv[i], "--help")) {
      std::printf("ipsd [--config config.json]\nSecrets are read from .env in the working directory.\n");
      return 0;
    }
  try {
    ips::load_dotenv(".env");                          // secrets (AI key, token) into the environment
    ips::Hub hub(ips::Config::load(cfg_path));         // settings, storage, calibration
    ips::run_server(hub);                              // serve forever
  } catch (const std::exception& e) {
    // Any startup failure (bad config, port in use, ...) ends up here with a readable message.
    std::fprintf(stderr, "fatal: %s\n", e.what());
    return 1;
  }
}
