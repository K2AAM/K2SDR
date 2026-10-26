#include "io.hpp"

#include <fcntl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>

namespace k2 {

bool set_nonblocking(int fd) {
    int fl = ::fcntl(fd, F_GETFL);
    return fl >= 0 && ::fcntl(fd, F_SETFL, fl | O_NONBLOCK) == 0;
}

void OutBuf::append(const void* p, size_t n) {
    if (off_ == buf_.size()) {
        buf_.clear();
        off_ = 0;
    }
    const auto* b = static_cast<const uint8_t*>(p);
    buf_.insert(buf_.end(), b, b + n);
}

bool OutBuf::flush(int fd) {
    while (off_ < buf_.size()) {
        ssize_t w = ::send(fd, buf_.data() + off_, buf_.size() - off_, MSG_NOSIGNAL);
        if (w > 0) {
            off_ += static_cast<size_t>(w);
            continue;
        }
        if (w < 0 && errno == EINTR) continue;
        if (w < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return true;
        return false;
    }
    buf_.clear();
    off_ = 0;
    return true;
}

InBuf::ReadResult InBuf::fill(int fd) {
    uint8_t tmp[4096];
    bool got = false;
    for (;;) {
        ssize_t r = ::recv(fd, tmp, sizeof tmp, 0);
        if (r > 0) {
            if (off_ > 0 && off_ == buf_.size()) {
                buf_.clear();
                off_ = 0;
            }
            buf_.insert(buf_.end(), tmp, tmp + r);
            got = true;
            continue;
        }
        if (r == 0) return got ? ReadResult::Data : ReadResult::Eof;
        if (errno == EINTR) continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) return got ? ReadResult::Data : ReadResult::WouldBlock;
        return ReadResult::Error;
    }
}

void InBuf::consume(size_t n) {
    off_ += n;
    if (off_ == buf_.size()) {
        buf_.clear();
        off_ = 0;
    }
}

}  // namespace k2
