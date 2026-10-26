#include "replay_source.hpp"

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>

namespace k2 {

ReplaySource::~ReplaySource() {
    if (base_ != nullptr) ::munmap(const_cast<uint8_t*>(base_), len_);
}

bool ReplaySource::open(const std::string& path, uint32_t bytes_per_sample, std::string& err) {
    if (bytes_per_sample == 0) {
        err = "unknown sample format";
        return false;
    }
    int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        err = path + ": " + std::strerror(errno);
        return false;
    }
    struct stat st {};
    if (::fstat(fd, &st) != 0 || !S_ISREG(st.st_mode)) {
        err = path + ": not a regular file";
        ::close(fd);
        return false;
    }
    auto size = static_cast<uint64_t>(st.st_size);
    if (size == 0 || size % bytes_per_sample != 0) {
        err = path + ": size is not a positive multiple of the sample size";
        ::close(fd);
        return false;
    }
    void* p = ::mmap(nullptr, static_cast<size_t>(size), PROT_READ, MAP_PRIVATE, fd, 0);
    ::close(fd);
    if (p == MAP_FAILED) {
        err = path + ": mmap: " + std::strerror(errno);
        return false;
    }
    base_ = static_cast<const uint8_t*>(p);
    len_ = static_cast<size_t>(size);
    bps_ = bytes_per_sample;
    samples_ = size / bytes_per_sample;
    return true;
}

}  // namespace k2
