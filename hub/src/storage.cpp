#include "storage.hpp"

#include <sqlite3.h>

#include <ctime>
#include <map>
#include <stdexcept>

namespace ips {
namespace fs = std::filesystem;

std::string utc_now_iso() {
  std::time_t t = std::time(nullptr);
  std::tm tm{};
#ifdef _WIN32
  gmtime_s(&tm, &t);
#else
  gmtime_r(&t, &tm);
#endif
  char buf[32];
  std::strftime(buf, sizeof buf, "%Y-%m-%dT%H:%M:%SZ", &tm);
  return buf;
}

// ---- blob store ------------------------------------------------------------
namespace {
class LocalWriter : public BlobWriter {
 public:
  explicit LocalWriter(const fs::path& p) : out_(p, std::ios::binary) {
    if (!out_) throw std::runtime_error("cannot open blob " + p.string());
  }
  void write(const void* d, size_t n) override {
    out_.write(static_cast<const char*>(d), std::streamsize(n));
    bytes_ += n;
  }
  void flush() override { out_.flush(); }
  uint64_t close() override {
    if (out_.is_open()) out_.close();
    return bytes_;
  }

 private:
  std::ofstream out_;
  uint64_t bytes_ = 0;
};
}  // namespace

LocalFsBlobStore::LocalFsBlobStore(fs::path root) : root_(fs::absolute(std::move(root))) {
  fs::create_directories(root_);
}

std::string LocalFsBlobStore::base_uri() const {
  return "file:///" + root_.generic_string();
}

std::string LocalFsBlobStore::uri(const std::string& key) const {
  return base_uri() + "/" + key;
}

std::unique_ptr<BlobWriter> LocalFsBlobStore::create(const std::string& key) {
  fs::path p = root_ / key;
  fs::create_directories(p.parent_path());
  return std::make_unique<LocalWriter>(p);
}

std::unique_ptr<IBlobStore> make_blob_store(const std::string& blob_uri) {
  if (blob_uri.rfind("file:", 0) == 0) {
    std::string p = blob_uri.substr(5);
    while (p.size() > 1 && p[0] == '/' && p[1] == '/') p.erase(0, 1);  // file://x -> /x
    return std::make_unique<LocalFsBlobStore>(p);
  }
  throw std::runtime_error("unsupported blob_uri scheme: " + blob_uri +
                           " (only file: is implemented; cloud stores plug in via IBlobStore)");
}

// ---- catalog ---------------------------------------------------------------
Catalog::Catalog(const std::string& path) {
  auto parent = fs::path(path).parent_path();
  if (!parent.empty()) fs::create_directories(parent);
  if (sqlite3_open(path.c_str(), &db_) != SQLITE_OK)
    throw std::runtime_error(std::string("sqlite open failed: ") + sqlite3_errmsg(db_));
  exec("PRAGMA journal_mode=WAL;");
  exec("CREATE TABLE IF NOT EXISTS sessions("
       "id INTEGER PRIMARY KEY, started_utc TEXT, ended_utc TEXT, device TEXT, source TEXT, meta TEXT);");
  exec("CREATE TABLE IF NOT EXISTS assets("
       "id INTEGER PRIMARY KEY, session_id INTEGER, kind TEXT, key TEXT UNIQUE, uri TEXT, "
       "bytes INTEGER, meta TEXT, created_utc TEXT);");
  exec("CREATE INDEX IF NOT EXISTS assets_kind ON assets(kind);");
}

Catalog::~Catalog() {
  if (db_) sqlite3_close(db_);
}

void Catalog::exec(const char* sql) {
  char* err = nullptr;
  if (sqlite3_exec(db_, sql, nullptr, nullptr, &err) != SQLITE_OK) {
    std::string e = err ? err : "?";
    sqlite3_free(err);
    throw std::runtime_error("sqlite: " + e);
  }
}

namespace {
struct Stmt {
  sqlite3_stmt* s = nullptr;
  Stmt(sqlite3* db, const char* sql) {
    if (sqlite3_prepare_v2(db, sql, -1, &s, nullptr) != SQLITE_OK)
      throw std::runtime_error(std::string("sqlite prepare: ") + sqlite3_errmsg(db));
  }
  ~Stmt() { sqlite3_finalize(s); }
  void text(int i, const std::string& v) { sqlite3_bind_text(s, i, v.c_str(), -1, SQLITE_TRANSIENT); }
};
}  // namespace

int64_t Catalog::begin_session(const std::string& device, const std::string& source, const json& meta) {
  std::lock_guard lk(m_);
  Stmt st(db_, "INSERT INTO sessions(started_utc,device,source,meta) VALUES(?,?,?,?)");
  st.text(1, utc_now_iso()); st.text(2, device); st.text(3, source); st.text(4, meta.dump());
  sqlite3_step(st.s);
  return sqlite3_last_insert_rowid(db_);
}

void Catalog::end_session(int64_t id) {
  std::lock_guard lk(m_);
  Stmt st(db_, "UPDATE sessions SET ended_utc=? WHERE id=?");
  st.text(1, utc_now_iso());
  sqlite3_bind_int64(st.s, 2, id);
  sqlite3_step(st.s);
}

int64_t Catalog::add_asset(int64_t session, const std::string& kind, const std::string& key,
                           const std::string& uri, uint64_t bytes, const json& meta) {
  std::lock_guard lk(m_);
  Stmt st(db_, "INSERT OR REPLACE INTO assets(session_id,kind,key,uri,bytes,meta,created_utc) "
               "VALUES(?,?,?,?,?,?,?)");
  sqlite3_bind_int64(st.s, 1, session);
  st.text(2, kind); st.text(3, key); st.text(4, uri);
  sqlite3_bind_int64(st.s, 5, int64_t(bytes));
  st.text(6, meta.dump()); st.text(7, utc_now_iso());
  sqlite3_step(st.s);
  return sqlite3_last_insert_rowid(db_);
}

void Catalog::set_asset_bytes(int64_t id, uint64_t bytes) {
  std::lock_guard lk(m_);
  Stmt st(db_, "UPDATE assets SET bytes=? WHERE id=?");
  sqlite3_bind_int64(st.s, 1, int64_t(bytes));
  sqlite3_bind_int64(st.s, 2, id);
  sqlite3_step(st.s);
}

static std::string col(sqlite3_stmt* s, int i) {
  const unsigned char* t = sqlite3_column_text(s, i);
  return t ? reinterpret_cast<const char*>(t) : "";
}

json Catalog::sessions(int limit) {
  std::lock_guard lk(m_);
  Stmt st(db_, "SELECT id,started_utc,ended_utc,device,source,meta FROM sessions ORDER BY id DESC LIMIT ?");
  sqlite3_bind_int(st.s, 1, limit);
  json out = json::array();
  while (sqlite3_step(st.s) == SQLITE_ROW)
    out.push_back({{"id", sqlite3_column_int64(st.s, 0)}, {"started_utc", col(st.s, 1)},
                   {"ended_utc", col(st.s, 2)}, {"device", col(st.s, 3)}, {"source", col(st.s, 4)},
                   {"meta", json::parse(col(st.s, 5), nullptr, false)}});
  return out;
}

json Catalog::assets(const std::string& kind, int64_t session, int limit) {
  std::lock_guard lk(m_);
  std::string sql = "SELECT id,session_id,kind,key,uri,bytes,meta,created_utc FROM assets WHERE 1=1";
  if (!kind.empty()) sql += " AND kind=?";
  if (session) sql += " AND session_id=?";
  sql += " ORDER BY id DESC LIMIT ?";
  Stmt st(db_, sql.c_str());
  int i = 1;
  if (!kind.empty()) st.text(i++, kind);
  if (session) sqlite3_bind_int64(st.s, i++, session);
  sqlite3_bind_int(st.s, i, limit);
  json out = json::array();
  while (sqlite3_step(st.s) == SQLITE_ROW)
    out.push_back({{"id", sqlite3_column_int64(st.s, 0)}, {"session_id", sqlite3_column_int64(st.s, 1)},
                   {"kind", col(st.s, 2)}, {"key", col(st.s, 3)}, {"uri", col(st.s, 4)},
                   {"bytes", sqlite3_column_int64(st.s, 5)},
                   {"meta", json::parse(col(st.s, 6), nullptr, false)}, {"created_utc", col(st.s, 7)}});
  return out;
}

// ---- recorder --------------------------------------------------------------
SessionRecorder::SessionRecorder(Catalog& cat, IBlobStore& blobs, const std::string& device,
                                 const std::string& source, bool landmarks, bool frames)
    : cat_(cat), blobs_(blobs), landmarks_(landmarks), frames_(frames) {
  sid_ = cat_.begin_session(device, source, {{"record_landmarks", landmarks}, {"record_frames", frames}});
  stamp_ = utc_now_iso();
  for (auto& c : stamp_) if (c == ':') c = '-';
  if (landmarks_) {
    std::string key = "sessions/" + stamp_ + "_s" + std::to_string(sid_) + "/events.ndjson";
    events_.w = blobs_.create(key);
    events_.asset = cat_.add_asset(sid_, "events_ndjson", key, blobs_.uri(key), 0, {{"format", "ndjson"}});
  }
}

SessionRecorder::~SessionRecorder() { close(); }

void SessionRecorder::event(const json& ev) {
  std::lock_guard lk(m_);
  if (!events_.w || closed_) return;
  std::string line = ev.dump() + "\n";
  events_.w->write(line.data(), line.size());
  if (++lines_ % 50 == 0) events_.w->flush();
}

void SessionRecorder::frame(int cam, const uint8_t* jpeg, size_t n) {
  std::lock_guard lk(m_);
  if (!frames_ || closed_) return;
  auto it = cams_.find(cam);
  if (it == cams_.end()) {
    std::string key = "sessions/" + stamp_ + "_s" + std::to_string(sid_) + "/cam" + std::to_string(cam) + ".mjpeg";
    Stream s;
    s.w = blobs_.create(key);
    s.asset = cat_.add_asset(sid_, "video_mjpeg", key, blobs_.uri(key), 0,
                             {{"camera", cam}, {"format", "mjpeg (concatenated JPEG)"}});
    it = cams_.emplace(cam, std::move(s)).first;
  }
  it->second.w->write(jpeg, n);  // concatenated JPEGs are a valid MJPEG stream (ffmpeg -f mjpeg)
}

void SessionRecorder::close() {
  std::lock_guard lk(m_);
  if (closed_) return;
  closed_ = true;
  if (events_.w) cat_.set_asset_bytes(events_.asset, events_.w->close());
  for (auto& [cam, s] : cams_) cat_.set_asset_bytes(s.asset, s.w->close());
  cat_.end_session(sid_);
}

}  // namespace ips
