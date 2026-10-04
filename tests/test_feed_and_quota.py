"""推荐流、分组展示、评价、配额与编辑功能的回归测试。"""

from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient

from tests.test_app import _client, _complete_setup, _extract_csrf

# 测试库是**文件级共享**的（表结构复用，数据累积），而 users.email 与
# users.username 都是唯一列。本模块的辅助函数必须为每次调用生成唯一标识，
# 否则会撞上其他测试文件留下的记录，报出与本模块无关的 IntegrityError。
_seq = itertools.count(1)


def _uniq() -> int:
    return next(_seq)


def _mkuser(db, email: str | None = None, username: str | None = None, admin: bool = False):
    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.models.user import User

    n = _uniq()
    email = email or f"u{n}@example.com"
    username = username or f"u{n}"
    with db() as s:
        s.add(User(email=email, username=username, is_admin=admin,
                   password_hash=hash_password("Passw0rd!x"), created_at=utc_iso()))
        s.commit()
    with db() as s:
        return int(s.query(User).filter(User.email == email).one().id)


def _mkinterest(db, user_id: int, name: str = "订阅A", **kw):
    from app.core.utils import utc_iso
    from app.models.interest import Interest

    with db() as s:
        it = Interest(
            user_id=user_id, name=name, description=kw.get("description", "d"),
            include_keywords_json=kw.get("include", "[]"),
            exclude_keywords_json=kw.get("exclude", "[]"),
            source_keys_json="[]", arxiv_categories_json="[]", queries_json="{}",
            min_score=kw.get("min_score", 0),
            max_papers_per_day=kw.get("max_papers", 10),
            lookback_days=7, send_at="08:30", timezone="Asia/Shanghai",
            is_active=kw.get("is_active", 1),
            created_at=utc_iso(),
        )
        s.add(it)
        s.commit()
    with db() as s:
        return int(s.get(Interest, it.id).id)


def _mkscore(db, user_id: int, interest_id: int, paper_id: int, score: int, title: str = ""):
    """写入一篇论文及其 LLM 评分（推荐流的数据源）。

    走生产代码路径 `upsert_paper` 而不是手工构造 ORM 行 —— 手工构造漏一个
    NOT NULL 字段就会 IntegrityError，而这类失败与被测逻辑无关，纯属噪音。
    """
    from app.core.utils import utc_iso
    from app.models.score import LlmScore
    from app.pipeline.fetch import upsert_paper
    from app.sources.base import PaperItem

    real_id, _ = upsert_paper(
        PaperItem(
            source_key="test",
            source_id=f"s{paper_id}",
            title=title or f"P{paper_id}",
            abstract="abs",
            authors=["A"],
            venue="V",
            url=f"https://e.com/{paper_id}",
            doi=f"10.1/{paper_id}",
            published_at="2026-10-01T00:00:00+00:00",
        )
    )
    with db() as s:
        s.add(LlmScore(paper_id=real_id, interest_id=interest_id,
                       interest_version=1, score=score, reason="r",
                       model="m", created_at=utc_iso()))
        s.commit()
    return real_id


def _mkdigest(db, user_id: int, interest_id: int, paper_ids: list[int], date: str = ""):
    """造一份当天的摘要。/feed 读的是摘要条目，故分组与评价测试都需要它。"""
    from app.core.utils import today_local, utc_iso
    from app.models.digest import Digest, DigestItem

    day = date or today_local("Asia/Shanghai")
    with db() as s:
        d = Digest(user_id=user_id, interest_id=interest_id, digest_date=day,
                   status="pending", item_count=len(paper_ids), created_at=utc_iso())
        s.add(d)
        s.flush()
        for pos, pid in enumerate(paper_ids):
            s.add(DigestItem(digest_id=d.id, paper_id=pid, final_score=5.0,
                             llm_score=5, reason="r", position=pos))
        s.commit()
        return int(d.id)


def _login(email: str, password: str = "Passw0rd!x") -> TestClient:
    c = _client()
    c.post("/login", data={"email": email, "password": password,
                           "csrf": _extract_csrf(c.get("/login").text)})
    return c


def _login_uid(db, uid: int) -> TestClient:
    """按 id 登录，避免依赖具体邮箱字符串。"""
    from app.models.user import User

    with db() as s:
        email = s.get(User, uid).email
    return _login(email)


@pytest.fixture(autouse=True)
def _reset():
    from app.core.config import reload_settings
    from app.web.deps import _buckets

    _buckets.clear()
    yield
    _buckets.clear()
    reload_settings()


# ------------------------------------------------------------ 推荐流

def test_stream_is_mixed_and_sorted_by_llm_score(db):
    """需求：首页混合推荐流，按各订阅内 LLM 打分排序，数量不限。"""
    uid = _mkuser(db)
    a = _mkinterest(db, uid, "城市形态")
    b = _mkinterest(db, uid, "碳中和")
    # 城市形态订阅打 3 分，碳中和打 5 分 —— 混合后 5 分应排前面
    _mkscore(db, uid, a, 101, 3, "Urban morphology study")
    _mkscore(db, uid, b, 102, 5, "Carbon neutrality study")
    _mkscore(db, uid, b, 103, 4, "Renewable energy study")
    _complete_setup()

    c = _login_uid(db, uid)
    r = c.get("/stream")
    assert r.status_code == 200, r.text[:400]
    body = r.text
    # 顺序：5 > 4 > 3
    assert body.index("Carbon neutrality") < body.index("Renewable energy")
    assert body.index("Renewable energy") < body.index("Urban morphology")
    # 两个订阅名都要出现（证明是混合而非单订阅）
    assert "城市形态" in body and "碳中和" in body


def test_stream_marks_papers_hit_by_multiple_interests(db):
    uid = _mkuser(db)
    a = _mkinterest(db, uid, "A")
    b = _mkinterest(db, uid, "B")
    _mkscore(db, uid, a, 201, 4, "Shared topic")
    _mkscore(db, uid, b, 201, 5, "Shared topic")
    _complete_setup()

    r = _login_uid(db, uid).get("/stream")
    assert "2 个订阅同时命中" in r.text


def test_stream_deduplicates_same_paper_across_interests(db):
    """同一篇论文被两个订阅命中时只出现一次，取最高分。"""
    uid = _mkuser(db)
    a = _mkinterest(db, uid, "A")
    b = _mkinterest(db, uid, "B")
    _mkscore(db, uid, a, 301, 3, "Only once please")
    _mkscore(db, uid, b, 301, 5, "Only once please")
    _complete_setup()

    body = _login_uid(db, uid).get("/stream").text
    assert body.count("Only once please") == 1


def test_root_redirects_to_stream(db):
    uid = _mkuser(db)
    _mkinterest(db, uid)
    _complete_setup()
    c = _login_uid(db, uid)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and "/stream" in r.headers["location"]


# ---------------------------------------------------- feed 按订阅分组

def test_feed_groups_by_interest(db):
    """需求：/feed 依次展示每个兴趣点今日推荐。"""
    uid = _mkuser(db)
    a = _mkinterest(db, uid, "订阅甲")
    _mkinterest(db, uid, "订阅乙", min_score=5)
    # 甲有一篇 5 分并已生成今日摘要；乙没有任何论文
    pid = _mkscore(db, uid, a, 401, 5, "Paper for A")
    _mkdigest(db, uid, a, [pid])
    _complete_setup()

    body = _login_uid(db, uid).get("/feed").text
    assert "订阅甲" in body
    assert "订阅乙" in body
    # 两组都要有独立的 section 头
    assert body.count('class="section-head"') == 2
    # 乙没有达标论文，应给出空态而不是消失
    assert "今天还没有可推荐的论文" in body


def test_feed_shows_rate_buttons(db):
    """需求：/feed 每条推荐带评价按钮。

    /feed 读的是**摘要条目**（digest_items），不是 llm_scores —— 这一点与
    /stream 不同，所以这里必须先造一份摘要。
    """
    uid = _mkuser(db)
    a = _mkinterest(db, uid)
    pid = _mkscore(db, uid, a, 501, 5, "Rateable paper")
    _mkdigest(db, uid, a, [pid])
    _complete_setup()

    body = _login_uid(db, uid).get("/feed").text
    assert "Rateable paper" in body
    assert "这条推荐准吗？" in body
    assert 'action="/feed/rate"' in body
    assert "效果与邮件内点击一致" in body


def test_feed_rate_records_feedback_like_email(db):
    """需求：/feed 评价与邮件评价效果相同 —— 都落到 feedbacks 表并调整画像。"""
    from app.models.digest import Feedback

    uid = _mkuser(db)
    a = _mkinterest(db, uid)
    pid = _mkscore(db, uid, a, 601, 5, "Rate me")
    _mkdigest(db, uid, a, [pid])
    _complete_setup()

    c = _login_uid(db, uid)
    csrf = _extract_csrf(c.get("/feed").text)
    r = c.post("/feed/rate", data={
        "paper_id": pid, "interest_id": a, "rating": 5, "csrf": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303

    with db() as s:
        fb = s.query(Feedback).filter(Feedback.paper_id == pid).one()
        assert fb.rating == 5
        assert fb.user_id == uid
        assert fb.interest_id == a
        # 与邮件路径一致：useful/boring 分类
        assert fb.action == "useful"


def test_feed_rate_rejects_other_users_interest(db):
    """IDOR：不能给别人的订阅打分。"""
    uid_a = _mkuser(db, "a@example.com", "ua")
    uid_b = _mkuser(db, f"b{_uniq()}@example.com", f"ub{_uniq()}")
    a_of_b = _mkinterest(db, uid_b, "B的订阅")
    pid = _mkscore(db, uid_b, a_of_b, 701, 5, "Not yours")
    _complete_setup()

    from app.models.digest import Feedback

    c = _login_uid(db, uid_a)
    csrf = _extract_csrf(c.get("/feed").text)
    c.post("/feed/rate", data={
        "paper_id": pid, "interest_id": a_of_b, "rating": 1, "csrf": csrf,
    }, follow_redirects=False)
    with db() as s:
        assert s.query(Feedback).filter(Feedback.paper_id == pid).count() == 0


# ------------------------------------------------------------ 立刻推荐

def test_recommend_now_requires_csrf(db):
    uid = _mkuser(db)
    _mkinterest(db, uid)
    _complete_setup()
    c = _login_uid(db, uid)
    c.post("/feed/recommend", data={}, follow_redirects=False)
    # 无 CSRF 不应触发任何副作用；此处只断言不 500
    assert c.get("/feed").status_code == 200


def test_recommend_respects_daily_limit(db):
    """需求：管理员可限制每日推荐次数以控 token。"""
    from app.core.config import reload_settings

    uid = _mkuser(db)
    _mkinterest(db, uid)
    _complete_setup()
    _login_uid(db, uid)

    reload_settings({"quota": {"recommend_runs_per_day": 2}})
    try:
        from app.core.quota import recommend_allowed, record_recommend_run

        assert recommend_allowed(uid)[0] is True
        record_recommend_run(uid)
        record_recommend_run(uid)
        allowed, reason = recommend_allowed(uid)
        assert allowed is False
        assert "立刻推荐" in reason
    finally:
        reload_settings()


def test_recommend_limit_exempted_for_own_key(db):
    """需求：用户自己配的 API Key 不受推荐次数限制。"""
    from app.core.config import reload_settings
    from app.core.security import encrypt_value

    uid = _mkuser(db)
    _mkinterest(db, uid)
    _complete_setup()

    with db() as s:
        from app.models.user import User

        u = s.get(User, uid)
        u.llm_base_url = "https://api.example.com/v1"
        u.llm_api_key_enc = encrypt_value("sk-test")
        s.add(u)
        s.commit()

    reload_settings({"quota": {"recommend_runs_per_day": 1}})
    try:
        from app.core.quota import recommend_allowed, record_recommend_run

        for _ in range(5):
            record_recommend_run(uid)
        assert recommend_allowed(uid)[0] is True, "自带 Key 不应受次数限制"
    finally:
        reload_settings()


def test_email_quota_does_not_block_web_recommend(db):
    """需求：邮件额度用尽后仍可在网页内推荐。"""
    from app.core.config import reload_settings
    from app.core.utils import utc_iso
    from app.models.delivery import Delivery
    from app.models.digest import Digest

    uid = _mkuser(db)
    iid = _mkinterest(db, uid)
    _complete_setup()

    with db() as s:
        d = Digest(user_id=uid, interest_id=iid, digest_date="2026-10-04",
                   status="sent", item_count=1, created_at=utc_iso())
        s.add(d)
        s.flush()
        s.add(Delivery(digest_id=d.id, to_email="u@example.com", status="sent",
                       sent_at=utc_iso()))
        s.commit()

    reload_settings({"quota": {"emails_per_day": 1, "recommend_runs_per_day": 5}})
    try:
        from app.core.quota import email_allowed, recommend_allowed

        assert email_allowed(uid)[0] is False, "邮件额度应用尽"
        assert recommend_allowed(uid)[0] is True, "邮件额度不该影响站内推荐"
    finally:
        reload_settings()


# ------------------------------------------------------------ 订阅编辑

def test_interest_edit_page_loads(db):
    uid = _mkuser(db)
    iid = _mkinterest(db, uid, "可编辑")
    _complete_setup()
    c = _login_uid(db, uid)
    r = c.get(f"/interests/{iid}/edit")
    assert r.status_code == 200
    assert "编辑订阅" in r.text
    assert "让 AI 重新解析" in r.text


def test_interest_edit_updates_settings(db):
    uid = _mkuser(db)
    iid = _mkinterest(db, uid, include='["old"]')
    _complete_setup()
    c = _login_uid(db, uid)
    csrf = _extract_csrf(c.get(f"/interests/{iid}/edit").text)
    r = c.post(f"/interests/{iid}/edit", data={
        "description": "改了描述", "include_keywords": "old, brand new",
        "exclude_keywords": "", "min_score": "3", "max_papers_per_day": "9",
        "lookback_days": "14", "send_at": "07:15", "timezone": "Asia/Tokyo",
        "use_llm": "", "csrf": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303

    from app.models.interest import Interest

    with db() as s:
        row = s.get(Interest, iid)
        assert row.min_score == 3
        assert row.max_papers_per_day == 9
        assert row.lookback_days == 14
        assert row.send_at == "07:15"
        assert row.timezone == "Asia/Tokyo"
        import json as _json

        assert "brand new" in _json.loads(row.include_keywords_json)


def test_interest_edit_clamps_out_of_range_values(db):
    uid = _mkuser(db)
    iid = _mkinterest(db, uid)
    _complete_setup()
    c = _login_uid(db, uid)
    csrf = _extract_csrf(c.get(f"/interests/{iid}/edit").text)
    c.post(f"/interests/{iid}/edit", data={
        "description": "x", "include_keywords": "", "exclude_keywords": "",
        "min_score": "99", "max_papers_per_day": "9999", "lookback_days": "0",
        "send_at": "08:00", "timezone": "UTC", "use_llm": "", "csrf": csrf,
    }, follow_redirects=False)

    from app.models.interest import Interest

    with db() as s:
        row = s.get(Interest, iid)
        assert row.min_score == 5
        assert row.max_papers_per_day == 50
        assert row.lookback_days == 1


def test_interest_edit_rejects_other_user(db):
    uid_a = _mkuser(db, f"a{_uniq()}@example.com", f"ua{_uniq()}")
    uid_b = _mkuser(db, f"b{_uniq()}@example.com", f"ub{_uniq()}")
    iid_b = _mkinterest(db, uid_b, "别人的")
    _complete_setup()
    c = _login_uid(db, uid_a)
    r = c.get(f"/interests/{iid_b}/edit", follow_redirects=False)
    assert r.status_code == 303 and "/interests" in r.headers["location"]


# ------------------------------------------------------ 管理员按用户额度

def test_admin_can_set_per_user_email_quota(db):
    uid = _mkuser(db, f"t{_uniq()}@example.com", f"tgt{_uniq()}")
    admin_id = _mkuser(db, f"adm{_uniq()}@example.com", f"adm{_uniq()}", admin=True)
    _complete_setup()
    c = _login_uid(db, admin_id)
    csrf = _extract_csrf(c.get(f"/admin/users/{uid}").text)
    r = c.post(f"/admin/users/{uid}/quota", data={
        "daily_email_quota": "3", "daily_recommend_quota": "1",
        "email_quota_unlimited": "", "csrf": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303

    from app.core.quota import effective_limits

    limits = effective_limits(uid)
    assert limits["emails_per_day"] == 3
    assert limits["recommend_runs_per_day"] == 1
    # 另一个用户不受影响
    assert effective_limits(admin_id)["emails_per_day"] != 3


def test_user_quota_blank_means_follow_default(db):
    """留空 = 跟随系统默认，调整全局默认时自动跟随。"""
    from app.core.config import reload_settings
    from app.core.quota import effective_limits

    uid = _mkuser(db, f"t{_uniq()}@example.com", f"tt{_uniq()}")
    _complete_setup()
    reload_settings({"quota": {"emails_per_day": 7}})
    try:
        assert effective_limits(uid)["emails_per_day"] == 7
        reload_settings({"quota": {"emails_per_day": 9}})
        assert effective_limits(uid)["emails_per_day"] == 9, "应自动跟随新默认"
    finally:
        reload_settings()


def test_email_quota_unlimited_flag(db):
    from app.core.config import reload_settings

    uid = _mkuser(db, f"u{_uniq()}@example.com", f"u{_uniq()}")
    _complete_setup()
    with db() as s:
        from app.models.user import User

        u = s.get(User, uid)
        u.email_quota_unlimited = 1
        s.add(u)
        s.commit()
    reload_settings({"quota": {"emails_per_day": 1}})
    try:
        from app.core.quota import email_allowed

        assert email_allowed(uid)[0] is True
    finally:
        reload_settings()


# ------------------------------------------------------------ 账户页

def test_account_shows_all_limits(db):
    """需求：账户界面显示限额。"""
    uid = _mkuser(db)
    _mkinterest(db, uid)
    _complete_setup()
    body = _login_uid(db, uid).get("/account").text
    assert "今日用量与限额" in body
    assert "AI 调用" in body
    assert "推送邮件" in body
    assert "立刻推荐" in body


def test_account_mentions_byok_benefit(db):
    uid = _mkuser(db)
    _mkinterest(db, uid)
    _complete_setup()
    body = _login_uid(db, uid).get("/account").text
    assert "配置自己的 API Key" in body


def test_register_always_guides_to_own_key(db):
    """需求：新用户注册时优先建议自行配置 LLM API Key。

    原逻辑只在「系统未配全局凭据」时引导；站点配了全局 Key 时新用户
    从不看到这一步，会默默消耗站点额度直到撞墙。
    """
    _complete_setup()
    c = _client()
    r = c.post("/register", data={
        "email": "newbie@example.com", "password": "Passw0rd!x",
        "username": "newb", "csrf": _extract_csrf(c.get("/register").text),
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "/account/llm" in r.headers["location"]
    assert "welcome=1" in r.headers["location"]


def test_llm_welcome_page_explains_benefits(db):
    uid = _mkuser(db)
    _complete_setup()
    body = _login_uid(db, uid).get("/account/llm?welcome=1").text
    assert "建议现在就配置自己的 Key" in body
    assert "不受「每日立刻推荐次数」限制" in body


# ------------------------------------------------------ 密码实时校验 / 标签

def test_register_page_loads_live_check_assets(db):
    _complete_setup()
    body = _client().get("/register").text
    assert "form-enhance.js" in body
    assert 'id="reg-password"' in body
    assert 'id="pw-rules"' in body


def test_keyword_picker_assets_present_on_subscribe_pages(db):
    """订阅配置页与编辑页都要有关键词点击选择。"""
    from app.interest.presets import KEYWORD_PRESETS

    uid = _mkuser(db)
    iid = _mkinterest(db, uid)
    _complete_setup()
    c = _login_uid(db, uid)

    edit = c.get(f"/interests/{iid}/edit").text
    assert "form-enhance.js" in edit
    assert "kw-presets" in edit
    assert 'id="inc-kw"' in edit

    # 预置词表是有效 JSON，且不含空串
    import json
    import re

    m = re.search(r'<script id="kw-presets" type="application/json">(.*?)</script>', edit, re.S)
    assert m, "预置词表数据块缺失"
    presets = json.loads(m.group(1))
    assert len(presets) == len(KEYWORD_PRESETS)
    assert all(p.strip() for p in presets)


def test_keyword_presets_have_no_duplicates():
    from app.interest.presets import KEYWORD_PRESETS

    lowered = [k.lower() for k in KEYWORD_PRESETS]
    assert len(set(lowered)) == len(lowered), "预置词不应重复（忽略大小写）"
