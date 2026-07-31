"""Windows process identity helpers (stdlib ``ctypes`` only, no psutil).

SPEC.md 24章 requires that a saved PID never be trusted alone: the
recorded process creation time must match the OS-reported creation time
before a process is treated as "still running", and a process must never
be terminated based on PID alone. If the creation time cannot be
determined, the process is treated as not running (STALE candidate),
never as running.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes

from timeutil import datetime_to_iso
from datetime import datetime, timezone

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_EPOCH_AS_FILETIME = 116444736000000000  # 1601-01-01 -> 1970-01-01, in 100ns units
_HUNDREDS_OF_NS_PER_SEC = 10_000_000

CREATE_NEW_PROCESS_GROUP = 0x00000200


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def _is_windows() -> bool:
    return sys.platform == "win32"


def _filetime_to_iso(filetime: "_FILETIME") -> str:
    value = (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime
    unix_seconds = (value - _EPOCH_AS_FILETIME) / _HUNDREDS_OF_NS_PER_SEC
    dt = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
    return datetime_to_iso(dt)


def get_process_start_time_iso(pid: int) -> str | None:
    """Return the OS-recorded process creation time, or None if unknown."""
    if not _is_windows():
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        creation = _FILETIME()
        exit_time = _FILETIME()
        kernel_time = _FILETIME()
        user_time = _FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return None
        return _filetime_to_iso(creation)
    finally:
        kernel32.CloseHandle(handle)


def is_same_running_process(pid: int, recorded_start_time_iso: str) -> bool:
    """True only if ``pid`` is alive and its creation time matches exactly.

    A PID whose current start time cannot be determined is never treated
    as running (SPEC.md 24章: "プロセス開始時刻を確認できない場合は安全の
    ため実行中とはみなさず、STALE候補として扱う").
    """
    current = get_process_start_time_iso(pid)
    if current is None:
        return False
    return current == recorded_start_time_iso


def terminate_process_tree(pid: int) -> None:
    """Terminate ``pid`` and any descendants it spawned (Windows-native)."""
    if not _is_windows():
        return
    subprocess.run(
        ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
        capture_output=True,
        check=False,
    )
