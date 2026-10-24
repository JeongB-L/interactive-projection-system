// ai.hpp
// ---------------------------------------------------------------------------
// WHAT THIS FILE DECLARES
//   One function, ai_ask(), used by the AI-chat app. The display page sends a question to the hub
//   (POST /api/ai); the hub calls the AI provider. The API key is read from the hub's environment
//   (.env file), so the key never reaches the Pi or the browser.
// ---------------------------------------------------------------------------
#pragma once
#include <string>

#include "common.hpp"

namespace ips {

// Returns {"ok":true,"text":...} or {"ok":false,"error":...}. `status` receives the HTTP status
// the hub should answer with (200 ok, 503 not configured, 502 provider unreachable, or the provider's).
json ai_ask(const std::string& prompt, int& status);

}  // namespace ips
