// common.hpp
// ---------------------------------------------------------------------------
// WHAT THIS FILE DOES
//   Small shared building blocks used by every other hub file:
//     - the "Hand" and "HandFrame" data types (what the Pi sends us),
//     - clock helpers,
//     - a parser that turns the Pi's JSON message into a HandFrame,
//     - tiny helpers for reading files, environment variables and the .env secrets file.
//   It contains no networking and no gesture logic.
// ---------------------------------------------------------------------------
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

// "json" is our shorthand for the JSON library type used for all messages.
using json = nlohmann::json;

// ---- clocks ---------------------------------------------------------------

// Monotonic clock in microseconds: never jumps backwards, used for measuring durations.
inline int64_t steady_us() {
  using namespace std::chrono;
  return duration_cast<microseconds>(steady_clock::now().time_since_epoch()).count();
}
// Wall-clock time in microseconds: comparable with timestamps the Pi sends (needs synced clocks).
inline int64_t wall_us() {
  using namespace std::chrono;
  return duration_cast<microseconds>(system_clock::now().time_since_epoch()).count();
}

// ---- geometry -------------------------------------------------------------

// A 2D point (normalized camera coordinates, or projector pixels depending on context).
struct Vec2 {
  double x = 0, y = 0;
};

// Straight-line distance between two points.
inline double dist(Vec2 a, Vec2 b) { return std::hypot(a.x - b.x, a.y - b.y); }

// ---- hand data from the Pi -------------------------------------------------

// One detected hand: 21 landmarks stored flat as x,y,z triples (MediaPipe order, normalized 0..1).
//   landmark 0 = wrist, 4 = thumb tip, 8 = index tip, 9 = middle-finger base (palm center), etc.
struct Hand {
  bool right = true;           // true = right hand
  float score = 0;             // detector confidence 0..1
  std::array<float, 63> xyz{}; // 21 landmarks * 3 numbers
  // Convenience accessor: the x,y of landmark i (z is ignored by the 2D logic).
  Vec2 pt(int i) const { return {xyz[i * 3], xyz[i * 3 + 1]}; }
};

// Everything one camera reports for one video frame.
struct HandFrame {
  int camera = 0;         // which camera (0, 1, ...)
  uint64_t seq = 0;       // per-camera counter, lets us notice dropped frames
  int64_t capture_us = 0; // when the Pi captured it (wall clock), for latency measurement
  float track_ms = 0;     // how long the Pi spent tracking, for benchmarking
  std::vector<Hand> hands; // empty list = no hand visible
};

// Turn the Pi's JSON message into a HandFrame. Returns false (and the hub counts a "bad message")
// if anything is missing or malformed, instead of crashing.
inline bool parse_hand_frame(const json& j, HandFrame& f) {
  try {
    // Header fields, each with a safe default.
    f.camera = j.value("cam", 0);
    f.seq = j.value<uint64_t>("seq", 0);
    f.capture_us = j.value<int64_t>("ts", 0);
    f.track_ms = j.value("track_ms", 0.0f);
    // One entry per detected hand.
    for (const auto& h : j.at("hands")) {
      Hand hd;
      hd.right = h.value("right", true);
      hd.score = h.value("score", 1.0f);
      // A hand must have exactly 21 landmarks, each [x, y, z].
      const auto& pts = h.at("pts");
      if (pts.size() != 21) return false;
      for (int i = 0; i < 21; ++i)
        for (int k = 0; k < 3; ++k) hd.xyz[i * 3 + k] = pts[i].at(k).get<float>();
      f.hands.push_back(hd);
    }
    return true;
  } catch (...) {
    return false; // wrong types or missing keys
  }
}

// ---- files and environment ---------------------------------------------------

// Read a whole file into a string; returns an empty string if it can't be opened.
inline std::string load_text(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) return {};
  std::stringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

// Set an environment variable (Windows and POSIX spell this differently).
inline void set_env(const std::string& k, const std::string& v) {
#ifdef _WIN32
  _putenv_s(k.c_str(), v.c_str());
#else
  setenv(k.c_str(), v.c_str(), 0);
#endif
}

// Load KEY=VALUE lines from the .env secrets file into the environment.
// Variables already set in the real environment win, so the file never overrides them.
inline void load_dotenv(const std::string& path) {
  std::ifstream in(path);
  std::string line;
  while (std::getline(in, line)) {
    // Skip Windows line-ending leftovers, blank lines and comments.
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (line.empty() || line[0] == '#') continue;
    // Split at the first '='.
    auto eq = line.find('=');
    if (eq == std::string::npos) continue;
    std::string k = line.substr(0, eq), v = line.substr(eq + 1);
    // Remove optional surrounding quotes.
    if (v.size() >= 2 && (v.front() == '"' || v.front() == '\'') && v.back() == v.front())
      v = v.substr(1, v.size() - 2);
    if (!std::getenv(k.c_str())) set_env(k, v);
  }
}

// Read an environment variable, falling back to a default when it is unset or empty.
inline std::string env_or(const char* k, const std::string& dflt = "") {
  const char* v = std::getenv(k);
  return (v && *v) ? std::string(v) : dflt;
}

}  // namespace ips
