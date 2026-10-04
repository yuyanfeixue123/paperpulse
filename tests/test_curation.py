"""邮件可送达性裁剪 + 站内今日推荐。"""

from __future__ import annotations

from app.core.config import reload_settings
from app.pipeline.curation import (
    is_emailable,
    matched_pattern,
    split_for_email,
)


def _item(title: str, reason: str = "") -> dict:
    return {"id": 1, "title": title, "reason": reason, "llm_score": 4}


def test_no_filter_keeps_everything():
    reload_settings({"email": {"content_filter_patterns": []}})
    emailable, withheld = split_for_email([_item("A"), _item("B")])
    assert len(emailable) == 2 and withheld == []


def test_filter_splits_by_pattern():
    reload_settings({"email": {"content_filter_patterns": [r"transgender", r"性别"]}})
    items = [
        _item("Urban morphology and housing price"),
        _item("Eating disorders in transgender youth"),
        _item("性别认同与城市空间"),
    ]
    emailable, withheld = split_for_email(items)
    assert [i["title"] for i in emailable] == ["Urban morphology and housing price"]
    assert len(withheld) == 2
    assert matched_pattern(items[1]) == "transgender"


def test_filter_is_case_insensitive():
    reload_settings({"email": {"content_filter_patterns": [r"transgender"]}})
    assert is_emailable(_item("Transgender health outcomes")) is False
    assert is_emailable(_item("TRANSGENDER study")) is False


def test_filter_matches_reason_field():
    reload_settings({"email": {"content_filter_patterns": [r"LGBT"]}})
    assert is_emailable(_item("A neutral title", "讨论 LGBT 群体权益")) is False


def test_filter_ignores_abstract():
    """只匹配标题与推荐理由，摘要里的偶然措辞不该让整篇被判定不可送达。"""
    reload_settings({"email": {"content_filter_patterns": [r"gender"]}})
    item = _item("A perfectly neutral title")
    item["abstract"] = "The study discusses gender differences in mice."
    assert is_emailable(item) is True


def test_broken_pattern_does_not_break_delivery():
    reload_settings({"email": {"content_filter_patterns": [r"[unclosed", r"ok"]}})
    emailable, withheld = split_for_email([_item("fine paper")])
    assert len(emailable) == 1 and withheld == []


def test_empty_fields_are_safe():
    reload_settings({"email": {"content_filter_patterns": [r"x"]}})
    assert is_emailable({"title": "", "reason": ""}) is True


def test_digest_email_contains_summary_doi_and_withheld_notice():
    from app.pipeline.render import render_digest

    reload_settings({"email": {"content_filter_patterns": [r"transgender"]}})
    items = [
        {"id": 1, "title": "Urban morphology and housing price",
         "abstract": "We analyse 12 cities. " * 20, "authors": ["A Li", "B Wang"],
         "venue": "Cities", "published_at": "2026-10-03T00:00:00+00:00",
         "url": "https://example.com/1", "doi": "10.1234/ok",
         "llm_score": 5, "reason": "高度相关"},
        {"id": 2, "title": "Health outcomes among transgender adults",
         "abstract": "A large cohort study. " * 20, "authors": ["C Duan"],
         "venue": "Lancet", "published_at": "2026-10-03T00:00:00+00:00",
         "url": "https://example.com/2", "doi": "10.1234/filtered",
         "llm_score": 5, "reason": "跨性别相关"},
    ]
    _, html, text = render_digest(
        site_url="https://pp.example.com", site_name="PaperPulse",
        interest_name="测试订阅", digest_date="2026-10-04", items=items,
        user_id=1, interest_id=1, salutation="小明", lookback_days=3,
    )

    # 被过滤的论文不进邮件正文
    assert "transgender" not in html
    assert "10.1234/filtered" not in html
    # 保留的论文有标题、DOI、简述
    assert "Urban morphology" in html
    assert "10.1234/ok" in html
    assert "We analyse 12 cities." in html
    assert "10.1234/ok" in text
    # 明确告知有 N 篇被拦下，并给出登录入口
    assert "另有 1 篇未通过本邮件通道送达" in html
    assert "登录查看完整推荐" in html
    assert "https://pp.example.com/feed" in html
    assert "另有 1 篇未通过本邮件通道送达" in text
    # 称呼与问候
    assert "小明，这是你的每日论文" in html
    assert "早上好" in html
    reload_settings()


def test_feed_route_requires_login(db):
    from fastapi.testclient import TestClient

    from app.main import app

    _complete_setup_client()
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/feed", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


def test_feed_shows_ranked_items(db):
    from fastapi.testclient import TestClient

    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.main import app
    from app.models.interest import Interest
    from app.models.user import User
    from tests.test_app import _complete_setup, _extract_csrf

    with db() as s:
        u = User(email="feed@example.com", username="feeder",
                 password_hash=hash_password("Passw0rd!x"),
                 display_name="小明", created_at=utc_iso())
        s.add(u)
        s.flush()
        s.add(Interest(user_id=u.id, name="测试订阅", description="d",
                       include_keywords_json="[]", exclude_keywords_json="[]",
                       source_keys_json="[]", arxiv_categories_json="[]",
                       queries_json="{}", created_at=utc_iso()))
        s.commit()

    _complete_setup()
    reload_settings({"email": {"content_filter_patterns": []}})
    c = TestClient(app, raise_server_exceptions=False)
    c.post("/login", data={"email": "feeder", "password": "Passw0rd!x",
                           "csrf": _extract_csrf(c.get("/login").text)})
    r = c.get("/feed")
    assert r.status_code == 200
    assert "今日推荐" in r.text
    assert "按相关度从高到低排序" in r.text
    reload_settings()


def _complete_setup_client() -> None:
    from tests.test_app import _complete_setup

    _complete_setup()


def test_all_items_filtered_does_not_contradict_itself():
    """回归：全部命中时兜底把完整列表发出去，此时若仍报「其余 N 篇未送达」，
    会出现「上方列出 2 篇 + 其余 2 篇 = 总共 2 篇」的自相矛盾。"""
    import re

    from app.pipeline.render import render_digest

    reload_settings({"email": {"content_filter_patterns": [r".*"]}})  # 全命中
    items = [
        {"id": i, "title": f"T{i}", "abstract": "abs " * 60, "authors": ["A"],
         "venue": "V", "published_at": "2026-10-03T00:00:00+00:00",
         "url": "https://e.com", "doi": f"10.1/{i}", "llm_score": 5, "reason": "r"}
        for i in (1, 2)
    ]
    _, html, text = render_digest(
        site_url="https://pp.example.com", site_name="PP", interest_name="I",
        digest_date="2026-10-04", items=items, user_id=1, interest_id=1,
    )
    # 完整列表在正文里
    assert "T1" in html and "T2" in html
    # 不应出现自相矛盾的提示
    assert "未通过本邮件通道送达" not in html
    assert "未通过本邮件通道送达" not in text

    # 正常有裁剪时，数字必须自洽
    reload_settings({"email": {"content_filter_patterns": [r"T1\b"]}})
    _, html2, _ = render_digest(
        site_url="https://pp.example.com", site_name="PP", interest_name="I",
        digest_date="2026-10-04", items=items, user_id=1, interest_id=1,
    )
    m = re.search(r"本期共推荐 (\d+) 篇，上方列出 (\d+) 篇。其余 (\d+) 篇", html2, re.S)
    assert m, "应显示裁剪提示"
    total, shown, held = (int(x) for x in m.groups())
    assert total == shown + held, f"{total} != {shown} + {held}"
    assert total == 2 and shown == 1 and held == 1
    reload_settings()
