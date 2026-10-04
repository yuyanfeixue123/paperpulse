"""第三轮审查修复的回归测试。

覆盖：setup 窗口期、rollback IDOR、改密吊销、byok SSRF、
keyword 模式死锁、立刻推荐入队、stream 评分、管理员建用户。
"""

from __future__ import annotations

import itertools
import re

import pytest
from fastapi.testclient import TestClient

from tests.test_app import _client, _complete_setup, _extract_csrf


def _mkuser(
    db,
    email: str | None = None,
    username: str = "",
    password: str = "Passw0rd!x",
    admin: bool = False,
):
    """建用户。默认生成唯一邮箱 —— 测试库文件级共享，固定邮箱会跨文件冲突。"""
    from app.core.security import hash_password
    from app.core.utils import utc_iso
    from app.models.user import User

    n = _uniq()
    email = email or f"u{n}@example.com"
    with db() as s:
        s.add(User(email=email, username=username or None, is_admin=admin,
                   password_hash=hash_password(password), created_at=utc_iso()))
        s.commit()
    with db() as s:
        return int(s.query(User).filter(User.email == email).one().id)


def _mkinterest(db, user_id: int, name: str = "订阅A"):
    from app.core.utils import utc_iso
    from app.models.interest import Interest

    with db() as s:
        it = Interest(
            user_id=user_id, name=name, description="d",
            include_keywords_json="[]", exclude_keywords_json="[]",
            source_keys_json="[]", arxiv_categories_json="[]", queries_json="{}",
            created_at=utc_iso(),
        )
        s.add(it)
        s.commit()
    with db() as s:
        return int(s.get(Interest, it.id).id)


def _login(db, uid: int) -> TestClient:
    from app.models.user import User

    with db() as s:
        email = s.get(User, uid).email
    c = _client()
    c.post("/login", data={"email": email, "password": "Passw0rd!x",
                           "csrf": _extract_csrf(c.get("/login").text)})
    return c


_SEQ = itertools.count(1)


def _uniq() -> int:
    return next(_SEQ)


@pytest.fixture(autouse=True)
def _reset():
    from app.core.config import reload_settings
    from app.web.deps import _buckets

    _buckets.clear()
    yield
    _buckets.clear()
    reload_settings()


def _unconfigured(db) -> None:
    """把系统置为「引导未完成」且库中无用户 —— setup 的可自举窗口。"""
    from app.core.db import SessionLocal
    from app.models.system import SystemSettings
    from app.models.user import User

    with SessionLocal() as s:
        s.query(User).delete()
        s.commit()
    with SessionLocal() as s:
        row = s.get(SystemSettings, 1)
        if row is not None:
            row.setup_completed_at = None
            row.setup_step = 1
            s.commit()


# ------------------------------------------------------ setup 窗口期

def test_setup_1_blocked_after_completion(db):
    """回归：引导完成后 setup_1 仍无守卫 → 任何访客可 POST 它。

    攻击链：用攻击者自己的邮箱调 setup/1，库里没这个邮箱就新建
    一个 is_admin=True 的账号，并直接 set_session 拿到登录态。
    部署者会被跳过整个引导，全程无感知。
    """
    _mkuser(db, f"owner{_uniq()}@example.com", f"owner{_uniq()}", admin=True)
    _complete_setup()
    c = _client()
    r = c.post("/admin/setup/1", data={
        "email": "attacker@evil.test", "password": "Passw0rd!x",
        "timezone": "UTC", "csrf": "x",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "/admin" in r.headers["location"] or "/login" in r.headers["location"]

    from app.models.user import User

    with db() as s:
        assert s.query(User).filter(User.email == "attacker@evil.test").first() is None, \
            "setup_1 在引导完成后不得创建任何账号"


def test_setup_3_and_6_blocked_after_completion(db):
    """引导完成后 setup_3（改全局 LLM）/ setup_6（提前完成）也必须被拒。"""
    _mkuser(db, f"owner2{_uniq()}@example.com", f"owner2{_uniq()}", admin=True)
    _complete_setup()
    c = _client()
    for path in ("/admin/setup/3", "/admin/setup/6"):
        r = c.post(path, data={"csrf": "x", "api_key": "sk-evil", "base_url": "http://127.0.0.1"},
                   follow_redirects=False)
        assert r.status_code == 303, f"{path} 应被重定向而非执行"
        assert "/admin" in r.headers["location"] or "/login" in r.headers["location"]


def test_setup_1_blocks_existing_admin_password_reset(db):
    """回归：setup_1 会重置**已有管理员**的密码。

    拿到管理员邮箱的人无需知道密码即可改掉它、接管账号。
    现在改为：已是管理员的邮箱直接拒绝，提示走后台处理。
    """
    from app.core.db import SessionLocal
    from app.core.security import verify_password
    from app.models.system import SystemSettings
    from app.models.user import User

    boss_email = f"boss-{_uniq()}@example.com"
    boss_name = f"boss{_uniq()}"
    _mkuser(db, boss_email, boss_name, admin=True)
    # 置为「引导未完成」，但库里已有管理员 —— 这正是窗口期场景
    with SessionLocal() as s:
        row = s.get(SystemSettings, 1)
        if row is not None:
            row.setup_completed_at = None
            row.setup_step = 1
            s.commit()

    c = _client()
    c.post("/admin/setup/1", data={
        "email": boss_email, "password": "Hacked12345x",
        "timezone": "UTC", "csrf": _extract_csrf(c.get("/admin/setup").text),
    }, follow_redirects=False)

    with db() as s:
        row = s.query(User).filter(User.email == boss_email).one()
        assert verify_password("Hacked12345x", row.password_hash) is False, \
            "不得重置已有管理员的密码"
        assert verify_password("Passw0rd!x", row.password_hash) is True, \
            "原密码应仍然有效"


# -------------------------------------------------------- rollback IDOR

def test_rollback_rejects_other_users_interest(db):
    """回归：路由未传 user_id，rollback_to 的属主校验被整个跳过。"""
    from sqlalchemy import text

    from app.core.db import SessionLocal
    from app.models.digest import Feedback

    uid_a = _mkuser(db, f"ra{_uniq()}@example.com", f"ra{_uniq()}")
    uid_b = _mkuser(db, f"rb{_uniq()}@example.com", f"rb{_uniq()}")
    iid_b = _mkinterest(db, uid_b, "B的订阅")

    # 造一条 B 的修订历史
    with SessionLocal() as s:
        s.execute(
            text(
                "INSERT INTO interest_revisions (interest_id, from_version, to_version, "
                "patch_json, rationale, created_at) VALUES "
                "(:i, 1, 2, '{\"add_include\": [\"x\"]}', 'r', '2026-01-01')"
            ),
            {"i": iid_b},
        )
        s.execute(
            text("UPDATE interests SET version = 2 WHERE id = :i"), {"i": iid_b}
        )
        s.commit()

    c = _login(db, uid_a)
    csrf = _extract_csrf(c.get("/interests").text)
    r = c.post(f"/interests/{iid_b}/rollback",
               data={"version": "1", "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303

    with SessionLocal() as s:
        row = s.execute(
            text("SELECT version FROM interests WHERE id = :i"), {"i": iid_b}
        ).scalar()
        assert row == 2, "他人订阅的版本不得被回滚"
    assert Feedback is not None  # 仅避免未使用告警


# -------------------------------------------------- 改密吊销旧会话

def test_change_password_invalidates_other_sessions(db):
    """回归：update_password 未写 password_changed_at，旧会话仍可用。

    这是「密码泄露后改密」场景的核心保障，此前只有忘记密码流程写了。
    """
    import shutil

    from app.core.security import read_token
    from app.models.user import User

    uid = _mkuser(db, f"chpw{_uniq()}@example.com", f"chpw{_uniq()}")
    _complete_setup()

    # 设备 A：先登录，拿到会话 cookie
    device_a = _login(db, uid)
    assert device_a.get("/account", follow_redirects=False).status_code == 200
    token_a = device_a.cookies.get("pp_session")
    assert read_token(token_a) is not None

    # 设备 B：另起一个客户端（不同 cookie jar）
    from app.core.db import SessionLocal

    with SessionLocal() as s:
        email = s.get(User, uid).email
    device_b = _client()
    device_b.post("/login", data={"email": email, "password": "Passw0rd!x",
                                  "csrf": _extract_csrf(device_b.get("/login").text)})
    assert device_b.get("/account", follow_redirects=False).status_code == 200

    # 在设备 A 上改密
    csrf = _extract_csrf(device_a.get("/account").text)
    r = device_a.post("/account/password", data={
        "old_password": "Passw0rd!x", "new_password": "NewPassw0rd!x", "csrf": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303

    # 设备 B 的旧会话必须立即失效
    assert device_b.get("/account", follow_redirects=False).status_code == 303, \
        "改密后其他设备的会话仍有效 —— 吊销未生效"

    # 设备 A 重新签发了会话，仍可登录
    assert device_a.get("/account", follow_redirects=False).status_code == 200, \
        "改密设备自身应保持登录"

    # 新密码可登录，旧密码不可
    fresh = _client()
    fresh.post("/login", data={"email": email, "password": "NewPassw0rd!x",
                               "csrf": _extract_csrf(fresh.get("/login").text)})
    assert fresh.get("/account", follow_redirects=False).status_code == 200
    assert shutil is not None


# ------------------------------------------------------------ byok SSRF

def test_byok_rejects_private_base_url(db):
    """回归：/account/byok 未做 safe_base_url 校验，可把 LLM 请求指向内网。

    provider 会直接 POST {base_url}/chat/completions —— 盲 SSRF。
    """
    uid = _mkuser(db, f"ssrf{_uniq()}@example.com", f"ssrf{_uniq()}")
    _complete_setup()
    c = _login(db, uid)
    csrf = _extract_csrf(c.get("/account").text)
    r = c.post("/account/byok", data={
        "llm_base_url": "http://127.0.0.1:8000/v1",
        "llm_api_key": "sk-x", "llm_model": "m", "csrf": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303

    from app.models.user import User

    with db() as s:
        row = s.get(User, uid)
        assert row.llm_base_url != "http://127.0.0.1:8000/v1", \
            "内网地址不得被保存为 LLM 接口地址"


def test_byok_accepts_public_base_url(db):
    uid = _mkuser(db, f"ok{_uniq()}@example.com", f"okuser{_uniq()}")
    _complete_setup()
    c = _login(db, uid)
    csrf = _extract_csrf(c.get("/account").text)
    c.post("/account/byok", data={
        "llm_base_url": "https://api.deepseek.com/v1",
        "llm_api_key": "sk-x", "llm_model": "deepseek-chat", "csrf": csrf,
    }, follow_redirects=False)

    from app.models.user import User

    with db() as s:
        assert s.get(User, uid).llm_base_url == "https://api.deepseek.com/v1"


# ------------------------------------------------- keyword 模式不死锁

def test_keyword_mode_allows_subscription_flow(db):
    """回归：选了关键词模式，llm_gate 仍把用户拦去配 Key —— 与模式矛盾。"""
    from app.core.config import reload_settings
    from app.core.db import SessionLocal
    from app.models.system import SystemSettings

    uid = _mkuser(db, f"kw{_uniq()}@example.com", f"kwuser{_uniq()}")
    _complete_setup()
    with SessionLocal() as s:
        s.get(SystemSettings, 1).llm_mode = "keyword"
        s.commit()
    reload_settings({"llm": {"base_url": "", "api_key": ""}})
    try:
        c = _login(db, uid)
        r = c.get("/interests/new", follow_redirects=False)
        assert r.status_code == 200, "关键词模式下应能直接进入创建页"
        assert "/account/llm" not in r.headers.get("location", "")
    finally:
        with SessionLocal() as s:
            s.get(SystemSettings, 1).llm_mode = "llm"
            s.commit()


# ------------------------------------------------------ 立刻推荐入队

def test_recommend_now_enqueues_instead_of_blocking(db):
    """回归：曾在请求处理器里同步重建所有订阅摘要，多订阅用户会卡几分钟。"""
    from app.scheduler import runner

    uid = _mkuser(db, f"enq{_uniq()}@example.com", f"enq{_uniq()}")
    iid1 = _mkinterest(db, uid, "订阅一")
    iid2 = _mkinterest(db, uid, "订阅二")
    _complete_setup()

    c = _login(db, uid)
    csrf = _extract_csrf(c.get("/feed").text)
    r = c.post("/feed/recommend", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    # 应跳到带 running 参数的地址，让页面轮询
    assert "running=" in r.headers["location"]

    task_id = int(
        r.headers["location"].split("running=")[1].split("&")[0].split(" ")[0]
    )
    assert task_id > 0, "应入队成功"

    # enqueue 会顺带确保 handler 已注册（CLI 路径绕过 start_scheduler 时）
    assert "recommend_now" in runner.HANDLERS, "必须注册异步任务处理器"

    from app.core.db import SessionLocal
    from app.models.task import TaskRun

    with SessionLocal() as s:
        row = s.get(TaskRun, task_id)
        assert row is not None
        assert row.kind == "recommend_now"
        assert row.status in ("pending", "running", "done")

    # 同 payload 重复提交应被去重，不会产生第二个任务
    again = runner.enqueue(
        "recommend_now", {"user_id": uid, "interest_ids": [iid1, iid2]}
    )
    assert again is None, "同 payload 应去重"


def test_recommend_status_requires_login(db):
    _complete_setup()
    c = _client()
    r = c.get("/feed/status?task_id=1", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_recommend_status_does_not_leak_other_users_task(db):
    """不能靠 task_id 窥探他人任务状态。"""
    import json

    from app.core.db import SessionLocal
    from app.models.task import TaskRun

    uid_a = _mkuser(db, f"sa{_uniq()}@example.com", f"sa{_uniq()}")
    _complete_setup()
    with SessionLocal() as s:
        s.add(TaskRun(kind="recommend_now",
                      payload_json=json.dumps({"user_id": 999, "interest_ids": [1]}),
                      status="pending", scheduled_at="2026-01-01T00:00:00+00:00"))
        s.commit()
        other_id = int(s.query(TaskRun).order_by(TaskRun.id.desc()).first().id)

    c = _login(db, uid_a)
    r = c.get(f"/feed/status?task_id={other_id}")
    assert r.json().get("state") == "unknown", "他人任务状态不应可见"


# -------------------------------------------------------- stream 评分

def test_stream_has_rate_buttons(db):
    """需求：登录后首页是 /stream，评分不能只在 /feed。"""
    from tests.test_feed_and_quota import _mkscore

    uid = _mkuser(db, f"sr{_uniq()}@example.com", f"sr{_uniq()}")
    iid = _mkinterest(db, uid, "测试订阅")
    _mkscore(db, uid, iid, 801, 5, "Rateable in stream")
    _complete_setup()

    body = _login(db, uid).get("/stream").text
    assert "这条推荐准吗？" in body, "推荐流卡片应带评分按钮"
    assert 'action="/feed/rate"' in body


def test_stream_rating_records_feedback(db):
    from tests.test_feed_and_quota import _mkscore

    uid = _mkuser(db, f"sr2{_uniq()}@example.com", f"sr2{_uniq()}")
    iid = _mkinterest(db, uid, "测试订阅")
    pid = _mkscore(db, uid, iid, 802, 5, "Rate me here")
    _complete_setup()

    c = _login(db, uid)
    page = c.get("/stream").text
    csrf = _extract_csrf(page)
    c.post("/feed/rate", data={
        "paper_id": pid, "interest_id": iid, "rating": 5, "csrf": csrf,
    }, follow_redirects=False)

    from app.models.digest import Feedback

    with db() as s:
        fb = s.query(Feedback).filter(Feedback.paper_id == pid).one()
        assert fb.rating == 5 and fb.user_id == uid


# ------------------------------------------------------ 管理员建用户

def test_admin_can_create_user(db):
    admin_id = _mkuser(db, f"adm3-{_uniq()}@example.com", f"adm3{_uniq()}", admin=True)
    _complete_setup()
    c = _login(db, admin_id)
    csrf = _extract_csrf(c.get("/admin/users").text)
    r = c.post("/admin/users/create", data={
        "email": "newbie@corp.test", "username": "newbie",
        "display_name": "新人", "password": "Passw0rd!x",
        "timezone": "Asia/Shanghai", "csrf": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303

    from app.models.user import User

    with db() as s:
        u = s.query(User).filter(User.email == "newbie@corp.test").one()
        assert u.username == "newbie"
        assert u.is_admin is False
        assert u.is_active is True


def test_admin_user_page_has_create_form(db):
    """关闭注册后承诺了「管理员在用户管理中创建」，页面必须有该表单。"""
    admin_id = _mkuser(db, f"adm4-{_uniq()}@example.com", f"adm4{_uniq()}", admin=True)
    _complete_setup()
    body = _login(db, admin_id).get("/admin/users").text
    assert 'action="/admin/users/create"' in body
    assert "创建用户" in body


def test_admin_create_user_rejects_weak_password(db):
    admin_id = _mkuser(db, f"adm5-{_uniq()}@example.com", f"adm5{_uniq()}", admin=True)
    _complete_setup()
    c = _login(db, admin_id)
    csrf = _extract_csrf(c.get("/admin/users").text)
    c.post("/admin/users/create", data={
        "email": "weak@corp.test", "password": "short", "csrf": csrf,
    }, follow_redirects=False)

    from app.models.user import User

    with db() as s:
        assert s.query(User).filter(User.email == "weak@corp.test").first() is None


def test_non_admin_cannot_create_user(db):
    _mkuser(db, f"adm6-{_uniq()}@example.com", f"adm6{_uniq()}", admin=True)
    uid = _mkuser(db, f"normal-{_uniq()}@example.com", f"normal{_uniq()}")
    _complete_setup()
    c = _login(db, uid)
    r = c.post("/admin/users/create", data={
        "email": "sneaky@corp.test", "password": "Passw0rd!x", "csrf": "x",
    }, follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]

    from app.models.user import User

    with db() as s:
        assert s.query(User).filter(User.email == "sneaky@corp.test").first() is None


# ---------------------------------------------------------- 小遗留

def test_create_interest_clamps_params(db):
    """创建页此前不钳制参数（编辑页有 _clamp），可用超大篇数耗尽部署者预算。"""
    from app.core.config import reload_settings

    uid = _mkuser(db, f"clamp-{_uniq()}@example.com", f"clamp{_uniq()}")
    _complete_setup()

    from app.core.db import SessionLocal
    from app.models.system import SystemSettings

    with SessionLocal() as s:
        s.get(SystemSettings, 1).llm_mode = "keyword"
        s.commit()
    reload_settings({"llm": {"base_url": "", "api_key": ""}})
    try:
        c = _login(db, uid)
        csrf = _extract_csrf(c.get("/interests/new").text)
        r = c.post("/interests", data={
            "name": "越界测试", "description": "d", "include_keywords": "",
            "exclude_keywords": "", "queries": "{}",
            "min_score": "99", "max_papers_per_day": "9999", "lookback_days": "0",
            "send_at": "08:30", "timezone": "Asia/Shanghai", "csrf": csrf,
        }, follow_redirects=False)
        assert r.status_code in (200, 303)

        from app.models.interest import Interest

        with db() as s:
            row = s.query(Interest).order_by(Interest.id.desc()).first()
            assert row is not None
            assert row.max_papers_per_day == 50, f"未钳制：{row.max_papers_per_day}"
            assert row.min_score == 5
            assert row.lookback_days == 1
    finally:
        with SessionLocal() as s:
            s.get(SystemSettings, 1).llm_mode = "llm"
            s.commit()


def test_feed_source_label_is_user_friendly(db):
    """「摘要 #123（日期）」中的内部 id 对用户没有信息量。"""
    uid = _mkuser(db, f"lbl-{_uniq()}@example.com", f"lbl{_uniq()}")
    _mkinterest(db, uid, "订阅X")
    _complete_setup()
    body = _login(db, uid).get("/feed").text
    assert "摘要 #" not in body


def test_stream_title_has_no_empty_href(db):
    """doi 与 url 都为空时不应渲染 href=""（点击会跳回本页）。"""
    from app.core.utils import utc_iso
    from app.models.paper import Paper
    from app.models.score import LlmScore
    from tests.test_feed_and_quota import _mkscore

    uid = _mkuser(db, f"nohref-{_uniq()}@example.com", f"nohref{_uniq()}")
    iid = _mkinterest(db, uid, "订阅Y")
    _mkscore(db, uid, iid, 901, 5, "Normal paper")

    # 一篇既无 doi 也无 url 的论文
    from app.core.db import SessionLocal

    with SessionLocal() as s:
        s.add(Paper(id=999, dedup_key="t:no-link-paper", source_key="t",
                    source_id="x", doi=None, arxiv_id=None,
                    title="Paper without any link", abstract="abs",
                    authors_json="[]", venue="", url="",
                    published_at="2026-10-01T00:00:00+00:00", first_seen_at=utc_iso()))
        s.add(LlmScore(paper_id=999, interest_id=iid, interest_version=1,
                       score=4, reason="r", model="m", created_at=utc_iso()))
        s.commit()

    _complete_setup()
    body = _login(db, uid).get("/stream").text
    assert "Paper without any link" in body
    assert 'href=""' not in body, "无链接论文不应产生空 href"


def test_stream_interest_names_with_comma(db):
    """订阅名含逗号时不能被 GROUP_CONCAT 拆碎。"""
    from tests.test_feed_and_quota import _mkscore

    uid = _mkuser(db, f"comma-{_uniq()}@example.com", f"comma{_uniq()}")
    iid = _mkinterest(db, uid, "城市, 交通, 治理")
    _mkscore(db, uid, iid, 902, 5, "Comma paper")
    _complete_setup()

    body = _login(db, uid).get("/stream").text
    assert "城市, 交通, 治理" in body, "含逗号的订阅名应完整显示"


def test_no_dead_branch_in_feed():
    """回归：曾有一行三元表达式两个分支字符串完全相同。"""
    from pathlib import Path

    from app.web.routes import feed as feed_mod

    src = Path(feed_mod.__file__).read_text(encoding="utf-8")
    # 形如  "x" if cond else "x"
    assert not re.search(r'"([^"]+)"\s+if\s+.+\s+else\s+"\1"', src), \
        "存在两个分支相同的三元表达式"

