// Storage: bulk bytes go to a blob store (local disk now, cloud later);
// SQLite only keeps an index of what is in the blob store (key + URI + metadata).
#pragma once
#include <filesystem>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "common.hpp"

struct sqlite3;

namespace ips {

class BlobWriter {
 public:
  virtual ~BlobWriter() = default;
  virtual void write(const void* data, size_t n) = 0;
  virtual void flush() = 0;
  virtual uint64_t close() = 0;  // returns total bytes written
};

class IBlobStore {
 public:
  virtual ~IBlobStore() = default;
  virtual std::string base_uri() const = 0;
  virtual std::string uri(const std::string& key) const = 0;
  virtual std::unique_ptr<BlobWriter> create(const std::string& key) = 0;
};

// "file:./data/blobs" -> local directory. Other schemes (s3:, gs:) plug in here later.
class LocalFsBlobStore : public IBlobStore {
 public:
  explicit LocalFsBlobStore(std::filesystem::path root);
  std::string base_uri() const override;
  std::string uri(const std::string& key) const override;
  std::unique_ptr<BlobWriter> create(const std::string& key) override;

 private:
  std::filesystem::path root_;
};

std::unique_ptr<IBlobStore> make_blob_store(const std::string& blob_uri);  // throws on unknown scheme

class Catalog {
 public:
  explicit Catalog(const std::string& db_path);
  ~Catalog();
  Catalog(const Catalog&) = delete;
  Catalog& operator=(const Catalog&) = delete;

  int64_t begin_session(const std::string& device, const std::string& source, const json& meta);
  void end_session(int64_t id);
  int64_t add_asset(int64_t session, const std::string& kind, const std::string& key,
                    const std::string& uri, uint64_t bytes, const json& meta);
  void set_asset_bytes(int64_t asset_id, uint64_t bytes);
  json sessions(int limit = 50);
  json assets(const std::string& kind = "", int64_t session = 0, int limit = 200);

 private:
  void exec(const char* sql);
  sqlite3* db_ = nullptr;
  std::mutex m_;
};

// One recording per edge connection: events as NDJSON, camera frames as MJPEG (one file per camera).
class SessionRecorder {
 public:
  SessionRecorder(Catalog& cat, IBlobStore& blobs, const std::string& device, const std::string& source,
                  bool landmarks, bool frames);
  ~SessionRecorder();
  int64_t session_id() const { return sid_; }
  void event(const json& ev);
  void frame(int cam, const uint8_t* jpeg, size_t n);
  void close();

 private:
  struct Stream {
    std::unique_ptr<BlobWriter> w;
    int64_t asset = 0;
  };
  Catalog& cat_;
  IBlobStore& blobs_;
  bool landmarks_, frames_, closed_ = false;
  int64_t sid_ = 0;
  Stream events_;
  std::map<int, Stream> cams_;
  std::string stamp_;
  uint64_t lines_ = 0;
  std::mutex m_;
};

std::string utc_now_iso();

}  // namespace ips
