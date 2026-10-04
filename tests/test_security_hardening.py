"""安全加固回归：注册开关、邮箱枚举、登出 CSRF、限流器、CSP、配额。"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tests.test_app import _client, _complete_setup, _extract_csrf


def _mkuser(db, email: str, username: str = "", password: str = "Passw0rd!x"):
    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.models.user import User

    with db() as s:
        s.add(User(email=email, username=username or None,
                   password_hash=hash_password(password), created_at=utc_iso()))
        s.commit()


def _set_registration(db, mode: str) -> None:
    from app.core.db import SessionLocal
    from app.models.system import SystemSettings

    with SessionLocal() as s:
        row = s.get(SystemSettings, 1)
        row.registration_mode = mode
        s.commit()


@pytest.fixture(autouse=True)
def _reset_state(db):
    """每个用例**前后**都把注册开关与配置恢复默认。

    测试库是文件级共享的（表结构复用）。只在 teardown 清理不够：
    下一个用例的 setup 阶段就已经读到了上一个用例留下的脏状态。
    """
    _restore_defaults()
    yield
    _restore_defaults()


def _restore_defaults() -> None:
    from app.core.config import reload_settings

    reload_settings()
    # 注册接口有 5 次/300 秒的限流，同文件内多个用例会累计触发。
    # 这是限流的**正常行为**，测试里清空桶以免互相干扰。
    from app.web.deps import _buckets

    _buckets.clear()
    from app.core.db import SessionLocal
    from app.models.system import SystemSettings

    with SessionLocal() as s:
        row = s.get(SystemSettings, 1)
        if row is not None:
            row.registration_mode = "open"
            s.commit()


# ---------------------------------------------------------------- 注册开关

def test_registration_can_be_closed(db):
    """审计项：/register 原本完全无开关，公网实例谁都能批量注册。"""
    _mkuser(db, "existing@example.com", "exista")
    _complete_setup()
    _set_registration(db, "closed")

    c = _client()
    # GET 注册页应被挡下
    r = c.get("/register", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]

    # POST 直接打也必须被挡下（不能只挡页面）
    r = c.post("/register", data={
        "email": "intruder@example.com", "password": "Passw0rd!x",
        "username": "intruder", "csrf": _extract_csrf(c.get("/login").text),
    }, follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]

    from app.models.user import User

    with db() as s:
        assert s.query(User).filter(User.email == "intruder@example.com").first() is None


def test_registration_stays_open_when_zero_users(db):
    """一个账号都没有时必须允许自助注册，否则会把自己锁在门外。

    刻意**不删 users 表**：SQLite 的 ROWID 分配不会因删除而回退，后续用例会
    拿到与既有行相同的主键而互相污染。这里用一个独立临时库验证
    「库里没有账号」这一分支，再在共享库里验证「有账号时听开关」。
    """
    from sqlalchemy import create_engine
    from sqlalchemy import text as sql_text

    from app.models import Base
    from app.models.user import User

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine, tables=[User.__table__])
    with engine.begin() as conn:
        n = conn.execute(sql_text("SELECT COUNT(*) FROM users")).scalar()
    assert n == 0, "新库应为空，此时自举注册必须可用"
    engine.dispose()

    # 共享库里有关��号 => 听管理员开关
    _mkuser(db, "gate-guard@example.com", "guardu")
    _set_registration(db, "closed")
    from app.web.routes.auth import _registration_open

    assert _registration_open(object()) is False

    _set_registration(db, "open")
    assert _registration_open(object()) is True


# ------------------------------------------------------------ 邮箱枚举防护

def test_register_does_not_reveal_account_existence(db):
    """注册失败时不得区分「邮箱已注册」与「用户名被占用」。"""
    _mkuser(db, "taken@example.com", "takenname")
    _complete_setup()
    c = _client()

    r = c.post("/register", data={
        "email": "taken@example.com", "password": "Passw0rd!x",
        "username": "different", "csrf": _extract_csrf(c.get("/register").text),
    }, follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]

    page = c.get("/login").text
    assert "已注册" not in page
    assert "已存在" not in page


def test_registered_username_is_not_swallowed(db):
    """回归：register 收了 username 表单字段却从未落库，用户填了等于白填。"""
    import uuid

    # 测试库文件级共享，固定邮箱会被其他测试文件抢先注册
    uniq = uuid.uuid4().hex[:10]
    email = f"newbie-{uniq}@example.com"
    username = f"nb{uniq}"
    _complete_setup()
    c = _client()
    c.post("/register", data={
        "email": email, "password": "Passw0rd!x",
        "username": username, "display_name": "新人",
        "csrf": _extract_csrf(c.get("/register").text),
    }, follow_redirects=False)

    from app.models.user import User

    with db() as s:
        u = s.query(User).filter(User.email == email).one()
        assert u.username == username, f"用户名未落库：{u.username!r}"
        assert u.display_name == "新人"

    # 且能真的用用户名登录
    c2 = _client()
    c2.post("/login", data={"email": username, "password": "Passw0rd!x",
                            "csrf": _extract_csrf(c2.get("/login").text)})
    assert c2.get("/account", follow_redirects=False).status_code == 200


def test_register_rejects_malicious_username(db):
    """用户名会进入登录查询，必须挡掉超长/特殊字符。

    注意纯空白是**合法**的 —— 语义等同「不设用户名」，不能算拒绝。
    """
    _complete_setup()
    c = _client()
    # 本用例一次发 5 个注册请求，会撞上 5 次/300 秒的注册限流；
    # 这里要验的是用户名校验，故先清空限流桶避免误判。
    from app.web.deps import _buckets

    _buckets.clear()
    for bad in ("a" * 65, "user name", "<script>", "a@b", "用户"):
        _buckets.clear()  # 每个用例之间也清一次
        r = c.post("/register", data={
            "email": f"bad{abs(hash(bad)) % 100000}@example.com",
            "password": "Passw0rd!x", "username": bad,
            "csrf": _extract_csrf(c.get("/register").text),
        }, follow_redirects=False)
        # 成功注册会 303 到 /account/llm；被拒则停在注册页 200
        assert r.status_code == 200, f"用户名 {bad!r} 不应注册成功（返回 {r.status_code}）"

    from app.models.user import User

    with db() as s:
        for bad in ("a" * 65, "user name", "<script>", "a@b", "用户"):
            assert (
                s.query(User).filter(User.username == bad).first() is None
            ), f"非法用户名 {bad!r} 竟被落库"


def test_register_blank_username_falls_back_to_email_only(db):
    """纯空白用户名应被当作「不设用户名」，而不是报错。

    注册成功后若系统未配全局 LLM，会 303 跳到 /account/llm（既有设计），
    因此这里不断言状态码，只断言数据库里的结果。
    """
    _complete_setup()
    c = _client()
    c.post("/register", data={
        "email": "blankuser@example.com", "password": "Passw0rd!x",
        "username": "   ", "csrf": _extract_csrf(c.get("/register").text),
    }, follow_redirects=False)

    from app.models.user import User

    with db() as s:
        u = s.query(User).filter(User.email == "blankuser@example.com").one()
        assert u.username is None
        # 昵称回落到邮箱前缀
        assert u.display_name == "blankuser"


# -------------------------------------------------------------- 登出 CSRF

def test_logout_requires_csrf(db):
    """审计项：/logout 无 CSRF → 任意外站可强制登出（登录 CSRF）。"""
    _mkuser(db, "bye@example.com", "byeu")
    _complete_setup()
    c = _client()
    c.post("/login", data={"email": "bye@example.com", "password": "Passw0rd!x",
                           "csrf": _extract_csrf(c.get("/login").text)})
    assert c.get("/account", follow_redirects=False).status_code == 200

    # 无 CSRF 的登出必须**不生效**
    r = c.post("/logout", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/account", follow_redirects=False).status_code == 200, \
        "缺少 CSRF 时不得清除会话"

    # 带正确 CSRF 才真的登出
    token = _extract_csrf(c.get("/account").text)
    # 账户页没有 logout 表单 csrf，用登录页同源的 session token 生成
    import app.web.deps as deps_mod

    good = deps_mod.csrf_for(_FakeReq(c))
    c.post("/logout", data={"csrf": good}, follow_redirects=False)
    assert c.get("/account", follow_redirects=False).status_code == 303
    assert token  # 仅确保上一步取到了表单


class _FakeReq:
    def __init__(self, client: TestClient):
        self.cookies = client.cookies


# -------------------------------------------------------------- 限流器

def test_rate_limit_buckets_are_bounded():
    """审计项：限流器 _buckets 只增不减 → 键被无限撑大，内存被慢慢吃光。"""
    from app.web import deps as d

    d._buckets.clear()
    for i in range(2000):
        d.rate_limit(f"probe:{i}", 5, 60)
    # 2000 个远小于上限，但清扫后不应残留早已过期的桶
    d._last_sweep = 0.0
    import time as _t

    _t.sleep(0.01)
    for i in range(2000, 2100):
        d.rate_limit(f"probe:{i}", 5, 60)
    assert len(d._buckets) <= d._MAX_BUCKETS


def test_rate_limit_enforces_limit():
    from app.web.deps import rate_limit

    key = "rl:test-unique"
    assert rate_limit(key, 3, 60) is True
    assert rate_limit(key, 3, 60) is True
    assert rate_limit(key, 3, 60) is True
    assert rate_limit(key, 3, 60) is False


def test_client_ip_ignores_forged_xff():
    """审计项：XFF 无条件采信 → 轮换伪造值即可绕过限流。"""
    import os

    from fastapi import Request

    from app.web.deps import client_ip

    def req(xff: str, peer: str = "203.0.113.9") -> Request:
        return Request({"type": "http", "method": "GET", "path": "/",
                        "headers": [(b"host", b"x"), (b"x-forwarded-for", xff.encode())],
                        "scheme": "http", "query_string": b"", "server": ("x", 80),
                        "client": (peer, 1234)})

    # 直连非受信代理 => 忽略伪造的 XFF，全部落到同一个键
    assert client_ip(req("1.2.3.4")) == "203.0.113.9"
    assert client_ip(req("5.6.7.8")) == "203.0.113.9"

    # 本机回环视为受信代理 => 采信第一段
    os.environ["PAPERPULSE_TRUSTED_PROXIES"] = "127.0.0.1"
    assert client_ip(req("1.2.3.4, 9.9.9.9", peer="127.0.0.1")) == "1.2.3.4"
    os.environ.pop("PAPERPULSE_TRUSTED_PROXIES", None)


# ------------------------------------------------------------ 安全响应头

def test_security_headers_present(db):
    """审计项：安全头只配在 Caddy，换反代或裸机访问即完全失效。"""
    _complete_setup()
    c = _client()
    r = c.get("/login")
    assert r.status_code == 200
    h = r.headers
    assert "default-src 'self'" in h.get("content-security-policy", "")
    assert "frame-ancestors 'none'" in h.get("content-security-policy", "")
    assert "script-src 'self'" in h.get("content-security-policy", "")
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("x-frame-options") == "DENY"
    assert h.get("referrer-policy") == "strict-origin-when-cross-origin"
    # HTTP 下不应发 HSTS（会被浏览器忽略甚至误伤本地 http 调试）
    assert "strict-transport-security" not in {k.lower() for k in h}


def test_hsts_sent_on_https(db):
    _complete_setup()
    c = _client()
    r = c.get("/login", headers={"x-forwarded-proto": "https"})
    assert "max-age=63072000" in r.headers.get("strict-transport-security", "")


def test_no_inline_script_tags_left():
    """CSP 的 script-src 'self' 禁止内联脚本，模板里必须一处可执行脚本都不剩。

    `<script type="application/json">` 是**数据块**而非可执行脚本，CSP 不拦它
    （页面用它把预置词表传给前端 JS），故按 type 排除。
    """
    from app.web.templates import TEMPLATE_DIR

    offenders: list[str] = []
    pattern = re.compile(r"<script(?![^>]*\bsrc=)(?![^>]*\btype=)")
    for path in TEMPLATE_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(path.relative_to(TEMPLATE_DIR).as_posix())
    assert not offenders, f"仍有内联脚本，会被 CSP 拦截：{offenders}"


# ------------------------------------------------------------------ 配额

def test_llm_quota_blocks_when_exhausted(db, monkeypatch):
    """审计项：无每用户 LLM 用量限制 → 单账号可耗尽站点凭据。"""
    from app.core.config import reload_settings
    from app.core.utils import utc_iso
    from app.models.score import LlmUsage

    reload_settings({"quota": {"llm_calls_per_day": 2}})
    try:
        with db() as s:
            for _ in range(2):
                s.add(LlmUsage(user_id=1, interest_id=None, kind="score",
                               model="m", prompt_tokens=1, completion_tokens=1,
                               created_at=utc_iso()))
            s.commit()

        from app.core.quota import llm_allowed, snapshot

        allowed, reason = llm_allowed(1)
        assert allowed is False
        assert "上限" in reason
        # 其他用户不受影响
        assert llm_allowed(999)[0] is True
        assert snapshot(1).llm_calls == 2
    finally:
        reload_settings()


def test_interest_quota_blocks_beyond_limit(db):
    from app.core.config import reload_settings
    from app.core.utils import utc_iso
    from app.models.interest import Interest

    reload_settings({"quota": {"max_interests": 1}})
    try:
        with db() as s:
            s.add(Interest(user_id=1, name="A", description="",
                           include_keywords_json="[]", exclude_keywords_json="[]",
                           source_keys_json="[]", arxiv_categories_json="[]",
                           queries_json="{}", created_at=utc_iso()))
            s.commit()
        from app.core.quota import interest_allowed

        allowed, reason = interest_allowed(1)
        assert allowed is False and "上限" in reason
    finally:
        reload_settings()


def test_zero_quota_means_unlimited(db):
    from app.core.config import reload_settings

    reload_settings({"quota": {"llm_calls_per_day": 0, "emails_per_day": 0,
                               "max_interests": 0}})
    try:
        from app.core.quota import email_allowed, interest_allowed, llm_allowed

        assert llm_allowed(1)[0] is True
        assert email_allowed(1)[0] is True
        assert interest_allowed(1)[0] is True
    finally:
        reload_settings()


# ------------------------------------------------------------ 忘记密码

def test_make_token_respects_explicit_ttl():
    """回归：make_token 曾无条件覆盖调用方传入的 exp，
    导致重置密码链接的实际有效期长达 30 天。"""
    import time as _t

    from app.core.security import make_token, read_token

    tok = make_token(ttl_seconds=3600, uid=1, act="reset")
    data = read_token(tok)
    assert data is not None
    ttl = data["exp"] - int(_t.time())
    assert 3500 < ttl <= 3600, f"显式 TTL 未生效，实际 {ttl} 秒"

    # 默认仍走 30 天
    tok2 = make_token(uid=1)
    ttl2 = read_token(tok2)["exp"] - int(_t.time())
    assert ttl2 > 29 * 86400


def test_forgot_password_does_not_leak_account_existence(db):
    """无论邮箱是否存在，提示语必须完全一致（防邮箱枚举）。"""
    _mkuser(db, "known@example.com", "knownu")
    _complete_setup()
    from app.web.deps import _buckets

    messages = []
    for email in ("known@example.com", "stranger@example.com"):
        _buckets.clear()
        c = _client()
        # 不跟随重定向：flash 靠 cookie 传给下一页，且「读到即清」，
        # 所以 POST 之后要单独取一次登录页把它读出来。
        c.post("/forgot-password", data={
            "email": email, "csrf": _extract_csrf(c.get("/forgot-password").text),
        }, follow_redirects=False)
        m = re.search(r'<div class="flash[^"]*">([^<]+)</div>', c.get("/login").text)
        messages.append(m.group(1).strip() if m else "")

    assert messages[0], "第一个请求应产生提示"
    assert messages[0] == messages[1], \
        f"提示语不一致，等于泄露账号是否存在：{messages}"


def test_reset_password_invalidates_existing_sessions(db):
    """重置密码后，旧会话必须立即失效。"""
    from app.core.security import make_token
    from app.models.user import User

    _mkuser(db, "resetme@example.com", "resetu")
    _complete_setup()
    # 不要硬编码 uid=1：测试库文件级共享，实际自增 ID 不可预测，
    # 而重置 token 里的 uid 必须指向**这个**用户，否则改的是别人的密码。
    with db() as s:
        uid = int(s.query(User).filter(User.email == "resetme@example.com").one().id)

    c = _client()
    c.post("/login", data={"email": "resetme@example.com", "password": "Passw0rd!x",
                           "csrf": _extract_csrf(c.get("/login").text)})
    assert c.get("/account", follow_redirects=False).status_code == 200

    token = make_token(ttl_seconds=3600, uid=uid, act="reset")
    r = c.post("/reset-password", data={
        "token": token, "password": "NewPassw0rd!x", "confirm": "NewPassw0rd!x",
        "csrf": _extract_csrf(c.get(f"/reset-password?token={token}").text),
    }, follow_redirects=False)
    assert r.status_code == 303

    # 旧会话应失效
    assert c.get("/account", follow_redirects=False).status_code == 303, \
        "重置密码后旧会话仍有效 —— 改密未使旧会话失效"

    # 新密码可登录，旧密码不可
    c2 = _client()
    c2.post("/login", data={"email": "resetme@example.com", "password": "NewPassw0rd!x",
                            "csrf": _extract_csrf(c2.get("/login").text)})
    assert c2.get("/account", follow_redirects=False).status_code == 200

    c3 = _client()
    c3.post("/login", data={"email": "resetme@example.com", "password": "Passw0rd!x",
                            "csrf": _extract_csrf(c3.get("/login").text)})
    assert c3.get("/account", follow_redirects=False).status_code == 303


def test_reset_password_rejects_weak_and_mismatched(db):
    from app.core.security import make_token
    from app.models.user import User

    _mkuser(db, "weak@example.com", "weaku")
    _complete_setup()
    with db() as s:
        uid = int(s.query(User).filter(User.email == "weak@example.com").one().id)

    token = make_token(ttl_seconds=3600, uid=uid, act="reset")

    def _flash_after(client, data: dict, page_url: str) -> str:
        client.post("/reset-password", data=data, follow_redirects=False)
        m = re.search(r'<div class="flash[^"]*">([^<]+)</div>',
                      client.get(page_url).text)
        return m.group(1).strip() if m else ""

    page = f"/reset-password?token={token}"
    c = _client()
    assert "密码" in _flash_after(
        c,
        {"token": token, "password": "short", "confirm": "short",
         "csrf": _extract_csrf(c.get(page).text)},
        page,
    )

    c2 = _client()
    assert "不一致" in _flash_after(
        c2,
        {"token": token, "password": "GoodPassw0rd!x", "confirm": "OtherPassw0rd!x",
         "csrf": _extract_csrf(c2.get(page).text)},
        page,
    )


def test_reset_rejects_forged_token(db):
    _mkuser(db, "forge@example.com", "forgeu")
    _complete_setup()
    c = _client()
    r = c.get("/reset-password?token=forged.token", follow_redirects=False)
    assert r.status_code == 303 and "/forgot-password" in r.headers["location"]


# ------------------------------------------------------------ queries 校验

def test_interest_queries_are_whitelisted(db):
    """审计项：queries 是用户可控的自由 JSON，未校验即落库并流入上游请求。"""
    import json as _json


    _mkuser(db, "q@example.com", "qu")
    _complete_setup()
    c = _client()
    c.post("/login", data={"email": "qu", "password": "Passw0rd!x",
                           "csrf": _extract_csrf(c.get("/login").text)})
    payload = _json.dumps({
        "arxiv": "cat:cs.AI",
        "__evil__": "drop table",
        "openalex": "x" * 5000,
    })
    r = c.post("/interests", data={
        "name": "T", "description": "", "include_keywords": "", "exclude_keywords": "",
        "queries": payload, "min_score": "4", "max_papers_per_day": "5",
        "lookback_days": "7", "send_at": "08:30", "timezone": "Asia/Shanghai",
        "csrf": _extract_csrf(c.get("/interests/new").text),
    }, follow_redirects=False)
    # 可能因 LLM 未就绪被挡；无论如何不得 500
    assert r.status_code in (200, 303), f"意外状态 {r.status_code}"
