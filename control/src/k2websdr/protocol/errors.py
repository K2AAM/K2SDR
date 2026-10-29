"""Wire error codes for control protocol v1."""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    E_SCHEMA = "E_SCHEMA"                      # malformed message, wrong types, unknown fields
    E_PAYLOAD_TOO_LARGE = "E_PAYLOAD_TOO_LARGE"
    E_UNSUPPORTED_VERSION = "E_UNSUPPORTED_VERSION"
    E_UNAUTHENTICATED = "E_UNAUTHENTICATED"
    E_FORBIDDEN = "E_FORBIDDEN"                # authenticated, lacks the permission
    E_NOT_LEASE_HOLDER = "E_NOT_LEASE_HOLDER"
    E_UNKNOWN_RECEIVER = "E_UNKNOWN_RECEIVER"
    E_UNKNOWN_CHANNEL = "E_UNKNOWN_CHANNEL"
    E_OUT_OF_RANGE = "E_OUT_OF_RANGE"          # outside device capability or configured policy
    E_UNSUPPORTED = "E_UNSUPPORTED"            # operation/value not supported by this receiver
    E_MODE_UNAVAILABLE = "E_MODE_UNAVAILABLE"  # mode not available per ledger + capabilities
    E_BUSY = "E_BUSY"
    E_DEVICE_EXTERNAL = "E_DEVICE_EXTERNAL"    # device held outside this application (e.g. OWRX)
    E_DEVICE_FAILED = "E_DEVICE_FAILED"
    E_TIMEOUT = "E_TIMEOUT"
    E_STALE_PRECONDITION = "E_STALE_PRECONDITION"
    E_RATE_LIMITED = "E_RATE_LIMITED"
    E_STORAGE_FULL = "E_STORAGE_FULL"          # recording storage cap reached or disk nearly full
    E_UNKNOWN_RECORDING = "E_UNKNOWN_RECORDING"
    E_RECORDING_ACTIVE = "E_RECORDING_ACTIVE"  # a retune would end a recording: retry with end_recordings
    E_INTERNAL = "E_INTERNAL"
