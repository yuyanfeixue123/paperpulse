"""一键校验所有数据源连通性。

    python scripts/verify_sources.py [key ...]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import init_db  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.sources.registry import (  # noqa: E402
    build_source,
    has_credential,
    load_all_specs,
    sync_sources_to_db,
)


def main() -> int:
    setup_logging()
    init_db()
    sync_sources_to_db()

    only = set(sys.argv[1:])
    specs = load_all_specs()
    if only:
        specs = [s for s in specs if s["key"] in only]

    ok_count = 0
    for spec in specs:
        if spec["requires_key"] and not has_credential(spec["key"]):
            print(f"—  {spec['key']:<24} 需 Key（未配置）")
            continue
        src = build_source(spec)
        try:
            ok, msg = src.healthcheck()
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        flag = "✅" if ok else "❌"
        if ok:
            ok_count += 1
        print(f"{flag} {spec['key']:<24} {msg}")

    print(f"\n可连通 {ok_count} / {len(specs)} 个源")
    return 0


if __name__ == "__main__":
    sys.exit(main())
