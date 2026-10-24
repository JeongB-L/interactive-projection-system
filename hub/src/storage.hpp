// storage.hpp
// ---------------------------------------------------------------------------
// WHAT THIS FILE DECLARES
//   How the hub keeps data. The rule: big bytes live in FILES, the database only INDEXES them.
//     - IBlobStore / LocalFsBlobStore : writes files (camera video, recordings). Local disk today;
//                                       another store (e.g. a NAS or cloud bucket) could be added later
//                                       by implementing the same interface.
//     - Catalog                       : the SQLite database. One row per session and one row per file
//                                       (key + URI + size + metadata), never the file contents.
//     - SessionRecorder               : used while a Pi is connected; writes events and video frames
//                                       into the blob store and registers them in the Catalog.
// ---------------------------------------------------------------------------
#pragma once
#include <filesystem>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "common.hpp"

struct sqlite3; // forward declaration so we don't need <sqlite3.h> in this header

namespace ips {

// ---- blob store ---------------------------------------------------------------

// A handle for writing one file piece by piece.
class BlobWriter {
 public:
  virtual ~BlobWriter() = default;
  virtual void write(const void* data, size_t n) = 0;
  virtual void flush() = 0;
  virtual uint64_t close() = 0;  // finishes the file and returns the total bytes written
};

// Where files live. "key" is the path inside the store, e.g. sessions/<time>_s1/cam0.mjpeg.
class IBlobStore {
 public:
  virtual ~IBlobStore() = default;
  virtual std::string base_uri() const = 0;                    // e.g. file:///C:/.../data/blobs
  virtual std::string uri(const std::string& key) const = 0;   // full location of one file
  virtual std::unique_ptr<BlobWriter> create(const std::string& key) = 0; // start writing a new file
};

// Blob store backed by a folder on this computer.
class LocalFsBlobStore : public IBlobStore {
 public:
  explicit LocalFsBlobStore(std::filesystem::path root);
  std::string base_uri() const override;
  std::string uri(const std::string& key) const override;
  std::unique_ptr<BlobWriter> create(const std::string& key) override;

 private:
  std::filesystem::path root_;
};

// Pick a store from config's blob_uri. "file:./data/blobs" -> local folder.
// Any other scheme throws, because only local storage is implemented (by design: no cloud costs).
std::unique_ptr<IBlobStore> make_blob_store(const std::string& blob_uri);

// ---- catalog (SQLite index) ----------------------------------------------------

class Catalog {
 public:
  explicit Catalog(const std::string& db_path); // opens/creates the database and its tables
  ~Catalog();
  Catalog(const Catalog&) = delete;             // owns a database handle: not copyable
  Catalog& operator=(const Catalog&) = delete;

  // A session = one connection of a Pi. begin returns its id; end stamps the finish time.
  int64_t begin_session(const std::string& device, const std::string& source, const json& meta);
  void end_session(int64_t id);
  // Register a stored file. "kind" says what it is, e.g. events_ndjson or video_mjpeg.
  int64_t add_asset(int64_t session, const std::string& kind, const std::string& key,
                    const std::string& uri, uint64_t bytes, const json& meta);
  void set_asset_bytes(int64_t asset_id, uint64_t bytes); // update size once a file is finished
  json sessions(int limit = 50);                           // newest first
  json assets(const std::string& kind = "", int64_t session = 0, int limit = 200); // optional filters

 private:
  void exec(const char* sql); // run a statement, throw on error
  sqlite3* db_ = nullptr;
  std::mutex m_;              // many connection threads share one database handle
};

// ---- session recorder -------------------------------------------------------------

// One recording per Pi connection: events as one NDJSON file, camera frames as one MJPEG file per camera.
class SessionRecorder {
 public:
  SessionRecorder(Catalog& cat, IBlobStore& blobs, const std::string& device, const std::string& source,
                  bool landmarks, bool frames);
  ~SessionRecorder();
  int64_t session_id() const { return sid_; }
  void event(const json& ev);                          // append one JSON line (hand data or action)
  void frame(int cam, const uint8_t* jpeg, size_t n);  // append one camera JPEG frame
  void close();                                        // finish files and close the session row

 private:
  // A file being written plus the database row describing it.
  struct Stream {
    std::unique_ptr<BlobWriter> w;
    int64_t asset = 0;
  };
  Catalog& cat_;
  IBlobStore& blobs_;
  bool landmarks_, frames_, closed_ = false; // what to record, and whether we are finished
  int64_t sid_ = 0;
  Stream events_;
  std::map<int, Stream> cams_;               // one video file per camera, created on first frame
  std::string stamp_;                        // timestamp used in file names
  uint64_t lines_ = 0;
  std::mutex m_;
};

// Current UTC time as "2026-10-24T18:40:51Z".
std::string utc_now_iso();

}  // namespace ips
