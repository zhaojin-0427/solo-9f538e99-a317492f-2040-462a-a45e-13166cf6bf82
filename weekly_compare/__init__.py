"""Application factory for the same-store weekly comparison tool."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from . import db
from .formatting import cents_to_yuan, cents_to_yuan_signed, pct_change
from .receipts import format_hours
from .services import display_timestamp, weekday_name


def _default_database_path() -> str:
    return str(Path(__file__).resolve().parent.parent / "data" / "reports.db")


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)

    app.config.from_mapping(
        DATABASE=os.environ.get("DATABASE_PATH") or _default_database_path(),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_BYTES", str(4 * 1024 * 1024))),
    )
    if test_config:
        app.config.update(test_config)

    # Keep Chinese store names readable in API responses (Flask >= 2.3 API).
    app.json.ensure_ascii = False

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    db.init_db(app.config["DATABASE"])

    app.jinja_env.filters["yuan"] = cents_to_yuan
    app.jinja_env.filters["yuan_signed"] = cents_to_yuan_signed
    app.jinja_env.filters["pct"] = pct_change
    app.jinja_env.filters["hours"] = format_hours
    app.jinja_env.filters["ts"] = display_timestamp
    app.jinja_env.filters["weekday"] = weekday_name

    from .routes import bp

    app.register_blueprint(bp)
    return app
