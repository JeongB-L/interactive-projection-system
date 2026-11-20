// hub.cpp
// ---------------------------------------------------------------------------
// WHAT THIS FILE DOES
//   Implements the Hub (see hub.hpp). In real time, for every message from the Pi:
//        parse -> fuse the two cameras -> recognize gestures -> send actions to the display page
//   and alongside that it records the session, runs calibration, switches apps, and keeps the
//   latency/fps numbers shown on the status page.
//   Thread safety: many connection threads call in, so one mutex (m_) guards all hub state.
// ---------------------------------------------------------------------------
#include "hub.hpp"

#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <numeric>

namespace ips
{

	// ---- configuration ---------------------------------------------------------

	// Read config.json. Missing file -> built-in defaults. Missing keys -> defaults for those keys.
	Config Config::load(const std::string &path)
	{
		Config c;
		std::string txt = load_text(path);
		if (txt.empty())
		{
			std::fprintf(stderr, "config %s not found, using defaults\n", path.c_str());
			return c;
		}
		json j = json::parse(txt, nullptr, true, true); // last argument: allow // comments in the file
		// Simple top-level settings.
		c.port = j.value("port", c.port);
		c.bind = j.value("bind", c.bind);
		c.ui_dir = j.value("ui_dir", c.ui_dir);
		c.db_path = j.value("db_path", c.db_path);
		c.blob_uri = j.value("blob_uri", c.blob_uri);
		c.calib_path = j.value("calib_path", c.calib_path);
		c.screen_w = j.value("screen_w", c.screen_w);
		c.screen_h = j.value("screen_h", c.screen_h);
		c.launcher_url = j.value("launcher_url", c.launcher_url);
		// What to record.
		if (j.contains("record"))
		{
			c.record_landmarks = j["record"].value("landmarks", true);
			c.record_frames = j["record"].value("frames", false);
		}
		// Gesture thresholds and the list of apps.
		if (j.contains("gesture"))
			c.gesture = GestureConfig::from_json(j["gesture"]);
		// Which engine decides gestures: "hub" (our C++ rules, the default) or "edge" (Johnny's engine on the Pi).
		std::string src = j.value("gesture_source", c.gesture_source);
		if (src == "hub" || src == "edge")
			c.gesture_source = src;
		else
			std::fprintf(stderr, "unknown gesture_source '%s', using 'hub'\n", src.c_str());
		if (j.contains("apps"))
			for (const auto &a : j["apps"])
				c.apps.push_back({a.value("id", ""), a.value("name", ""), a.value("url", ""),
								  a.value("needs_internet", false)});
		return c;
	}

	// ---- construction and shutdown -------------------------------------------------

	// Set up storage and the mapper, load any saved calibration, and start on the launcher.
	Hub::Hub(Config cfg)
		: cfg_(cfg), mapper_(cfg.screen_w, cfg.screen_h), rec_(cfg.gesture, cfg.screen_w, cfg.screen_h)
	{
		// The edge adapter uses the same cursor dead zone as our own recognizer.
		edge_adapter_ = EdgeAdapter(cfg_.gesture.dead_zone_px);
		blobs_ = make_blob_store(cfg_.blob_uri);
		catalog_ = std::make_unique<Catalog>(cfg_.db_path);
		// Reuse the calibration saved on a previous run, if there is one.
		std::string txt = load_text(cfg_.calib_path);
		if (!txt.empty())
		{
			try
			{
				mapper_.from_json(json::parse(txt));
			}
			catch (...)
			{
				std::fprintf(stderr, "bad calibration file ignored\n");
			}
		}
		current_url_ = cfg_.launcher_url;
		current_app_ = "launcher";
	}

	Hub::~Hub() { shutdown(); }

	// Finish the active recording so its files and database rows are complete.
	void Hub::shutdown()
	{
		std::lock_guard lk(m_);
		if (recorder_)
		{
			recorder_->close();
			recorder_.reset();
		}
	}

	// ---- connections -----------------------------------------------------------

	// A projected page connected: tell it what to show right now, and remember it for future broadcasts.
	int Hub::add_display(Sender s)
	{
		std::lock_guard lk(m_);
		int id = next_display_++;
		s(json{{"t", "content"}, {"url", current_url_}, {"app", current_app_}}.dump());
		s(json{{"t", "brightness"}, {"value", brightness_}}.dump());
		displays_[id] = std::move(s);
		return id;
	}

	void Hub::remove_display(int id)
	{
		std::lock_guard lk(m_);
		displays_.erase(id);
	}

	// The Pi agent said hello: remember how to talk to it and start a fresh recording session.
	void Hub::edge_connected(const json &hello, Sender s)
	{
		std::lock_guard lk(m_);
		edge_ = std::move(s);
		edge_device_ = hello.value("device", "unknown");
		edge_os_pointer_ = hello.value("os_pointer", false);
		std::string source = hello.value("source", "unknown");
		recorder_ = std::make_unique<SessionRecorder>(*catalog_, *blobs_, edge_device_, source,
													  cfg_.record_landmarks, cfg_.record_frames);
		// Start the pipeline from a clean slate so old state can't leak into the new session.
		fuser_ = Fuser();
		rec_.reset();
		edge_adapter_.reset();
		edge_cam_ = -1;
		last_seq_.clear();
		edge_(json{{"t", "hello_ack"}, {"session", recorder_->session_id()}, {"record_frames", cfg_.record_frames}}.dump());
	}

	// The Pi agent disconnected: close the recording and hide the cursor on the display.
	void Hub::edge_closed()
	{
		std::lock_guard lk(m_);
		edge_ = nullptr;
		if (recorder_)
		{
			recorder_->close();
			recorder_.reset();
		}
		broadcast({{"t", "action"}, {"kind", "hide"}});
		// An automatic calibration cannot finish without the agent: end it, and never leave the pattern on screen.
		if (auto_calib_active_)
		{
			auto_calib_last_ = {{"id", auto_calib_active_}, {"ok", false}, {"cameras_calibrated", 0},
								{"error", "the camera agent disconnected during calibration"}, {"results", json::array()}};
			auto_calib_active_ = 0;
			broadcast({{"t", "calib_pattern"}, {"show", false}});
		}
	}

	// ---- pipeline --------------------------------------------------------------

	// Send a message to every display page; drop pages whose connection has died.
	void Hub::broadcast(const json &j)
	{
		std::string s = j.dump();
		for (auto it = displays_.begin(); it != displays_.end();)
		{
			if (!it->second(s))
				it = displays_.erase(it);
			else
				++it;
		}
	}

	// Send a message to the Pi agent (if connected); forget it if the send fails.
	void Hub::send_edge(const json &j)
	{
		if (edge_ && !edge_(j.dump()))
			edge_ = nullptr;
	}

	static double clamp01(double v) { return std::min(1.0, std::max(0.0, v)); }

	// THE REAL-TIME PATH. Called for every hand message from the Pi (about 30 per second per camera).
	void Hub::on_hands(const json &msg)
	{
		int64_t t0 = steady_us(); // start of our processing time
		HandFrame f;
		std::lock_guard lk(m_);
		// Step 1: parse. A malformed message is counted, not fatal.
		if (!parse_hand_frame(msg, f))
		{
			++bad_;
			return;
		}

		// Step 2: bookkeeping for the status page: detect skipped frames, count frames, track fps and Pi tracking time.
		auto ls = last_seq_.find(f.camera);
		if (ls != last_seq_.end() && f.seq > ls->second + 1)
			dropped_ += f.seq - ls->second - 1;
		last_seq_[f.camera] = f.seq;
		++frames_by_cam_[f.camera];
		frame_times_.push_back(t0);
		while (frame_times_.size() > 240)
			frame_times_.pop_front();
		track_ms_.push_back(f.track_ms);
		if (track_ms_.size() > 500)
			track_ms_.pop_front();

		// Step 3: fuse the cameras (cameras silent for stale_ms are ignored).
		int64_t stale = int64_t(cfg_.gesture.stale_ms) * 1000;
		fuser_.update(f, mapper_, t0);
		Fused fu = fuser_.fuse(t0, stale);

		// Our own rule-based recognizer always runs (for comparison, and for the "home" gesture).
		std::vector<Gesture> hub_gs = rec_.update(fu, t0);
		// Johnny's motion engine ran on the Pi and attached its result to the message (if the agent has it switched on).
		bool has_motion = msg.contains("motion") && msg["motion"].is_object();
		std::vector<Gesture> edge_gs;
		if (has_motion)
		{
			++motion_frames_;
			if (edge_cam_ < 0)
				edge_cam_ = f.camera; // edge mode follows ONE camera: the first one that sends motion data
			if (f.camera == edge_cam_)
				edge_gs = edge_adapter_.update(msg["motion"], mapper_, f.camera, t0);
		}
		// Count what each engine detected, so the two can be compared on the same live or replayed data.
		for (const auto &g : hub_gs)
			if (g.kind != "move")
				++compare_hub_[g.kind];
		for (const auto &g : edge_gs)
			if (g.kind != "move")
				++compare_edge_[g.kind];
		// Decide which engine acts. "edge" mode uses Johnny's gestures plus our "home" (his engine has none).
		// If the agent sends no motion data, fall back to our own recognizer so the system keeps working.
		std::vector<Gesture> gs;
		if (cfg_.gesture_source == "edge" && has_motion)
		{
			if (f.camera == edge_cam_)
				gs = edge_gs;
			for (const auto &g : hub_gs)
				if (g.kind == "home")
					gs.push_back(g);
		}
		else
		{
			gs = hub_gs;
		}
		for (const auto &g : gs)
			handle_gesture(g, t0);

		// Step 5: record the raw message for later replay.
		if (recorder_)
			recorder_->event(msg);

		// Step 6: measure how long we took, and (when something happened) the delay since the Pi captured the frame.
		double hub_ms = (steady_us() - t0) / 1000.0;
		hub_ms_.push_back(hub_ms);
		if (hub_ms_.size() > 1000)
			hub_ms_.pop_front();
		if (f.capture_us > 0 && !gs.empty())
		{
			e2e_ms_.push_back((wall_us() - f.capture_us) / 1000.0);
			if (e2e_ms_.size() > 1000)
				e2e_ms_.pop_front();
		}
	}

	// Turn one gesture into its effect: a calibration step, or an action sent to the display (and Pi).
	void Hub::handle_gesture(const Gesture &g, int64_t t_us)
	{
		// --- Calibration mode: pinches are consumed as "I am pointing at the marker now" ---------
		if (calib_step_ >= 0 && g.kind == "down")
		{
			// Record every fresh camera's raw fingertip position for this marker.
			std::map<int, Vec2> sample;
			for (const auto &[cam, p] : fuser_.tips(t_us, int64_t(cfg_.gesture.stale_ms) * 1000))
				sample[cam] = p;
			calib_samples_.push_back(sample);
			++calib_step_;
			// The 4 marker positions as fractions of the screen: top-left, top-right, bottom-right, bottom-left.
			const double ix[4] = {0.1, 0.9, 0.9, 0.1}, iy[4] = {0.1, 0.1, 0.9, 0.9};
			if (calib_step_ >= 4)
			{
				// All four collected: solve one homography per camera that saw every marker.
				std::array<Vec2, 4> dst;
				for (int i = 0; i < 4; ++i)
					dst[i] = {ix[i], iy[i]}; // fractions of the screen, so the calibration still fits if the display size changes
				std::map<int, int> n;
				for (auto &s : calib_samples_)
					for (auto &[c, p] : s)
						++n[c];
				int solved = 0;
				for (auto &[cam, cnt] : n)
				{
					if (cnt < 4)
						continue; // this camera missed a marker: skip it
					std::array<Vec2, 4> src;
					bool ok = true;
					for (int i = 0; i < 4; ++i)
					{
						auto it = calib_samples_[i].find(cam);
						if (it == calib_samples_[i].end())
						{
							ok = false;
							break;
						}
						src[i] = it->second;
					}
					Homography h;
					if (ok && solve_homography(src, dst, h))
					{
						mapper_.set(cam, h);
						++solved;
					}
				}
				calib_step_ = -1;
				save_calibration();
				broadcast({{"t", "calib"}, {"done", true}, {"cameras_calibrated", solved}});
			}
			else
			{
				// More markers to go: show the next one.
				broadcast({{"t", "calib"}, {"step", calib_step_}, {"total", 4}, {"x", ix[calib_step_] * mapper_.width()}, {"y", iy[calib_step_] * mapper_.height()}});
			}
			return;
		}

		// --- Normal mode: count the gesture and send it as an action ---------------------------
		++gesture_counts_[g.kind];
		++actions_;
		json a = {{"t", "action"}, {"kind", g.kind}, {"x", int(std::lround(g.x))}, {"y", int(std::lround(g.y))}, {"detail", g.dir}};
		// Keep a short list of recent non-move gestures for the status page.
		if (g.kind != "move")
		{
			last_gestures_.push_back({{"kind", g.kind}, {"dir", g.dir}, {"x", a["x"]}, {"y", a["y"]}});
			if (last_gestures_.size() > 12)
				last_gestures_.pop_front();
		}
		if (recorder_)
			recorder_->event({{"t", "act"}, {"kind", g.kind}, {"x", a["x"]}, {"y", a["y"]}, {"dir", g.dir}});
		broadcast(a); // the display page draws the cursor / reacts
		if (edge_os_pointer_)
			send_edge(a); // optionally also drive real OS mouse events on the Pi
		// The "home" gesture returns to the launcher app.
		if (g.kind == "home" && current_app_ != "launcher")
		{
			current_app_ = "launcher";
			content_locked(cfg_.launcher_url);
		}
	}

	// ---- control ---------------------------------------------------------------

	// Switch what the display page shows (caller must already hold the lock).
	void Hub::content_locked(const std::string &url)
	{
		current_url_ = url;
		broadcast({{"t", "content"}, {"url", url}, {"app", current_app_}});
	}

	// Switch to a configured app id, "launcher", or any URL.
	bool Hub::launch(const std::string &app_or_url)
	{
		std::lock_guard lk(m_);
		for (const auto &a : cfg_.apps)
			if (a.id == app_or_url)
			{
				current_app_ = a.id;
				content_locked(a.url);
				return true;
			}
		if (app_or_url == "launcher")
		{
			current_app_ = "launcher";
			content_locked(cfg_.launcher_url);
			return true;
		}
		if (app_or_url.rfind("http", 0) == 0 || app_or_url.rfind("/", 0) == 0)
		{
			current_app_ = "custom";
			content_locked(app_or_url);
			return true;
		}
		return false; // unknown app
	}

	void Hub::home() { launch("launcher"); }

	// Begin calibration: show the first marker; the next 4 pinches will be treated as calibration clicks.
	void Hub::calibrate_start()
	{
		std::lock_guard lk(m_);
		calib_step_ = 0;
		calib_samples_.clear();
		broadcast({{"t", "calib"}, {"step", 0}, {"total", 4}, {"x", 0.1 * mapper_.width()}, {"y", 0.1 * mapper_.height()}});
	}

	// Throw away calibration (cameras go back to plain scaling).
	void Hub::calibrate_reset()
	{
		std::lock_guard lk(m_);
		calib_step_ = -1;
		mapper_.clear();
		save_calibration();
		broadcast({{"t", "calib"}, {"done", true}, {"cameras_calibrated", 0}});
	}

	// ---- automatic surface calibration -----------------------------------------------
	// The agent owns the camera, so it does the measuring (edge/surface_calib.py); the hub only starts it, relays the
	// pattern to the display, checks the agent's answer and installs it. See docs/PROTOCOL.md for the messages.

	json Hub::calibrate_auto()
	{
		std::lock_guard lk(m_);
		if (!edge_)
			return {{"ok", false}, {"error", "no camera agent is connected (start it with `python3 ips.py pi-start`)"}};
		calib_step_ = -1; // an unfinished pinch calibration is abandoned
		auto_calib_active_ = ++auto_calib_seq_;
		// The agent lays the markers out for the display's real size, so they come out square on screen.
		send_edge({{"t", "calib_capture"}, {"id", auto_calib_active_}, {"screen", {mapper_.width(), mapper_.height()}}});
		if (!edge_) // the send failed: the agent has gone
		{
			auto_calib_active_ = 0;
			return {{"ok", false}, {"error", "the camera agent stopped responding"}};
		}
		return {{"ok", true}, {"id", auto_calib_seq_}};
	}

	void Hub::calibrate_cancel()
	{
		std::lock_guard lk(m_);
		if (auto_calib_active_)
		{
			send_edge({{"t", "calib_cancel"}, {"id", auto_calib_active_}});
			auto_calib_last_ = {{"id", auto_calib_active_}, {"ok", false}, {"cameras_calibrated", 0},
								{"error", "cancelled"}, {"results", json::array()}};
		}
		auto_calib_active_ = 0;
		broadcast({{"t", "calib_pattern"}, {"show", false}});
	}

	bool Hub::calibrate_set(int cam, const json &hj, std::string &error)
	{
		Homography h;
		if (cam < 0 || cam > 15)
		{
			error = "camera index out of range";
			return false;
		}
		if (!homography_from_json(hj, h))
		{
			error = "h must be 9 finite numbers forming a usable perspective mapping";
			return false;
		}
		std::lock_guard lk(m_);
		mapper_.set(cam, h);
		calib_step_ = -1;
		save_calibration();
		broadcast({{"t", "calib"}, {"done", true}, {"cameras_calibrated", 1}});
		return true;
	}

	// The request id in an agent message (-1 when missing or not a whole number).
	static int id_of(const json &j)
	{
		return j.contains("id") && j["id"].is_number_integer() ? j["id"].get<int>() : -1;
	}

	// A text field from a message, or `fallback` when it is missing or not text (never throws), cut to `max` characters.
	static std::string text_of(const json &j, const char *key, const std::string &fallback, size_t max = 300)
	{
		if (j.contains(key) && j[key].is_string())
			return j[key].get<std::string>().substr(0, max);
		return fallback;
	}

	// Copy only well-formed markers into a fresh message for the display: a bad message cannot inject anything.
	static bool clean_pattern(const json &msg, json &out)
	{
		out = {{"t", "calib_pattern"}, {"show", msg.contains("show") && msg["show"].is_boolean() && msg["show"].get<bool>()}};
		if (!out["show"].get<bool>())
			return true;
		if (!msg.contains("markers") || !msg["markers"].is_array() || msg["markers"].empty() || msg["markers"].size() > 64)
			return false;
		int grid = msg.contains("grid") && msg["grid"].is_number_integer() ? msg["grid"].get<int>() : 6;
		if (grid < 5 || grid > 9)
			return false;
		json ms = json::array();
		for (const auto &m : msg["markers"])
		{
			if (!m.is_object())
				return false;
			json c = {{"id", m.contains("id") && m["id"].is_number_integer() ? m["id"].get<int>() : 0}};
			for (const char *k : {"x0", "y0", "x1", "y1"})
			{
				if (!m.contains(k) || !m[k].is_number())
					return false;
				double v = m[k].get<double>();
				if (!std::isfinite(v) || v < -0.01 || v > 1.01)
					return false;
				c[k] = v;
			}
			if (c["x1"].get<double>() <= c["x0"].get<double>() || c["y1"].get<double>() <= c["y0"].get<double>())
				return false;
			std::string bits = text_of(m, "bits", "", 64);
			if (bits.size() != size_t((grid - 2) * (grid - 2)) || bits.find_first_not_of("01") != std::string::npos)
				return false;
			c["bits"] = bits;
			ms.push_back(c);
		}
		out["grid"] = grid;
		out["markers"] = ms;
		return true;
	}

	bool Hub::show_calib_pattern(const json &msg)
	{
		json clean;
		if (!clean_pattern(msg, clean))
			return false;
		std::lock_guard lk(m_);
		broadcast(clean);
		return true;
	}

	void Hub::on_calib_pattern(const json &msg)
	{
		json clean;
		std::lock_guard lk(m_);
		if (!auto_calib_active_ || id_of(msg) != auto_calib_active_)
			return; // not (or no longer) calibrating: ignore a late or stray message
		if (clean_pattern(msg, clean))
			broadcast(clean);
		else
			++bad_;
	}

	void Hub::on_calib_result(const json &msg)
	{
		std::lock_guard lk(m_);
		if (!auto_calib_active_ || id_of(msg) != auto_calib_active_)
			return; // a cancelled or superseded request
		int solved = 0;
		json summary = json::array();
		std::string first_error;
		if (msg.contains("results") && msg["results"].is_array())
			for (const auto &r : msg["results"])
			{
				if (!r.is_object())
					continue;
				int cam = r.contains("cam") && r["cam"].is_number_integer() ? r["cam"].get<int>() : -1;
				bool ok = r.contains("ok") && r["ok"].is_boolean() && r["ok"].get<bool>();
				Homography h;
				if (ok && (cam < 0 || cam > 15 || !r.contains("h") || !homography_from_json(r["h"], h)))
				{
					ok = false; // the agent claimed success but sent something unusable
					if (first_error.empty())
						first_error = "the agent sent an invalid mapping";
				}
				if (ok)
				{
					mapper_.set(cam, h);
					++solved;
				}
				else if (first_error.empty())
					first_error = text_of(r, "reason", "calibration failed");
				// Keep the numbers for `ips.py calibrate auto` and the status page (not the matrix itself).
				json s = {{"cam", cam}, {"ok", ok}};
				for (const char *k : {"rms_px", "max_px", "markers", "of", "frames", "inliers"})
					if (r.contains(k) && r[k].is_number())
						s[k] = r[k];
				if (r.contains("quality") && r["quality"].is_string())
					s["quality"] = text_of(r, "quality", "", 20);
				if (!ok)
					s["reason"] = text_of(r, "reason", "calibration failed");
				summary.push_back(s);
			}
		if (solved)
			save_calibration();
		auto_calib_last_ = {{"id", auto_calib_active_}, {"ok", solved > 0}, {"cameras_calibrated", solved}, {"results", summary}};
		if (!solved)
			auto_calib_last_["error"] = first_error.empty() ? "no camera result received" : first_error;
		auto_calib_active_ = 0;
		broadcast({{"t", "calib_pattern"}, {"show", false}});
		json done = {{"t", "calib"}, {"done", true}, {"auto", true}, {"cameras_calibrated", solved}};
		if (!solved)
			done["error"] = auto_calib_last_["error"];
		broadcast(done);
	}

	// Write the calibration to disk so it survives a restart.
	void Hub::save_calibration()
	{
		auto parent = std::filesystem::path(cfg_.calib_path).parent_path();
		if (!parent.empty())
			std::filesystem::create_directories(parent);
		std::ofstream(cfg_.calib_path) << mapper_.to_json().dump(2);
	}

	// The display page reports its real pixel size so cursor coordinates match what is on screen.
	void Hub::set_display_size(int w, int h)
	{
		std::lock_guard lk(m_);
		if (w < 100 || h < 100)
			return; // ignore nonsense sizes
		mapper_.set_screen(w, h);
		rec_.set_screen(w, h);
	}

	// Dim the projected image (useful for testing tracking under different light).
	void Hub::set_brightness(double b)
	{
		std::lock_guard lk(m_);
		brightness_ = clamp01(b);
		broadcast({{"t", "brightness"}, {"value", brightness_}});
	}

	// Store one camera JPEG (only has an effect when frame recording is enabled).
	void Hub::on_frame(int cam, const uint8_t *jpeg, size_t n)
	{
		std::lock_guard lk(m_);
		++jpeg_frames_;
		if (recorder_)
			recorder_->frame(cam, jpeg, n);
	}

	// ---- queries ---------------------------------------------------------------

	// Percentile p (0..1) of a list of numbers; used for p50/p95 latency.
	double Hub::pct(std::vector<double> v, double p)
	{
		if (v.empty())
			return 0;
		std::sort(v.begin(), v.end());
		return v[size_t(p * (v.size() - 1))];
	}

	// Everything the status page and `ips.py status` display.
	json Hub::status()
	{
		std::lock_guard lk(m_);
		int64_t now = steady_us();
		// Frames per second from the recent arrival times (0 if nothing arrived in the last 2 s).
		double fps = 0; // frames per second over the last 2 seconds only (gaps between runs must not drag it down)
		{
			size_t n = 0;
			int64_t oldest = now;
			for (auto it = frame_times_.rbegin(); it != frame_times_.rend() && now - *it <= 2'000'000; ++it)
			{
				++n;
				oldest = *it;
			}
			if (n >= 2 && frame_times_.back() > oldest)
				fps = (n - 1) / ((frame_times_.back() - oldest) / 1e6);
		}
		// Helper: median and 95th percentile of a timing list.
		auto stats = [&](const std::deque<double> &d)
		{
			std::vector<double> v(d.begin(), d.end());
			return json{{"p50", pct(v, 0.5)}, {"p95", pct(v, 0.95)}, {"n", v.size()}};
		};
		json cams = json::object();
		for (auto &[c, n] : frames_by_cam_)
			cams[std::to_string(c)] = {{"frames", n}, {"calibrated", mapper_.calibrated(c)}};
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
				{"gesture_source", cfg_.gesture_source},
				{"motion_frames", motion_frames_},
				{"compare", {{"hub", compare_hub_}, {"edge", compare_edge_}}},
				{"calibrating_step", calib_step_},
				{"auto_calib", {{"active", auto_calib_active_}, {"last", auto_calib_last_}}},
				{"last_gestures", last_gestures_},
				{"blob_base", blobs_->base_uri()}};
	}

	// Count a message the server could not understand (shown as "bad_messages" on the status page).
	void Hub::note_bad_message()
	{
		std::lock_guard lk(m_);
		++bad_;
	}
	// The configured apps as JSON (used by the launcher page).
	json Hub::apps_json() const
	{
		json a = json::array();
		for (const auto &app : cfg_.apps)
			a.push_back({{"id", app.id}, {"name", app.name}, {"url", app.url}, {"needs_internet", app.needs_internet}});
		return a;
	}

} // namespace ips
