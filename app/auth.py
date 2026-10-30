from __future__ import annotations

import os
import secrets
from datetime import datetime
from functools import wraps
from typing import Callable

from fastapi import HTTPException, Request
from passlib.context import CryptContext

from .db import connect, init_db

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _default_users() -> list[tuple[str, str, str]]:
    return [
        ("admin", os.getenv("ADMIN_PASSWORD", "admin123"), "admin"),
        ("viewer", os.getenv("VIEWER_PASSWORD", "view123"), "viewer"),
    ]


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def ensure_users() -> None:
    init_db()
    with connect() as conn:
        for username, password, role in _default_users():
            row = conn.execute(
                "SELECT id, password_hash FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                    (username, hash_password(password), role),
                )
                continue
            # Env passwords win: update hash when ADMIN_PASSWORD / VIEWER_PASSWORD change.
            if not verify_password(password, row["password_hash"]):
                conn.execute(
                    "UPDATE users SET password_hash = ?, role = ? WHERE id = ?",
                    (hash_password(password), role, row["id"]),
                )


def authenticate(username: str, password: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def get_current_user(request: Request) -> dict | None:
    return request.session.get("user")


def client_ip(request: Request) -> str:
    """线上跑在 Render 后面，直连 IP 是代理，真实 IP 在 X-Forwarded-For 第一段。"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def record_login(request: Request, username: str, success: bool) -> None:
    # 记录失败不能影响登录本身
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO login_history (username, success, ip, user_agent, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    username[:64] or None,
                    1 if success else 0,
                    client_ip(request)[:128] or None,
                    request.headers.get("user-agent", "")[:256] or None,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
    except Exception:  # noqa: BLE001
        pass


def list_logins(limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT username, success, ip, user_agent, created_at
            FROM login_history ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def require_login(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def require_admin(request: Request) -> dict:
    user = require_login(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def login_required(view: Callable):
    @wraps(view)
    async def wrapper(request: Request, *args, **kwargs):
        if not get_current_user(request):
            from fastapi.responses import RedirectResponse

            return RedirectResponse(url="/login", status_code=303)
        return await view(request, *args, **kwargs)

    return wrapper


def new_session_secret() -> str:
    env = os.getenv("SESSION_SECRET", "").strip()
    if env:
        return env
    from .db import DATA_DIR

    path = DATA_DIR / "session_secret.txt"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    path.write_text(secret, encoding="utf-8")
    return secret


def is_production() -> bool:
    return bool(os.getenv("DATABASE_URL") or os.getenv("RENDER") or os.getenv("PRODUCTION"))
