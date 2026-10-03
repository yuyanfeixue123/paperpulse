"""systemd 看门狗心跳与优雅关机。

仅在 NOTIFY_SOCKET 存在时生效（裸机 systemd 部署）；其他环境静默跳过。
"""

from __future__ import annotations

import os
import socket
import threading

from app.core.logging import get_logger

log = get_logger(__name__)

_interval = 30
_stop = threading.Event()
_thread: threading.Thread | None = None


def notify(state: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)  # type: ignore[attr-defined]
        sock.connect(addr)
        sock.sendall(state.encode())
        sock.close()
        return True
    except OSError as exc:
        log.debug("watchdog.notify_failed", error=str(exc))
        return False


def _loop() -> None:
    notify("READY=1")
    while not _stop.wait(_interval):
        notify("WATCHDOG=1")
    notify("STOPPING=1")


def start() -> None:
    global _thread
    if not os.environ.get("NOTIFY_SOCKET"):
        return
    if _thread is not None:
        return
    _thread = threading.Thread(target=_loop, name="pp-watchdog", daemon=True)
    _thread.start()
    log.info("watchdog.started", interval=_interval)


def stop() -> None:
    _stop.set()
