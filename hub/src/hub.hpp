// hub.hpp
// ---------------------------------------------------------------------------
// WHAT THIS FILE DECLARES
//   The Hub: the coordinator that ties everything together. It receives parsed messages from the
//   server, runs them through the pipeline
//        hand data -> fuse cameras -> recognize gestures -> actions
//   and then pushes the actions to the display page(s). It also owns:
//     - calibration (camera -> projector mapping),
//     - which app is on screen,
//     - recording (via storage.hpp),
//     - live metrics for the status page.
//   It does NOT do any networking itself: the server gives it messages and "sender" callbacks.
// ---------------------------------------------------------------------------
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

// One app that can be shown on the projector (an entry in config.json "apps").
struct AppDef {
  std::string id, name, url;
  bool needs_internet = false;
};

// Everything read from config.json (each field has a built-in default).
struct Config {
  int port = 8080;
  std::string bind = "0.0.0.0";                   // network interface to listen on
  std::string ui_dir = "ui";                      // folder with the display page and apps
  std::string db_path = "data/ips.db";            // SQLite catalog
  std::string blob_uri = "file:./data/blobs";     // where files (video, recordings) are stored
  std::string calib_path = "data/calibration.json";
  int screen_w = 1920, screen_h = 1080;           // projector size until the display page reports its own
  bool record_landmarks = true, record_frames = false; // camera frames are OFF by default (privacy, size)
  GestureConfig gesture;
  std::vector<AppDef> apps;
  std::string launcher_url = "/apps/launcher.html";
  static Config load(const std::string& path);    // read config.json
};

// A "sender" pushes one text message to one connected peer; it returns false once that peer is gone.
using Sender = std::function<bool(const std::string&)>;

class Hub {
 public:
  explicit Hub(Config cfg);
  ~Hub();

  // --- connections (called by the server) ---
  int add_display(Sender s);                     // a projected page connected; returns its id
  void remove_display(int id);
  void edge_connected(const json& hello, Sender s); // the Pi agent said hello: start a recording session
  void edge_closed();                            // the Pi agent disconnected: finish the recording

  // --- data in (called from the Pi connection's thread) ---
  void on_hands(const json& msg);                // one camera's hand landmarks for one frame
  void on_frame(int cam, const uint8_t* jpeg, size_t n); // one camera JPEG (stored if enabled)

  // --- control (called from HTTP handlers, i.e. the command-line tool) ---
  bool launch(const std::string& app_or_url);   // switch the displayed app
  void home();                                   // back to the launcher
  void calibrate_start();                        // show 4 markers and learn from the next 4 pinches
  void calibrate_reset();                        // forget calibration
  void set_display_size(int w, int h);           // the display page reports its pixel size
  void set_brightness(double b);                 // dim the projected image (0..1)
  void shutdown();                               // flush recordings before exit

  // --- queries ---
  json status();                                 // live numbers for the status page
  json apps_json() const;                        // list of configured apps
  Catalog& catalog() { return *catalog_; }
  const Config& config() const { return cfg_; }

 private:
  void handle_gesture(const Gesture& g, int64_t t_us); // turn one gesture into actions / calibration steps
  void broadcast(const json& j);                 // send a message to every display page
  void send_edge(const json& j);                 // send a message to the Pi agent
  void save_calibration();
  void content_locked(const std::string& url);   // switch app (caller already holds the lock)
  static double pct(std::vector<double> v, double p); // percentile helper for latency stats

  Config cfg_;
  std::mutex m_;                                 // ONE lock guards all the state below
  Mapper mapper_;                                // camera -> projector mapping
  Fuser fuser_;                                  // merges the cameras
  RuleRecognizer rec_;                           // the (default example) gesture detector
  std::unique_ptr<IBlobStore> blobs_;            // where files are written
  std::unique_ptr<Catalog> catalog_;             // SQLite index of those files
  std::unique_ptr<SessionRecorder> recorder_;    // active only while a Pi is connected

  std::map<int, Sender> displays_;               // connected projected pages
  int next_display_ = 1;
  Sender edge_;                                  // the connected Pi agent (empty if none)
  bool edge_os_pointer_ = false;                 // did the agent ask for real mouse events too?
  std::string edge_device_;

  std::string current_url_, current_app_;        // what is on screen right now
  double brightness_ = 1.0;

  // calibration state: which marker we are on, and the fingertip samples collected so far
  int calib_step_ = -1;                          // -1 = not calibrating
  std::vector<std::map<int, Vec2>> calib_samples_; // per step: camera -> raw fingertip

  // metrics shown on the status page
  int64_t started_us_ = steady_us();
  std::deque<double> hub_ms_, e2e_ms_, track_ms_; // recent timings (hub work, capture->action, Pi tracking)
  std::deque<int64_t> frame_times_;              // arrival times, to compute frames per second
  std::map<int, uint64_t> last_seq_;             // last sequence number per camera (to spot drops)
  std::map<int, uint64_t> frames_by_cam_;
  uint64_t dropped_ = 0, bad_ = 0, actions_ = 0;
  std::map<std::string, uint64_t> gesture_counts_;
  std::deque<json> last_gestures_;
  uint64_t jpeg_frames_ = 0;
};

}  // namespace ips
