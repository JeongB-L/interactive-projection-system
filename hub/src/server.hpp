// server.hpp
// ---------------------------------------------------------------------------
// WHAT THIS FILE DECLARES
//   run_server(): the hub's network front door (implemented in server.cpp).
// ---------------------------------------------------------------------------
#pragma once
#include "hub.hpp"

namespace ips {
// Listens on the configured port and serves forever: web pages, the HTTP API, and the two
// WebSocket paths (/edge for the Pi agent, /display for the projected page). Never returns normally.
void run_server(Hub& hub);
}  // namespace ips
