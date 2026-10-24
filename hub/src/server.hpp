#pragma once
#include "hub.hpp"

namespace ips {
// Blocks forever: accepts HTTP + WebSocket connections, one thread per connection.
void run_server(Hub& hub);
}  // namespace ips
