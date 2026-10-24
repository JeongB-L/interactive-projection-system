#include "gesture.hpp"

#include <algorithm>

namespace ips {

GestureConfig GestureConfig::from_json(const json& j) {
  GestureConfig c;
  c.pinch_close = j.value("pinch_close", c.pinch_close);
  c.pinch_open = j.value("pinch_open", c.pinch_open);
  c.swipe_min_speed = j.value("swipe_min_speed", c.swipe_min_speed);
  c.swipe_window_ms = j.value("swipe_window_ms", c.swipe_window_ms);
  c.swipe_cooldown_ms = j.value("swipe_cooldown_ms", c.swipe_cooldown_ms);
  c.dead_zone_px = j.value("dead_zone_px", c.dead_zone_px);
  c.min_cutoff = j.value("min_cutoff", c.min_cutoff);
  c.beta = j.value("beta", c.beta);
  c.home_hold_ms = j.value("home_hold_ms", c.home_hold_ms);
  c.stale_ms = j.value("stale_ms", c.stale_ms);
  return c;
}

// ---- homography ------------------------------------------------------------
Vec2 Homography::apply(Vec2 p) const {
  double w = h[6] * p.x + h[7] * p.y + h[8];
  if (std::fabs(w) < 1e-12) w = 1e-12;
  return {(h[0] * p.x + h[1] * p.y + h[2]) / w, (h[3] * p.x + h[4] * p.y + h[5]) / w};
}

// Direct linear transform with h33 = 1: 8 equations, 8 unknowns, Gaussian elimination.
bool solve_homography(const std::array<Vec2, 4>& s, const std::array<Vec2, 4>& d, Homography& out) {
  double a[8][9] = {};
  for (int i = 0; i < 4; ++i) {
    double x = s[i].x, y = s[i].y, u = d[i].x, v = d[i].y;
    double r1[9] = {x, y, 1, 0, 0, 0, -u * x, -u * y, u};
    double r2[9] = {0, 0, 0, x, y, 1, -v * x, -v * y, v};
    for (int k = 0; k < 9; ++k) { a[2 * i][k] = r1[k]; a[2 * i + 1][k] = r2[k]; }
  }
  for (int c = 0; c < 8; ++c) {
    int p = c;
    for (int r = c + 1; r < 8; ++r)
      if (std::fabs(a[r][c]) > std::fabs(a[p][c])) p = r;
    if (std::fabs(a[p][c]) < 1e-12) return false;  // degenerate (collinear points)
    for (int k = 0; k < 9; ++k) std::swap(a[c][k], a[p][k]);
    for (int r = 0; r < 8; ++r) {
      if (r == c) continue;
      double f = a[r][c] / a[c][c];
      for (int k = c; k < 9; ++k) a[r][k] -= f * a[c][k];
    }
  }
  for (int i = 0; i < 8; ++i) out.h[i] = a[i][8] / a[i][i];
  out.h[8] = 1;
  return true;
}

Vec2 Mapper::to_px(int cam, Vec2 n) const {
  auto it = hs_.find(cam);
  if (it != hs_.end()) return it->second.apply(n);
  return {n.x * w_, n.y * h_};
}

json Mapper::to_json() const {
  json j;
  j["screen"] = {w_, h_};
  for (const auto& [c, h] : hs_) j["cams"][std::to_string(c)] = h.h;
  return j;
}

void Mapper::from_json(const json& j) {
  hs_.clear();
  if (j.contains("cams"))
    for (const auto& [k, v] : j["cams"].items()) {
      Homography h;
      for (int i = 0; i < 9; ++i) h.h[i] = v.at(i).get<double>();
      hs_[std::stoi(k)] = h;
    }
}

// ---- features --------------------------------------------------------------
static bool extended(const Hand& h, int tip, int pip) {
  return dist(h.pt(tip), h.pt(0)) > dist(h.pt(pip), h.pt(0)) * 1.05;
}

CamFeature extract_feature(const Hand& h, int cam, const Mapper& m, int64_t t_us) {
  CamFeature f;
  f.t_us = t_us;
  f.score = h.score;
  f.norm_tip = h.pt(8);
  f.tip_px = m.to_px(cam, h.pt(8));
  f.palm_px = m.to_px(cam, h.pt(9));
  double scale = dist(h.pt(0), h.pt(9));
  f.ratio = scale > 1e-6 ? dist(h.pt(4), h.pt(8)) / scale : 9;
  f.open = extended(h, 8, 6) && extended(h, 12, 10) && extended(h, 16, 14) && extended(h, 20, 18);
  return f;
}

void Fuser::update(const HandFrame& f, const Mapper& m, int64_t t_us) {
  const Hand* best = nullptr;
  for (const auto& h : f.hands)
    if (!best || h.score > best->score) best = &h;
  if (!best) { slots_.erase(f.camera); return; }
  slots_[f.camera] = extract_feature(*best, f.camera, m, t_us);
}

Fused Fuser::fuse(int64_t t_us, int64_t stale_us) const {
  Fused r;
  // Cameras are not synchronized, so one may already show an open palm while the other still shows
  // the previous pose. Decide the pose by weighted vote, then average only cameras that agree with it;
  // mixing positions across poses would look like a sudden jump (a fake swipe).
  double total = 0, open_w = 0;
  for (const auto& [cam, s] : slots_) {
    if (t_us - s.t_us > stale_us) continue;
    double w = std::max(0.05, s.score);
    total += w;
    if (s.open) open_w += w;
  }
  if (total <= 0) return r;
  bool open = open_w / total > 0.5;
  double wsum = 0, ratio = 0;
  Vec2 tip, palm;
  for (const auto& [cam, s] : slots_) {
    if (t_us - s.t_us > stale_us || s.open != open) continue;
    double w = std::max(0.05, s.score);
    wsum += w;
    tip.x += w * s.tip_px.x; tip.y += w * s.tip_px.y;
    palm.x += w * s.palm_px.x; palm.y += w * s.palm_px.y;
    ratio += w * s.ratio;
  }
  r.present = true;
  r.open = open;
  r.tip_px = {tip.x / wsum, tip.y / wsum};
  r.palm_px = {palm.x / wsum, palm.y / wsum};
  r.ratio = ratio / wsum;
  return r;
}

std::vector<std::pair<int, Vec2>> Fuser::tips(int64_t t_us, int64_t stale_us) const {
  std::vector<std::pair<int, Vec2>> v;
  for (const auto& [cam, s] : slots_)
    if (t_us - s.t_us <= stale_us) v.push_back({cam, s.norm_tip});
  return v;
}

int Fuser::active_cameras(int64_t t_us, int64_t stale_us) const {
  int n = 0;
  for (const auto& [cam, s] : slots_)
    if (t_us - s.t_us <= stale_us) ++n;
  return n;
}

// ---- recognizer ------------------------------------------------------------
double OneEuro::filter(double x, int64_t t_us) {
  auto alpha = [](double cutoff, double dt) {
    double tau = 1.0 / (2 * 3.14159265358979 * cutoff);
    return 1.0 / (1.0 + tau / dt);
  };
  if (!init_) { init_ = true; xp_ = x; dxp_ = 0; tp_ = t_us; return x; }
  double dt = std::max((t_us - tp_) / 1e6, 1e-3);
  double dx = (x - xp_) / dt;
  double ad = alpha(dcut_, dt);
  double dxh = ad * dx + (1 - ad) * dxp_;
  double a = alpha(mc_ + beta_ * std::fabs(dxh), dt);
  double xh = a * x + (1 - a) * xp_;
  xp_ = xh; dxp_ = dxh; tp_ = t_us;
  return xh;
}

RuleRecognizer::RuleRecognizer(GestureConfig c, int w, int h) : c_(c), w_(w), h_(h) {
  fx_.configure(c.min_cutoff, c.beta);
  fy_.configure(c.min_cutoff, c.beta);
}

void RuleRecognizer::reset() {
  fx_.reset(); fy_.reset();
  present_ = pinched_ = home_latched_ = false;
  palm_hist_.clear();
  still_since_ = 0;
  last_emit_ = {-1e9, -1e9};
}

std::vector<Gesture> RuleRecognizer::update(const Fused& f, int64_t t) {
  std::vector<Gesture> out;
  auto emit = [&](const char* kind, Vec2 p, const std::string& dir = "") {
    out.push_back({kind, p.x, p.y, dir, t});
  };

  if (!f.present) {
    if (pinched_) emit("up", cursor_);
    if (present_) emit("hide", cursor_);
    reset();
    return out;
  }
  present_ = true;

  if (f.open && f.ratio > c_.pinch_open) {
    // Open palm: swipe by velocity, home by holding still. Cursor is hidden meanwhile.
    if (pinched_) { emit("up", cursor_); pinched_ = false; }
    palm_hist_.push_back({t, f.palm_px});
    int64_t keep = std::max<int64_t>(c_.swipe_window_ms, 300) * 1000;
    while (!palm_hist_.empty() && t - palm_hist_.front().first > keep) palm_hist_.pop_front();

    // swipe: displacement over the swipe window
    const auto& newest = palm_hist_.back();
    for (const auto& s : palm_hist_) {
      if (t - s.first > int64_t(c_.swipe_window_ms) * 1000) continue;
      double dt = (newest.first - s.first) / 1e6;
      if (dt < 0.08) break;
      double dx = newest.second.x - s.second.x, dy = newest.second.y - s.second.y;
      double adx = std::fabs(dx), ady = std::fabs(dy);
      double major = std::max(adx, ady), span = adx >= ady ? w_ : h_;
      double speed = major / span / dt;
      if (speed >= c_.swipe_min_speed && major >= 0.15 * span &&
          t - last_swipe_ > int64_t(c_.swipe_cooldown_ms) * 1000) {
        std::string dir = adx >= ady ? (dx > 0 ? "right" : "left") : (dy > 0 ? "down" : "up");
        emit("swipe", newest.second, dir);
        last_swipe_ = t;
        palm_hist_.clear();
        still_since_ = 0;
        home_latched_ = true;  // a swipe must not also count as a home hold
      }
      break;
    }

    // home: palm barely moved over the last 300 ms, for home_hold_ms
    if (!palm_hist_.empty()) {
      double minx = 1e18, maxx = -1e18, miny = 1e18, maxy = -1e18;
      for (const auto& s : palm_hist_) {
        minx = std::min(minx, s.second.x); maxx = std::max(maxx, s.second.x);
        miny = std::min(miny, s.second.y); maxy = std::max(maxy, s.second.y);
      }
      bool still = (maxx - minx) < 0.04 * w_ && (maxy - miny) < 0.04 * h_;
      if (still) {
        if (still_since_ == 0) still_since_ = t;
        if (!home_latched_ && t - still_since_ >= int64_t(c_.home_hold_ms) * 1000) {
          emit("home", f.palm_px);
          home_latched_ = true;
        }
      } else {
        still_since_ = 0;
      }
    }
    return out;
  }

  // Pointing / pinching: cursor follows the (fused) index fingertip.
  palm_hist_.clear();
  still_since_ = 0;
  home_latched_ = false;
  Vec2 p{fx_.filter(f.tip_px.x, t), fy_.filter(f.tip_px.y, t)};
  cursor_ = p;
  if (dist(p, last_emit_) >= c_.dead_zone_px) {
    emit("move", p);
    last_emit_ = p;
  }
  if (!pinched_ && f.ratio < c_.pinch_close) {
    pinched_ = true;
    emit("down", p);
  } else if (pinched_ && f.ratio > c_.pinch_open) {
    pinched_ = false;
    emit("up", p);
  }
  return out;
}

}  // namespace ips
