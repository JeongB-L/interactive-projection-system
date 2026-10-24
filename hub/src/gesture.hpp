// gesture.hpp
// ---------------------------------------------------------------------------
// WHAT THIS FILE DECLARES
//   The "brain" types of the hub, all pure logic (no sockets, no files; time is passed in, so a
//   recorded session replays to exactly the same result):
//     1. Mapper / Homography : converts a position in camera space to projector pixels.
//     2. Fuser               : combines what the two cameras see into one hand.
//     3. RuleRecognizer      : turns the fused hand into gestures (move, click, swipe, home).
//   RuleRecognizer is the DEFAULT EXAMPLE gesture detector; a teammate's smarter recognizer
//   can replace it (see docs/INTERFACES.md).
// ---------------------------------------------------------------------------
#pragma once
#include <array>
#include <deque>
#include <map>
#include <string>
#include <vector>

#include "common.hpp"

namespace ips {

// All tunable numbers for gesture detection. Defaults here; overridden by "gesture" in config.json.
struct GestureConfig {
  double pinch_close = 0.25;     // thumb-index distance / hand size below which we call it a pinch
  double pinch_open = 0.40;      // ratio above which a pinch is released (bigger than close = hysteresis)
  double swipe_min_speed = 1.2;  // screen widths per second the palm must move to count as a swipe
  int swipe_window_ms = 250;     // look-back time used to measure swipe speed
  int swipe_cooldown_ms = 400;   // minimum gap between two swipes
  double dead_zone_px = 6;       // ignore cursor movement smaller than this (hides jitter)
  double min_cutoff = 1.0;       // one-euro smoothing: lower = smoother but laggier at rest
  double beta = 0.02;            // one-euro smoothing: higher = less lag during fast motion
  int home_hold_ms = 1000;       // how long an open palm must stay still to trigger "home"
  int stale_ms = 150;            // a camera silent for this long is ignored
  static GestureConfig from_json(const json& j); // read overrides from config.json
};

// ---- 1. camera -> projector mapping -----------------------------------------

// A 3x3 matrix (row-major) that maps a camera point to a projector pixel, including perspective.
struct Homography {
  std::array<double, 9> h{1, 0, 0, 0, 1, 0, 0, 0, 1}; // identity by default
  Vec2 apply(Vec2 p) const;                            // transform one point
};
// Compute the homography from 4 camera points to the 4 projector points they correspond to.
// Returns false if the points are degenerate (e.g. all on one line).
bool solve_homography(const std::array<Vec2, 4>& src, const std::array<Vec2, 4>& dst, Homography& out);

// Holds one Homography per camera plus the projector's pixel size.
class Mapper {
 public:
  Mapper(int w, int h) : w_(w), h_(h) {}
  void set_screen(int w, int h) { w_ = w; h_ = h; }
  int width() const { return w_; }
  int height() const { return h_; }
  // Camera point -> projector pixel. An uncalibrated camera just scales 0..1 to the screen size.
  Vec2 to_px(int cam, Vec2 norm) const;
  bool calibrated(int cam) const { return hs_.count(cam) > 0; }
  void set(int cam, const Homography& h) { hs_[cam] = h; }
  void clear() { hs_.clear(); }
  json to_json() const;          // save calibration
  void from_json(const json& j); // load calibration

 private:
  int w_, h_;
  std::map<int, Homography> hs_; // camera index -> its calibration
};

// ---- 2. features and camera fusion --------------------------------------------

// What we extract from ONE camera's view of ONE hand.
struct CamFeature {
  int64_t t_us = 0;                // hub time we received it
  Vec2 norm_tip, tip_px, palm_px;  // index fingertip (raw camera), fingertip and palm in projector px
  double ratio = 9;                // pinch ratio (small = fingers touching)
  bool open = false;               // true if all four fingers are extended (open palm)
  double score = 0;                // detector confidence
};

// The single combined hand after merging the cameras.
struct Fused {
  bool present = false; // is any camera seeing a hand right now?
  Vec2 tip_px, palm_px;
  double ratio = 9;
  bool open = false;
};

// Compute a CamFeature from raw landmarks (finger-extension test, pinch ratio, pixel positions).
CamFeature extract_feature(const Hand& h, int cam, const Mapper& m, int64_t t_us);

// Remembers the newest feature from each camera and merges them on demand.
class Fuser {
 public:
  void update(const HandFrame& f, const Mapper& m, int64_t t_us);       // store one camera's newest view
  Fused fuse(int64_t t_us, int64_t stale_us) const;                      // merge all fresh cameras
  std::vector<std::pair<int, Vec2>> tips(int64_t t_us, int64_t stale_us) const; // raw tips (for calibration)
  int active_cameras(int64_t t_us, int64_t stale_us) const;

 private:
  std::map<int, CamFeature> slots_; // camera index -> newest feature
};

// ---- 3. gesture recognizer ----------------------------------------------------

// One thing the user did.
struct Gesture {
  std::string kind;  // move | down | up | swipe | home | hide
  double x = 0, y = 0; // projector pixels
  std::string dir;   // for swipe: left | right | up | down
  int64_t t_us = 0;
};

// One-euro filter: smooths a noisy value while staying responsive when it changes fast.
class OneEuro {
 public:
  void configure(double min_cutoff, double beta) { mc_ = min_cutoff; beta_ = beta; }
  double filter(double x, int64_t t_us); // feed the next sample, get the smoothed one
  void reset() { init_ = false; }

 private:
  double mc_ = 1.0, beta_ = 0.02, dcut_ = 1.0, xp_ = 0, dxp_ = 0;
  int64_t tp_ = 0;
  bool init_ = false;
};

// DEFAULT EXAMPLE gesture detector. Feed it the fused hand each frame; it returns the gestures
// that just happened. It remembers a little state between frames (is a pinch active, palm history...).
class RuleRecognizer {
 public:
  RuleRecognizer(GestureConfig c, int w, int h);
  void set_screen(int w, int h) { w_ = w; h_ = h; }
  std::vector<Gesture> update(const Fused& f, int64_t t_us);
  void reset(); // forget all state (new session / hand lost)

 private:
  GestureConfig c_;
  int w_, h_;                               // projector size in pixels
  OneEuro fx_, fy_;                         // smoothing filters for cursor x and y
  bool present_ = false, pinched_ = false, home_latched_ = false;
  Vec2 last_emit_{-1e9, -1e9}, cursor_;     // last cursor position we reported / current cursor
  std::deque<std::pair<int64_t, Vec2>> palm_hist_; // recent palm positions (for swipe + stillness)
  int64_t still_since_ = 0, last_swipe_ = -1'000'000'000;
};

}  // namespace ips
