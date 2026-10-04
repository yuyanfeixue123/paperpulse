"""/admin/setup 六步引导。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text as sql

from app.core.config import get_settings, reload_settings
from app.core.db import SessionLocal
from app.core.security import encrypt_value, hash_password, password_strength_ok
from app.core.utils import utc_iso
from app.email.providers import PRESETS
from app.models.delivery import EmailProvider
from app.models.user import User
from app.setup import wizard
from app.sources.registry import load_all_specs, set_enabled, sync_sources_to_db
from app.web.deps import check_csrf, csrf_for, set_session
from app.web.templates import render

router = APIRouter()


def _require_unconfigured(request: Request):
    """未完成部署引导时，setup 端点只允许「已登录的站点管理员」或「完全未初始化」两种情形。

    修复的漏洞：此前六个 POST 端点零校验，攻击者填任意邮箱即可把该账号密码改掉
    并提权为管理员（接管），或改写全局 LLM / 邮件通道。
    现在：
    - 引导已完成 → 一律拒绝（引导是一次性动作）
    - 已有账号 → 必须携带管理员会话
    - 库为空（全新部署）→ 允许首位管理员自举
    """
    from app.web.deps import current_admin

    if wizard.setup_completed():
        return RedirectResponse("/admin", status_code=303)

    with SessionLocal() as session:
        user_count = int(session.execute(sql("SELECT COUNT(*) FROM users")).scalar() or 0)

    if user_count > 0 and current_admin(request) is None:
        return RedirectResponse("/login", status_code=303)
    return None


def _flash(request: Request, msg: str, kind: str = "") -> None:
    """设置一次性提示语。

    写在 request.state 上，由 flash_middleware 落到响应的 cookie ——
    因为重定向后是全新请求，state 不会跨请求存活。
    """
    request.state.flash = msg
    request.state.flash_kind = kind


@router.get("/admin/setup")
def setup_page(request: Request):
    # 引导完成后不再提供任何可写入口，直接去后台
    if wizard.setup_completed():
        from app.web.deps import current_admin

        return RedirectResponse("/admin" if current_admin(request) else "/login",
                                status_code=303)
    step = wizard.current_step()
    ctx = wizard.setup_context()
    ctx.update({"csrf": csrf_for(request)})
    if step >= 6 and not wizard.setup_completed():
        from app.setup.checks import run_all

        ctx["checks"] = run_all()
    elif step >= 6:
        return RedirectResponse("/admin", status_code=303)
    if step == 5:
        sync_sources_to_db()
        ctx["sources"] = load_all_specs()
    if step == 3:
        ctx["settings"] = get_settings()
    if step == 4:
        ctx["presets"] = PRESETS
    return render(request, f"setup/step{step}.html", **ctx)


@router.post("/admin/setup/1")
def setup_1(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    timezone: str = Form("Asia/Shanghai"),
    csrf: str = Form(""),
):
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/setup", status_code=303)
    ok, msg = password_strength_ok(password)
    if not ok:
        _flash(request, msg, "error")
        return RedirectResponse("/admin/setup", status_code=303)
    email = email.strip().lower()
    with SessionLocal() as session:
        user = session.query(User).filter(User.email == email).first()
        if user is None:
            user = User(
                email=email,
                password_hash=hash_password(password),
                display_name="admin",
                timezone=timezone,
                is_admin=True,
                is_active=True,
                email_verified=True,
                created_at=utc_iso(),
            )
            session.add(user)
        else:
            user.is_admin = True
            user.password_hash = hash_password(password)
        session.commit()
        uid = int(user.id)
    wizard.update(default_timezone=timezone)
    wizard.advance_to(2)
    resp = RedirectResponse("/admin/setup", status_code=303)
    set_session(resp, uid, request)
    return resp


@router.post("/admin/setup/2")
def setup_2(
    request: Request,
    site_name: str = Form("PaperPulse"),
    site_url: str = Form(""),
    timezone: str = Form("Asia/Shanghai"),
    csrf: str = Form(""),
):
    guard = _require_unconfigured(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/setup", status_code=303)
    wizard.update(
        site_name=site_name.strip() or "PaperPulse",
        site_url=site_url.strip().rstrip("/"),
        default_timezone=timezone,
    )
    reload_settings({"site": {"default_timezone": timezone}})
    wizard.advance_to(3)
    return RedirectResponse("/admin/setup", status_code=303)


@router.post("/admin/setup/3")
def setup_3(
    request: Request,
    provider: str = Form("openai_compatible"),
    base_url: str = Form(""),
    api_key: str = Form(""),
    model_score: str = Form(""),
    model_parse: str = Form(""),
    mode: str = Form("llm"),
    csrf: str = Form(""),
):
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/setup", status_code=303)

    if mode == "keyword":
        wizard.update(llm_mode="keyword")
        wizard.advance_to(4)
        return RedirectResponse("/admin/setup", status_code=303)

    import yaml

    from app.core.config import ROOT

    cfg_path = ROOT / "config" / "config.yaml"
    data: dict[str, Any] = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    data.setdefault("llm", {}).update(
        {
            "provider": provider,
            "base_url": base_url.strip(),
            "api_key": f"enc:{encrypt_value(api_key.strip())}" if api_key.strip() else "",
            "model_score": model_score.strip(),
            "model_parse": model_parse.strip() or model_score.strip(),
        }
    )
    cfg_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    reload_settings({"llm": data["llm"]})

    from app.llm.client import test_connection

    ok, msg = test_connection()
    if not ok:
        _flash(request, f"LLM 连接测试失败：{msg}", "error")
        return RedirectResponse("/admin/setup", status_code=303)
    wizard.update(llm_mode="llm")
    wizard.advance_to(4)
    return RedirectResponse("/admin/setup", status_code=303)


@router.post("/admin/setup/4")
def setup_4(
    request: Request,
    kind: str = Form("brevo"),
    api_key: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    host: str = Form(""),
    port: str = Form("587"),
    region: str = Form("us-east-1"),
    from_email: str = Form(""),
    daily_budget: str = Form("250"),
    csrf: str = Form(""),
):
    guard = _require_unconfigured(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/setup", status_code=303)
    cfg = dict(PRESETS.get(kind, {}))
    cfg.update(
        {
            "api_key": encrypt_value(api_key) if api_key else "",
            "username": username,
            "password": encrypt_value(password) if password else "",
            "from_email": from_email.strip(),
        }
    )
    if host:
        cfg["host"] = host
    if port:
        cfg["port"] = int(port)
    if region:
        cfg["region"] = region

    with SessionLocal() as session:
        row = session.get(EmailProvider, kind)
        if row is None:
            row = EmailProvider(key=kind)
        row.kind = kind
        row.role = "primary"
        row.config_json = json.dumps(
            {k: (f"enc:{v}" if k in ("api_key", "password") and v else v) for k, v in cfg.items()},
            ensure_ascii=False,
        )
        row.daily_budget = int(daily_budget or 250)
        row.enabled = 1
        row.priority = 0
        session.add(row)
        session.commit()

    wizard.advance_to(5)
    return RedirectResponse("/admin/setup", status_code=303)


@router.post("/admin/setup/5")
def setup_5(request: Request, enabled: list[str] = Form([]), csrf: str = Form("")):
    guard = _require_unconfigured(request)
    if guard:
        return guard
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/setup", status_code=303)
    for spec in load_all_specs():
        set_enabled(spec["key"], spec["key"] in enabled)
    wizard.advance_to(6)
    return RedirectResponse("/admin/setup", status_code=303)


@router.post("/admin/setup/6")
def setup_6(request: Request, csrf: str = Form("")):
    if not check_csrf(request, csrf):
        return RedirectResponse("/admin/setup", status_code=303)
    wizard.complete()
    return RedirectResponse("/admin", status_code=303)
