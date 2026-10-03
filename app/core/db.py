"""SQLAlchemy engine / SessionLocal / SQLite PRAGMA。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, scoped_session, sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
SessionLocal: scoped_session[Any] = scoped_session(sessionmaker(autoflush=False, expire_on_commit=False))


def resolve_db_path(url: str) -> Path:
    if url.startswith("sqlite:///./"):
        rel = url[len("sqlite:///./") :]
    elif url.startswith("sqlite:///"):
        rel = url[len("sqlite:///") :]
    else:
        rel = url
    p = Path(rel)
    if not p.is_absolute():
        p = ROOT / p
    return p


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine
    settings = get_settings()
    url = settings.db.url
    if url.startswith("sqlite"):
        path = resolve_db_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path.as_posix()}"

    engine = create_engine(url, future=True)

    if url.startswith("sqlite"):
        mmap_size = settings.db.mmap_size_mb * 1024 * 1024

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            if settings.db.wal:
                cur.execute("PRAGMA journal_mode=WAL")
            cur.execute(f"PRAGMA busy_timeout={settings.db.busy_timeout_ms}")
            cur.execute(f"PRAGMA mmap_size={mmap_size}")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    SessionLocal.configure(bind=engine)
    _engine = engine
    return engine


def init_db() -> None:
    """建表（幂等）。正式环境走 Alembic；此处用于测试与 CLI 快速初始化。"""
    import app.models  # noqa: F401  注册全部模型（延迟导入避免循环）

    engine = get_engine()
    Base.metadata.create_all(engine)
    ensure_fts(engine)
    log.info("db.init", url=settings_url())


def settings_url() -> str:
    return get_settings().db.url


def fts_ddl(conn) -> None:
    """在已有连接上建立 FTS5 虚表与三个同步触发器（幂等）。

    拆成独立函数，便于 Alembic 迁移在自己的事务里直接调用。
    """
    exists = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='papers_fts'")
    ).first()
    if exists:
        return
    conn.execute(
        text(
            "CREATE VIRTUAL TABLE papers_fts USING fts5("
            "title, abstract, content='papers', content_rowid='id')"
        )
    )
    conn.execute(
        text(
            "CREATE TRIGGER papers_ai AFTER INSERT ON papers BEGIN "
            "INSERT INTO papers_fts(rowid, title, abstract) "
            "VALUES (new.id, new.title, new.abstract); END"
        )
    )
    conn.execute(
        text(
            "CREATE TRIGGER papers_ad AFTER DELETE ON papers BEGIN "
            "INSERT INTO papers_fts(papers_fts, rowid, title, abstract) "
            "VALUES('delete', old.id, old.title, old.abstract); END"
        )
    )
    conn.execute(
        text(
            "CREATE TRIGGER papers_au AFTER UPDATE ON papers BEGIN "
            "INSERT INTO papers_fts(papers_fts, rowid, title, abstract) "
            "VALUES('delete', old.id, old.title, old.abstract); "
            "INSERT INTO papers_fts(rowid, title, abstract) "
            "VALUES (new.id, new.title, new.abstract); END"
        )
    )


def ensure_fts(engine: Engine) -> None:
    with engine.begin() as conn:
        fts_ddl(conn)


def get_db() -> Iterator[Any]:
    """FastAPI 依赖：每请求一个 session。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def reset_stale_tasks() -> None:
    """进程启动时把 running 的任务改回 pending（自愈）。"""
    from app.models.task import TaskRun

    with SessionLocal() as session:
        updated = session.query(TaskRun).filter(TaskRun.status == "running").update(
            {TaskRun.status: "pending"}, synchronize_session=False
        )
        session.commit()
    if updated:
        log.info("db.reset_stale_tasks", count=updated)
