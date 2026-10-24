// AI chat proxy: the API key stays in the hub's environment (.env), never reaches the Pi or browser.
#pragma once
#include <string>

#include "common.hpp"

namespace ips {

// Returns {"ok":bool,"text":...} or {"ok":false,"error":...}; http status in `status`.
json ai_ask(const std::string& prompt, int& status);

}  // namespace ips
