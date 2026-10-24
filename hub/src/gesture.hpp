// Camera -> projector mapping, multi-camera fusion, and the rule-based gesture recognizer.
// Pure logic: no sockets, no files, time is passed in. Same input -> same output.
#pragma once
#include <array>
#include <deque>
#include <map>
#include <string>
#include <vector>

#include "common.hpp"

namespace ips {

struct GestureConfig {
  double pinch_close = 0.25;     // thumb-index distance / hand size
  double pinch_open = 0.40;
  double swipe_min_speed = 1.2;  // screen widths per second
  int swipe_window_ms = 250;
  int swipe_cooldown_ms = 400;
  double dead_zone_px = 6;
  double min_cutoff = 1.0;       // one-euro filter
  double beta = 0.02;
  int home_hold_ms = 1000;
  int stale_ms = 150;            // ignore a camera not heard from for this long
  static GestureConfig from_json(const json& j);
};

// ---- mapping -------------------------------------------------------------
struct Homography {
  std::array<double, 9> h{1, 0, 0, 0, 1, 0, 0, 0, 1};
  Vec2 apply(Vec2 p) const;
};
bool solve_homography(const std::array<Vec2, 4>& src, const std::array<Vec2, 4>& dst, Homography& out);

class Mapper {
 public:
  Mapper(int w, int h) : w_(w), h_(h) {}
  void set_screen(int w, int h) { w_ = w; h_ = h; }
  int width() const { return w_; }
  int height() const { return h_; }
  Vec2 to_px(int cam, Vec2 norm) const;  // uncalibrated cameras just scale to the screen
  bool calibrated(int cam) const { return hs_.count(cam) > 0; }
  void set(int cam, const Homography& h) { hs_[cam] = h; }
  void clear() { hs_.clear(); }
  json to_json() const;
  void from_json(const json& j);

 private:
  int w_, h_;
  std::map<int, Homography> hs_;
};

// ---- features and fusion ---------------------------------------------------
struct CamFeature {
  int64_t t_us = 0;
  Vec2 norm_tip, tip_px, palm_px;
  double ratio = 9;  // pinch ratio
  bool open = false;
  double score = 0;
};

struct Fused {
  bool present = false;
  Vec2 tip_px, palm_px;
  double ratio = 9;
  bool open = false;
};

CamFeature extract_feature(const Hand& h, int cam, const Mapper& m, int64_t t_us);

class Fuser {
 public:
  void update(const HandFrame& f, const Mapper& m, int64_t t_us);
  Fused fuse(int64_t t_us, int64_t stale_us) const;
  std::vector<std::pair<int, Vec2>> tips(int64_t t_us, int64_t stale_us) const;  // raw camera tips
  int active_cameras(int64_t t_us, int64_t stale_us) const;

 private:
  std::map<int, CamFeature> slots_;
};

// ---- recognizer ------------------------------------------------------------
struct Gesture {
  std::string kind;  // move | down | up | swipe | home | hide
  double x = 0, y = 0;
  std::string dir;   // for swipe: left | right | up | down
  int64_t t_us = 0;
};

class OneEuro {
 public:
  void configure(double min_cutoff, double beta) { mc_ = min_cutoff; beta_ = beta; }
  double filter(double x, int64_t t_us);
  void reset() { init_ = false; }

 private:
  double mc_ = 1.0, beta_ = 0.02, dcut_ = 1.0, xp_ = 0, dxp_ = 0;
  int64_t tp_ = 0;
  bool init_ = false;
};

class RuleRecognizer {
 public:
  RuleRecognizer(GestureConfig c, int w, int h);
  void set_screen(int w, int h) { w_ = w; h_ = h; }
  std::vector<Gesture> update(const Fused& f, int64_t t_us);
  void reset();

 private:
  GestureConfig c_;
  int w_, h_;
  OneEuro fx_, fy_;
  bool present_ = false, pinched_ = false, home_latched_ = false;
  Vec2 last_emit_{-1e9, -1e9}, cursor_;
  std::deque<std::pair<int64_t, Vec2>> palm_hist_;
  int64_t still_since_ = 0, last_swipe_ = -1'000'000'000;
};

}  // namespace ips
