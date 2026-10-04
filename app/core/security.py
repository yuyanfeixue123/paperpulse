"""argon2id 哈希、Fernet 加解密、HMAC token。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

_ph = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_strength_ok(password: str) -> tuple[bool, str]:
    if len(password) < 10:
        return False, "密码至少 10 位"
    if not any(c.islower() for c in password) or not any(c.isupper() for c in password):
        return False, "密码需包含大小写字母"
    if not any(c.isdigit() for c in password):
        return False, "密码需包含数字"
    return True, ""


# 允许使用弱密钥的环境：本地开发与测试。生产环境一律 fail-closed。
_WEAK_ENV = {"development", "test", "testing"}


def _is_production() -> bool:
    """是否处于生产模式。

    注意 `run_mode: lite` 本身就是文档推荐的生产形态，因此**不能**拿它当
    「非生产」的判据。只有显式声明 PAPERPULSE_ENV=development / test
    才允许弱密钥；其余一律按生产处理（安全默认）。

    注意：这里**不能**调用 get_settings() —— 它要解密配置里的凭据，
    而解密又需本函数判断环境，构成无限递归。故只读环境变量。
    """
    explicit = os.environ.get("PAPERPULSE_ENV", "").strip().lower()
    return explicit not in _WEAK_ENV


def _insecure_secret() -> bytes:
    return b"paperpulse-insecure-dev-secret"


def require_secret() -> str:
    """取 SECRET_KEY，缺失或过弱则拒绝启动。

    修复的漏洞：此前缺密钥会静默回退到可公开计算的常量，
    导致 HMAC 签名形同无签名 —— 任何人伪造 uid=1 即成管理员。
    """
    value = os.environ.get("PAPERPULSE_SECRET_KEY", "")
    if value.strip():
        return value
    if not _is_production():
        return ""
    raise RuntimeError(
        "PAPERPULSE_SECRET_KEY 未设置，拒绝启动。"
        "生成：python -c \"import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())\""
    )


def _fernet() -> Fernet:
    key = os.environ.get("PAPERPULSE_ENCRYPTION_KEY", "")
    if key.strip():
        return Fernet(key)
    if not _is_production():
        # 开发环境：随机生成一次性密钥（本进程内稳定，重启后旧密文失效）
        if not hasattr(_fernet, "_dev_key"):
            _fernet._dev_key = Fernet.generate_key()  # type: ignore[attr-defined]
        return _fernet._dev_key  # type: ignore[attr-defined]
    raise RuntimeError(
        "PAPERPULSE_ENCRYPTION_KEY 未设置，拒绝启动。"
        "凭据加密依赖它，缺失等于明文存储。"
        "生成：python -c \"import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())\""
    )


def encrypt_value(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_value(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def _secret() -> bytes:
    value = os.environ.get("PAPERPULSE_SECRET_KEY", "")
    if value.strip():
        return value.encode()
    if not _is_production():
        return _insecure_secret()
    raise RuntimeError("PAPERPULSE_SECRET_KEY 未设置，拒绝启动")


def make_token(ttl_seconds: int | None = None, **payload: Any) -> str:
    """HMAC 签名 token：payload 明文 + 签名，免登录链接用。

    `ttl_seconds` 用于「安全敏感」链接（重置密码）显式指定短窗口。
    缺省走 `PAPERPULSE_TOKEN_TTL_DAYS`（默认 30 天）。
    注意：早先的实现会**无条件**覆盖调用方传入的 `exp`，导致重置密码
    链接实际有效期长达 30 天 —— 链接一旦泄漏即等同于永久夺号。
    """
    if ttl_seconds is None:
        ttl_seconds = int(os.environ.get(
            "PAPERPULSE_TOKEN_TTL_DAYS", "30"
        )) * 86400
    payload["exp"] = int(time.time()) + int(ttl_seconds)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def read_token(token: str) -> dict[str, Any] | None:
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    pad = "=" * (-len(body) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(body + pad))
    except Exception:
        return None
    if data.get("exp", 0) < int(time.time()):
        return None
    return data


def sign_session(user_id: int, pwd_at: str = "") -> str:
    """会话 token。pwd_at 是改密时间戳，校验时比对，改密后旧会话立即失效。"""
    return make_token(uid=user_id, t=int(time.time()), pv=pwd_at or "")


def session_valid(token: str) -> dict[str, Any] | None:
    """读会话并校验改密时间。"""
    data = read_token(token)
    if not data:
        return None
    try:
        from app.core.db import SessionLocal
        from app.models.user import User

        with SessionLocal() as session:
            user = session.get(User, int(data["uid"]))
            if user is None or not user.is_active:
                return None
            # 改密时间不一致 => token 是旧会话
            if (data.get("pv") or "") != (user.password_changed_at or ""):
                return None
    except Exception:  # noqa: BLE001 校验失败一律视为未登录
        return None
    return data


def csrf_token(session_id: str) -> str:
    return hmac.new(_secret(), f"csrf:{session_id}".encode(), hashlib.sha256).hexdigest()[:32]


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
