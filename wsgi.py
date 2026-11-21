"""WSGI entry point used by gunicorn (``gunicorn wsgi:app``)."""

from __future__ import annotations

from weekly_compare import create_app

app = create_app()
