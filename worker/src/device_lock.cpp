#include "device_lock.hpp"

#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstring>

namespace k2 {

DeviceLock::~DeviceLock() {
    if (fd_ >= 0) ::close(fd_);  // closing the descriptor releases the flock
}

DeviceLock::Result DeviceLock::acquire(const std::string& lock_dir, const std::string& device_id,
                                       std::string& err) {
    std::string path = lock_dir + "/" + device_id + ".lock";
    int fd = ::open(path.c_str(), O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0644);
    if (fd < 0) {
        err = path + ": " + std::strerror(errno);
        return Result::Error;
    }
    if (::flock(fd, LOCK_EX | LOCK_NB) != 0) {
        int e = errno;
        ::close(fd);
        if (e == EWOULDBLOCK) {
            err = path + ": held by another process";
            return Result::Busy;
        }
        err = path + ": " + std::strerror(e);
        return Result::Error;
    }
    // Informational: record the holder's PID. The lock, not this text, is authoritative.
    char buf[32];
    int n = std::snprintf(buf, sizeof buf, "%ld\n", static_cast<long>(::getpid()));
    if (::ftruncate(fd, 0) != 0 || ::pwrite(fd, buf, static_cast<size_t>(n), 0) != n) {
        // Not fatal: the flock is held regardless.
    }
    fd_ = fd;
    return Result::Acquired;
}

}  // namespace k2
