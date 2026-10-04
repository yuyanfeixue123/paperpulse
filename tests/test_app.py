"""Web 层：健康检查、引导门禁、认证、反馈链接、存储清理、安全工具。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.core.utils import utc_iso


def _client():
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def _complete_setup():
    from app.core.db import SessionLocal
    from app.models.system import SystemSettings

    with SessionLocal() as s:
        row = s.get(SystemSettings, 1)
        if row is None:
            row = SystemSettings(
                id=1, setup_step=6, site_name="PaperPulse",
                site_url="http://localhost:8000", default_timezone="Asia/Shanghai",
                llm_mode="keyword", retention_days=30, purge_scope="all",
                max_pool_rows=200000, updated_at=utc_iso(),
            )
            s.add(row)
        row.setup_completed_at = utc_iso()
        row.setup_step = 6
        s.commit()


def test_healthz():
    c = _client()
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_setup_gate_redirects():
    from app.core.db import SessionLocal
    from app.models.system import SystemSettings

    with SessionLocal() as s:
        row = s.get(SystemSettings, 1)
        if row:
            row.setup_completed_at = None
            row.setup_step = 1
            s.commit()
    c = _client()
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/admin/setup")
    assert c.get("/admin/setup").status_code == 200


def test_login_page_and_flow():
    _complete_setup()
    c = _client()
    assert c.get("/login").status_code == 200
    r = c.post(
        "/login",
        data={"email": "nobody@example.com", "password": "wrong", "csrf": ""},
        follow_redirects=False,
    )
    assert r.status_code == 200  # CSRF 失败时回到登录页


def test_register_password_policy():
    _complete_setup()
    c = _client()
    token = c.get("/register").text
    csrf = _extract_csrf(token)
    r = c.post(
        "/register",
        data={"email": "weak@example.com", "password": "short", "csrf": csrf},
    )
    assert "密码" in r.text


def _extract_csrf(html: str) -> str:
    import re

    m = re.search(r'name="csrf"\s+value="([^"]+)"', html)
    return m.group(1) if m else ""


def test_hmac_token_roundtrip():
    from app.core.security import make_token, read_token

    t = make_token(uid=7, paper_id=8, interest_id=9, act="rate", rating=5)
    data = read_token(t)
    assert data["uid"] == 7 and data["rating"] == 5
    assert read_token(t + "x") is None


def test_password_hashing():
    from app.core.security import hash_password, password_strength_ok, verify_password

    assert password_strength_ok("Abcdef12345")[0] is True
    assert password_strength_ok("abc")[0] is False
    h = hash_password("Abcdef12345")
    assert verify_password("Abcdef12345", h) is True
    assert verify_password("wrong", h) is False


def test_encryption_roundtrip():
    from app.core.security import decrypt_value, encrypt_value

    assert decrypt_value(encrypt_value("secret-key")) == "secret-key"


def test_session_cookie_secure_only_on_https(db):
    """回归：会话 cookie 不得无条件带 Secure。

    浏览器/httpx 在 HTTP 连接下会**拒收** Secure cookie，一旦无条件置位，
    未配 TLS 的部署（内网直连、裸机 HTTP）会表现为「登录成功 → 立刻被登出」，
    且日志里完全看不出原因。正确做法是：确定 HTTPS 才加 Secure。
    """
    from fastapi import Request

    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.models.user import User
    from app.web.deps import request_is_https

    def _req(scheme: str, xfp: str = "") -> Request:
        raw = [(b"host", b"x")]
        if xfp:
            raw.append((b"x-forwarded-proto", xfp.encode()))
        return Request({"type": "http", "method": "GET", "path": "/",
                        "headers": raw, "scheme": scheme,
                        "query_string": b"", "server": ("x", 80)})

    assert request_is_https(_req("https")) is True
    assert request_is_https(_req("http", "https")) is True
    assert request_is_https(_req("http", "https,http")) is True  # 多级反代
    assert request_is_https(_req("http")) is False
    assert request_is_https(_req("http", "http")) is False
    assert request_is_https(None) is False

    with db() as s:
        s.add(User(email="sec@example.com", username="secu",
                   password_hash=hash_password("Passw0rd!x"), created_at=utc_iso()))
        s.commit()
    _complete_setup()

    c = _client()
    r = c.post("/login", data={"email": "secu", "password": "Passw0rd!x",
                               "csrf": _extract_csrf(c.get("/login").text)},
               follow_redirects=False)
    cookie = r.headers.get("set-cookie", "")
    assert "Secure" not in cookie, f"HTTP 下不应带 Secure：{cookie}"
    # 端到端：HTTP 访问时登录态必须真正可用
    assert c.get("/account", follow_redirects=False).status_code == 200

    # 反代终结 TLS 的生产形态：XFP=https 时必须带 Secure
    c2 = _client()
    r2 = c2.post("/login", data={"email": "secu", "password": "Passw0rd!x",
                                 "csrf": _extract_csrf(c2.get("/login").text)},
                 follow_redirects=False,
                 headers={"x-forwarded-proto": "https"})
    assert "Secure" in r2.headers.get("set-cookie", "")


def test_session_cookie_invalidated_by_password_change(db):
    """回归：改密后旧会话必须立即失效（否则无法踢下线）。"""
    from starlette.responses import Response

    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.models.user import User
    from app.web.deps import SESSION_COOKIE, clear_session, set_session

    with db() as s:
        s.add(User(email="rot@example.com", username="rotu",
                   password_hash=hash_password("Passw0rd!x"), created_at=utc_iso()))
        s.commit()

    old = Response()
    set_session(old, 1)
    old_token = old.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    assert SESSION_COOKIE  # cookie 名常量存在

    from app.core.security import session_valid

    assert session_valid(old_token) is not None

    # 模拟改密：password_changed_at 前移
    with db() as s:
        u = s.get(User, 1)
        u.password_changed_at = utc_iso()
        s.add(u)
        s.commit()

    assert session_valid(old_token) is None, "改密后旧 token 必须失效"

    fresh = Response()
    set_session(fresh, 1)
    new_token = fresh.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    assert session_valid(new_token) is not None

    cleared = Response()
    clear_session(cleared)
    assert "Max-Age=0" in cleared.headers["set-cookie"] or \
        cleared.headers["set-cookie"].startswith(f'{SESSION_COOKIE}=""')


def test_purge_respects_retention(db):
    from datetime import timedelta

    from app.core.retention import estimate_purge, purge_once
    from app.core.utils import now_utc
    from app.models.paper import Paper
    from app.pipeline.fetch import upsert_paper
    from app.sources.base import PaperItem

    old = utc_iso(now_utc() - timedelta(days=60))
    for i in range(3):
        upsert_paper(
            PaperItem(
                source_key="test", source_id=f"old-{i}", title=f"Old Paper {i}",
                abstract="x" * 300, doi=f"10.1000/old{i}", published_at=old,
            )
        )
    upsert_paper(
        PaperItem(
            source_key="test", source_id="new-1", title="New Paper",
            abstract="y" * 300, doi="10.1000/new1", published_at=utc_iso(),
        )
    )
    # 清理按 first_seen_at 判定，这里把三篇旧论文的入库时间回拨
    with db() as s:
        for p in s.query(Paper).filter(Paper.title.like("Old Paper%")).all():
            p.first_seen_at = old
        s.commit()
    est = estimate_purge(30, "all")
    assert est["rows"] == 3
    deleted = purge_once(30, "all")
    assert deleted == 3
    with db() as s:
        assert s.query(Paper).count() == 1


def test_quota_deferral(db):
    from app.models.delivery import EmailProvider

    with db() as s:
        s.add(
            EmailProvider(
                key="p1", kind="smtp", role="primary",
                config_json=json.dumps({"host": "127.0.0.1", "port": 1, "from_email": "a@b.c"}),
                daily_budget=1, enabled=1, priority=0,
            )
        )
        s.commit()
        from app.pipeline import deliver

        ok_first = deliver._reserve_quota("p1", "2026-01-01", 1)
        ok_second = deliver._reserve_quota("p1", "2026-01-01", 1)
    assert ok_first is True
    assert ok_second is False  # 额度耗尽，后续邮件标记 deferred


def test_email_rendering_contains_headers(db):
    from app.pipeline.render import render_digest

    items = [
        {
            "id": 1, "title": "Test Paper", "abstract": "abs",
            "authors": ["A", "B", "C", "D"], "venue": "Nature",
            "published_at": "2026-10-01T00:00:00+00:00",
            "url": "https://example.com/1", "doi": "10.1000/x",
            "llm_score": 5, "reason": "高度相关",
        }
    ]
    subject, html, text = render_digest(
        site_url="https://paper.example.com",
        site_name="PaperPulse",
        interest_name="城市科学",
        digest_date="2026-10-03",
        items=items,
        user_id=1,
        interest_id=1,
    )
    assert "城市科学" in subject
    assert "etc" not in html and "等" in html  # 作者前 3 位 + 等
    assert "有用" in html and "不感兴趣" in html
    assert "Test Paper" in text
    assert "/f/" in html  # 反馈链接


def test_memory_guard_thresholds(db):
    from app.core import memory
    from app.core.config import reload_settings

    reload_settings({"memory": {"rss_warn_mb": 0, "rss_hard_mb": 1}})
    assert memory.evaluate() in ("warn", "hard")

    reload_settings({"memory": {"rss_warn_mb": 10_000, "rss_hard_mb": 20_000}})
    assert memory.evaluate() == "ok"
    assert memory.fetch_allowed() is True


def test_disk_guard_returns_state(db, monkeypatch):
    from app.core import retention
    from app.core.config import reload_settings

    reload_settings({"disk": {"usage_warn_pct": 80, "usage_hard_pct": 90}})

    monkeypatch.setattr(retention, "disk_usage_pct", lambda: 50.0)
    assert retention.disk_guard() == "ok"

    monkeypatch.setattr(retention, "disk_usage_pct", lambda: 85.0)
    called = []
    monkeypatch.setattr(retention, "purge_once", lambda **kw: called.append(kw) or 0)
    assert retention.disk_guard() == "warn"
    assert called and called[0]["days"] == 7  # 收紧到 7 天

    monkeypatch.setattr(retention, "disk_usage_pct", lambda: 95.0)
    assert retention.disk_guard() == "hard"


def test_guard_task_runs(db):
    from app.scheduler.jobs import task_guard

    task_guard({})  # 不抛异常即通过


def test_llm_gate_blocks_interest_creation(db):
    """未配置任何 LLM 凭据时，创建订阅的三个入口都应重定向到配置页。"""

    from app.core.config import reload_settings
    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.models.user import User

    reload_settings({"llm": {"base_url": "", "api_key": ""}})
    with db() as s:
        u = s.query(User).filter(User.email == "gate@example.com").first()
        if u is None:
            s.add(
                User(
                    email="gate@example.com",
                    password_hash=hash_password("Gate123456"),
                    display_name="gate",
                    is_active=True,
                    email_verified=True,
                    created_at=utc_iso(),
                )
            )
            s.commit()

    _complete_setup()
    c = _client()
    csrf = _extract_csrf(c.get("/login").text)
    r = c.post(
        "/login",
        data={"email": "gate@example.com", "password": "Gate123456", "csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/account/llm" in r.headers["location"]  # 登录后被引导配置

    for path in ("/interests/new",):
        rr = c.get(path, follow_redirects=False)
        assert rr.status_code == 303
        assert "/account/llm" in rr.headers["location"]

    rr = c.post(
        "/interests/parse",
        data={"description": "urban planning", "csrf": csrf},
        follow_redirects=False,
    )
    assert rr.status_code == 303 and "/account/llm" in rr.headers["location"]

    # 配置页可访问
    assert c.get("/account/llm").status_code == 200
    reload_settings()


def test_llm_ready_with_global_config(db):
    from app.core.config import reload_settings
    from app.llm.client import llm_ready

    reload_settings({"llm": {"base_url": "https://x.test/v1", "api_key": "k"}})
    assert llm_ready(None)[0] is True
    assert llm_ready(1)[0] is True

    reload_settings({"llm": {"base_url": "", "api_key": ""}})
    assert llm_ready(None)[0] is False


def test_llm_ready_with_user_byok(db):
    """管理员未配置时，用户自带 Key 即视为就绪。"""
    from app.core.config import reload_settings
    from app.core.security import encrypt_value
    from app.core.utils import utc_iso
    from app.llm.client import llm_ready
    from app.models.user import User

    reload_settings({"llm": {"base_url": "", "api_key": ""}})
    with db() as s:
        u = s.query(User).filter(User.email == "byok@example.com").first()
        if u is None:
            u = User(
                email="byok@example.com",
                password_hash="x",
                display_name="byok",
                is_active=True,
                email_verified=True,
                created_at=utc_iso(),
            )
        u.llm_provider = "openai_compatible"
        u.llm_base_url = "https://api.deepseek.com/v1"
        u.llm_api_key_enc = encrypt_value("sk-user-own")
        u.llm_model = "deepseek-chat"
        s.add(u)
        s.commit()
        uid = int(u.id)

    ready, reason = llm_ready(uid)
    assert ready is True, reason

    creds = __import__("app.llm.client", fromlist=["x"]).resolve_credentials(uid)
    assert creds.base_url == "https://api.deepseek.com/v1"
    assert creds.api_key == "sk-user-own"  # 解密后可用
    reload_settings()


def test_welcome_email_renders_config_echo(db):
    """订阅创建后的确认邮件应回显配置，便于用户核对。"""
    from app.pipeline.render import render_welcome

    subject, html, text = render_welcome(
        site_url="https://example.com",
        site_name="PaperPulse",
        interest_name="城市感知",
        description="关注街景与建成环境",
        include_keywords=["街景图像", "street view"],
        exclude_keywords=["医学分割"],
        send_at="08:30",
        timezone="Asia/Shanghai",
        lookback_days=7,
        max_papers_per_day=8,
        min_score=4,
        first_digest_at="2026-10-05T00:30:00+00:00",
        user_id=1,
        interest_id=2,
    )
    assert "城市感知" in subject and "PaperPulse" in subject
    assert "街景图像" in html and "street view" in html
    assert "医学分割" in html
    assert "08:30" in html and "Asia/Shanghai" in html
    assert "08:30" in text and "街景图像" in text
    assert "/verify-email?token=" in html
    assert "multipart" not in html  # 模板本身不该声明 multipart


def test_welcome_email_handles_empty_lists(db):
    from app.pipeline.render import render_welcome

    _, html, text = render_welcome(
        site_url="https://example.com",
        site_name="PP",
        interest_name="空关键词",
        description="",
        include_keywords=[],
        exclude_keywords=[],
        send_at="09:00",
        timezone="UTC",
        lookback_days=3,
        max_papers_per_day=5,
        min_score=3,
        first_digest_at="2026-10-05T09:00:00+00:00",
        user_id=1,
        interest_id=1,
    )
    assert "未设置关键词" in html
    assert "空关键词" in text


def test_welcome_render_and_send_signatures_agree(db, monkeypatch):
    """回归：deliver.send_welcome_email 组装的 payload 含 to_email，
    而 render_welcome 不接受该参数 —— 直接单测 render_welcome 抓不到，
    只有走完整调用链才会暴露。"""
    import inspect

    from app.core.utils import utc_iso
    from app.models.interest import Interest
    from app.models.user import User
    from app.pipeline import deliver, render

    render_params = set(inspect.signature(render.render_welcome).parameters)
    src = inspect.getsource(deliver.send_welcome_email)
    assert '"to_email": user.email' in src
    # 传给 render_welcome 的键必须都是 render_welcome 接受的（除 to_email）
    filtered = {"site_url", "site_name", "interest_name", "description",
                "include_keywords", "exclude_keywords", "send_at", "timezone",
                "lookback_days", "max_papers_per_day", "min_score",
                "first_digest_at", "user_id", "interest_id"}
    assert filtered <= render_params, f"缺少参数: {filtered - render_params}"

    with db() as s:
        u = s.query(User).filter(User.email == "welcome-t@example.com").first()
        if u is None:
            u = User(email="welcome-t@example.com", password_hash="x", created_at=utc_iso())
            s.add(u)
            s.flush()  # 先拿到 user_id
        it = s.query(Interest).filter(Interest.user_id == u.id).first()
        if it is None:
            it = Interest(user_id=u.id, name="N", description="d",
                          include_keywords_json="[]", exclude_keywords_json="[]",
                          source_keys_json="[]", arxiv_categories_json="[]",
                          queries_json="{}", created_at=utc_iso())
            s.add(it)
        s.commit()
        iid = int(it.id)

    captured = {}

    class FakeProvider:
        def send(self, msg):
            captured["to"] = msg.to_email
            captured["subject"] = msg.subject
            captured["html"] = msg.html
            return "mid-1"

    monkeypatch.setattr(deliver, "primary_provider", lambda: type(
        "P", (), {"kind": "smtp", "config_json": json.dumps({"from_email": "a@b.c"})})())
    monkeypatch.setattr(deliver, "build_provider", lambda kind, cfg: FakeProvider())
    monkeypatch.setattr(deliver, "_provider_config", lambda row: {})

    ok, msg = deliver.send_welcome_email(iid)
    assert ok is True, msg
    assert captured["to"] == "welcome-t@example.com"
    assert "N" in captured["subject"]
    assert "/verify-email?token=" in captured["html"]


def test_send_welcome_requires_existing_interest(db):
    from app.pipeline.deliver import send_welcome_email

    ok, msg = send_welcome_email(999999)
    assert ok is False and "不存在" in msg


def test_verify_email_route_rejects_bad_token(db):
    _complete_setup()
    c = _client()
    r = c.get("/verify-email?token=forged.token")
    assert r.status_code == 200
    assert "无效" in r.text or "过期" in r.text


def test_verify_email_marks_user(db):
    from app.core.security import make_token
    from app.models.user import User

    _complete_setup()
    with db() as s:
        s.add(
            User(
                email="verify-me@example.com",
                password_hash="x",
                email_verified=False,
                created_at=utc_iso(),
            )
        )
        s.commit()
        uid = int(s.query(User).filter(User.email == "verify-me@example.com").one().id)

    c = _client()
    token = make_token(uid=uid, act="verify-email", iid=1)
    r = c.get(f"/verify-email?token={token}")
    assert r.status_code == 200
    assert "验证成功" in r.text
    with db() as s:
        assert s.get(User, uid).email_verified is True


def test_welcome_task_is_registered():
    from app.scheduler.runner import HANDLERS

    assert "send_welcome" in HANDLERS


def test_digest_email_contains_salutation_greeting_body_unsub(db):
    """推送邮件必须包含称呼、问候、正文与退订链接四要素。"""
    from app.pipeline.render import render_digest

    items = [{
        "id": 1, "title": "Street View Deep Learning for Built Environment",
        "abstract": "abs", "authors": ["Ann Lee", "Bo Zhao", "Cy Xu", "Dee Wang"],
        "venue": "Cities", "published_at": "2026-10-03T00:00:00+00:00",
        "url": "https://example.com/1", "doi": "10.1234/abc",
        "llm_score": 5, "reason": "高度相关",
    }]
    subject, html, text = render_digest(
        site_url="https://pp.example.com",
        site_name="PaperPulse",
        interest_name="建成环境与LLM",
        digest_date="2026-10-04",
        items=items,
        user_id=1,
        interest_id=9,
        salutation="yuyanfeixue",
        lookback_days=3,
    )
    # 称呼
    assert "yuyanfeixue，这是你的每日论文" in html
    assert "yuyanfeixue，这是你的每日论文" in text
    # 问候
    assert "早上好" in html and "早上好" in text
    # 正文
    assert "Street View Deep Learning" in html
    assert "Cities" in html and "高度相关" in html
    # 退订链接
    assert "/u/" in html and "退订" in html
    assert "退订" in text and "pp.example.com" in text
    # 设置说明
    assert "最近 3 天" in html
    assert "调整推送设置" in html


def test_digest_email_falls_back_to_generic_salutation(db):
    from app.pipeline.render import render_digest

    _, html, _ = render_digest(
        site_url="https://pp.example.com", site_name="PP", interest_name="I",
        digest_date="2026-10-04", items=[], user_id=1, interest_id=1,
    )
    assert "你好，这是你的每日论文" in html


def test_login_accepts_username_or_email(db):
    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.models.user import User

    with db() as s:
        s.add(User(
            email="by-name@example.com", username="namel login".replace(" ", ""),
            password_hash=hash_password("Passw0rd!x"), display_name="N",
            email_verified=True, created_at=utc_iso(),
        ))
        s.commit()

    _complete_setup()
    c = _client()
    csrf = _extract_csrf(c.get("/login").text)
    r = c.post("/login", data={
        "email": "namellogin", "password": "Passw0rd!x", "csrf": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303, "用用户名登录应成功"
    assert c.get("/account").status_code == 200


def test_username_must_be_unique(db):
    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.models.user import User

    with db() as s:
        for un in ("dupuser", "other"):
            s.add(User(email=f"{un}@example.com", username=un,
                       password_hash=hash_password("Passw0rd!x"),
                       created_at=utc_iso()))
        s.commit()

    _complete_setup()
    c = _client()
    c.post("/login", data={"email": "dupuser", "password": "Passw0rd!x",
                           "csrf": _extract_csrf(c.get("/login").text)})
    csrf = _extract_csrf(c.get("/account").text)
    r = c.post("/account/profile", data={
        "username": "other", "email": "dupuser@example.com",
        "display_name": "x", "timezone": "UTC", "csrf": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "已被占用" in c.get("/account").text


def test_account_defaults_apply_to_all_interests(db):
    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.models.interest import Interest
    from app.models.user import User

    with db() as s:
        u = User(email="defs@example.com", username="defs",
                 password_hash=hash_password("Passw0rd!x"), created_at=utc_iso())
        s.add(u)
        s.flush()
        for n in range(2):
            s.add(Interest(user_id=u.id, name=f"sub-{n}", description="d",
                           include_keywords_json="[]", exclude_keywords_json="[]",
                           source_keys_json="[]", arxiv_categories_json="[]",
                           queries_json="{}", max_papers_per_day=10,
                           lookback_days=7, send_at="08:30", created_at=utc_iso()))
        s.commit()
        uid = int(u.id)

    _complete_setup()
    c = _client()
    c.post("/login", data={"email": "defs", "password": "Passw0rd!x",
                           "csrf": _extract_csrf(c.get("/login").text)})
    csrf = _extract_csrf(c.get("/account").text)
    r = c.post("/account/defaults", data={
        "max_papers_per_day": "5", "lookback_days": "3",
        "send_at": "09:15", "csrf": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303
    with db() as s:
        rows = s.query(Interest).filter(Interest.user_id == uid).all()
    assert len(rows) == 2
    for it in rows:
        assert it.max_papers_per_day == 5
        assert it.lookback_days == 3
        assert it.send_at == "09:15"


def test_account_defaults_reject_bad_input(db):
    assert _hhmm_ok("08:30") is True
    assert _hhmm_ok("25:00") is False
    assert _hhmm_ok("abc") is False
    assert _hhmm_ok("8:5") is True


def _hhmm_ok(v: str) -> bool:
    from app.web.routes.account import _valid_hhmm

    return _valid_hhmm(v)


def test_flash_message_survives_redirect_with_chinese(db):
    """回归：flash 走 cookie 传递，而 cookie 只能编码 latin-1，
    中文提示语若不 URL 编码会抛 UnicodeEncodeError → 全站任何写操作都 500。"""
    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.models.user import User

    with db() as s:
        s.add(User(email="flash@example.com", username="flashu",
                   password_hash=hash_password("Passw0rd!x"),
                   created_at=utc_iso()))
        s.commit()

    _complete_setup()
    c = _client()
    c.post("/login", data={"email": "flashu", "password": "Passw0rd!x",
                           "csrf": _extract_csrf(c.get("/login").text)})
    r = c.post("/account/defaults", data={
        "max_papers_per_day": "5", "lookback_days": "3",
        "send_at": "09:15", "csrf": _extract_csrf(c.get("/account").text),
    }, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/account")
    assert page.status_code == 200
    assert "已设为每日 5 篇" in page.text, "中文提示语应能跨重定向显示"
    # 读到即清：刷新不应重复显示
    assert "已设为每日 5 篇" not in c.get("/account").text


def test_flash_rejects_bad_values(db):
    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.models.user import User

    with db() as s:
        s.add(User(email="flash2@example.com", username="flashu2",
                   password_hash=hash_password("Passw0rd!x"),
                   created_at=utc_iso()))
        s.commit()

    _complete_setup()
    c = _client()
    c.post("/login", data={"email": "flashu2", "password": "Passw0rd!x",
                           "csrf": _extract_csrf(c.get("/login").text)})
    c.post("/account/defaults", data={
        "max_papers_per_day": "abc", "lookback_days": "3",
        "send_at": "09:15", "csrf": _extract_csrf(c.get("/account").text),
    }, follow_redirects=False)
    assert "必须是整数" in c.get("/account").text
    c.post("/account/defaults", data={
        "max_papers_per_day": "5", "lookback_days": "3",
        "send_at": "99:99", "csrf": _extract_csrf(c.get("/account").text),
    }, follow_redirects=False)
    assert "HH:MM" in c.get("/account").text
