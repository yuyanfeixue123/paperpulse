"""邮件通道：brevo / resend / ses / smtp / postfix。

统一接口 send(msg) -> message_id。
- brevo / resend 走 HTTP API
- ses / smtp / postfix 走 SMTP（ses 用 AWS 的 SMTP 端点，避免引入 boto3）
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any, Protocol, cast

from app.core.http import limited_post
from app.core.logging import get_logger

log = get_logger(__name__)


class OutgoingMessage:
    def __init__(
        self,
        *,
        to_email: str,
        subject: str,
        html: str,
        text: str,
        from_email: str,
        from_name: str = "PaperPulse",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.to_email = to_email
        self.subject = subject
        self.html = html
        self.text = text
        self.from_email = from_email
        self.from_name = from_name
        self.headers = headers or {}


class EmailProvider(Protocol):
    def send(self, msg: OutgoingMessage) -> str: ...


class SmtpProvider:
    """通用 SMTP。config: host / port / username / password / use_tls / use_ssl"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def send(self, msg: OutgoingMessage) -> str:
        cfg = self.config
        host = cfg.get("host", "localhost")
        port = int(cfg.get("port", 587))
        use_ssl = bool(cfg.get("use_ssl", False))
        use_tls = bool(cfg.get("use_tls", not use_ssl))

        em = EmailMessage()
        em["Subject"] = msg.subject
        em["From"] = f"{msg.from_name} <{msg.from_email}>"
        em["To"] = msg.to_email
        for k, v in msg.headers.items():
            em[k] = v
        em.set_content(msg.text)
        em.add_alternative(msg.html, subtype="html")

        server: smtplib.SMTP
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        try:
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()
            username = cfg.get("username", "")
            if username:
                server.login(username, cfg.get("password", ""))
            server.send_message(em)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
        return em["Message-ID"] or ""


class BrevoProvider:
    """Brevo REST API：POST https://api.brevo.com/v3/smtp/email"""

    API = "https://api.brevo.com/v3/smtp/email"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._smtp: SmtpProvider | None = None

    def send(self, msg: OutgoingMessage) -> str:
        api_key = self.config.get("api_key", "")
        if not api_key:
            # 无 API Key 时回落到 SMTP relay
            if self._smtp is None:
                self._smtp = SmtpProvider(
                    {
                        "host": self.config.get("host", "smtp-relay.brevo.com"),
                        "port": int(self.config.get("port", 587)),
                        "username": self.config.get("username", ""),
                        "password": self.config.get("password", ""),
                        "use_tls": True,
                    }
                )
            return self._smtp.send(msg)

        payload = {
            "sender": {"name": msg.from_name, "email": msg.from_email},
            "to": [{"email": msg.to_email}],
            "subject": msg.subject,
            "htmlContent": msg.html,
            "textContent": msg.text,
            "headers": msg.headers,
        }
        resp = limited_post(
            self.API,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json_body=payload,
            timeout=30,
        )
        data = resp.json()
        return str(data.get("messageId", ""))


class ResendProvider:
    API = "https://api.resend.com/emails"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def send(self, msg: OutgoingMessage) -> str:
        payload = {
            "from": f"{msg.from_name} <{msg.from_email}>",
            "to": [msg.to_email],
            "subject": msg.subject,
            "html": msg.html,
            "text": msg.text,
            "headers": msg.headers,
        }
        resp = limited_post(
            self.API,
            headers={
                "Authorization": f"Bearer {self.config.get('api_key','')}",
                "Content-Type": "application/json",
            },
            json_body=payload,
            timeout=30,
        )
        data = resp.json()
        return str(data.get("id", ""))


class SesProvider(SmtpProvider):
    """AWS SES 走其 SMTP 端点：email-smtp.{region}.amazonaws.com:587"""

    def __init__(self, config: dict[str, Any]) -> None:
        region = config.get("region", "us-east-1")
        super().__init__(
            {
                "host": config.get("host", f"email-smtp.{region}.amazonaws.com"),
                "port": int(config.get("port", 587)),
                "username": config.get("username", ""),
                "password": config.get("password", ""),
                "use_tls": True,
            }
        )


class PostfixProvider(SmtpProvider):
    """本机 Postfix 直发（兜底，默认关闭）：127.0.0.1:25，无认证。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        cfg.setdefault("host", "127.0.0.1")
        cfg.setdefault("port", 25)
        cfg.setdefault("use_tls", False)
        cfg.setdefault("use_ssl", False)
        cfg.setdefault("username", "")
        cfg.setdefault("password", "")
        super().__init__(cfg)


KINDS = {
    "brevo": BrevoProvider,
    "resend": ResendProvider,
    "ses": SesProvider,
    "smtp": SmtpProvider,
    "postfix": PostfixProvider,
}

PRESETS = {
    "brevo": {
        "kind": "brevo",
        "host": "smtp-relay.brevo.com",
        "port": 587,
        "api_key": "",
        "daily_budget": 250,
        "note": "免费 300 封/天（含品牌角标）",
    },
    "resend": {
        "kind": "resend",
        "api_key": "",
        "daily_budget": 100,
        "note": "免费 3,000 封/月，无角标",
    },
    "ses": {
        "kind": "ses",
        "region": "us-east-1",
        "username": "",
        "password": "",
        "daily_budget": 1000,
        "note": "按量 $0.10/1000，需自行设置额度",
    },
    "smtp": {
        "kind": "smtp",
        "host": "",
        "port": 587,
        "username": "",
        "password": "",
        "use_tls": True,
        "daily_budget": 250,
        "note": "自建或第三方 SMTP",
    },
    "postfix": {
        "kind": "postfix",
        "host": "127.0.0.1",
        "port": 25,
        "daily_budget": 200,
        "note": "本机直发，仅当 25 端口未被封禁时启用",
    },
}


def build_provider(kind: str, config: dict[str, Any]) -> EmailProvider:
    cls = KINDS.get(kind)
    if cls is None:
        raise ValueError(f"未支持的邮件通道：{kind}")
    return cast(EmailProvider, cls(config))
