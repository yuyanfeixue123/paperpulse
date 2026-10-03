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


def _fernet() -> Fernet:
    key = os.environ.get("PAPERPULSE_ENCRYPTION_KEY", "")
    if not key:
        # 开发/测试环境回退：从 SECRET_KEY 派生，保证跨进程稳定
        seed = os.environ.get("PAPERPULSE_SECRET_KEY", "paperpulse-dev-secret")
        key = base64.urlsafe_b64encode(hashlib.sha256(seed.encode()).digest()).decode()
    return Fernet(key)


def encrypt_value(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_value(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def _secret() -> bytes:
    return os.environ.get("PAPERPULSE_SECRET_KEY", "paperpulse-dev-secret").encode()


def make_token(**payload: Any) -> str:
    """HMAC 签名 token：payload 明文 + 签名，免登录链接用。"""
    payload["exp"] = int(time.time()) + 60 * 60 * 24 * 180
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


def sign_session(user_id: int) -> str:
    return make_token(uid=user_id, t=int(time.time()))


def csrf_token(session_id: str) -> str:
    return hmac.new(_secret(), f"csrf:{session_id}".encode(), hashlib.sha256).hexdigest()[:32]


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
