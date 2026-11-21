"""Minimal CSRF protection (no third-party dependency).

Every HTML form embeds the session token, and every POST must echo it back as a
form field or as an ``X-CSRF-Token`` header.  Tokens are compared in constant
time.  Scripted/API clients can obtain a token (and the matching session cookie)
from ``GET /api/csrf`` first.
"""

from __future__ import annotations

import hmac
import secrets

from flask import abort, request, session

SESSION_KEY = "_csrf_token"
FORM_FIELD = "csrf_token"
HEADER = "X-CSRF-Token"


def get_token() -> str:
    token = session.get(SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_KEY] = token
    return token


def validate_request() -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    expected = session.get(SESSION_KEY) or ""
    supplied = request.form.get(FORM_FIELD) or request.headers.get(HEADER) or ""
    if not expected or not hmac.compare_digest(str(expected), str(supplied)):
        abort(400, description="CSRF 校验失败，请刷新页面后重试。")
