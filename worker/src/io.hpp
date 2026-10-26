// Non-blocking socket helpers. Data and control are separate sockets so a slow
// sample consumer can never delay command handling.
#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

namespace k2 {

bool set_nonblocking(int fd);

// Outgoing byte queue for one non-blocking fd.
class OutBuf {
public:
    void append(const void* p, size_t n);
    bool empty() const { return off_ == buf_.size(); }
    size_t pending() const { return buf_.size() - off_; }
    // Writes as much as the socket accepts. Returns false on a fatal error
    // (e.g. EPIPE: the peer is gone).
    bool flush(int fd);

private:
    std::vector<uint8_t> buf_;
    size_t off_ = 0;
};

// Incoming byte accumulator for one non-blocking fd.
class InBuf {
public:
    enum class ReadResult { Data, WouldBlock, Eof, Error };
    ReadResult fill(int fd);
    const uint8_t* data() const { return buf_.data() + off_; }
    size_t size() const { return buf_.size() - off_; }
    void consume(size_t n);

private:
    std::vector<uint8_t> buf_;
    size_t off_ = 0;
};

}  // namespace k2
