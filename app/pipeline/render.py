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
    """返回 (subject, html, text)。

    items 为**全部**推荐条目；内部按内容过滤规则裁剪出可进邮件的部分，
    其余转为「未通过邮件通道送达」的提示，引导用户登录站内查看完整列表。
    """
    from app.core.utils import truncate
    from app.pipeline.curation import context_matches, split_for_email

    # 订阅名本身也是邮件正文的一部分。用户自取的订阅名若含通道不便展示的词汇，
    # 无论论文是否过滤都会被整封拒收 —— 这里改用中性名称，并在邮件里说明。
    name_softened = context_matches(interest_name) is not None
    shown_name = "你的订阅" if name_softened else interest_name

    emailable, withheld = split_for_email(items)
    if emailable:
        shown = emailable
        withheld_count = len(withheld)
    else:
        # 全部命中时：宁可完整发出去，也不发一封空信。
        # 此时不能说「其余 N 篇未送达」——它们就在正文里，数量会自相矛盾。
        shown = items
        withheld_count = 0

    links = []
    for it in shown:
        links.append(
            {
                **it,
                "summary": truncate(str(it.get("abstract") or ""), 220),
                "stars": "★" * int(it.get("llm_score", 0)) + "☆" * (5 - int(it.get("llm_score", 0))),
                "rate_url": {
                    r: feedback_url(site_url, user_id, int(it["id"]), interest_id, r)
                    for r in (1, 5)
                },
                "boring_url": feedback_url(site_url, user_id, int(it["id"]), interest_id, 1),
            }
        )

    ctx = {
        "site_name": site_name,
        "site_url": site_url,
        "interest_name": shown_name,
        "interest_name_softened": name_softened,
        "digest_date": digest_date,
        "salutation": salutation or "你好",
        "lookback_days": lookback_days,
        "items": links,
        "withheld_count": withheld_count,
        "total_count": len(items),
        "feed_url": f"{site_url.rstrip('/')}/feed",
        "unsub_url": one_click_unsubscribe_url(site_url, user_id, interest_id),
        "keyword_mode": keyword_mode,
    }
    subject = f"[{site_name}] {shown_name} · {digest_date} · {len(items)} 篇"
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
