"""终端配置向导 —— 无浏览器环境下的图形化配置。

服务器上通过 SSH 部署时，Caddy/HTTPS 尚未就绪、Web 后台不可访问，
本模块提供与 Web 引导等价的 6 步终端向导，支持断点续配。

零第三方依赖：只用 ANSI 转义序列绘制，不引入 curses / prompt_toolkit，
以保证 SSH、串口、重连场景下都能正常工作。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from getpass import getpass
from typing import Any

# ---------- ANSI 绘制 ----------

_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str) -> str:
    return f"\033[{code}m" if _TTY else ""


BOLD = "1"
DIM = "2"
GREEN = "32"
YELLOW = "33"
RED = "31"
CYAN = "36"


def _w(code: str, text: str) -> str:
    """把文本包上 ANSI 属性。"""
    return f"{_c(code)}{text}{_c('0')}" if _TTY else text

WIDTH = 68


def rule(title: str = "") -> None:
    if title:
        pad = WIDTH - len(title) - 4
        print(f"{_c(CYAN)}┌─ {_w(BOLD, title)} {'─' * max(pad, 0)}┐{_c('0')}")
    else:
        print(f"{_c(CYAN)}{'─' * WIDTH}{_c('0')}")


def line(text: str = "", indent: int = 2) -> None:
    print(f"{' ' * indent}{text}")


def ok(msg: str) -> None:
    print(f"{_c(GREEN)}  ✓{_c('0')} {msg}")


def warn(msg: str) -> None:
    print(f"{_c(YELLOW)}  !{_c('0')} {msg}")


def fail(msg: str) -> None:
    print(f"{_c(RED)}  ✗{_c('0')} {msg}")


def info(msg: str) -> None:
    print(f"{_c(DIM)}  ·{_c('0')} {msg}")


def step_bar(current: int, total: int = 6) -> None:
    labels = ["管理员", "站点", "LLM", "邮件", "数据源", "完成"]
    parts = []
    for i, name in enumerate(labels, 1):
        if i < current:
            parts.append(f"{_c(GREEN)}✓{name}{_c('0')}")
        elif i == current:
            parts.append(f"{_c(BOLD)}{_c(CYAN)}▶ {name}{_c('0')}")
        else:
            parts.append(f"{_c(DIM)}· {name}{_c('0')}")
    print(f"\n{'  '.join(parts)}")
    rule()
    print()


# ---------- 输入辅助 ----------


def _read_line(prompt: str, secret: bool) -> str:
    """TTY 下密码不回显；非 TTY（管道 / 自动化）退回普通读，避免 getpass 阻塞。"""
    if secret and sys.stdin.isatty():
        return getpass(prompt)
    return input(prompt)


def ask(
    prompt: str,
    default: str = "",
    required: bool = False,
    secret: bool = False,
) -> str:
    """required=True 时空输入会重问，直到拿到非空值（或有默认值可用）。"""
    hint = f" {_c(DIM)}[{default}]{_c('0')}" if default and not secret else ""
    suffix = f" {_c(DIM)}(不回显){_c('0')}" if secret and sys.stdin.isatty() else ""
    while True:
        try:
            raw = _read_line(f"  {prompt}{hint}{suffix}: ", secret).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit(1) from None
        if not raw and default:
            return default
        if raw:
            return raw
        if not required:
            return ""


def ask_int(prompt: str, default: int, minimum: int | None = None) -> int:
    """数值输入：非法或越界时重问，不让 ValueError 冒到顶层。"""
    while True:
        raw = ask(prompt, str(default), required=True)
        try:
            value = int(raw)
        except ValueError:
            fail("请输入整数")
            continue
        if minimum is not None and value < minimum:
            fail(f"不能小于 {minimum}")
            continue
        return value


def ask_choice(prompt: str, options: list[tuple[str, str]], default_idx: int = 0) -> str:
    """options: [(key, label)]，返回被选中的 key。"""
    print(f"\n  {_w(BOLD, prompt)}{_c('0')}")
    width = max(len(k) for k, _ in options)
    for i, (key, label) in enumerate(options, 1):
        mark = f"{_c(CYAN)}❯{_c('0')}" if i - 1 == default_idx else " "
        print(f"   {mark} {_w(BOLD, key.ljust(width))}{_c('0')}  {label}")
    while True:
        raw = ask("请选择", str(default_idx + 1))
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        for key, _ in options:
            if raw == key:
                return key
        warn("无效选项，请输入序号或标识")


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = ask(f"{prompt} {_c(DIM)}({hint}){_c('0')}", "").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "是")


def confirm(prompt: str) -> bool:
    return ask_yes_no(f"{_c(RED)}{prompt}{_c('0')}", default=False)


# ---------- 步骤实现 ----------


def _step_admin() -> None:
    from app.core.db import SessionLocal
    from app.core.security import hash_password, password_strength_ok
    from app.core.utils import utc_iso
    from app.models.user import User
    from app.setup import wizard

    existing = []
    with SessionLocal() as s:
        for u in s.query(User).order_by(User.id).all():
            tag = "（管理员）" if u.is_admin else ""
            existing.append((u.email, f"{u.display_name}{tag}"))
    if existing:
        info("已存在账号：")
        for email, label in existing:
            line(f"{_c(DIM)}·{_c('0')} {email}  {_c(DIM)}{label}{_c('0')}")

    email = ask("管理员邮箱", default=existing[0][0] if existing else "", required=True)
    pwd = ask("密码（不回显）", secret=True, required=True)
    pwd2 = ask("再输一次", secret=True, required=True)
    if pwd != pwd2:
        fail("两次输入不一致")
        return
    good, msg = password_strength_ok(pwd)
    if not good:
        fail(msg)
        return
    tz = ask("时区", default="Asia/Shanghai")

    with SessionLocal() as s:
        user = s.query(User).filter(User.email == email.lower()).first()
        if user is None:
            user = User(
                email=email.lower(),
                password_hash=hash_password(pwd),
                display_name=email.split("@")[0],
                created_at=utc_iso(),
            )
            s.add(user)
        user.password_hash = hash_password(pwd)
        user.timezone = tz
        user.is_admin = True
        user.is_active = True
        user.email_verified = True
        s.commit()
    wizard.update(default_timezone=tz)
    wizard.advance_to(2)
    ok(f"管理员 {email} 已就绪")


LLM_PRESETS: list[tuple[str, str]] = [
    ("deepseek", "DeepSeek        api.deepseek.com/v1"),
    ("qwen", "通义千问         dashscope.aliyuncs.com/compatible-mode/v1"),
    ("moonshot", "Moonshot        api.moonshot.cn/v1"),
    ("zhipu", "智谱 GLM        open.bigmodel.cn/api/paas/v4"),
    ("siliconflow", "硅基流动         api.siliconflow.cn/v1"),
    ("openai", "OpenAI          api.openai.com/v1"),
    ("anthropic", "Anthropic      api.anthropic.com"),
    ("gemini", "Gemini         generativelanguage.googleapis.com"),
    ("ollama", "Ollama 本地     127.0.0.1:11434"),
    ("custom", "自定义           手动填写 base_url"),
]

_PROVIDER_OF = {
    "deepseek": "openai_compatible",
    "qwen": "openai_compatible",
    "moonshot": "openai_compatible",
    "zhipu": "openai_compatible",
    "siliconflow": "openai_compatible",
    "openai": "openai_compatible",
    "anthropic": "anthropic",
    "gemini": "gemini",
    "ollama": "ollama",
    "custom": "openai_compatible",
}

_URL_OF = {
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "ollama": "http://127.0.0.1:11434",
}


def _step_llm() -> bool:
    """返回是否启用 LLM。"""
    import yaml

    from app.core.config import ROOT, reload_settings
    from app.setup import wizard

    while True:
        choice = ask_choice("选择 LLM 厂商", LLM_PRESETS, default_idx=0)
        if choice == "skip":
            if confirm("确认跳过 LLM？订阅将只能使用关键词模式，中文描述召回会明显受限"):
                wizard.update(llm_mode="keyword")
                ok("已设为关键词模式（可稍后在后台补配）")
                return False
            warn("已取消")
            continue

        base = ask("Base URL", default=_URL_OF.get(choice, ""), required=True)
        key = ask("API Key（不回显）", secret=True, required=True)
        model = ask("模型名", required=True)

        cfg_path = ROOT / "config" / "config.yaml"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        llm = data.setdefault("llm", {})
        llm.update(
            {
                "provider": _PROVIDER_OF[choice],
                "base_url": base.strip(),
                "api_key": _encrypt_or_plain(key),
                "model_score": model.strip(),
                "model_parse": model.strip(),
            }
        )
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        try:
            cfg_path.chmod(0o600)
        except OSError:
            pass
        reload_settings({"llm": llm})

        info("正在测试连接 …")
        from app.llm.client import test_connection

        good, msg = test_connection()
        if good:
            wizard.update(llm_mode="llm")
            ok(f"LLM 就绪：{msg[:70]}")
            return True
        fail(f"连接失败：{msg[:110]}")
        if not ask_yes_no("重新配置？", default=True):
            wizard.update(llm_mode="keyword")
            return False


def _encrypt_or_plain(value: str) -> str:
    from app.core.security import encrypt_value

    return f"enc:{encrypt_value(value)}" if value else ""


MAIL_PRESETS: list[tuple[str, str]] = [
    ("brevo", "Brevo        免费 300 封/天（含品牌角标）"),
    ("resend", "Resend       免费 3000 封/月，无角标"),
    ("ses", "AWS SES      按量 $0.10/1000（走 SMTP 端点）"),
    ("smtp", "自建 SMTP    任意第三方或自建中继"),
    ("postfix", "本机 Postfix 直发（兜底，默认不建议）"),
]


def _step_mail() -> bool:
    from app.core.db import SessionLocal
    from app.core.utils import dumps
    from app.email.providers import PRESETS
    from app.models.delivery import EmailProvider
    from app.setup import wizard

    kind = ask_choice("选择邮件通道", MAIL_PRESETS, default_idx=0)
    raw_preset: dict[str, Any] = dict(PRESETS[kind])

    from_email = ask("发信地址（须与发信域名一致）", required=True)
    api_key = ask("API Key（Brevo / Resend，不回显）", secret=True, required=False)
    username = ask("SMTP 用户名（SES / SMTP）", required=False)
    password = ask("SMTP 密码（不回显）", secret=True, required=False)
    host = ask("SMTP 主机", default=str(raw_preset.get("host") or ""), required=False)
    port = ask_int("SMTP 端口", int(raw_preset.get("port") or 587), minimum=1)
    region = ask("AWS 区域（SES）", default=str(raw_preset.get("region") or "us-east-1"), required=False)
    budget = ask_int("日额度", int(raw_preset.get("daily_budget") or 250), minimum=1)

    cfg: dict[str, Any] = dict(raw_preset)
    cfg.update({"from_email": from_email.strip(), "username": username, "host": host, "region": region})
    if port:
        cfg["port"] = int(port)
    if api_key.strip():
        cfg["api_key"] = _encrypt_or_plain(api_key.strip())
    if password.strip():
        cfg["password"] = _encrypt_or_plain(password.strip())

    with SessionLocal() as s:
        row = s.get(EmailProvider, kind)
        if row is None:
            row = EmailProvider(key=kind)
        row.kind = kind
        row.role = "primary"
        row.config_json = dumps(cfg)
        row.daily_budget = budget
        row.priority = 0
        row.enabled = 1
        s.add(row)
        s.commit()
    ok(f"通道 {kind} 已保存")

    target = ask("发送测试邮件到", required=True)
    from app.pipeline.deliver import send_test_email

    info("正在发送 …")
    good, msg = send_test_email(target.strip())
    if good:
        ok(f"测试邮件已发送：{msg[:70]}")
        wizard.advance_to(5)
        return True
    fail(f"发送失败：{msg[:140]}")
    if ask_yes_no("重新配置通道？", default=True):
        return _step_mail()
    return False


def _step_sources() -> None:
    from app.setup import wizard
    from app.sources.registry import has_credential, load_all_specs, set_enabled, sync_sources_to_db

    sync_sources_to_db()
    specs = load_all_specs()
    need_key = [s for s in specs if s["requires_key"] and not has_credential(s["key"])]
    plain = [s for s in specs if not s["requires_key"]]

    info(f"免注册源 {len(plain)} 个（可直接启用），需 Key 源 {len(need_key)} 个（默认关闭）")
    print()
    for s in specs:
        mark = f"{_c(GREEN)}✓{_c('0')}" if s["enabled"] else f"{_c(DIM)}·{_c('0')}"
        auth = f"{_c(YELLOW)}需 Key{_c('0')}" if s["requires_key"] else f"{_c(DIM)}免注册{_c('0')}"
        line(f"{mark} {_c(BOLD)}{s['key']:<22}{_c('0')} {_c(DIM)}{s['field']:<18}{_c('0')} {auth}")
    print()

    want_all = ask_yes_no("启用全部免注册源？", default=True)
    if want_all:
        for s in plain:
            set_enabled(s["key"], True)
        ok(f"已启用 {len(plain)} 个免注册源")
    else:
        keys = ask("逐个启用（逗号分隔的 key）", required=True)
        chosen = {k.strip() for k in keys.split(",") if k.strip()}
        for s in plain:
            set_enabled(s["key"], s["key"] in chosen)
        ok(f"已启用 {len(chosen)} 个源")

    for s in need_key:
        if not ask_yes_no(f"为 {s['key']} 配置 Key？", default=False):
            continue
        value = ask(f"{s['key']} 的凭据（不回显）", secret=True, required=True)
        from app.sources.registry import save_credential

        save_credential(s["key"], value.strip())
        set_enabled(s["key"], True)
        ok(f"{s['key']} 已启用")
    wizard.advance_to(6)


def _step_finalize() -> bool:
    from app.setup import wizard

    print(f"  {_c(BOLD)}系统自检{_c('0')}\n")
    from app.setup.checks import run_all

    results = run_all()
    for name, r in results.items():
        mark = f"{_c(GREEN)}✓{_c('0')}" if r["ok"] else f"{_c(YELLOW)}!{_c('0')}"
        line(f"{mark} {name:<12} {_c(DIM)}{_short(r['detail'])}{_c('0')}")
    print()
    blocking = [n for n, r in results.items() if not r["ok"] and n in ("邮件通道", "磁盘")]
    if blocking:
        warn(f"以下项目需要关注：{'、'.join(blocking)}")

    if confirm("确认完成配置并启用系统？"):
        wizard.complete()
        ok("配置完成，系统已启用")
        return True
    info("已保存，可稍后重新运行 `python -m app.cli setup` 继续")
    return False


def _short(detail: Any) -> str:
    if isinstance(detail, dict):
        vals = [str(v) for v in detail.values()]
        return " · ".join(vals)[:60]
    return str(detail)[:60]


# ---------- 主流程 ----------


def _step_site() -> None:
    from app.core.config import reload_settings
    from app.setup import wizard

    name = ask("站点名称", default="PaperPulse", required=True)
    url = ask("对外 URL（含 https://）", required=True)
    if not url.startswith(("http://", "https://")):
        warn("URL 建议以 https:// 开头（邮件中的退订与反馈链接依赖它）")
    tz = ask("默认时区", default="Asia/Shanghai")
    wizard.update(site_name=name, site_url=url.strip().rstrip("/"), default_timezone=tz)
    reload_settings({"site": {"default_timezone": tz}})
    ok("站点信息已保存")
    wizard.advance_to(3)


STEPS: list[tuple[str, Callable[[], Any]]] = [
    ("管理员账号", _step_admin),
    ("站点信息", _step_site),
    ("LLM 接入", _step_llm),
    ("邮件通道", _step_mail),
    ("数据源确认", _step_sources),
    ("完成", _step_finalize),
]


def run_wizard(start_step: int | None = None) -> int:
    from app.setup import wizard

    if wizard.setup_completed() and not confirm("系统已完成配置，仍要重新运行向导吗？"):
        return 0

    step = start_step or wizard.current_step()
    if step < 1:
        step = 1

    print()
    rule("PaperPulse 配置向导")
    line(_w(DIM, "终端版 · 与 Web 引导等价，支持断点续配"))
    line()
    line(_w(DIM, "随时按 Ctrl-C 退出，已完成的步骤不会丢失。"))

    for idx in range(step - 1, len(STEPS)):
        title, fn = STEPS[idx]
        step_bar(idx + 1)
        line(f"{_c(BOLD)}{title}{_c('0')}")
        print()
        result = fn()
        if result is False and title in ("LLM 接入", "邮件通道"):
            warn("该步骤未完成，后续可重新运行本向导补齐")

    print()
    rule()
    line(f"{_c(GREEN)}全部完成。{_c('0')} 启动服务：")
    line(f"  {_c(BOLD)}systemctl enable --now paperpulse{_c('0')}   或   {_c(BOLD)}make run{_c('0')}")
    line(f"  然后访问 {_c(BOLD)}https://<你的域名>/{_c('0')} 即可注册使用")
    rule()
    print()
    return 0


def main() -> int:
    try:
        return run_wizard()
    except SystemExit:
        return 1
