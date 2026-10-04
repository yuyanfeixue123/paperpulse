"""摘要构建与定时派发。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import text as sql

from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.core.utils import now_utc, today_local, utc_iso, zone
from app.models.digest import Digest, DigestItem
from app.models.interest import Interest
from app.models.system import SystemSettings

log = get_logger(__name__)


def compute_next_due(send_at: str, tz: str, now: Any = None) -> str:
    """按用户本地时区算出下一次推送的 UTC 时刻。"""
    now = now or now_utc()
    local = now.astimezone(zone(tz))
    try:
        h, m = (int(x) for x in send_at.split(":"))
    except ValueError:
        h, m = 8, 30
    nxt = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if nxt <= local:
        nxt += timedelta(days=1)
    return nxt.astimezone(zone("UTC")).isoformat(timespec="seconds")


def select_candidates(interest: Interest) -> list[dict]:
    """W9 全流程：召回 -> 精排 -> 排序。LLM 不可用时降级为 BM25 顺序。"""
    from app.pipeline.prefilter import load_papers, recall_candidates
    from app.pipeline.rank import rank_papers

    ids = recall_candidates(interest)
    if not ids:
        return []
    papers = load_papers(ids)
    if not papers:
        return []

    try:
        from app.pipeline.score import score_papers

        scores = score_papers(interest, papers)
    except Exception as exc:  # noqa: BLE001
        log.warning("digest.score_degraded", interest=interest.id, error=str(exc)[:200])
        # 降级：按召回顺序给固定分（BM25 相关性），不启用 AI 精排
        scores = {
            int(p["id"]): {
                "score": int(interest.min_score),
                "reason": "本次未启用 AI 精排（BM25 召回顺序）",
                "model": "keyword",
            }
            for p in papers
        }
    return rank_papers(interest, papers, scores)


def _exclude_already_digested(
    user_id: int, digest_id: int, items: list[dict]
) -> list[dict]:
    """剔除此前摘要里已经出现过的论文。

    范围限定为「同一用户 + 同一订阅」的其他摘要（含更早的、也含更晚的），
    但**不含本次正在重建的这一份**（它的条目已被清空，留着也不影响）。
    按用户而非按订阅去重，是为了让「同���篇论文不要在两个订阅的邮件里
    反复出现」这一诉求也成立。
    """
    from sqlalchemy import text as sql

    paper_ids = [int(it["id"]) for it in items]
    if not paper_ids:
        return items
    placeholders = ",".join(str(i) for i in paper_ids)
    with SessionLocal() as session:
        rows = session.execute(
            sql(
                f"SELECT DISTINCT di.paper_id FROM digest_items di "
                f"JOIN digests g ON g.id = di.digest_id "
                f"WHERE g.user_id = :u AND di.digest_id != :self "
                f"AND di.paper_id IN ({placeholders})"
            ),
            {"u": user_id, "self": digest_id},
        ).all()
    seen = {int(r[0]) for r in rows}
    if not seen:
        return items
    kept = [it for it in items if int(it["id"]) not in seen]
    log.info("digest.dedup_dropped", dropped=len(items) - len(kept))
    return kept


def build_digest(interest_id: int, date: str = "", rebuild: bool = False) -> int | None:
    """构建一份摘要。UNIQUE(interest_id, digest_date) 保证幂等。

    `rebuild=True` 用于网页上的「立刻推荐」：当日摘要已存在时**重算并覆盖**
    它的条目，而不是直接返回旧摘要（否则用户点了没反应）。

    重算时的两条硬约束：
    1. **不再入队邮件**。用户主动点的「立刻推荐」只是想让站内列表刷新，
       不该在深夜给他发一封邮件；邮件仍由每日调度按 `send_at` 统一发出。
    2. **不重复推送已发过的论文**。已 `mark_sent` 的论文会从候选中剔除
       （`rank_papers` 内部按 user_papers 去重），所以反复点「立刻推荐」
       也不会把同一篇论文反复塞进邮件。
    """
    with SessionLocal() as session:
        interest = session.get(Interest, interest_id)
        if interest is None or not interest.is_active:
            return None
        tz = interest.timezone
        user_id = int(interest.user_id)
        max_items = int(interest.max_papers_per_day)
        min_score = int(interest.min_score)
    digest_date = date or today_local(tz)

    with SessionLocal() as session:
        existing = (
            session.query(Digest)
            .filter(Digest.interest_id == interest_id, Digest.digest_date == digest_date)
            .first()
        )
        if existing is not None and not rebuild:
            return int(existing.id)
        if existing is not None:
            # 重算：清空旧条目后原地重填，保持 digest_id 不变，
            # 这样已生成的免登录链接（评/退订）仍然有效。
            digest_id = int(existing.id)
            session.query(DigestItem).filter(
                DigestItem.digest_id == digest_id
            ).delete(synchronize_session=False)
            existing.status = "pending"
            existing.item_count = 0
            session.commit()
        else:
            row = Digest(
                user_id=user_id,
                interest_id=interest_id,
                digest_date=digest_date,
                status="pending",
                item_count=0,
                created_at=utc_iso(),
            )
            session.add(row)
            session.commit()
            digest_id = int(row.id)

    try:
        with SessionLocal() as session:
            interest = session.get(Interest, interest_id)
            items = select_candidates(interest)
    except Exception as exc:  # noqa: BLE001
        log.error("digest.build_failed", interest=interest_id, error=str(exc)[:300])
        with SessionLocal() as session:
            d = session.get(Digest, digest_id)
            d.status = "failed"
            session.commit()
        raise

    items = [it for it in items if int(it.get("llm_score", 0)) >= min_score][:max_items]

    # 硬去重：**已进入过摘要的论文不再重复进邮件**。
    #
    # `rank_papers` 里对已推论文只做 -0.5 降权而非排除，所以高分的旧论文
    # 仍可能在多天后的摘要里再次出现。用户明确要求「每日多次推荐不会
    # 推送邮箱里重复的论文」，故在入摘要这一层做硬过滤。
    # 站内列表不受影响 —— 它读的是 llm_scores 与摘要条目，论文仍在流里。
    items = _exclude_already_digested(user_id, digest_id, items)

    with SessionLocal() as session:
        for pos, it in enumerate(items):
            session.add(
                DigestItem(
                    digest_id=digest_id,
                    paper_id=int(it["id"]),
                    final_score=float(it["final_score"]),
                    llm_score=int(it["llm_score"]),
                    reason=str(it.get("reason", ""))[:200],
                    position=pos,
                )
            )
        d = session.get(Digest, digest_id)
        d.item_count = len(items)
        session.commit()

    from app.pipeline.rank import mark_sent

    mark_sent(user_id, [int(it["id"]) for it in items])

    if items and not rebuild:
        payload = render_digest_payload(digest_id)
        if payload:
            from app.pipeline.deliver import enqueue_digest

            with SessionLocal() as session:
                d = session.get(Digest, digest_id)
                enqueue_digest(d, payload["html"], payload["text"], payload["subject"])

    with SessionLocal() as session:
        interest = session.get(Interest, interest_id)
        interest.next_due_at = compute_next_due(interest.send_at, interest.timezone)
        session.commit()

    log.info(
        "digest.built",
        id=digest_id,
        interest=interest_id,
        items=len(items),
        rebuild=rebuild,
    )
    return digest_id


def render_digest_payload(digest_id: int) -> dict[str, Any] | None:
    """按 digest_items 重新渲染邮件内容。"""
    with SessionLocal() as session:
        d = session.get(Digest, digest_id)
        if d is None:
            return None
        interest = session.get(Interest, d.interest_id)
        rows = session.execute(
            sql(
                "SELECT p.id, p.title, p.abstract, p.authors_json, p.venue, p.published_at, "
                "p.url, p.doi, di.llm_score, di.reason "
                "FROM digest_items di JOIN papers p ON p.id = di.paper_id "
                "WHERE di.digest_id = :d ORDER BY di.position"
            ),
            {"d": digest_id},
        ).all()
        sysrow = session.get(SystemSettings, 1)
        site_url = (sysrow.site_url if sysrow else "") or ""
        site_name = (sysrow.site_name if sysrow else "") or "PaperPulse"
        llm_mode = (sysrow.llm_mode if sysrow else "keyword") or "keyword"
        row = session.execute(
            sql("SELECT email, display_name, username FROM users WHERE id = :u"),
            {"u": d.user_id},
        ).fetchone()
        to_email = row[0] if row else ""
        # 称呼优先级：昵称 -> 用户名 -> 邮箱前缀
        salutation = ""
        if row:
            salutation = (row[1] or row[2] or (row[0] or "").split("@")[0] or "").strip()
        interest_row = session.get(Interest, d.interest_id)
        lookback = int(getattr(interest_row, "lookback_days", 0) or 0)

    items = []
    for r in rows:
        items.append(
            {
                "id": int(r[0]),
                "title": r[1],
                "abstract": r[2],
                "authors": json.loads(r[3] or "[]"),
                "venue": r[4],
                "published_at": r[5],
                "url": r[6],
                "doi": r[7],
                "llm_score": int(r[8]),
                "reason": r[9],
            }
        )

    from app.pipeline.render import render_digest

    subject, html, text = render_digest(
        site_url=site_url,
        site_name=site_name,
        interest_name=interest.name,
        digest_date=d.digest_date,
        items=items,
        user_id=int(d.user_id),
        interest_id=int(d.interest_id),
        keyword_mode=(llm_mode != "llm"),
        salutation=salutation,
        lookback_days=lookback,
    )
    from app.core.security import make_token

    unsub_token = make_token(uid=int(d.user_id), interest_id=int(d.interest_id), act="unsub")
    unsub_url = f"{site_url.rstrip('/')}/u/{unsub_token}"
    headers = {
        # RFC 8058：一键退订必须用可 GET 的 https 地址，且与正文链接同域
        "List-Unsubscribe": f"<{unsub_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        "X-PaperPulse-Digest": str(digest_id),
    }
    return {
        "subject": subject,
        "html": html,
        "text": text,
        "to_email": to_email or "",
        "headers": headers,
    }


def dispatch_due_digests() -> int:
    """每 10 分钟：把到期的 interest 入队 build_digest。"""
    now = utc_iso()
    with SessionLocal() as session:
        rows = (
            session.query(Interest)
            .filter(
                Interest.is_active == 1,
                (Interest.next_due_at.is_(None)) | (Interest.next_due_at <= now),
            )
            .all()
        )
        ids = [int(r.id) for r in rows]
    from app.scheduler.runner import enqueue

    count = 0
    for interest_id in ids:
        if enqueue("build_digest", {"interest_id": interest_id}):
            count += 1
    if count:
        log.info("digest.dispatched", count=count)
    return count
