#include "hub.hpp"

#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <numeric>

namespace ips {

Config Config::load(const std::string& path) {
  Config c;
  std::string txt = load_text(path);
  if (txt.empty()) {
    std::fprintf(stderr, "config %s not found, using defaults\n", path.c_str());
    return c;
  }
  json j = json::parse(txt, nullptr, true, true);  // allow // comments
  c.port = j.value("port", c.port);
  c.bind = j.value("bind", c.bind);
  c.ui_dir = j.value("ui_dir", c.ui_dir);
  c.db_path = j.value("db_path", c.db_path);
  c.blob_uri = j.value("blob_uri", c.blob_uri);
  c.calib_path = j.value("calib_path", c.calib_path);
  c.screen_w = j.value("screen_w", c.screen_w);
  c.screen_h = j.value("screen_h", c.screen_h);
  c.launcher_url = j.value("launcher_url", c.launcher_url);
  if (j.contains("record")) {
    c.record_landmarks = j["record"].value("landmarks", true);
    c.record_frames = j["record"].value("frames", false);
  }
  if (j.contains("gesture")) c.gesture = GestureConfig::from_json(j["gesture"]);
  if (j.contains("apps"))
    for (const auto& a : j["apps"])
      c.apps.push_back({a.value("id", ""), a.value("name", ""), a.value("url", ""),
                        a.value("needs_internet", false)});
  return c;
}

Hub::Hub(Config cfg)
    : cfg_(cfg), mapper_(cfg.screen_w, cfg.screen_h), rec_(cfg.gesture, cfg.screen_w, cfg.screen_h) {
  blobs_ = make_blob_store(cfg_.blob_uri);
  catalog_ = std::make_unique<Catalog>(cfg_.db_path);
  std::string txt = load_text(cfg_.calib_path);
  if (!txt.empty()) {
    try { mapper_.from_json(json::parse(txt)); } catch (...) { std::fprintf(stderr, "bad calibration file ignored\n"); }
  }
  current_url_ = cfg_.launcher_url;
  current_app_ = "launcher";
}

Hub::~Hub() { shutdown(); }

void Hub::shutdown() {
  std::lock_guard lk(m_);
  if (recorder_) { recorder_->close(); recorder_.reset(); }
}

// ---- connections -----------------------------------------------------------
int Hub::add_display(Sender s) {
  std::lock_guard lk(m_);
  int id = next_display_++;
  s(json{{"t", "content"}, {"url", current_url_}, {"app", current_app_}}.dump());
  s(json{{"t", "brightness"}, {"value", brightness_}}.dump());
  displays_[id] = std::move(s);
  return id;
}

void Hub::remove_display(int id) {
  std::lock_guard lk(m_);
  displays_.erase(id);
}

void Hub::edge_connected(const json& hello, Sender s) {
  std::lock_guard lk(m_);
  edge_ = std::move(s);
  edge_device_ = hello.value("device", "unknown");
  edge_os_pointer_ = hello.value("os_pointer", false);
  std::string source = hello.value("source", "unknown");
  recorder_ = std::make_unique<SessionRecorder>(*catalog_, *blobs_, edge_device_, source,
                                                cfg_.record_landmarks, cfg_.record_frames);
  fuser_ = Fuser();
  rec_.reset();
  last_seq_.clear();
  edge_(json{{"t", "hello_ack"}, {"session", recorder_->session_id()},
             {"record_frames", cfg_.record_frames}}.dump());
}

void Hub::edge_closed() {
  std::lock_guard lk(m_);
  edge_ = nullptr;
  if (recorder_) { recorder_->close(); recorder_.reset(); }
  broadcast({{"t", "action"}, {"kind", "hide"}});
}

// ---- pipeline --------------------------------------------------------------
void Hub::broadcast(const json& j) {
  std::string s = j.dump();
  for (auto it = displays_.begin(); it != displays_.end();) {
    if (!it->second(s)) it = displays_.erase(it);
    else ++it;
  }
}

void Hub::send_edge(const json& j) {
  if (edge_ && !edge_(j.dump())) edge_ = nullptr;
}

static double clamp01(double v) { return std::min(1.0, std::max(0.0, v)); }

void Hub::on_hands(const json& msg) {
  int64_t t0 = steady_us();
  HandFrame f;
  std::lock_guard lk(m_);
  if (!parse_hand_frame(msg, f)) { ++bad_; return; }

  auto ls = last_seq_.find(f.camera);
  if (ls != last_seq_.end() && f.seq > ls->second + 1) dropped_ += f.seq - ls->second - 1;
  last_seq_[f.camera] = f.seq;
  ++frames_by_cam_[f.camera];
  frame_times_.push_back(t0);
  while (frame_times_.size() > 240) frame_times_.pop_front();
  track_ms_.push_back(f.track_ms);
  if (track_ms_.size() > 500) track_ms_.pop_front();

  int64_t stale = int64_t(cfg_.gesture.stale_ms) * 1000;
  fuser_.update(f, mapper_, t0);
  Fused fu = fuser_.fuse(t0, stale);

  std::vector<Gesture> gs = rec_.update(fu, t0);
  for (const auto& g : gs) handle_gesture(g, t0);

  if (recorder_) recorder_->event(msg);

  double hub_ms = (steady_us() - t0) / 1000.0;
  hub_ms_.push_back(hub_ms);
  if (hub_ms_.size() > 1000) hub_ms_.pop_front();
  if (f.capture_us > 0 && !gs.empty()) {
    e2e_ms_.push_back((wall_us() - f.capture_us) / 1000.0);
    if (e2e_ms_.size() > 1000) e2e_ms_.pop_front();
  }
}

void Hub::handle_gesture(const Gesture& g, int64_t t_us) {
  // calibration consumes pinch-downs
  if (calib_step_ >= 0 && g.kind == "down") {
    std::map<int, Vec2> sample;
    for (const auto& [cam, p] : fuser_.tips(t_us, int64_t(cfg_.gesture.stale_ms) * 1000)) sample[cam] = p;
    calib_samples_.push_back(sample);
    ++calib_step_;
    const double ix[4] = {0.1, 0.9, 0.9, 0.1}, iy[4] = {0.1, 0.1, 0.9, 0.9};
    if (calib_step_ >= 4) {
      std::array<Vec2, 4> dst;
      for (int i = 0; i < 4; ++i) dst[i] = {ix[i] * mapper_.width(), iy[i] * mapper_.height()};
      std::map<int, int> n;
      for (auto& s : calib_samples_) for (auto& [c, p] : s) ++n[c];
      int solved = 0;
      for (auto& [cam, cnt] : n) {
        if (cnt < 4) continue;
        std::array<Vec2, 4> src;
        bool ok = true;
        for (int i = 0; i < 4; ++i) {
          auto it = calib_samples_[i].find(cam);
          if (it == calib_samples_[i].end()) { ok = false; break; }
          src[i] = it->second;
        }
        Homography h;
        if (ok && solve_homography(src, dst, h)) { mapper_.set(cam, h); ++solved; }
      }
      calib_step_ = -1;
      save_calibration();
      broadcast({{"t", "calib"}, {"done", true}, {"cameras_calibrated", solved}});
    } else {
      broadcast({{"t", "calib"}, {"step", calib_step_}, {"total", 4},
                 {"x", ix[calib_step_] * mapper_.width()}, {"y", iy[calib_step_] * mapper_.height()}});
    }
    return;
  }

  ++gesture_counts_[g.kind];
  ++actions_;
  json a = {{"t", "action"}, {"kind", g.kind}, {"x", int(std::lround(g.x))}, {"y", int(std::lround(g.y))},
            {"detail", g.dir}};
  if (g.kind != "move") {
    last_gestures_.push_back({{"kind", g.kind}, {"dir", g.dir}, {"x", a["x"]}, {"y", a["y"]}});
    if (last_gestures_.size() > 12) last_gestures_.pop_front();
  }
  if (recorder_) recorder_->event({{"t", "act"}, {"kind", g.kind}, {"x", a["x"]}, {"y", a["y"]}, {"dir", g.dir}});
  broadcast(a);
  if (edge_os_pointer_) send_edge(a);
  if (g.kind == "home" && current_app_ != "launcher") {
    current_app_ = "launcher";
    content_locked(cfg_.launcher_url);
  }
}

// ---- control ---------------------------------------------------------------
void Hub::content_locked(const std::string& url) {
  current_url_ = url;
  broadcast({{"t", "content"}, {"url", url}, {"app", current_app_}});
}

bool Hub::launch(const std::string& app_or_url) {
  std::lock_guard lk(m_);
  for (const auto& a : cfg_.apps)
    if (a.id == app_or_url) { current_app_ = a.id; content_locked(a.url); return true; }
  if (app_or_url == "launcher") { current_app_ = "launcher"; content_locked(cfg_.launcher_url); return true; }
  if (app_or_url.rfind("http", 0) == 0 || app_or_url.rfind("/", 0) == 0) {
    current_app_ = "custom";
    content_locked(app_or_url);
    return true;
  }
  return false;
}

void Hub::home() { launch("launcher"); }

void Hub::calibrate_start() {
  std::lock_guard lk(m_);
  calib_step_ = 0;
  calib_samples_.clear();
  broadcast({{"t", "calib"}, {"step", 0}, {"total", 4}, {"x", 0.1 * mapper_.width()}, {"y", 0.1 * mapper_.height()}});
}

void Hub::calibrate_reset() {
  std::lock_guard lk(m_);
  calib_step_ = -1;
  mapper_.clear();
  save_calibration();
  broadcast({{"t", "calib"}, {"done", true}, {"cameras_calibrated", 0}});
}

void Hub::save_calibration() {
  auto parent = std::filesystem::path(cfg_.calib_path).parent_path();
  if (!parent.empty()) std::filesystem::create_directories(parent);
  std::ofstream(cfg_.calib_path) << mapper_.to_json().dump(2);
}

void Hub::set_display_size(int w, int h) {
  std::lock_guard lk(m_);
  if (w < 100 || h < 100) return;
  mapper_.set_screen(w, h);
  rec_.set_screen(w, h);
}

void Hub::set_brightness(double b) {
  std::lock_guard lk(m_);
  brightness_ = clamp01(b);
  broadcast({{"t", "brightness"}, {"value", brightness_}});
}

void Hub::on_frame(int cam, const uint8_t* jpeg, size_t n) {
  std::lock_guard lk(m_);
  ++jpeg_frames_;
  if (recorder_) recorder_->frame(cam, jpeg, n);
}

// ---- queries ---------------------------------------------------------------
double Hub::pct(std::vector<double> v, double p) {
  if (v.empty()) return 0;
  std::sort(v.begin(), v.end());
  return v[size_t(p * (v.size() - 1))];
}

json Hub::status() {
  std::lock_guard lk(m_);
  int64_t now = steady_us();
  double fps = 0;
  if (frame_times_.size() > 5) {
    double span = (frame_times_.back() - frame_times_.front()) / 1e6;
    if (span > 0 && now - frame_times_.back() < 2'000'000) fps = (frame_times_.size() - 1) / span;
  }
  auto stats = [&](const std::deque<double>& d) {
    std::vector<double> v(d.begin(), d.end());
    return json{{"p50", pct(v, 0.5)}, {"p95", pct(v, 0.95)}, {"n", v.size()}};
  };
  json cams = json::object();
  for (auto& [c, n] : frames_by_cam_) cams[std::to_string(c)] = {{"frames", n}, {"calibrated", mapper_.calibrated(c)}};
  double track_avg = track_ms_.empty() ? 0 : std::accumulate(track_ms_.begin(), track_ms_.end(), 0.0) / track_ms_.size();
  return {{"uptime_s", (now - started_us_) / 1e6},
          {"edge_connected", edge_ != nullptr},
          {"edge_device", edge_device_},
          {"displays", displays_.size()},
          {"session", recorder_ ? recorder_->session_id() : 0},
          {"app", current_app_},
          {"screen", {mapper_.width(), mapper_.height()}},
          {"fps_total", fps},
          {"cameras", cams},
          {"hub_ms", stats(hub_ms_)},
          {"e2e_ms", stats(e2e_ms_)},
          {"track_ms_avg", track_avg},
          {"dropped", dropped_},
          {"bad_messages", bad_},
          {"jpeg_frames", jpeg_frames_},
          {"actions", actions_},
          {"gesture_counts", gesture_counts_},
          {"calibrating_step", calib_step_},
          {"last_gestures", last_gestures_},
          {"blob_base", blobs_->base_uri()}};
}

json Hub::apps_json() const {
  json a = json::array();
  for (const auto& app : cfg_.apps)
    a.push_back({{"id", app.id}, {"name", app.name}, {"url", app.url}, {"needs_internet", app.needs_internet}});
  return a;
}

}  // namespace ips
