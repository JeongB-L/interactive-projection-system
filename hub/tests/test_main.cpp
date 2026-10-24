// Minimal self-contained tests (no framework). Run: ips_tests
#include <cstdio>
#include <filesystem>

#include "gesture.hpp"
#include "storage.hpp"

using namespace ips;
static int g_fail = 0, g_run = 0;
#define CHECK(c) do { ++g_run; if (!(c)) { ++g_fail; std::printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #c); } } while (0)
#define TEST(name) static void name(); static struct R_##name { R_##name() { tests().push_back({#name, name}); } } r_##name; static void name()
struct T { const char* n; void (*f)(); };
static std::vector<T>& tests() { static std::vector<T> v; return v; }

static int count(const std::vector<Gesture>& v, const char* k) {
  int n = 0;
  for (auto& g : v) if (g.kind == k) ++n;
  return n;
}

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

TEST(homography_rejects_degenerate) {
  std::array<Vec2, 4> src{{{0, 0}, {1, 1}, {2, 2}, {3, 3}}};  // collinear
  std::array<Vec2, 4> dst{{{0, 0}, {1, 0}, {1, 1}, {0, 1}}};
  Homography h;
  CHECK(!solve_homography(src, dst, h));
}

TEST(mapper_uncalibrated_scales_to_screen) {
  Mapper m(1000, 500);
  Vec2 p = m.to_px(0, {0.5, 0.5});
  CHECK(std::fabs(p.x - 500) < 1e-9 && std::fabs(p.y - 250) < 1e-9);
}

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

static Hand hand_at(double x, double y, float score) {
  Hand h; h.score = score;
  for (int i = 0; i < 21; ++i) { h.xyz[i * 3] = float(x); h.xyz[i * 3 + 1] = float(y); }
  return h;
}

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
