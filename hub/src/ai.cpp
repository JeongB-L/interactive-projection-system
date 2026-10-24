#include "ai.hpp"

#include <boost/asio.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/beast.hpp>
#include <boost/beast/ssl.hpp>

namespace ips {
namespace beast = boost::beast;
namespace http = beast::http;
namespace net = boost::asio;
namespace ssl = net::ssl;
using tcp = net::ip::tcp;

json ai_ask(const std::string& prompt, int& status) {
  std::string key = env_or("ANTHROPIC_API_KEY");
  if (key.empty()) {
    status = 503;
    return {{"ok", false}, {"error", "AI is not configured: set ANTHROPIC_API_KEY in .env on the hub."}};
  }
  std::string host = env_or("AI_HOST", "api.anthropic.com");
  std::string model = env_or("AI_MODEL", "claude-haiku-5-5");
  try {
    net::io_context ioc;
    ssl::context ctx(ssl::context::tls_client);
    ctx.set_default_verify_paths();
    std::string ca = env_or("SSL_CERT_FILE");
    if (!ca.empty()) ctx.load_verify_file(ca);
    ctx.set_verify_mode(ssl::verify_peer);

    tcp::resolver resolver(ioc);
    beast::ssl_stream<beast::tcp_stream> stream(ioc, ctx);
    if (!SSL_set_tlsext_host_name(stream.native_handle(), host.c_str())) throw std::runtime_error("SNI failed");
    stream.set_verify_callback(ssl::host_name_verification(host));
    beast::get_lowest_layer(stream).expires_after(std::chrono::seconds(30));
    beast::get_lowest_layer(stream).connect(resolver.resolve(host, "443"));
    stream.handshake(ssl::stream_base::client);

    json body = {{"model", model}, {"max_tokens", 400},
                 {"messages", json::array({{{"role", "user"}, {"content", prompt}}})}};
    http::request<http::string_body> req{http::verb::post, "/v1/messages", 11};
    req.set(http::field::host, host);
    req.set(http::field::content_type, "application/json");
    req.set("x-api-key", key);
    req.set("anthropic-version", "2023-06-01");
    req.body() = body.dump();
    req.prepare_payload();
    http::write(stream, req);

    beast::flat_buffer buf;
    http::response<http::string_body> res;
    http::read(stream, buf, res);
    beast::error_code ec;
    stream.shutdown(ec);

    json j = json::parse(res.body(), nullptr, false);
    if (res.result_int() != 200) {
      status = int(res.result_int());
      std::string msg = j.is_object() && j.contains("error") ? j["error"].value("message", "error") : "provider error";
      return {{"ok", false}, {"error", msg}, {"provider_status", res.result_int()}};
    }
    std::string text;
    for (const auto& c : j.value("content", json::array()))
      if (c.value("type", "") == "text") text += c.value("text", "");
    status = 200;
    return {{"ok", true}, {"text", text}, {"model", model}};
  } catch (const std::exception& e) {
    status = 502;
    return {{"ok", false}, {"error", std::string("cannot reach AI provider (offline?): ") + e.what()}};
  }
}

}  // namespace ips
