// The hub: owns the pipeline (fuse -> recognize -> actions), calibration, apps, metrics and recording.
// Transport-agnostic: the server hands it parsed messages and gives it "sender" callbacks.
#pragma once
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "gesture.hpp"
#include "storage.hpp"

namespace ips {

struct AppDef {
  std::string id, name, url;
  bool needs_internet = false;
};

struct Config {
  int port = 8080;
  std::string bind = "0.0.0.0";
  std::string ui_dir = "ui";
  std::string db_path = "data/ips.db";
  std::string blob_uri = "file:./data/blobs";
  std::string calib_path = "data/calibration.json";
  int screen_w = 1920, screen_h = 1080;
  bool record_landmarks = true, record_frames = false;
  GestureConfig gesture;
  std::vector<AppDef> apps;
  std::string launcher_url = "/apps/launcher.html";
  static Config load(const std::string& path);
};

// Sender returns false when the peer is gone.
using Sender = std::function<bool(const std::string&)>;

class Hub {
 public:
  explicit Hub(Config cfg);
  ~Hub();

  // connections
  int add_display(Sender s);
  void remove_display(int id);
  void edge_connected(const json& hello, Sender s);
  void edge_closed();

  // data in (called from the edge connection thread)
  void on_hands(const json& msg);
  void on_frame(int cam, const uint8_t* jpeg, size_t n);

  // control (called from HTTP threads)
  bool launch(const std::string& app_or_url);
  void home();
  void calibrate_start();
  void calibrate_reset();
  void set_display_size(int w, int h);
  void set_brightness(double b);
  void shutdown();

  // queries
  json status();
  json apps_json() const;
  Catalog& catalog() { return *catalog_; }
  const Config& config() const { return cfg_; }

 private:
  void handle_gesture(const Gesture& g, int64_t t_us);
  void broadcast(const json& j);
  void send_edge(const json& j);
  void save_calibration();
  void content_locked(const std::string& url);
  static double pct(std::vector<double> v, double p);

  Config cfg_;
  std::mutex m_;
  Mapper mapper_;
  Fuser fuser_;
  RuleRecognizer rec_;
  std::unique_ptr<IBlobStore> blobs_;
  std::unique_ptr<Catalog> catalog_;
  std::unique_ptr<SessionRecorder> recorder_;

  std::map<int, Sender> displays_;
  int next_display_ = 1;
  Sender edge_;
  bool edge_os_pointer_ = false;
  std::string edge_device_;

  std::string current_url_, current_app_;
  double brightness_ = 1.0;

  // calibration
  int calib_step_ = -1;
  std::vector<std::map<int, Vec2>> calib_samples_;  // per step: camera -> normalized tip

  // metrics
  int64_t started_us_ = steady_us();
  std::deque<double> hub_ms_, e2e_ms_, track_ms_;
  std::deque<int64_t> frame_times_;
  std::map<int, uint64_t> last_seq_;
  std::map<int, uint64_t> frames_by_cam_;
  uint64_t dropped_ = 0, bad_ = 0, actions_ = 0;
  std::map<std::string, uint64_t> gesture_counts_;
  std::deque<json> last_gestures_;
  uint64_t jpeg_frames_ = 0;
};

}  // namespace ips
