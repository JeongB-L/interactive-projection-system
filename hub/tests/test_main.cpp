// test_main.cpp
// ---------------------------------------------------------------------------
// WHAT THIS FILE DOES
//   Unit tests for the hub's pure logic and storage. No test framework is needed: each TEST(...)
//   below registers itself, main() at the bottom runs them all, and CHECK(...) records a failure.
//   Run with:  python ips.py test      (exit code 0 = all passed)
//   Covered: homography math, cursor mapping, pinch hysteresis, swipe, home, hand loss, dead zone,
//            two-camera fusion (including the pose-change bug fix), and the blob store + SQLite catalog.
// ---------------------------------------------------------------------------
// Minimal self-contained tests (no framework). Run: ips_tests
#include <cstdio>
#include <filesystem>

#include "hub.hpp"
#include <thread>
#include "gesture.hpp"
#include "storage.hpp"

using namespace ips;
// Counters for the final summary: failed checks and total checks run.
static int g_fail = 0, g_run = 0;
// CHECK(cond): count it, and print the file/line if the condition is false.
#define CHECK(c) do { ++g_run; if (!(c)) { ++g_fail; std::printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #c); } } while (0)
// TEST(name): defines a test function and registers it in a list so main() can run it.
#define TEST(name) static void name(); static struct R_##name { R_##name() { tests().push_back({#name, name}); } } r_##name; static void name()
struct T { const char* n; void (*f)(); };
static std::vector<T>& tests() { static std::vector<T> v; return v; }

// Helper: how many gestures of a given kind are in a list.
static int count(const std::vector<Gesture>& v, const char* k) {
  int n = 0;
  for (auto& g : v) if (g.kind == k) ++n;
  return n;
}

// The 4 points used to solve the homography must map exactly onto their targets,
// and a point in the middle must land in the middle of the screen.
TEST(homography_maps_corners) {
  std::array<Vec2, 4> src{{{0.2, 0.2}, {0.8, 0.25}, {0.85, 0.8}, {0.15, 0.75}}};
  std::array<Vec2, 4> dst{{{192, 108}, {1728, 108}, {1728, 972}, {192, 972}}};
  Homography h;
  CHECK(solve_homography(src, dst, h));
  for (int i = 0; i < 4; ++i) {
    Vec2 p = h.apply(src[i]);
    CHECK(std::fabs(p.x - dst[i].x) < 1e-6 && std::fabs(p.y - dst[i].y) < 1e-6);
  }
  Vec2 mid = h.apply({0.5, 0.5});
  CHECK(mid.x > 800 && mid.x < 1100 && mid.y > 400 && mid.y < 700);
}

// Four points on one line cannot define a mapping: the solver must refuse, not return garbage.
TEST(homography_rejects_degenerate) {
  std::array<Vec2, 4> src{{{0, 0}, {1, 1}, {2, 2}, {3, 3}}};  // collinear
  std::array<Vec2, 4> dst{{{0, 0}, {1, 0}, {1, 1}, {0, 1}}};
  Homography h;
  CHECK(!solve_homography(src, dst, h));
}

// Before calibration, a camera point (0..1) is simply scaled to the screen size.
TEST(mapper_uncalibrated_scales_to_screen) {
  Mapper m(1000, 500);
  Vec2 p = m.to_px(0, {0.5, 0.5});
  CHECK(std::fabs(p.x - 500) < 1e-9 && std::fabs(p.y - 250) < 1e-9);
}

// Thumb-index ratio wobbles between the two thresholds: that must produce exactly ONE click,
// not a flickering series of clicks.
TEST(pinch_hysteresis_one_click) {
  GestureConfig c;
  RuleRecognizer r(c, 1920, 1080);
  std::vector<Gesture> all;
  const double ratios[] = {0.9, 0.5, 0.3, 0.22, 0.30, 0.35, 0.28, 0.24, 0.39, 0.35, 0.45, 0.8};  // wobbles between thresholds
  int64_t t = 1'000'000;
  for (double ratio : ratios) {
    Fused f; f.present = true; f.tip_px = {900, 500}; f.ratio = ratio; f.open = false;
    for (auto& g : r.update(f, t)) all.push_back(g);
    t += 33'000;
  }
  CHECK(count(all, "down") == 1);
  CHECK(count(all, "up") == 1);
}

// An open palm moving quickly to the right must produce exactly one 'right' swipe (and no 'home').
TEST(swipe_right_detected_once) {
  GestureConfig c;
  RuleRecognizer r(c, 1920, 1080);
  std::vector<Gesture> all;
  int64_t t = 1'000'000;
  for (int i = 0; i < 12; ++i) {
    Fused f; f.present = true; f.open = true; f.ratio = 1.0; f.palm_px = {300.0 + 100 * i, 500};
    for (auto& g : r.update(f, t)) all.push_back(g);
    t += 33'000;
  }
  CHECK(count(all, "swipe") == 1);
  for (auto& g : all) if (g.kind == "swipe") CHECK(g.dir == "right");
  CHECK(count(all, "home") == 0);
}

// Slow drifting of an open palm must NOT count as a swipe.
TEST(slow_palm_motion_is_not_a_swipe) {
  GestureConfig c;
  RuleRecognizer r(c, 1920, 1080);
  std::vector<Gesture> all;
  int64_t t = 1'000'000;
  for (int i = 0; i < 40; ++i) {
    Fused f; f.present = true; f.open = true; f.ratio = 1.0; f.palm_px = {300.0 + 5 * i, 500};
    for (auto& g : r.update(f, t)) all.push_back(g);
    t += 33'000;
  }
  CHECK(count(all, "swipe") == 0);
}

// An open palm held still: no 'home' before about 1 second, exactly one after that (latched, no repeats).
TEST(home_after_hold) {
  GestureConfig c;
  RuleRecognizer r(c, 1920, 1080);
  int homes_before = 0, homes_after = 0;
  int64_t t = 1'000'000;
  for (int i = 0; i < 50; ++i) {  // 50 * 33 ms = 1.65 s
    Fused f; f.present = true; f.open = true; f.ratio = 1.0; f.palm_px = {960, 540};
    int h = count(r.update(f, t), "home");
    if (t - 1'000'000 < 950'000) homes_before += h; else homes_after += h;
    t += 33'000;
  }
  CHECK(homes_before == 0);
  CHECK(homes_after == 1);  // latched: no repeat while the palm stays up
}

// If the hand disappears while pinching, the click is released and the cursor hidden.
TEST(hand_lost_releases_and_hides) {
  GestureConfig c;
  RuleRecognizer r(c, 1920, 1080);
  int64_t t = 1'000'000;
  for (int i = 0; i < 3; ++i) { Fused f; f.present = true; f.tip_px = {500, 500}; f.ratio = 0.1; r.update(f, t); t += 33'000; }
  Fused none;
  auto g = r.update(none, t);
  CHECK(count(g, "up") == 1);
  CHECK(count(g, "hide") == 1);
}

// Tiny 2 px jitter inside a 10 px dead zone must not generate a stream of move events.
TEST(dead_zone_suppresses_jitter) {
  GestureConfig c;
  c.dead_zone_px = 10;
  RuleRecognizer r(c, 1920, 1080);
  int moves = 0;
  int64_t t = 1'000'000;
  for (int i = 0; i < 30; ++i) {
    Fused f; f.present = true; f.ratio = 1.0; f.tip_px = {900.0 + (i % 2) * 2, 500};  // 2 px jitter
    moves += count(r.update(f, t), "move");
    t += 33'000;
  }
  CHECK(moves == 1);  // only the first position is emitted
}

// Helper: a hand whose 21 landmarks are all at one point (enough for position/confidence tests).
static Hand hand_at(double x, double y, float score) {
  Hand h; h.score = score;
  for (int i = 0; i < 21; ++i) { h.xyz[i * 3] = float(x); h.xyz[i * 3 + 1] = float(y); }
  return h;
}

// Two cameras disagree: the result leans toward the more confident one, and a camera that
// stops reporting is ignored after the stale timeout.
TEST(fusion_weights_by_confidence_and_drops_stale) {
  Mapper m(1000, 1000);
  Fuser fu;
  HandFrame a; a.camera = 0; a.hands = {hand_at(0.40, 0.5, 0.9f)};
  HandFrame b; b.camera = 1; b.hands = {hand_at(0.60, 0.5, 0.3f)};
  fu.update(a, m, 1'000'000);
  fu.update(b, m, 1'010'000);
  Fused f = fu.fuse(1'020'000, 150'000);
  CHECK(f.present);
  CHECK(f.tip_px.x > 400 && f.tip_px.x < 500);  // pulled toward the confident camera (expect 450)
  CHECK(fu.active_cameras(1'020'000, 150'000) == 2);
  Fused stale = fu.fuse(2'000'000, 150'000);
  CHECK(!stale.present);
  fu.update(a, m, 3'000'000);
  CHECK(std::fabs(fu.fuse(3'010'000, 150'000).tip_px.x - 400) < 0.01);  // camera 1 is stale, camera 0 alone
}


// (helper below builds a hand with realistic finger positions, so open palm vs pointing can be told apart)
// A hand with real finger geometry: open palm centred at (px,py), or pointing with the index tip there.
static Hand pose_hand(bool open, double px, double py, float score) {
  Hand h; h.score = score;
  double wx = open ? px : px + 0.04, wy = open ? py + 0.15 : py + 0.32;
  auto set = [&](int i, double x, double y) { h.xyz[i * 3] = float(x); h.xyz[i * 3 + 1] = float(y); };
  set(0, wx, wy);
  set(1, wx - .04, wy - .04); set(2, wx - .06, wy - .08); set(3, wx - .08, wy - .12); set(4, wx - .09, wy - .16);
  const double dx[4] = {-0.04, 0.0, 0.035, 0.065};
  for (int f = 0; f < 4; ++f) {
    int b = 5 + 4 * f; double mx = wx + dx[f], my = wy - 0.15; bool ext = f == 0 || open;
    set(b, mx, my);
    set(b + 1, mx, ext ? my - .07 : my - .04); set(b + 2, mx, ext ? my - .12 : my); set(b + 3, mx, ext ? my - .17 : my + .03);
  }
  return h;
}

// Regression test for a real bug: when the cameras notice the pose change one frame apart,
// fusion must not average two different poses and invent a swipe.
TEST(staggered_pose_change_across_cameras_is_not_a_swipe) {
  Mapper m(1920, 1080);
  Fuser fu;
  RuleRecognizer r(GestureConfig{}, 1920, 1080);
  std::vector<Gesture> all;
  int64_t t = 1'000'000;
  auto feed = [&](int cam, Hand h) {
    HandFrame f; f.camera = cam; f.hands = {h};
    fu.update(f, m, t);
    for (auto& g : r.update(fu.fuse(t, 150'000), t)) all.push_back(g);
    t += 8'000;
  };
  for (int i = 0; i < 20; ++i) { feed(0, pose_hand(false, 0.55, 0.5, .95f)); feed(1, pose_hand(false, 0.55, 0.5, .85f)); }
  // camera 0 sees the open palm one frame before camera 1 does
  feed(0, pose_hand(true, 0.15, 0.5, .95f));
  for (int i = 0; i < 30; ++i) { feed(1, pose_hand(true, 0.15, 0.5, .85f)); feed(0, pose_hand(true, 0.15, 0.5, .95f)); }
  CHECK(count(all, "swipe") == 0);
}

// Record a fake session (events + two cameras of video), then check that the files exist on disk
// and that the SQLite catalog holds only correct keys, URIs and sizes that point at them.
TEST(storage_catalog_points_at_blobs) {
  namespace fs = std::filesystem;
  fs::path tmp = fs::temp_directory_path() / "ips_test_storage";
  std::error_code ec0;
  fs::remove_all(tmp, ec0);
  auto blobs = make_blob_store("file:" + (tmp / "blobs").generic_string());
  auto catp = std::make_unique<Catalog>((tmp / "t.db").generic_string());
  Catalog& cat = *catp;
  int64_t sid = 0;
  {
    SessionRecorder rec(cat, *blobs, "testpi", "unit", true, true);
    sid = rec.session_id();
    rec.event({{"t", "hands"}, {"n", 1}});
    rec.event({{"t", "act"}, {"kind", "down"}});
    const uint8_t jpeg[4] = {0xFF, 0xD8, 0xFF, 0xD9};
    rec.frame(0, jpeg, 4);
    rec.frame(1, jpeg, 4);
  }  // destructor closes
  json a = cat.assets();
  CHECK(a.size() == 3);
  bool saw_video = false, saw_events = false;
  for (auto& row : a) {
    CHECK(row["bytes"].get<int64_t>() > 0);
    std::string uri = row["uri"], key = row["key"];
    CHECK(uri.rfind("file:///", 0) == 0);
    CHECK(uri.size() > key.size() && uri.compare(uri.size() - key.size(), key.size(), key) == 0);
    CHECK(fs::exists(tmp / "blobs" / key));
    if (row["kind"] == "video_mjpeg") saw_video = true;
    if (row["kind"] == "events_ndjson") saw_events = true;
  }
  CHECK(saw_video && saw_events);
  CHECK(cat.assets("video_mjpeg").size() == 2);
  json s = cat.sessions();
  CHECK(s.size() == 1 && s[0]["id"].get<int64_t>() == sid && !s[0]["ended_utc"].get<std::string>().empty());
  bool threw = false;
  try { make_blob_store("s3://bucket/x"); } catch (const std::exception&) { threw = true; }
  CHECK(threw);
  catp.reset();  // close the database before deleting its folder
  std::error_code ec1;
  fs::remove_all(tmp, ec1);
}

// Runs every registered test, prints ok/FAIL per test and a summary, and returns non-zero on failure.
// ============================================================================================
// Johnny's motion engine -> gestures (EdgeAdapter), and the hub's "hub" / "edge" gesture_source modes
// ============================================================================================

// A hand as Johnny's engine reports it (normalized fingertip), and a whole motion result with events.
static json m_hand(int id, double x, double y, bool detected = true) {
	return {{"id", id}, {"handedness", "Right"}, {"detected", detected}, {"predicted", !detected},
					{"tip", {x, y}}, {"center", {x, y}}, {"pinching", false}};
}
static json m_event(const char *type, int hand = 1) { return {{"type", type}, {"hand_id", hand}}; }
static json motion_of(std::vector<json> hands, std::vector<json> events = {}) {
	json m = {{"hands", json::array()}, {"detected_hands", 0}, {"events", json::array()}};
	int detected = 0;
	for (auto &h : hands) {
		m["hands"].push_back(h);
		if (h["detected"].get<bool>()) ++detected;
	}
	for (auto &e : events) m["events"].push_back(e);
	m["detected_hands"] = detected;
	return m;
}
static std::string kinds(const std::vector<Gesture> &g) {
	std::string s;
	for (auto &x : g) s += (s.empty() ? "" : ",") + x.kind;
	return s;
}

TEST(edge_cursor_maps_the_fingertip_to_pixels) {
	Mapper m(1000, 500);
	EdgeAdapter a(6);
	auto g = a.update(motion_of({m_hand(1, 0.25, 0.5)}), m, 0, 1000);
	CHECK(g.size() == 1 && g[0].kind == "move");
	CHECK(std::fabs(g[0].x - 250) < 1e-6 && std::fabs(g[0].y - 250) < 1e-6);
}

TEST(edge_dead_zone_hides_tiny_movements) {
	Mapper m(1000, 500);
	EdgeAdapter a(6);
	a.update(motion_of({m_hand(1, 0.500, 0.5)}), m, 0, 1000);
	CHECK(a.update(motion_of({m_hand(1, 0.502, 0.5)}), m, 0, 2000).empty());  // 2 px: ignored
	CHECK(kinds(a.update(motion_of({m_hand(1, 0.540, 0.5)}), m, 0, 3000)) == "move");  // 38 px: reported
}

TEST(edge_pinch_gives_one_down_then_one_up) {
	Mapper m(1000, 500);
	EdgeAdapter a(6);
	a.update(motion_of({m_hand(1, 0.5, 0.5)}), m, 0, 1000);
	CHECK(kinds(a.update(motion_of({m_hand(1, 0.5, 0.5)}, {m_event("PINCH_START")}), m, 0, 2000)) == "down");
	CHECK(a.update(motion_of({m_hand(1, 0.5, 0.5)}, {m_event("PINCH_START")}), m, 0, 3000).empty());  // repeat ignored
	CHECK(kinds(a.update(motion_of({m_hand(1, 0.5, 0.5)}, {m_event("PINCH_END")}), m, 0, 4000)) == "up");
	CHECK(a.update(motion_of({m_hand(1, 0.5, 0.5)}, {m_event("PINCH_END")}), m, 0, 5000).empty());  // stray end ignored
}

TEST(edge_move_is_reported_before_the_click_in_the_same_frame) {
	Mapper m(1000, 500);
	EdgeAdapter a(6);
	auto g = a.update(motion_of({m_hand(1, 0.3, 0.5)}, {m_event("PINCH_START")}), m, 0, 1000);
	CHECK(kinds(g) == "move,down");
	CHECK(std::fabs(g[1].x - 300) < 1e-6);  // the click happens where the cursor is
}

TEST(edge_only_the_controlling_hand_can_click) {
	Mapper m(1000, 500);
	EdgeAdapter a(6);
	json two = {m_hand(1, 0.2, 0.5), m_hand(2, 0.8, 0.5)};
	auto g = a.update(motion_of({two[0], two[1]}, {m_event("PINCH_START", 2)}), m, 0, 1000);
	CHECK(kinds(g) == "move");  // the second hand pinching does not click
}

TEST(edge_cursor_follows_the_lowest_id_hand) {
	Mapper m(1000, 500);
	EdgeAdapter a(6);
	auto g = a.update(motion_of({m_hand(2, 0.8, 0.5), m_hand(1, 0.2, 0.5)}), m, 0, 1000);
	CHECK(g.size() == 1 && std::fabs(g[0].x - 200) < 1e-6);
}

TEST(edge_lost_hand_releases_the_pinch_and_hides_once) {
	Mapper m(1000, 500);
	EdgeAdapter a(6);
	a.update(motion_of({m_hand(1, 0.5, 0.5)}, {m_event("PINCH_START")}), m, 0, 1000);
	CHECK(kinds(a.update(motion_of({}), m, 0, 2000)) == "up,hide");
	CHECK(a.update(motion_of({}), m, 0, 3000).empty());  // nothing more once hidden
}

TEST(edge_a_merely_predicted_hand_counts_as_no_hand) {
	Mapper m(1000, 500);
	EdgeAdapter a(6);
	a.update(motion_of({m_hand(1, 0.5, 0.5)}), m, 0, 1000);
	CHECK(kinds(a.update(motion_of({m_hand(1, 0.5, 0.5, false)}), m, 0, 2000)) == "hide");
}

TEST(edge_changing_the_controlling_hand_releases_the_old_pinch) {
	Mapper m(1000, 500);
	EdgeAdapter a(6);
	a.update(motion_of({m_hand(1, 0.3, 0.5)}, {m_event("PINCH_START")}), m, 0, 1000);
	auto g = a.update(motion_of({m_hand(2, 0.7, 0.5)}), m, 0, 2000);  // hand 1 left, hand 2 took over
	CHECK(kinds(g) == "up,move");
}

TEST(edge_swipes_map_to_four_directions) {
	Mapper m(1000, 500);
	const char *types[4] = {"SWIPE_LEFT", "SWIPE_RIGHT", "SWIPE_UP", "SWIPE_DOWN"};
	const char *dirs[4] = {"left", "right", "up", "down"};
	for (int i = 0; i < 4; ++i) {
		EdgeAdapter a(6);
		auto g = a.update(motion_of({m_hand(1, 0.5, 0.5)}, {m_event(types[i])}), m, 0, 1000);
		CHECK(g.size() == 2 && g[1].kind == "swipe" && g[1].dir == dirs[i]);
	}
}

TEST(edge_zoom_maps_to_in_and_out) {
	Mapper m(1000, 500);
	EdgeAdapter a(6);
	auto g = a.update(motion_of({m_hand(1, 0.5, 0.5)}, {m_event("ZOOM_IN")}), m, 0, 1000);
	CHECK(g.size() == 2 && g[1].kind == "zoom" && g[1].dir == "in");
	g = a.update(motion_of({m_hand(1, 0.5, 0.5)}, {m_event("ZOOM_OUT")}), m, 0, 2000);
	CHECK(g.size() == 1 && g[0].kind == "zoom" && g[0].dir == "out");
}

TEST(edge_cursor_uses_the_calibration) {
	Mapper m(1000, 500);
	Homography h;
	h.h = {2, 0, 0, 0, 2, 0, 0, 0, 1};  // a made-up calibration that doubles every coordinate (as fractions of the screen)
	m.set(0, h);
	EdgeAdapter a(0);
	auto g = a.update(motion_of({m_hand(1, 0.25, 0.25)}), m, 0, 1000);
	CHECK(g.size() == 1 && std::fabs(g[0].x - 500) < 1e-9 && std::fabs(g[0].y - 250) < 1e-9);  // doubled (0.5) as a fraction of a 1000x500 screen
	auto other = a.update(motion_of({m_hand(1, 0.25, 0.25)}), m, 1, 2000);  // camera 1 is uncalibrated: plain scaling
	CHECK(other.size() == 1 && std::fabs(other[0].x - 250) < 1e-6);
}

TEST(edge_garbage_input_never_throws) {
	Mapper m(1000, 500);
	EdgeAdapter a(6);
	CHECK(a.update(json("garbage"), m, 0, 1000).empty());
	CHECK(a.update(json::object(), m, 0, 2000).empty());
	CHECK(a.update(json{{"hands", "oops"}}, m, 0, 3000).empty());
	json bad_tip = {{"hands", json::array({{{"id", 1}, {"detected", true}, {"tip", {"a", "b"}}}})}};
	CHECK(a.update(bad_tip, m, 0, 4000).empty());  // wrong types: the whole frame is ignored
	json no_tip = {{"hands", json::array({{{"id", 1}, {"detected", true}}})}};
	CHECK(kinds(a.update(no_tip, m, 0, 5000)) == "move");  // missing tip: treated as the screen centre, no crash
}

TEST(edge_reset_forgets_everything) {
	Mapper m(1000, 500);
	EdgeAdapter a(6);
	a.update(motion_of({m_hand(1, 0.5, 0.5)}, {m_event("PINCH_START")}), m, 0, 1000);
	a.reset();
	CHECK(a.update(motion_of({}), m, 0, 2000).empty());  // no phantom "up" or "hide" after a reset
}

// ---- the whole hub, in both modes ----------------------------------------------------------------

// A real Hub in a temporary folder whose display output is captured (no network involved).
struct HubFixture {
	std::filesystem::path dir;
	std::unique_ptr<Hub> hub;
	std::vector<json> all, actions;
	explicit HubFixture(const std::string &source) {
		static int counter = 0;
		dir = std::filesystem::temp_directory_path() / ("ips_hub_test_" + std::to_string(++counter));
		std::error_code ec;
		std::filesystem::remove_all(dir, ec);
		Config c;
		c.db_path = (dir / "t.db").generic_string();
		c.blob_uri = "file:" + (dir / "blobs").generic_string();
		c.calib_path = (dir / "calib.json").generic_string();
		c.gesture_source = source;
		c.screen_w = 1000;
		c.screen_h = 500;
		c.gesture.calib_hold_ms = 60;  // short hold / gap so the calibration tests run quickly
		c.gesture.calib_gap_ms = 100;
		hub = std::make_unique<Hub>(c);
		hub->add_display([this](const std::string &s) {
			json j = json::parse(s);
			all.push_back(j);
			if (j.value("t", "") == "action") actions.push_back(j);
			return true;
		});
	}
	~HubFixture() {
		hub.reset();
		std::error_code ec;
		std::filesystem::remove_all(dir, ec);
	}
	int count(const std::string &kind) const {
		int n = 0;
		for (auto &a : actions) if (a.value("kind", "") == kind) ++n;
		return n;
	}
	const json *first(const std::string &kind) const {
		for (auto &a : actions) if (a.value("kind", "") == kind) return &a;
		return nullptr;
	}
};

static uint64_t g_seq = 0;
static json hands_msg(int cam, const std::vector<Hand> &hs, json motion = nullptr) {
	json j = {{"t", "hands"}, {"cam", cam}, {"seq", ++g_seq}, {"ts", wall_us()}, {"track_ms", 10.0}, {"hands", json::array()}};
	for (auto &h : hs) {
		json pts = json::array();
		for (int i = 0; i < 21; ++i) pts.push_back({h.xyz[i * 3], h.xyz[i * 3 + 1], 0.0});
		j["hands"].push_back({{"right", true}, {"score", h.score}, {"pts", pts}});
	}
	if (!motion.is_null()) j["motion"] = motion;
	return j;
}
// Our own recognizer's view of a pinch: bring the thumb tip (landmark 4) onto the index tip (landmark 8).
static Hand pinched(Hand h) {
	for (int k = 0; k < 2; ++k) h.xyz[4 * 3 + k] = h.xyz[8 * 3 + k] + 0.004f;
	return h;
}

TEST(hub_mode_acts_on_our_rules_and_only_counts_the_edge_events) {
	HubFixture f("hub");
	f.hub->on_hands(hands_msg(0, {pose_hand(false, 0.5, 0.5, .95f)}, motion_of({m_hand(1, 0.25, 0.5)}, {m_event("PINCH_START")})));
	CHECK(f.count("down") == 0);                      // the edge engine's pinch did NOT click
	const json *mv = f.first("move");
	CHECK(mv && std::abs((*mv)["x"].get<int>() - 500) <= 1);  // the cursor is where OUR recognizer put it (0.5), not his (0.25)
	json s = f.hub->status();
	CHECK(s["gesture_source"] == "hub");
	CHECK(s["compare"]["edge"]["down"] == 1);         // but it was counted for comparison
	CHECK(!s["compare"]["hub"].contains("down"));
}

TEST(edge_mode_acts_on_johnnys_events) {
	HubFixture f("edge");
	f.hub->on_hands(hands_msg(0, {pose_hand(false, 0.5, 0.5, .95f)}, motion_of({m_hand(1, 0.25, 0.5)}, {m_event("PINCH_START")})));
	CHECK(f.count("down") == 1);
	const json *mv = f.first("move");
	CHECK(mv && std::abs((*mv)["x"].get<int>() - 250) <= 1);  // the cursor follows HIS fingertip (0.25 of 1000 px)
	json s = f.hub->status();
	CHECK(s["gesture_source"] == "edge");
	CHECK(s["gesture_counts"]["down"] == 1);
	CHECK(s["compare"]["edge"]["down"] == 1);
}

TEST(both_engines_agree_on_an_obvious_pinch_and_only_one_clicks) {
	HubFixture f("hub");
	f.hub->on_hands(hands_msg(0, {pinched(pose_hand(false, 0.5, 0.5, .95f))}, motion_of({m_hand(1, 0.5, 0.5)}, {m_event("PINCH_START")})));
	json s = f.hub->status();
	CHECK(s["compare"]["hub"]["down"] == 1 && s["compare"]["edge"]["down"] == 1);
	CHECK(f.count("down") == 1);  // never a double click
}

TEST(edge_mode_falls_back_to_our_rules_when_no_motion_data_arrives) {
	HubFixture f("edge");
	f.hub->on_hands(hands_msg(0, {pinched(pose_hand(false, 0.5, 0.5, .95f))}));  // no "motion" key (agent started with --no-motion)
	CHECK(f.count("down") == 1);
	CHECK(f.hub->status()["motion_frames"] == 0);
}

TEST(edge_mode_follows_a_single_camera) {
	HubFixture f("edge");
	f.hub->on_hands(hands_msg(0, {pose_hand(false, 0.5, 0.5, .95f)}, motion_of({m_hand(1, 0.5, 0.5)})));
	f.hub->on_hands(hands_msg(1, {pose_hand(false, 0.5, 0.5, .95f)}, motion_of({m_hand(1, 0.5, 0.5)}, {m_event("PINCH_START")})));
	CHECK(f.count("down") == 0);  // camera 1's pinch is ignored: edge mode follows camera 0
	CHECK(!f.hub->status()["compare"]["edge"].contains("down"));
}

TEST(swipes_and_zooms_reach_the_display) {
	HubFixture f("edge");
	f.hub->on_hands(hands_msg(0, {pose_hand(false, 0.5, 0.5, .95f)}, motion_of({m_hand(1, 0.5, 0.5)}, {m_event("SWIPE_LEFT"), m_event("ZOOM_IN")})));
	const json *sw = f.first("swipe");
	const json *zm = f.first("zoom");
	CHECK(sw && (*sw)["detail"] == "left");
	CHECK(zm && (*zm)["detail"] == "in");
}

TEST(status_reports_how_many_frames_carried_motion_data) {
	HubFixture f("hub");
	for (int i = 0; i < 5; ++i) f.hub->on_hands(hands_msg(0, {pose_hand(false, 0.5, 0.5, .95f)}, motion_of({m_hand(1, 0.5, 0.5)})));
	for (int i = 0; i < 3; ++i) f.hub->on_hands(hands_msg(0, {pose_hand(false, 0.5, 0.5, .95f)}));
	CHECK(f.hub->status()["motion_frames"] == 5);
}

TEST(a_malformed_motion_field_does_not_break_the_hub) {
	HubFixture f("edge");
	json msg = hands_msg(0, {pose_hand(false, 0.5, 0.5, .95f)});
	msg["motion"] = "not an object";
	f.hub->on_hands(msg);  // falls back to our rules, no crash
	msg["motion"] = {{"hands", 5}, {"events", "x"}};
	f.hub->on_hands(msg);
	CHECK(f.hub->status()["bad_messages"] == 0);
}

TEST(edge_mode_still_provides_home_from_our_rules) {
	HubFixture f("edge");
	// An open palm held still for over a second: Johnny's engine has no such gesture, ours does.
	Hand palm = pose_hand(true, 0.5, 0.5, .95f);
	auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(1250);
	while (std::chrono::steady_clock::now() < end) {
		f.hub->on_hands(hands_msg(0, {palm}, motion_of({m_hand(1, 0.5, 0.5)})));
		std::this_thread::sleep_for(std::chrono::milliseconds(33));
	}
	CHECK(f.count("home") == 1);
}

// A deliberate calibration press with Johnny's engine: pinch, hold still past calib_hold_ms, release, then pause past calib_gap_ms.
static void calib_press(HubFixture &f, double x, double y) {
	Hand h = pose_hand(false, x, y, .95f);
	f.hub->on_hands(hands_msg(0, {h}, motion_of({m_hand(1, x, y)}, {m_event("PINCH_START")})));
	auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(90);
	while (std::chrono::steady_clock::now() < end) {
		f.hub->on_hands(hands_msg(0, {h}, motion_of({m_hand(1, x, y)})));
		std::this_thread::sleep_for(std::chrono::milliseconds(10));
	}
	f.hub->on_hands(hands_msg(0, {h}, motion_of({m_hand(1, x, y)}, {m_event("PINCH_END")})));
	std::this_thread::sleep_for(std::chrono::milliseconds(120));
}
static bool calib_done(const HubFixture &f) {
	for (auto &j : f.all) if (j.value("t", "") == "calib" && j.value("done", false)) return true;
	return false;
}

TEST(calibration_works_with_pinches_from_the_edge_engine) {
	HubFixture f("edge");
	f.hub->calibrate_start();
	const double xs[4] = {0.2, 0.8, 0.8, 0.2}, ys[4] = {0.2, 0.2, 0.8, 0.8};  // roughly where the four markers are
	for (int i = 0; i < 4; ++i) calib_press(f, xs[i], ys[i]);
	CHECK(f.count("down") == 0);  // the pinches were consumed by calibration, not delivered as clicks
	bool done = false;
	for (auto &j : f.all) if (j.value("t", "") == "calib" && j.value("done", false)) done = j["cameras_calibrated"] == 1;
	CHECK(done);
	CHECK(f.hub->status()["cameras"]["0"]["calibrated"] == true);
}

TEST(gesture_source_setting_is_read_from_the_config_file) {
	namespace fs = std::filesystem;
	fs::path dir = fs::temp_directory_path() / "ips_cfg_test";
	std::error_code ec;
	fs::remove_all(dir, ec);
	fs::create_directories(dir);
	auto load = [&](const std::string &text) {
		std::ofstream(dir / "c.json") << text;
		return Config::load((dir / "c.json").generic_string()).gesture_source;
	};
	CHECK(load("{}") == "hub");                                        // default
	CHECK(load("{\"gesture_source\": \"edge\"}") == "edge");
	CHECK(load("{\"gesture_source\": \"hub\"}") == "hub");
	CHECK(load("{\"gesture_source\": \"bogus\"}") == "hub");           // invalid value: safe default
	CHECK(load("// a comment\n{\"gesture_source\": \"edge\" /* inline */}") == "edge");  // comments are allowed
	fs::remove_all(dir, ec);
}
// ---- calibration must not depend on the display size at the moment it was made ------------------------------------

TEST(calibrated_mapping_scales_with_the_display_size) {
	Mapper m(1000, 500);
	Homography identity;  // camera (0..1) -> fraction of the screen (0..1)
	m.set(0, identity);
	Vec2 a = m.to_px(0, {0.25, 0.5});
	CHECK(std::fabs(a.x - 250) < 1e-9 && std::fabs(a.y - 250) < 1e-9);
	m.set_screen(2000, 1000);  // the display window changes size AFTER calibration (e.g. kiosk instead of a window)
	Vec2 b = m.to_px(0, {0.25, 0.5});
	CHECK(std::fabs(b.x - 500) < 1e-9 && std::fabs(b.y - 500) < 1e-9);  // still the same spot on the picture
}

TEST(saved_calibration_round_trips_and_is_marked_as_fractions) {
	Mapper m(1000, 500);
	Homography h;
	h.h = {1.5, 0, -0.1, 0, 1.5, -0.1, 0, 0, 1};
	m.set(0, h);
	json saved = m.to_json();
	CHECK(saved["normalized"] == true);
	Mapper loaded(1000, 500);
	loaded.from_json(saved);
	CHECK(loaded.calibrated(0));
	Vec2 a = m.to_px(0, {0.4, 0.4}), b = loaded.to_px(0, {0.4, 0.4});
	CHECK(std::fabs(a.x - b.x) < 1e-9 && std::fabs(a.y - b.y) < 1e-9);
}

TEST(old_pixel_calibration_files_are_ignored_instead_of_misused) {
	json old = {{"screen", {1000, 500}}, {"cams", {{"0", {2000, 0, 0, 0, 1000, 0, 0, 0, 1}}}}};  // no "normalized" key
	Mapper m(1000, 500);
	m.from_json(old);
	CHECK(!m.calibrated(0));
}

TEST(calibration_made_at_one_display_size_still_fits_after_a_resize) {
	HubFixture f("edge");
	f.hub->calibrate_start();
	const double xs[4] = {0.2, 0.8, 0.8, 0.2}, ys[4] = {0.2, 0.2, 0.8, 0.8};
	for (int i = 0; i < 4; ++i) calib_press(f, xs[i], ys[i]);
	CHECK(f.hub->status()["cameras"]["0"]["calibrated"] == true);
	f.actions.clear();
	f.hub->on_hands(hands_msg(0, {pose_hand(false, 0.5, 0.5, .95f)}, motion_of({m_hand(1, 0.5, 0.5)})));
	const json *before = f.first("move");
	CHECK(before && std::abs((*before)["x"].get<int>() - 500) <= 12);   // middle of a 1000 px wide screen
	f.hub->set_display_size(2000, 1000);                                  // the page was reloaded at another size
	f.actions.clear();
	f.hub->on_hands(hands_msg(0, {pose_hand(false, 0.5, 0.5, .95f)}, motion_of({m_hand(1, 0.5, 0.5)})));
	const json *after = f.first("move");
	CHECK(after && std::abs((*after)["x"].get<int>() - 1000) <= 25);    // the same camera spot is still the middle, now of a 2000 px wide screen
}
// ---- calibration: only a held, still pinch counts (stray pinches used to advance the markers) ----------------------

TEST(calib_hold_ignores_a_quick_pinch) {
	GestureConfig c;  // 500 ms hold
	CalibHold ch(c);
	std::map<int, Vec2> tips{{0, {0.2, 0.2}}}, s;
	ch.on_down(0, tips);
	CHECK(!ch.on_frame(100'000, tips, s));
	ch.on_up();                                  // released after 0.1 s: a flicker, not a press
	CHECK(!ch.on_frame(600'000, tips, s));
}

TEST(calib_hold_ignores_a_pinch_while_the_hand_moves) {
	GestureConfig c;
	CalibHold ch(c);
	std::map<int, Vec2> s;
	ch.on_down(0, {{0, {0.2, 0.2}}});
	for (int i = 1; i <= 10; ++i)                // the hand sweeps across the view while pinched
		CHECK(!ch.on_frame(i * 60'000, {{0, {0.2 + 0.03 * i, 0.2}}}, s));
}

TEST(calib_hold_accepts_a_held_still_pinch_and_averages_the_tip) {
	GestureConfig c;
	CalibHold ch(c);
	std::map<int, Vec2> s;
	ch.on_down(0, {{0, {0.20, 0.30}}});
	bool ok = false;
	for (int i = 0; i <= 20 && !ok; ++i)         // small jitter around (0.21, 0.30)
		ok = ch.on_frame(i * 33'000, {{0, {i % 2 ? 0.22 : 0.20, 0.30}}}, s);
	CHECK(ok);
	CHECK(s.count(0) && std::fabs(s[0].x - 0.21) < 0.01 && std::fabs(s[0].y - 0.30) < 1e-9);
}

TEST(calib_hold_needs_a_release_and_a_pause_between_markers) {
	GestureConfig c;  // 500 ms hold, 800 ms gap
	CalibHold ch(c);
	std::map<int, Vec2> tips{{0, {0.5, 0.5}}}, s;
	ch.on_down(0, tips);
	CHECK(ch.on_frame(500'000, tips, s));        // first marker accepted
	ch.on_down(600'000, tips);                   // still pinched, no release: ignored
	CHECK(!ch.on_frame(1'200'000, tips, s));
	ch.on_up();
	ch.on_down(1'000'000, tips);                 // released, but only 0.5 s after the last marker: ignored
	CHECK(!ch.on_frame(1'600'000, tips, s));
	ch.on_up();
	ch.on_down(1'400'000, tips);                 // released and 0.9 s later: counts
	CHECK(ch.on_frame(1'900'000, tips, s));
}

TEST(hub_calibration_ignores_stray_pinches) {
	HubFixture f("edge");
	f.hub->calibrate_start();
	Hand h = pose_hand(false, 0.5, 0.5, .95f);
	for (int i = 0; i < 6; ++i) {                // six instant pinch/release flickers
		f.hub->on_hands(hands_msg(0, {h}, motion_of({m_hand(1, 0.5, 0.5)}, {m_event("PINCH_START")})));
		f.hub->on_hands(hands_msg(0, {h}, motion_of({m_hand(1, 0.5, 0.5)}, {m_event("PINCH_END")})));
	}
	int steps = 0;
	for (auto &j : f.all) if (j.value("t", "") == "calib" && j.contains("step")) steps = std::max(steps, j["step"].get<int>());
	CHECK(steps == 0);                           // still on the first marker
	CHECK(!calib_done(f));
	CHECK(f.count("down") == 0 && f.count("up") == 0);  // and none of them became clicks
}

int main() {
  std::setvbuf(stdout, nullptr, _IONBF, 0);
  for (auto& t : tests()) {
    int before = g_fail;
    t.f();
    std::printf("%s %s\n", g_fail == before ? "ok  " : "FAIL", t.n);
  }
  std::printf("%d checks, %d failed\n", g_run, g_fail);
  return g_fail ? 1 : 0;
}
