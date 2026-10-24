#include <cstdio>
#include <cstring>

#include "server.hpp"

int main(int argc, char** argv) {
  std::string cfg_path = "config.json";
  for (int i = 1; i < argc; ++i)
    if (!std::strcmp(argv[i], "--config") && i + 1 < argc) cfg_path = argv[++i];
    else if (!std::strcmp(argv[i], "--help")) {
      std::printf("ipsd [--config config.json]\nSecrets are read from .env in the working directory.\n");
      return 0;
    }
  try {
    ips::load_dotenv(".env");
    ips::Hub hub(ips::Config::load(cfg_path));
    ips::run_server(hub);
  } catch (const std::exception& e) {
    std::fprintf(stderr, "fatal: %s\n", e.what());
    return 1;
  }
}
