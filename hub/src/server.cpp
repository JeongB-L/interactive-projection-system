// server.cpp
// ---------------------------------------------------------------------------
// WHAT THIS FILE DOES
//   The hub's network layer, built on Boost.Beast. One thread handles each connection. A connection is one of:
//     - plain HTTP : serves the display page, apps and status page, and the /api/... control calls
//     - WebSocket /edge    : the Pi agent streams hand data in (and receives replies)
//     - WebSocket /display : the projected page receives actions and content
//   It contains no gesture logic: it only moves messages between the network and the Hub.
// ---------------------------------------------------------------------------
#include "server.hpp"

#include <atomic>
#include <boost/asio.hpp>
#include <boost/beast.hpp>
#include <boost/beast/websocket.hpp>
#include <cstdio>
#include <filesystem>
#include <thread>

#include "ai.hpp"

namespace ips {
// Short names for the Boost namespaces used below.
namespace beast = boost::beast;
namespace http = beast::http;
namespace websocket = beast::websocket;
namespace net = boost::asio;
using tcp = net::ip::tcp;
namespace fs = std::filesystem;

namespace {

using Req = http::request<http::string_body>;

// ---- small HTTP helpers --------------------------------------------------------

// Pick a Content-Type header from a file extension.
std::string mime(const fs::path& p) {
  auto e = p.extension().string();
  if (e == ".html") return "text/html; charset=utf-8";
  if (e == ".js") return "application/javascript";
  if (e == ".css") return "text/css";
  if (e == ".json") return "application/json";
  if (e == ".svg") return "image/svg+xml";
  if (e == ".png") return "image/png";
  if (e == ".ico") return "image/x-icon";
  return "application/octet-stream";
}

// Send one HTTP response and close the connection. (Simple: no keep-alive.)
void reply(tcp::socket& s, const Req& req, http::status st, const std::string& type, std::string body,
           const std::string& location = "") {
  http::response<http::string_body> res{st, req.version()};
  res.set(http::field::server, "ipsd");
  res.set(http::field::content_type, type);
  res.set(http::field::cache_control, "no-store"); // always show the latest page while developing
  if (!location.empty()) res.set(http::field::location, location);
  res.keep_alive(false);
  res.body() = std::move(body);
  res.prepare_payload();
  beast::error_code ec;
  http::write(s, res, ec);
  s.shutdown(tcp::socket::shutdown_send, ec);
}

// Shortcut: reply with a JSON body.
void reply_json(tcp::socket& s, const Req& req, const json& j, http::status st = http::status::ok) {
  reply(s, req, st, "application/json", j.dump());
}

// Serve one file from disk, or 404 if it does not exist.
void serve_file(tcp::socket& s, const Req& req, const fs::path& p) {
  if (!fs::is_regular_file(p)) return reply(s, req, http::status::not_found, "text/plain", "not found");
  reply(s, req, http::status::ok, mime(p), load_text(p.string()));
}

// Reject paths that try to escape the web folder ("..") or name a drive (":").
bool safe(const std::string& rel) { return rel.find("..") == std::string::npos && rel.find(':') == std::string::npos; }

// Parse the request body as JSON; bad or empty bodies become an empty object so handlers stay simple.
json body_json(const Req& req) {
  if (req.body().empty()) return json::object();
  json j = json::parse(req.body(), nullptr, false);
  return j.is_discarded() ? json::object() : j;
}

// ---- HTTP routes ---------------------------------------------------------------

// Decide what to do with one plain HTTP request.
void handle_http(tcp::socket& s, const Req& req, Hub& hub) {
  // Split "/path?query" into its two parts.
  std::string target(req.target());
  std::string path = target.substr(0, target.find('?'));
  std::string query = target.find('?') == std::string::npos ? "" : target.substr(target.find('?') + 1);
  const Config& cfg = hub.config();
  bool post = req.method() == http::verb::post;

  // Static pages: "/" redirects to the display page; /status, /ui/... and /apps/... are files under ui/.
  if (path == "/") return reply(s, req, http::status::found, "text/plain", "", "/ui/display.html");
  if (path == "/status") return serve_file(s, req, fs::path(cfg.ui_dir) / "status.html");
  if (path.rfind("/ui/", 0) == 0 && safe(path)) return serve_file(s, req, fs::path(cfg.ui_dir) / path.substr(4));
  if (path.rfind("/apps/", 0) == 0 && safe(path)) return serve_file(s, req, fs::path(cfg.ui_dir) / "apps" / path.substr(6));

  // Read-only API calls (GET).
  if (path == "/api/status") return reply_json(s, req, hub.status());
  if (path == "/api/apps") return reply_json(s, req, hub.apps_json());
  if (path == "/api/sessions") return reply_json(s, req, hub.catalog().sessions());
  if (path == "/api/assets") {
    // Optional filter: /api/assets?kind=video_mjpeg
    std::string kind;
    auto k = query.find("kind=");
    if (k != std::string::npos) kind = query.substr(k + 5, query.find('&', k) - k - 5);
    return reply_json(s, req, hub.catalog().assets(kind));
  }
  if (!post) return reply(s, req, http::status::not_found, "text/plain", "not found");

  // Control API calls (POST with a JSON body).
  json b = body_json(req);
  if (path == "/api/launch") {
    bool ok = hub.launch(b.value("app", b.value("url", "")));
    return reply_json(s, req, {{"ok", ok}}, ok ? http::status::ok : http::status::bad_request);
  }
  if (path == "/api/home") { hub.home(); return reply_json(s, req, {{"ok", true}}); }
  if (path == "/api/calibrate/start") { hub.calibrate_start(); return reply_json(s, req, {{"ok", true}}); }
  if (path == "/api/calibrate/reset") { hub.calibrate_reset(); return reply_json(s, req, {{"ok", true}}); }
  if (path == "/api/display_size") {
    hub.set_display_size(b.value("w", 0), b.value("h", 0));
    return reply_json(s, req, {{"ok", true}});
  }
  if (path == "/api/brightness") { hub.set_brightness(b.value("value", 1.0)); return reply_json(s, req, {{"ok", true}}); }
  if (path == "/api/ai") {
    // Ask the AI provider (key stays on the hub); pass its answer or error back to the page.
    int st = 200;
    json r = ai_ask(b.value("prompt", ""), st);
    return reply_json(s, req, r, static_cast<http::status>(st));
  }
  if (path == "/api/shutdown") {
    // Answer first, then flush recordings and exit shortly after.
    reply_json(s, req, {{"ok", true}});
    std::thread([&hub] {
      std::this_thread::sleep_for(std::chrono::milliseconds(200));
      hub.shutdown();  // flush recordings, close the session row
      std::_Exit(0);
    }).detach();
    return;
  }
  reply(s, req, http::status::not_found, "text/plain", "not found");
}

// ---- WebSocket helpers ---------------------------------------------------------

// A WebSocket peer with a thread-safe "send text" method. Several threads may send to the same
// peer (e.g. the hub pushing actions while a heartbeat runs), so writes are serialized by a mutex.
struct WsPeer {
  websocket::stream<tcp::socket> ws;
  std::mutex m;
  std::atomic<bool> alive{true};
  explicit WsPeer(tcp::socket&& s) : ws(std::move(s)) {}
  bool send(const std::string& msg) {
    std::lock_guard lk(m);
    if (!alive) return false;
    beast::error_code ec;
    ws.text(true);
    ws.write(net::buffer(msg), ec);
    if (ec) alive = false; // a failed write means the peer is gone
    return !ec;
  }
};

// The Pi agent's connection: read messages until it disconnects.
void handle_edge(WsPeer& p, Hub& hub) {
  std::string token = env_or("IPS_TOKEN"); // optional shared secret from .env
  beast::flat_buffer buf;
  p.ws.read_message_max(16 * 1024 * 1024);  // allow large JPEG frames
  bool hello = false;
  for (;;) {
    // Wait for the next message; any error (including disconnect) ends the loop.
    beast::error_code ec;
    buf.clear();
    p.ws.read(buf, ec);
    if (ec) break;
    if (p.ws.got_text()) {
      // Text message = JSON control or hand data.
      json j = json::parse(beast::buffers_to_string(buf.data()), nullptr, false);
      if (!j.is_object()) continue;
      std::string t = j.value("t", "");
      if (t == "hello") {
        // First message: check the token (if one is configured), then register the Pi with the hub.
        if (!token.empty() && j.value("token", "") != token) {
          p.send(json{{"t", "error"}, {"error", "bad token"}}.dump());
          break;
        }
        hub.edge_connected(j, [&p](const std::string& m) { return p.send(m); });
        hello = true;
      } else if (!hello) {
        // Anything before hello is rejected.
        p.send(json{{"t", "error"}, {"error", "send hello first"}}.dump());
        break;
      } else if (t == "hands") {
        hub.on_hands(j); // the real-time path: see hub.cpp
      } else if (t == "ping") {
        p.send(json{{"t", "pong"}, {"ts", j.value<int64_t>("ts", 0)}}.dump());
      }
    } else if (hello && buf.size() > 1) {
      // Binary message = a camera JPEG: first byte is the camera index, the rest is the image.
      const auto* d = static_cast<const uint8_t*>(buf.data().data());
      hub.on_frame(d[0], d + 1, buf.size() - 1);
    }
  }
  p.alive = false;
  if (hello) hub.edge_closed(); // finish the recording
}

// A projected page's connection: register it, then keep it alive with a heartbeat. The hub pushes
// actions to it from other threads; a failed heartbeat means the page has closed.
void handle_display(WsPeer& p, Hub& hub) {
  int id = hub.add_display([&p](const std::string& m) { return p.send(m); });
  while (p.alive) {
    std::this_thread::sleep_for(std::chrono::seconds(1));
    if (!p.send("{\"t\":\"hb\"}")) break;
  }
  hub.remove_display(id);
}

// ---- one connection ----------------------------------------------------------

// Runs on its own thread for each incoming connection: read the first HTTP request, then either
// upgrade to a WebSocket (/edge or /display) or answer it as plain HTTP.
void session(tcp::socket sock, Hub& hub) {
  try {
    beast::flat_buffer buf;
    http::request_parser<http::string_body> parser;
    parser.body_limit(8 * 1024 * 1024);
    beast::error_code ec;
    http::read(sock, buf, parser, ec);
    if (ec) return;
    Req req = parser.release();
    if (websocket::is_upgrade(req)) {
      std::string target(req.target());
      WsPeer peer(std::move(sock));
      peer.ws.accept(req, ec);
      if (ec) return;
      if (target.rfind("/edge", 0) == 0) handle_edge(peer, hub);
      else if (target.rfind("/display", 0) == 0) handle_display(peer, hub);
      return;
    }
    handle_http(sock, req, hub);
  } catch (const std::exception& e) {
    std::fprintf(stderr, "connection error: %s\n", e.what());
  }
}

}  // namespace

// ---- accept loop -----------------------------------------------------------------

// Open the listening socket, then accept connections forever, giving each its own thread.
void run_server(Hub& hub) {
  const Config& cfg = hub.config();
  net::io_context ioc;
  tcp::acceptor acc(ioc, {net::ip::make_address(cfg.bind), static_cast<unsigned short>(cfg.port)});
  std::printf("ipsd listening on http://%s:%d  (display: /ui/display.html, status: /status)\n", cfg.bind.c_str(), cfg.port);
  std::fflush(stdout);
  for (;;) {
    tcp::socket sock(ioc);
    beast::error_code ec;
    acc.accept(sock, ec);
    if (ec) continue;
    std::thread(session, std::move(sock), std::ref(hub)).detach();
  }
}

}  // namespace ips
