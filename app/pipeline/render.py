"""邮件渲染：multipart/alternative，HTML 与纯文本两份。"""

from __future__ import annotations

from app.core.security import make_token
from app.web.templates import render_string


def one_click_unsubscribe_url(site_url: str, user_id: int, interest_id: int) -> str:
    token = make_token(uid=user_id, interest_id=interest_id, act="unsub")
    return f"{site_url.rstrip('/')}/u/{token}"


def feedback_url(site_url: str, user_id: int, paper_id: int, interest_id: int, rating: int) -> str:
    token = make_token(
        uid=user_id, paper_id=paper_id, interest_id=interest_id, act="rate", rating=rating
    )
    return f"{site_url.rstrip('/')}/f/{token}"


def render_digest(
    *,
    site_url: str,
    site_name: str,
    interest_name: str,
    digest_date: str,
    items: list[dict],
    user_id: int,
    interest_id: int,
    keyword_mode: bool = False,
    salutation: str = "",
    lookback_days: int = 0,
) -> tuple[str, str, str]:
    """返回 (subject, html, text)。"""
    links = []
    for it in items:
        links.append(
            {
                **it,
                "stars": "★" * int(it.get("llm_score", 0)) + "☆" * (5 - int(it.get("llm_score", 0))),
                "rate_url": {
                    r: feedback_url(site_url, user_id, int(it["id"]), interest_id, r)
                    for r in (1, 5)
                },
                "boring_url": feedback_url(site_url, user_id, int(it["id"]), interest_id, 1),
                "doi_url": f"https://doi.org/{it['doi']}" if it.get("doi") else it.get("url", ""),
            }
        )

    ctx = {
        "site_name": site_name,
        "site_url": site_url,
        "interest_name": interest_name,
        "digest_date": digest_date,
        "salutation": salutation or "你好",
        "lookback_days": lookback_days,
        "items": links,
        "unsub_url": one_click_unsubscribe_url(site_url, user_id, interest_id),
        "keyword_mode": keyword_mode,
    }
    subject = f"[{site_name}] {interest_name} · {digest_date} · {len(items)} 篇"
    html = render_string("email/digest.html", **ctx)
    text = render_string("email/digest.txt", **ctx)
    return subject, html, text


def render_welcome(
    *,
    site_url: str,
    site_name: str,
    interest_name: str,
    description: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
    send_at: str,
    timezone: str,
    lookback_days: int,
    max_papers_per_day: int,
    min_score: int,
    first_digest_at: str,
    user_id: int,
    interest_id: int,
) -> tuple[str, str, str]:
    """订阅创建成功后的确认邮件。

    同时承担邮箱验证职责：正文里的确认链接会把 users.email_verified 置 1。
    """
    verify_token = make_token(uid=user_id, act="verify-email", iid=interest_id)
    ctx = {
        "site_name": site_name,
        "site_url": site_url,
        "interest_name": interest_name,
        "description": description,
        "include_keywords": include_keywords,
        "exclude_keywords": exclude_keywords,
        "send_at": send_at,
        "timezone": timezone,
        "lookback_days": lookback_days,
        "max_papers_per_day": max_papers_per_day,
        "min_score": min_score,
        "first_digest_at": first_digest_at,
        "verify_url": f"{site_url.rstrip('/')}/verify-email?token={verify_token}",
        "manage_url": f"{site_url.rstrip('/')}/interests/{interest_id}",
        "all_interests_url": f"{site_url.rstrip('/')}/interests",
    }
    subject = f"[{site_name}] 订阅已创建：{interest_name}"
    html = render_string("email/welcome.html", **ctx)
    text = render_string("email/welcome.txt", **ctx)
    return subject, html, text


def render_verification(site_url: str, site_name: str, token: str) -> tuple[str, str, str]:
    url = f"{site_url.rstrip('/')}/verify?token={token}"
    subject = f"[{site_name}] 请验证你的邮箱"
    html = render_string("email/verify.html", site_name=site_name, url=url)
    text = f"请点击以下链接验证邮箱：\n{url}\n"
    return subject, html, text


def render_test(site_name: str) -> tuple[str, str, str]:
    subject = f"[{site_name}] 测试邮件"
    html = render_string("email/test.html", site_name=site_name)
    text = f"这是来自 {site_name} 的测试邮件。收到即表示邮件通道配置正确。\n"
    return subject, html, text
