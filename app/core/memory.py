"""内存水位自保。

设计 §1.3：RSS > rss_warn_mb 降并发 + 主动 GC + 记日志；
RSS > rss_hard_mb 暂停采集，只保留推送与清理。
"""

from __future__ import annotations

import gc
import os
import threading

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_state_lock = threading.Lock()
_state = "ok"  # ok | warn | hard


def _rss_linux() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return 0.0


def _rss_windows() -> float:
    """ctypes 走 psapi，无需引入 psutil。"""
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)

    # 必须先声明 restype/argtypes，否则伪句柄 (-1) 会被按 32 位 int 截断
    get_current = ctypes.windll.kernel32.GetCurrentProcess  # type: ignore[attr-defined]
    get_current.restype = wintypes.HANDLE
    get_info = ctypes.windll.psapi.GetProcessMemoryInfo  # type: ignore[attr-defined]
    get_info.restype = wintypes.BOOL
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]

    handle = get_current()
    if not get_info(handle, ctypes.byref(counters), counters.cb):
        return 0.0
    return round(counters.WorkingSetSize / 1024 / 1024, 1)


def rss_mb() -> float:
    """当前进程常驻内存（MB）。取不到时返回 0.0（此时自保逻辑视为不触发）。"""
    if os.name == "nt":
        try:
            return _rss_windows()
        except Exception:  # noqa: BLE001
            return 0.0
    value = _rss_linux()
    if value:
        return value
    try:  # 部分 Unix 没有 /proc，退回 resource
        import resource

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return 0.0


def state() -> str:
    with _state_lock:
        return _state


def evaluate() -> str:
    """重新评估水位并返回 ok / warn / hard。"""
    global _state
    mem = get_settings().memory
    mb = rss_mb()
    previous = state()

    if mb <= 0 or mb < mem.rss_warn_mb:
        new = "ok"
    elif mb < mem.rss_hard_mb:
        new = "warn"
    else:
        new = "hard"

    if new != previous:
        if new == "warn":
            gc.collect()
            log.warning("memory.warn", rss_mb=mb, threshold=mem.rss_warn_mb)
        elif new == "hard":
            gc.collect()
            log.error("memory.hard", rss_mb=mb, threshold=mem.rss_hard_mb)
        else:
            log.info("memory.recovered", rss_mb=mb)
    with _state_lock:
        _state = new
    return new


def fetch_allowed() -> bool:
    """硬限位下停止采集，只保留推送与清理。"""
    return state() != "hard"
