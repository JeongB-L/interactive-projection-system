// server.cpp
// ---------------------------------------------------------------------------
// WHAT THIS FILE DOES
//   The hub's network layer, built on Boost.Beast. One thread handles each connection. A connection is one of:
//     - plain HTTP : serves the display page, apps and status page, and the /api/... control calls
//     - WebSocket /edge    : the Pi agent streams hand data in (and receives replies)
//     - WebSocket /display : the projected page receives actions and content
//   It contains no gesture logic: it only moves messages between the network and the Hub.
//
//   Safety rules implemented here (each has a test in tests/e2e.py):
//     - A URL can only ever name a file INSIDE the ui/ folder (no "..", no "//absolute/paths", no drive letters).
//     - Sensitive calls (shut down, ask the AI, calibrate (all /api/calibrate/... calls), dim, list stored files, open an arbitrary URL) are
//       accepted from this computer, or from elsewhere only with the secret IPS_TOKEN in an X-IPS-Token header.
//     - One malformed message cannot silently kill a connection's reader, and one stuck client cannot freeze
//       the hub: messages to each client go through that client's own queue and writer thread.
// ---------------------------------------------------------------------------
#include "server.hpp"

#include <atomic>
#include <boost/asio.hpp>
#include <boost/beast.hpp>
#include <boost/beast/websocket.hpp>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <mutex>
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

// Turn the part of a URL after its prefix into a file path that is GUARANTEED to be inside `root`, or an empty
// path when the request tries to leave it. This is what stops "GET /ui//etc/passwd" (a leading slash makes a
// path absolute, so joining it to the web folder would silently replace the folder) and "/ui/../x".
fs::path resolve_under(const fs::path& root, std::string rel) {
  // Drop leading slashes so the remainder is always relative.
  while (!rel.empty() && (rel[0] == '/' || rel[0] == '\\')) rel.erase(0, 1);
  if (rel.empty()) return {};
  // Refuse anything that could mean "somewhere else": backslashes, drive letters / colons, control characters.
  for (char c : rel)
    if (c == '\\' || c == ':' || static_cast<unsigned char>(c) < 32) return {};
  fs::path p(rel);
  for (const auto& part : p)
    if (part == "..") return {};
  // Final check on the normalized absolute paths: the result must start with the root folder.
  fs::path full = (root / p).lexically_normal();
  std::string base = fs::absolute(root).lexically_normal().generic_string();
  std::string want = fs::absolute(full).lexically_normal().generic_string();
  if (base.empty() || base.back() != '/') base += '/';
  if (want.compare(0, base.size(), base) != 0) return {};
  return full;
}

// Parse the request body as JSON; bad or empty bodies become an empty object so handlers stay simple.
json body_json(const Req& req) {
  if (req.body().empty()) return json::object();
  json j = json::parse(req.body(), nullptr, false);
  return j.is_object() ? j : json::object();
}

// May this request do something sensitive? Yes when it comes from this computer, or when it carries the
// secret IPS_TOKEN (from .env) in an "X-IPS-Token" header. With no token configured, only this computer may.
bool authorized(const Req& req, bool from_this_computer) {
  if (from_this_computer) return true;
  std::string token = env_or("IPS_TOKEN");
  return !token.empty() && std::string(req["X-IPS-Token"]) == token;
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

  // Who is asking? Requests from this computer (the Pi's own browser, the ips.py tool) are trusted.
  beast::error_code ec_peer;
  auto peer = s.remote_endpoint(ec_peer);
  bool local = !ec_peer && peer.address().is_loopback();
  bool ok = authorized(req, local);
  // Answer "not allowed" for a sensitive call made from another machine without the token.
  auto forbidden = [&]() {
    return reply_json(s, req,
                      {{"ok", false}, {"error", "forbidden: this call is only allowed from the hub computer, "
                                                "or with the IPS_TOKEN in an X-IPS-Token header"}},
                      http::status::forbidden);
  };

  // Static pages: "/" redirects to the display page; /status, /ui/... and /apps/... are files under ui/.
  if (path == "/") return reply(s, req, http::status::found, "text/plain", "", "/ui/display.html");
  if (path == "/status") return serve_file(s, req, fs::path(cfg.ui_dir) / "status.html");
  if (path.rfind("/ui/", 0) == 0) return serve_file(s, req, resolve_under(fs::path(cfg.ui_dir), path.substr(4)));
  if (path.rfind("/apps/", 0) == 0) return serve_file(s, req, resolve_under(fs::path(cfg.ui_dir) / "apps", path.substr(6)));

  // Read-only API calls (GET). Status and the app list are open; stored-file locations are not.
  if (path == "/api/status") return reply_json(s, req, hub.status());
  if (path == "/api/apps") return reply_json(s, req, hub.apps_json());
  if (path == "/api/sessions") return ok ? reply_json(s, req, hub.catalog().sessions()) : forbidden();
  if (path == "/api/assets") {
    if (!ok) return forbidden();
    // Optional filter: /api/assets?kind=video_mjpeg
    std::string kind;
    auto k = query.find("kind=");
    if (k != std::string::npos) kind = query.substr(k + 5, query.find('&', k) - k - 5);
    return reply_json(s, req, hub.catalog().assets(kind));
  }
  if (!post) return reply(s, req, http::status::not_found, "text/plain", "not found");

  // Control API calls (POST with a JSON body). The display page needs these three even from another machine.
  json b = body_json(req);
  if (path == "/api/display_size") {
    hub.set_display_size(b.value("w", 0), b.value("h", 0));
    return reply_json(s, req, {{"ok", true}});
  }
  if (path == "/api/home") { hub.home(); return reply_json(s, req, {{"ok", true}}); }
  if (path == "/api/launch") {
    // Opening one of the configured apps is open to the launcher page; opening an arbitrary URL is not.
    std::string what = b.value("app", b.value("url", ""));
    bool configured = what == "launcher";
    for (const auto& a : cfg.apps) configured = configured || a.id == what;
    if (!configured && !ok) return forbidden();
    bool done = hub.launch(what);
    return reply_json(s, req, {{"ok", done}}, done ? http::status::ok : http::status::bad_request);
  }
  // Everything below is sensitive: it changes the hub or spends the AI key.
  if (!ok) return forbidden();
  if (path == "/api/calibrate/start") { hub.calibrate_start(); return reply_json(s, req, {{"ok", true}}); }
  if (path == "/api/calibrate/reset") { hub.calibrate_reset(); return reply_json(s, req, {{"ok", true}}); }
  // Automatic surface calibration: the agent shows a marker pattern and measures it with the camera.
  if (path == "/api/calibrate/auto") {
    json r = hub.calibrate_auto();
    return reply_json(s, req, r, r.value("ok", false) ? http::status::ok : http::status::conflict);
  }
  if (path == "/api/calibrate/cancel") { hub.calibrate_cancel(); return reply_json(s, req, {{"ok", true}}); }
  // Install a homography measured elsewhere (tools/surface_calib.py on a recorded video): {"cam": 0, "h": [9 numbers]}.
  if (path == "/api/calibrate/set") {
    std::string err;
    int cam = b.contains("cam") && b["cam"].is_number_integer() ? b["cam"].get<int>() : 0;
    if (!hub.calibrate_set(cam, b.contains("h") ? b["h"] : json(), err))
      return reply_json(s, req, {{"ok", false}, {"error", err}}, http::status::bad_request);
    return reply_json(s, req, {{"ok", true}});
  }
  // Show / hide the marker pattern without measuring (to record a calibration video by hand).
  if (path == "/api/calibrate/pattern") {
    if (!hub.show_calib_pattern(b))
      return reply_json(s, req, {{"ok", false}, {"error", "bad pattern"}}, http::status::bad_request);
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

// A WebSocket peer with a thread-safe, NON-BLOCKING send(). Messages are put in this peer's own queue and a
// dedicated writer thread does the (possibly slow) network write. So a frozen browser tab or a dead Wi-Fi link can
// only ever delay its own queue: the hub's shared state is never held up by a socket write. If a peer falls too
// far behind, its oldest queued messages are dropped (a stale cursor position is worthless anyway).
struct WsPeer {
  websocket::stream<tcp::socket> ws;
  std::mutex m;
  std::condition_variable cv;
  std::deque<std::string> queue;
  size_t queued_bytes = 0;
  std::atomic<bool> alive{true};
  std::thread writer;
  static constexpr size_t kMaxQueuedBytes = 4 * 1024 * 1024;

  explicit WsPeer(tcp::socket&& s) : ws(std::move(s)) {}
  ~WsPeer() { close(); }

  // Queue one text message. Returns false once the peer is gone.
  bool send(const std::string& msg) {
    std::lock_guard<std::mutex> lk(m);
    if (!alive) return false;
    if (!writer.joinable()) writer = std::thread([this] { write_loop(); });
    queue.push_back(msg);
    queued_bytes += msg.size();
    while (queued_bytes > kMaxQueuedBytes && queue.size() > 1) {  // too far behind: drop the oldest
      queued_bytes -= queue.front().size();
      queue.pop_front();
    }
    cv.notify_one();
    return true;
  }

  // The writer thread: send queued messages one at a time until the peer disappears or we are closed.
  void write_loop() {
    for (;;) {
      std::string msg;
      {
        std::unique_lock<std::mutex> lk(m);
        cv.wait(lk, [&] { return !queue.empty() || !alive; });
        if (!alive) return;
        msg = std::move(queue.front());
        queue.pop_front();
        queued_bytes -= msg.size();
      }
      beast::error_code ec;
      ws.text(true);
      ws.write(net::buffer(msg), ec);
      if (ec) {  // the peer is gone: stop, and let send() report it
        alive = false;
        return;
      }
    }
  }

  // Stop everything. Shutting the socket first makes a write stuck on a dead peer fail, so the join cannot hang.
  void close() {
    {
      std::lock_guard<std::mutex> lk(m);
      alive = false;
    }
    cv.notify_all();
    beast::error_code ec;
    ws.next_layer().shutdown(tcp::socket::shutdown_both, ec);
    ws.next_layer().close(ec);
    if (writer.joinable()) writer.join();
  }
};

// Runs when an edge connection ends, however it ends (normal close, error, or an unexpected exception), so the
// hub never keeps believing in an agent that is gone.
struct EdgeCleanup {
  WsPeer& peer;
  Hub& hub;
  bool registered = false;
  ~EdgeCleanup() {
    peer.alive = false;
    if (registered) hub.edge_closed();  // finish the recording
  }
};

// The Pi agent's connection: read messages until it disconnects. A message the hub cannot understand is
// counted and skipped; it never ends the connection.
void handle_edge(WsPeer& p, Hub& hub) {
  std::string token = env_or("IPS_TOKEN"); // optional shared secret from .env
  beast::flat_buffer buf;
  p.ws.read_message_max(16 * 1024 * 1024);  // allow large JPEG frames
  EdgeCleanup cleanup{p, hub};
  for (;;) {
    // Wait for the next message; any error (including disconnect) ends the loop.
    beast::error_code ec;
    buf.clear();
    p.ws.read(buf, ec);
    if (ec) break;
    try {
      if (p.ws.got_text()) {
        // Text message = JSON control or hand data.
        json j = json::parse(beast::buffers_to_string(buf.data()), nullptr, false);
        if (!j.is_object()) {
          hub.note_bad_message();
          continue;
        }
        std::string t = j.value("t", "");
        if (t == "hello") {
          // First message: check the token (if one is configured), then register the Pi with the hub.
          if (!token.empty() && j.value("token", "") != token) {
            p.send(json{{"t", "error"}, {"error", "bad token"}}.dump());
            break;
          }
          hub.edge_connected(j, [&p](const std::string& m) { return p.send(m); });
          cleanup.registered = true;
        } else if (!cleanup.registered) {
          // Anything before hello is rejected.
          p.send(json{{"t", "error"}, {"error", "send hello first"}}.dump());
          break;
        } else if (t == "hands") {
          hub.on_hands(j); // the real-time path: see hub.cpp
        } else if (t == "calib_pattern") {
          hub.on_calib_pattern(j);  // automatic calibration: show / hide the marker pattern on the display
        } else if (t == "calib_result") {
          hub.on_calib_result(j);   // automatic calibration: the measured camera -> screen mappings
        } else if (t == "ping") {
          p.send(json{{"t", "pong"}, {"ts", j.value<int64_t>("ts", 0)}}.dump());
        }
      } else if (cleanup.registered && buf.size() > 1) {
        // Binary message = a camera JPEG: first byte is the camera index, the rest is the image.
        const auto* d = static_cast<const uint8_t*>(buf.data().data());
        hub.on_frame(d[0], d + 1, buf.size() - 1);
      }
    } catch (const std::exception&) {
      // Wrong types inside the message (for example a number where text belongs): count it and carry on.
      hub.note_bad_message();
    }
  }
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
  if (cfg.bind != "127.0.0.1" && cfg.bind != "localhost")
    std::printf("note: listening beyond this computer. Set IPS_TOKEN in .env so agents and remote tools must authenticate.\n");
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
