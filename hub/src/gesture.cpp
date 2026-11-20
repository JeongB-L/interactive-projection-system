// gesture.cpp
// ---------------------------------------------------------------------------
// WHAT THIS FILE DOES
//   Implements the hub's pure logic (declared in gesture.hpp):
//     - reading gesture settings from config.json,
//     - the camera -> projector homography math,
//     - turning raw landmarks into simple features (fingertip, palm, pinch ratio, open palm),
//     - merging two cameras into one hand,
//     - the DEFAULT EXAMPLE gesture recognizer (RuleRecognizer): move, click, swipe, home.
//   No network or file access happens here, so unit tests can exercise it directly.
// ---------------------------------------------------------------------------
#include "gesture.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>

namespace ips
{

	// Read each tunable from config.json, keeping the built-in default when a key is missing.
	GestureConfig GestureConfig::from_json(const json &j)
	{
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
		c.calib_hold_ms = j.value("calib_hold_ms", c.calib_hold_ms);
		c.calib_still = j.value("calib_still", c.calib_still);
		c.calib_gap_ms = j.value("calib_gap_ms", c.calib_gap_ms);
		c.click_rewind_ms = j.value("click_rewind_ms", c.click_rewind_ms);
		c.click_drag_px = j.value("click_drag_px", c.click_drag_px);
		return c;
	}

	// ---- homography ------------------------------------------------------------

	// Apply the 3x3 matrix to a point: multiply, then divide by the perspective term w.
	Vec2 Homography::apply(Vec2 p) const
	{
		double w = h[6] * p.x + h[7] * p.y + h[8];
		if (std::fabs(w) < 1e-12)
			w = 1e-12; // avoid dividing by zero
		return {(h[0] * p.x + h[1] * p.y + h[2]) / w, (h[3] * p.x + h[4] * p.y + h[5]) / w};
	}

	// Solve for the matrix from 4 point pairs (direct linear transform, with the last entry fixed to 1):
	// 4 pairs give 8 equations for 8 unknowns, solved by Gaussian elimination.
	bool solve_homography(const std::array<Vec2, 4> &s, const std::array<Vec2, 4> &d, Homography &out)
	{
		double a[8][9] = {};
		// Build the 8x9 equation matrix: two rows per point pair.
		for (int i = 0; i < 4; ++i)
		{
			double x = s[i].x, y = s[i].y, u = d[i].x, v = d[i].y;
			double r1[9] = {x, y, 1, 0, 0, 0, -u * x, -u * y, u};
			double r2[9] = {0, 0, 0, x, y, 1, -v * x, -v * y, v};
			for (int k = 0; k < 9; ++k)
			{
				a[2 * i][k] = r1[k];
				a[2 * i + 1][k] = r2[k];
			}
		}
		// Gauss-Jordan elimination with partial pivoting (pick the biggest number in the column).
		for (int c = 0; c < 8; ++c)
		{
			int p = c;
			for (int r = c + 1; r < 8; ++r)
				if (std::fabs(a[r][c]) > std::fabs(a[p][c]))
					p = r;
			if (std::fabs(a[p][c]) < 1e-12)
				return false; // degenerate (collinear points)
			for (int k = 0; k < 9; ++k)
				std::swap(a[c][k], a[p][k]);
			for (int r = 0; r < 8; ++r)
			{
				if (r == c)
					continue;
				double f = a[r][c] / a[c][c];
				for (int k = c; k < 9; ++k)
					a[r][k] -= f * a[c][k];
			}
		}
		// Read off the solution; the 9th matrix entry is fixed at 1.
		for (int i = 0; i < 8; ++i)
			out.h[i] = a[i][8] / a[i][i];
		out.h[8] = 1;
		return true;
	}

	bool homography_from_json(const json &j, Homography &out)
	{
		if (!j.is_array() || j.size() != 9)
			return false;
		Homography h;
		for (int i = 0; i < 9; ++i)
		{
			if (!j[i].is_number())
				return false;
			h.h[i] = j[i].get<double>();
			if (!std::isfinite(h.h[i]))
				return false;
		}
		if (std::fabs(h.h[8]) < 1e-12)
			return false;
		const double scale = h.h[8];
		for (auto &v : h.h)
			v /= scale;
		h.h[8] = 1; // exact (guards against rounding)
		const auto &a = h.h;
		double det = a[0] * (a[4] * a[8] - a[5] * a[7]) - a[1] * (a[3] * a[8] - a[5] * a[6]) + a[2] * (a[3] * a[7] - a[4] * a[6]);
		if (!std::isfinite(det) || std::fabs(det) < 1e-12)
			return false; // singular: it would squash the camera image onto a line
		// The camera image's corners and centre must map to finite points.
		const Vec2 probe[5] = {{0, 0}, {1, 0}, {1, 1}, {0, 1}, {0.5, 0.5}};
		for (auto p : probe)
		{
			Vec2 q = h.apply(p);
			if (!std::isfinite(q.x) || !std::isfinite(q.y))
				return false;
		}
		out = h;
		return true;
	}

	// Convert a camera point (0..1) to projector pixels using that camera's calibration,
	// or simply scale to the screen size when the camera has not been calibrated yet.
	Vec2 Mapper::to_px(int cam, Vec2 n) const
	{
		auto it = hs_.find(cam);
		// Calibrated cameras map to FRACTIONS of the screen (0..1), scaled here by the current display size.
		if (it != hs_.end())
		{
			Vec2 f = it->second.apply(n);
			// Keep the point on the surface. Near the "horizon" of a steeply tilted view the perspective divide can
			// produce huge or non-finite values; those are held at the screen centre rather than thrown off-screen.
			if (!std::isfinite(f.x) || !std::isfinite(f.y))
				f = {0.5, 0.5};
			f.x = std::clamp(f.x, 0.0, 1.0);
			f.y = std::clamp(f.y, 0.0, 1.0);
			return {f.x * w_, f.y * h_};
		}
		return {n.x * w_, n.y * h_};
	}

	// Save: screen size plus the 9 numbers per calibrated camera.
	json Mapper::to_json() const
	{
		json j;
		j["screen"] = {w_, h_};
		j["normalized"] = true; // homographies map to fractions of the screen (older files stored raw pixels)
		for (const auto &[c, h] : hs_)
			j["cams"][std::to_string(c)] = h.h;
		return j;
	}

	// Load the calibration file written by to_json().
	void Mapper::from_json(const json &j)
	{
		hs_.clear();
		if (!j.value("normalized", false)) // an older calibration file in raw pixels cannot be reused safely
		{
			std::fprintf(stderr, "old calibration file format ignored: please run `python ips.py calibrate` again\n");
			return;
		}
		if (j.contains("cams"))
			for (const auto &[k, v] : j["cams"].items())
			{
				Homography h;
				for (int i = 0; i < 9; ++i)
					h.h[i] = v.at(i).get<double>();
				hs_[std::stoi(k)] = h;
			}
	}

	// ---- features --------------------------------------------------------------

	// A finger counts as "extended" when its tip is farther from the wrist than its middle joint is.
	static bool extended(const Hand &h, int tip, int pip)
	{
		return dist(h.pt(tip), h.pt(0)) > dist(h.pt(pip), h.pt(0)) * 1.05;
	}

	// Boil one camera's hand down to the few numbers the recognizer needs.
	CamFeature extract_feature(const Hand &h, int cam, const Mapper &m, int64_t t_us)
	{
		CamFeature f;
		f.t_us = t_us;
		f.score = h.score;
		// Positions: index fingertip (landmark 8) and palm center (landmark 9) in raw camera and projector space.
		f.norm_tip = h.pt(8);
		f.tip_px = m.to_px(cam, h.pt(8));
		f.palm_px = m.to_px(cam, h.pt(9));
		// Pinch ratio: thumb-tip to index-tip distance divided by hand size (wrist to palm center),
		// so the value does not change when the hand moves closer to or farther from the camera.
		double scale = dist(h.pt(0), h.pt(9));
		f.ratio = scale > 1e-6 ? dist(h.pt(4), h.pt(8)) / scale : 9;
		// Open palm = index, middle, ring and pinky all extended.
		f.open = extended(h, 8, 6) && extended(h, 12, 10) && extended(h, 16, 14) && extended(h, 20, 18);
		return f;
	}

	// ---- camera fusion -----------------------------------------------------------

	// Store the newest view from one camera, using the most confident hand in the frame.
	void Fuser::update(const HandFrame &f, const Mapper &m, int64_t t_us)
	{
		const Hand *best = nullptr;
		for (const auto &h : f.hands)
			if (!best || h.score > best->score)
				best = &h;
		if (!best)
		{
			slots_.erase(f.camera);
			return;
		} // that camera sees no hand any more
		slots_[f.camera] = extract_feature(*best, f.camera, m, t_us);
	}

	// Merge all fresh cameras into one hand.
	Fused Fuser::fuse(int64_t t_us, int64_t stale_us) const
	{
		Fused r;
		// Cameras are not synchronized, so one may already show an open palm while the other still shows
		// the previous pose. Decide the pose by weighted vote, then average only cameras that agree with it;
		// mixing positions across poses would look like a sudden jump (a fake swipe).
		double total = 0, open_w = 0;
		for (const auto &[cam, s] : slots_)
		{
			if (t_us - s.t_us > stale_us)
				continue;						// too old: ignore
			double w = std::max(0.05, s.score); // confidence acts as the vote weight
			total += w;
			if (s.open)
				open_w += w;
		}
		if (total <= 0)
			return r; // nobody sees a hand
		bool open = open_w / total > 0.5;
		// Weighted average of position and pinch ratio over the cameras that agree on the pose.
		double wsum = 0, ratio = 0;
		Vec2 tip, palm;
		for (const auto &[cam, s] : slots_)
		{
			if (t_us - s.t_us > stale_us || s.open != open)
				continue;
			double w = std::max(0.05, s.score);
			wsum += w;
			tip.x += w * s.tip_px.x;
			tip.y += w * s.tip_px.y;
			palm.x += w * s.palm_px.x;
			palm.y += w * s.palm_px.y;
			ratio += w * s.ratio;
		}
		r.present = true;
		r.open = open;
		r.tip_px = {tip.x / wsum, tip.y / wsum};
		r.palm_px = {palm.x / wsum, palm.y / wsum};
		r.ratio = ratio / wsum;
		return r;
	}

	// The raw (uncalibrated) fingertip of every fresh camera; used by the calibration routine.
	std::vector<std::pair<int, Vec2>> Fuser::tips(int64_t t_us, int64_t stale_us) const
	{
		std::vector<std::pair<int, Vec2>> v;
		for (const auto &[cam, s] : slots_)
			if (t_us - s.t_us <= stale_us)
				v.push_back({cam, s.norm_tip});
		return v;
	}

	// ---- calibration hold ---------------------------------------------------------

	void CalibHold::reset()
	{
		holding_ = false;
		armed_ = true;
		accepted_any_ = false;
		anchor_.clear();
		sum_.clear();
	}

	// A pinch started: begin a hold, unless the hand has not released since the last marker or it is too soon.
	void CalibHold::on_down(int64_t t_us, const std::map<int, Vec2> &tips)
	{
		if (!armed_ || tips.empty() || (accepted_any_ && t_us - last_accept_ < gap_us_))
			return;
		holding_ = true;
		start_ = t_us;
		anchor_ = tips;
		sum_.clear();
	}

	// The pinch ended: a hold in progress is cancelled (too short), and the next pinch may count.
	void CalibHold::on_up()
	{
		holding_ = false;
		armed_ = true;
	}

	// Called on every frame while calibrating. Cancels the hold if the hand vanished or the fingertip moved.
	bool CalibHold::on_frame(int64_t t_us, const std::map<int, Vec2> &tips, std::map<int, Vec2> &sample)
	{
		if (!holding_)
			return false;
		if (tips.empty())
		{
			holding_ = false; // hand lost mid-hold
			return false;
		}
		for (const auto &[cam, a] : anchor_)
		{
			auto it = tips.find(cam);
			if (it != tips.end() && std::hypot(it->second.x - a.x, it->second.y - a.y) > still_)
			{
				holding_ = false; // fingertip moved: not a deliberate press on the marker
				return false;
			}
		}
		for (const auto &[cam, p] : tips)
		{
			auto &s = sum_[cam];
			s.first.x += p.x;
			s.first.y += p.y;
			++s.second;
		}
		if (t_us - start_ < hold_us_)
			return false;
		sample.clear();
		for (const auto &[cam, s] : sum_)
			sample[cam] = {s.first.x / s.second, s.first.y / s.second};
		holding_ = false;
		armed_ = false; // the hand must release before the next marker
		accepted_any_ = true;
		last_accept_ = t_us;
		return true;
	}

	// How many cameras have reported recently.
	int Fuser::active_cameras(int64_t t_us, int64_t stale_us) const
	{
		int n = 0;
		for (const auto &[cam, s] : slots_)
			if (t_us - s.t_us <= stale_us)
				++n;
		return n;
	}

	// ---- recognizer ------------------------------------------------------------

	// One-euro filter step. The cutoff frequency rises with speed, so slow motion is smoothed hard
	// (no jitter) and fast motion is smoothed lightly (no lag).
	double OneEuro::filter(double x, int64_t t_us)
	{
		// Convert a cutoff frequency and time step into a blending factor between 0 and 1.
		auto alpha = [](double cutoff, double dt)
		{
			double tau = 1.0 / (2 * 3.14159265358979 * cutoff);
			return 1.0 / (1.0 + tau / dt);
		};
		// First sample: nothing to smooth against yet.
		if (!init_)
		{
			init_ = true;
			xp_ = x;
			dxp_ = 0;
			tp_ = t_us;
			return x;
		}
		double dt = std::max((t_us - tp_) / 1e6, 1e-3); // seconds since last sample (at least 1 ms)
		double dx = (x - xp_) / dt;						// raw speed
		double ad = alpha(dcut_, dt);
		double dxh = ad * dx + (1 - ad) * dxp_; // smoothed speed
		double a = alpha(mc_ + beta_ * std::fabs(dxh), dt);
		double xh = a * x + (1 - a) * xp_; // smoothed position
		xp_ = xh;
		dxp_ = dxh;
		tp_ = t_us;
		return xh;
	}

	// Set up the two cursor filters with the configured smoothing.
	RuleRecognizer::RuleRecognizer(GestureConfig c, int w, int h) : c_(c), w_(w), h_(h)
	{
		fx_.configure(c.min_cutoff, c.beta);
		fy_.configure(c.min_cutoff, c.beta);
	}

	// Forget everything (used when the hand disappears or a new session starts).
	void RuleRecognizer::reset()
	{
		fx_.reset();
		fy_.reset();
		present_ = pinched_ = home_latched_ = false;
		palm_hist_.clear();
		still_since_ = 0;
		last_emit_ = {-1e9, -1e9};
	}

	// THE MAIN DECISION FUNCTION. Called once per fused frame; returns the gestures that just happened.
	//   open palm (moving fast)  -> swipe
	//   open palm (held still)   -> home
	//   otherwise                -> cursor follows the fingertip; pinch = click (down/up)
	//   no hand                  -> release any pinch, hide the cursor
	std::vector<Gesture> RuleRecognizer::update(const Fused &f, int64_t t)
	{
		std::vector<Gesture> out;
		// Small helper that appends a gesture stamped with the current time.
		auto emit = [&](const char *kind, Vec2 p, const std::string &dir = "")
		{
			out.push_back({kind, p.x, p.y, dir, t});
		};

		// --- Case 1: no hand visible -------------------------------------------------
		if (!f.present)
		{
			if (pinched_)
				emit("up", cursor_); // never leave a click "stuck down"
			if (present_)
				emit("hide", cursor_); // tell the display to hide the cursor
			reset();
			return out;
		}
		present_ = true;

		// --- Case 2: open palm: swipe or home (the cursor is hidden meanwhile) ------------
		if (f.open && f.ratio > c_.pinch_open)
		{
			if (pinched_)
			{
				emit("up", cursor_);
				pinched_ = false;
			}
			// Remember where the palm has been over the last few hundred milliseconds.
			palm_hist_.push_back({t, f.palm_px});
			int64_t keep = std::max<int64_t>(c_.swipe_window_ms, 300) * 1000;
			while (!palm_hist_.empty() && t - palm_hist_.front().first > keep)
				palm_hist_.pop_front();

			// Swipe check: compare the newest palm position with the oldest one inside the swipe window.
			const auto &newest = palm_hist_.back();
			for (const auto &s : palm_hist_)
			{
				if (t - s.first > int64_t(c_.swipe_window_ms) * 1000)
					continue;
				double dt = (newest.first - s.first) / 1e6;
				if (dt < 0.08)
					break; // too little time elapsed to judge speed reliably
				double dx = newest.second.x - s.second.x, dy = newest.second.y - s.second.y;
				double adx = std::fabs(dx), ady = std::fabs(dy);
				double major = std::max(adx, ady), span = adx >= ady ? w_ : h_;
				double speed = major / span / dt; // screen widths (or heights) per second
				// Fast enough, far enough, and not right after another swipe -> it's a swipe.
				if (speed >= c_.swipe_min_speed && major >= 0.15 * span &&
					t - last_swipe_ > int64_t(c_.swipe_cooldown_ms) * 1000)
				{
					std::string dir = adx >= ady ? (dx > 0 ? "right" : "left") : (dy > 0 ? "down" : "up");
					emit("swipe", newest.second, dir);
					last_swipe_ = t;
					palm_hist_.clear();
					still_since_ = 0;
					home_latched_ = true; // a swipe must not also count as a home hold
				}
				break;
			}

			// Home check: has the palm stayed inside a small box for the whole recent history?
			if (!palm_hist_.empty())
			{
				double minx = 1e18, maxx = -1e18, miny = 1e18, maxy = -1e18;
				for (const auto &s : palm_hist_)
				{
					minx = std::min(minx, s.second.x);
					maxx = std::max(maxx, s.second.x);
					miny = std::min(miny, s.second.y);
					maxy = std::max(maxy, s.second.y);
				}
				bool still = (maxx - minx) < 0.04 * w_ && (maxy - miny) < 0.04 * h_;
				if (still)
				{
					if (still_since_ == 0)
						still_since_ = t; // start the stopwatch
					// Held still long enough, and not already triggered -> go home (once per raise of the palm).
					if (!home_latched_ && t - still_since_ >= int64_t(c_.home_hold_ms) * 1000)
					{
						emit("home", f.palm_px);
						home_latched_ = true;
					}
				}
				else
				{
					still_since_ = 0; // moved: restart the stopwatch
				}
			}
			return out;
		}

		// --- Case 3: pointing / pinching: the cursor follows the fused index fingertip ----------
		palm_hist_.clear();
		still_since_ = 0;
		home_latched_ = false; // lowering the palm re-arms "home"
		// Smooth the fingertip position.
		Vec2 p{fx_.filter(f.tip_px.x, t), fy_.filter(f.tip_px.y, t)};
		cursor_ = p;
		// Only report a move if the cursor travelled beyond the dead zone (hides tiny jitter).
		if (dist(p, last_emit_) >= c_.dead_zone_px)
		{
			emit("move", p);
			last_emit_ = p;
		}
		// Pinch with hysteresis: close below pinch_close to press, open above pinch_open to release.
		if (!pinched_ && f.ratio < c_.pinch_close)
		{
			pinched_ = true;
			emit("down", p);
		}
		else if (pinched_ && f.ratio > c_.pinch_open)
		{
			pinched_ = false;
			emit("up", p);
		}
		return out;
	}

// ---- click steadier ------------------------------------------------------------

	void ClickSteady::reset()
	{
		hist_.clear();
		pressed_ = dragging_ = false;
	}

	std::vector<Gesture> ClickSteady::filter(const std::vector<Gesture> &in)
	{
		std::vector<Gesture> out;
		for (Gesture g : in)
		{
			Vec2 p{g.x, g.y};
			if (g.kind == "move")
			{
				hist_.push_back({g.t_us, p});
				while (hist_.size() > 1 && g.t_us - hist_.front().first > rewind_us_ + 500'000)
					hist_.pop_front();
				if (pressed_ && !dragging_)
				{
					if (drag_px_ <= 0 || dist(p, press_) <= drag_px_)
						continue; // hold the cursor still while pinched
					dragging_ = true;
				}
				out.push_back(g);
			}
			else if (g.kind == "down")
			{
				// Newest remembered position at least rewind_us_ old; if the hand only just appeared, the oldest one.
				if (rewind_us_ > 0 && !hist_.empty())
				{
					Vec2 r = hist_.front().second;
					for (const auto &h : hist_)
						if (h.first <= g.t_us - rewind_us_)
							r = h.second;
					g.x = r.x;
					g.y = r.y;
				}
				pressed_ = true;
				dragging_ = false;
				press_ = {g.x, g.y};
				out.push_back(g);
			}
			else if (g.kind == "up")
			{
				if (pressed_ && !dragging_)
				{
					g.x = press_.x;
					g.y = press_.y;
				}
				pressed_ = dragging_ = false;
				out.push_back(g);
			}
			else
			{
				if (g.kind == "hide")
					reset();
				out.push_back(g);
			}
		}
		return out;
	}

// ---- edge adapter (Johnny's motion engine -> our gestures) -------------------

// Forget the current hand and any active pinch (new session).
void EdgeAdapter::reset()
{
	present_ = pinched_ = false;
	primary_ = -1;
	last_emit_ = {-1e9, -1e9};
}

// Convert one motion-engine result into gestures (see gesture.hpp for the input format).
// Malformed input never throws: it is treated as "no hand".
std::vector<Gesture> EdgeAdapter::update(const json &motion, const Mapper &m, int cam, int64_t t)
{
	std::vector<Gesture> out;
	// Small helper that appends a gesture stamped with the current time.
	auto emit = [&](const char *kind, Vec2 p, const std::string &dir = "")
	{ out.push_back({kind, p.x, p.y, dir, t}); };
	try
	{
		// Choose the controlling hand: the detected (not merely predicted) hand with the lowest id.
		const json *primary = nullptr;
		if (motion.is_object() && motion.contains("hands") && motion["hands"].is_array())
			for (const auto &h : motion["hands"])
				if (h.value("detected", false) && (!primary || h.value("id", 0) < primary->value("id", 0)))
					primary = &h;

		// No usable hand: never leave a click stuck down, and hide the cursor once.
		if (!primary)
		{
			if (pinched_)
				emit("up", cursor_);
			if (present_)
				emit("hide", cursor_);
			reset();
			return out;
		}

		// If the controlling hand changed (the first one left), release any pinch that belonged to the old hand.
		int id = primary->value("id", 0);
		if (primary_ != -1 && id != primary_ && pinched_)
		{
			emit("up", cursor_);
			pinched_ = false;
		}
		primary_ = id;
		present_ = true;

		// Cursor: the engine's smoothed fingertip (normalized 0..1) converted to projector pixels through the calibration.
		Vec2 tip{0.5, 0.5};
		if (primary->contains("tip") && (*primary)["tip"].is_array() && (*primary)["tip"].size() == 2)
			tip = {(*primary)["tip"][0].get<double>(), (*primary)["tip"][1].get<double>()};
		cursor_ = m.to_px(cam, tip);
		if (dist(cursor_, last_emit_) >= dz_) // only report movement beyond the dead zone
		{
			emit("move", cursor_);
			last_emit_ = cursor_;
		}

		// The engine's events, in the order it reported them. Only the controlling hand can click.
		if (motion.contains("events") && motion["events"].is_array())
			for (const auto &ev : motion["events"])
			{
				std::string type = ev.value("type", "");
				int hand = ev.value("hand_id", -1);
				if (type == "PINCH_START" && hand == id && !pinched_)
				{
					pinched_ = true;
					emit("down", cursor_);
				}
				else if (type == "PINCH_END" && hand == id && pinched_)
				{
					pinched_ = false;
					emit("up", cursor_);
				}
				else if (type == "SWIPE_LEFT")
					emit("swipe", cursor_, "left");
				else if (type == "SWIPE_RIGHT")
					emit("swipe", cursor_, "right");
				else if (type == "SWIPE_UP")
					emit("swipe", cursor_, "up");
				else if (type == "SWIPE_DOWN")
					emit("swipe", cursor_, "down");
				else if (type == "ZOOM_IN")
					emit("zoom", cursor_, "in");
				else if (type == "ZOOM_OUT")
					emit("zoom", cursor_, "out");
			}
	}
	catch (const std::exception &)
	{
		// Wrong types in the message (e.g. a text where a number should be): ignore this frame's motion data.
		out.clear();
	}
	return out;
}
} // namespace ips
