// Exclusive per-device lock. Held by the worker process for its lifetime, so
// the kernel releases it automatically if the worker dies. Prevents two of OUR
// processes (e.g. a dev instance and an installed service) from opening the
// same physical SDR. Programs that do not take this lock (OpenWebRX+) are not
// covered; coexistence with them is a deliberate, documented handoff.
#pragma once

#include <string>

namespace k2 {

class DeviceLock {
public:
    enum class Result { Acquired, Busy, Error };

    DeviceLock() = default;
    ~DeviceLock();
    DeviceLock(const DeviceLock&) = delete;
    DeviceLock& operator=(const DeviceLock&) = delete;

    // lock_dir must already exist. device_id is pre-validated ([a-z0-9._-]).
    Result acquire(const std::string& lock_dir, const std::string& device_id, std::string& err);

private:
    int fd_ = -1;
};

}  // namespace k2
