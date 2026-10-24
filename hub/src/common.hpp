// Shared types and small helpers. No I/O except load_text / load_dotenv.
#pragma once
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace ips {

using json = nlohmann::json;

inline int64_t steady_us() {
  using namespace std::chrono;
  return duration_cast<microseconds>(steady_clock::now().time_since_epoch()).count();
}
inline int64_t wall_us() {
  using namespace std::chrono;
  return duration_cast<microseconds>(system_clock::now().time_since_epoch()).count();
}

struct Vec2 {
  double x = 0, y = 0;
};

inline double dist(Vec2 a, Vec2 b) { return std::hypot(a.x - b.x, a.y - b.y); }

// MediaPipe layout: 21 landmarks x (x, y, z), normalized image coordinates.
struct Hand {
  bool right = true;
  float score = 0;
  std::array<float, 63> xyz{};
  Vec2 pt(int i) const { return {xyz[i * 3], xyz[i * 3 + 1]}; }
};

struct HandFrame {
  int camera = 0;
  uint64_t seq = 0;
  int64_t capture_us = 0;  // wall clock on the edge, microseconds
  float track_ms = 0;
  std::vector<Hand> hands;
};

inline bool parse_hand_frame(const json& j, HandFrame& f) {
  try {
    f.camera = j.value("cam", 0);
    f.seq = j.value<uint64_t>("seq", 0);
    f.capture_us = j.value<int64_t>("ts", 0);
    f.track_ms = j.value("track_ms", 0.0f);
    for (const auto& h : j.at("hands")) {
      Hand hd;
      hd.right = h.value("right", true);
      hd.score = h.value("score", 1.0f);
      const auto& pts = h.at("pts");
      if (pts.size() != 21) return false;
      for (int i = 0; i < 21; ++i)
        for (int k = 0; k < 3; ++k) hd.xyz[i * 3 + k] = pts[i].at(k).get<float>();
      f.hands.push_back(hd);
    }
    return true;
  } catch (...) {
    return false;
  }
}

inline std::string load_text(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) return {};
  std::stringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

inline void set_env(const std::string& k, const std::string& v) {
#ifdef _WIN32
  _putenv_s(k.c_str(), v.c_str());
#else
  setenv(k.c_str(), v.c_str(), 0);
#endif
}

// KEY=VALUE lines; does not override variables already set in the environment.
inline void load_dotenv(const std::string& path) {
  std::ifstream in(path);
  std::string line;
  while (std::getline(in, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (line.empty() || line[0] == '#') continue;
    auto eq = line.find('=');
    if (eq == std::string::npos) continue;
    std::string k = line.substr(0, eq), v = line.substr(eq + 1);
    if (v.size() >= 2 && (v.front() == '"' || v.front() == '\'') && v.back() == v.front())
      v = v.substr(1, v.size() - 2);
    if (!std::getenv(k.c_str())) set_env(k, v);
  }
}

inline std::string env_or(const char* k, const std::string& dflt = "") {
  const char* v = std::getenv(k);
  return (v && *v) ? std::string(v) : dflt;
}

}  // namespace ips
