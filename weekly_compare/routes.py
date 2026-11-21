"""HTTP routes: import form, report listing and the comparison page."""

from __future__ import annotations

from datetime import date

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import comparison, db, security, services
from .errors import AppError, ImportRejected, NotFound

bp = Blueprint("main", __name__)


def _db_path() -> str:
    return current_app.config["DATABASE"]


def _wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


@bp.app_errorhandler(AppError)
def _handle_app_error(err: AppError):
    if _wants_json():
        return jsonify({"ok": False, "error": err.to_dict()}), err.http_status
    return (
        render_template(
            "error.html",
            title="操作未完成",
            code=err.code,
            message=err.message,
            details=err.details,
        ),
        err.http_status,
    )


@bp.app_errorhandler(404)
def _handle_404(err):
    if _wants_json():
        return jsonify({"ok": False, "error": {"code": "not_found", "message": "页面不存在。"}}), 404
    return render_template("error.html", title="页面不存在", code="not_found",
                           message="页面不存在。", details=[]), 404


@bp.app_errorhandler(413)
def _handle_413(err):
    limit = current_app.config["MAX_CONTENT_LENGTH"] // 1024 // 1024
    message = f"上传内容超过 {limit} MB 上限。"
    if _wants_json():
        return jsonify({"ok": False, "error": {"code": "payload_too_large", "message": message}}), 413
    return render_template("error.html", title="文件过大", code="payload_too_large",
                           message=message, details=[]), 413


@bp.app_errorhandler(400)
def _handle_400(err):
    message = getattr(err, "description", None) or "请求无效。"
    if _wants_json():
        return jsonify({"ok": False, "error": {"code": "bad_request", "message": message}}), 400
    return render_template("error.html", title="请求无效", code="bad_request",
                           message=message, details=[]), 400


@bp.before_request
def _check_csrf():
    security.validate_request()


@bp.app_context_processor
def _inject_globals():
    return {
        "csrf_token": security.get_token(),
        "today": date.today().isoformat(),
    }


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@bp.get("/")
def index():
    """Daily-report list plus the two entry points (import / compare)."""

    filters = {
        "business_date": request.args.get("date", "").strip(),
        "code_query": request.args.get("q", "").strip(),
    }
    filter_error = None
    business_date = None
    if filters["business_date"]:
        try:
            business_date = services.parse_date(filters["business_date"]).isoformat()
        except ImportRejected as exc:
            filter_error = exc.message
            filters["business_date"] = ""

    try:
        reports = services.list_current_reports(
            _db_path(),
            business_date=business_date,
            code_query=filters["code_query"] or None,
            limit=100,
        )
    except ImportRejected as exc:  # pragma: no cover - defensive
        reports, filter_error = [], exc.message

    stores = services.list_stores(_db_path())
    return render_template(
        "index.html",
        title="日报与同店对比",
        reports=reports,
        stores=stores,
        filters=filters,
        filter_error=filter_error,
    )


@bp.get("/import")
def import_form():
    prefill = {
        "store_code": request.args.get("store_code", "").strip(),
        "store_name": "",
        "business_date": request.args.get("business_date", "").strip(),
        "revision": "",
        "complete_hours": "",
        "receipt_text": "",
    }
    if prefill["store_code"] and prefill["business_date"]:
        existing = services.get_current_report(
            _db_path(), prefill["store_code"], prefill["business_date"]
        )
        if existing is not None:
            prefill["store_name"] = existing.store_name
            prefill["revision"] = str(existing.revision + 1)
    return render_template(
        "import.html",
        title="导入日报",
        prefill=prefill,
        errors=[],
        stores=services.list_stores(_db_path()),
    )


@bp.post("/import")
def import_submit():
    store_code = request.form.get("store_code", "")
    store_name = request.form.get("store_name", "")
    business_date = request.form.get("business_date", "")
    revision = request.form.get("revision", "")
    complete_hours = request.form.get("complete_hours", "")

    upload = request.files.get("receipts")
    receipt_text = request.form.get("receipt_text", "")
    if upload is not None and upload.filename:
        receipts_csv = upload.read()
    else:
        receipts_csv = receipt_text.encode("utf-8")

    submitted = {
        "store_code": store_code,
        "store_name": store_name,
        "business_date": business_date,
        "revision": revision,
        "complete_hours": complete_hours,
        "receipt_text": receipt_text,
    }

    try:
        result = services.import_daily_report(
            _db_path(),
            store_code=store_code,
            store_name=store_name,
            business_date=business_date,
            revision=revision,
            complete_hours=complete_hours,
            receipts_csv=receipts_csv,
        )
    except AppError as err:
        return (
            render_template(
                "import.html",
                title="导入日报",
                prefill=submitted,
                errors=[err.message],
                error_code=err.code,
                stores=services.list_stores(_db_path()),
            ),
            err.http_status,
        )

    if result.replaced_revision is not None:
        flash(
            f"已用修订号 {result.revision} 整日替换 {result.store.code} "
            f"{result.business_date} 的原日报（原修订号 {result.replaced_revision}）。",
            "success",
        )
    else:
        flash(
            f"已导入 {result.store.code} {result.business_date} 日报（修订号 {result.revision}）。",
            "success",
        )
    return redirect(
        url_for(
            "main.report_detail",
            store_code=result.store.code,
            business_date=result.business_date,
        )
    )


@bp.get("/reports/<store_code>/<business_date>")
def report_detail(store_code: str, business_date: str):
    try:
        normalised_date = services.parse_date(business_date).isoformat()
    except ImportRejected as exc:
        raise NotFound(exc.message) from exc
    found = services.get_report_with_hours(_db_path(), store_code, normalised_date)
    if found is None:
        raise NotFound(f"没有找到 {store_code} 在 {normalised_date} 的已生效日报。")
    report, hours = found
    return render_template(
        "report.html",
        title=f"{report.store_code} {report.business_date} 日报",
        report=report,
        hours=hours,
    )


@bp.get("/compare")
def compare_page():
    date_a = request.args.get("date_a", "")
    date_b = request.args.get("date_b", "")
    result = None
    error = None
    if date_a or date_b:
        try:
            result = comparison.compare(_db_path(), date_a, date_b)
            # Show the normalised (earlier-first) pair so the form matches the
            # table and "差额" always means later minus earlier.
            date_a = result.date_a.isoformat()
            date_b = result.date_b.isoformat()
        except AppError as exc:
            error = exc.message
    return render_template(
        "compare.html",
        title="同店对比（两个周一）",
        date_a=date_a,
        date_b=date_b,
        result=result,
        error=error,
    )


@bp.get("/api/health")
def health():
    ok = db.healthy(_db_path())
    return jsonify({"ok": ok}), (200 if ok else 503)


@bp.get("/api/csrf")
def api_csrf():
    """Hand a scripted client the token plus the session cookie that owns it."""

    return jsonify({"ok": True, "csrf_token": security.get_token()})


@bp.get("/api/compare")
def api_compare():
    date_a = request.args.get("date_a", "")
    date_b = request.args.get("date_b", "")
    result = comparison.compare(_db_path(), date_a, date_b)
    return jsonify(
        {
            "ok": True,
            "date_a": result.date_a.isoformat(),
            "date_b": result.date_b.isoformat(),
            "weekday": result.weekday,
            "stores": [
                {
                    "store_id": row.store_id,
                    "store_code": row.store_code,
                    "store_name": row.store_name,
                    "revision_a": row.revision_a,
                    "revision_b": row.revision_b,
                    "comparable_hours": row.comparable_hours,
                    "sales_a_cents": row.sales_a_cents,
                    "sales_b_cents": row.sales_b_cents,
                    "difference_cents": row.difference_cents,
                }
                for row in result.rows
            ],
            "total": {
                "comparable_hours": result.total.comparable_hours,
                "sales_a_cents": result.total.sales_a_cents,
                "sales_b_cents": result.total.sales_b_cents,
                "difference_cents": result.total.difference_cents,
            },
        }
    )


@bp.get("/api/reports")
def api_reports():
    business_date = request.args.get("business_date", "").strip() or None
    store_code = request.args.get("store_code", "").strip()
    if store_code and business_date:
        report = services.get_current_report(_db_path(), store_code, business_date)
        reports = [report] if report is not None else []
    else:
        reports = services.list_current_reports(
            _db_path(), business_date=business_date, limit=200
        )
    return jsonify(
        {
            "ok": True,
            "reports": [
                {
                    "store_code": r.store_code,
                    "store_name": r.store_name,
                    "business_date": r.business_date,
                    "weekday": r.weekday,
                    "revision": r.revision,
                    "complete_hour_count": r.complete_hour_count,
                    "total_sales_cents": r.total_sales_cents,
                    "receipt_line_count": r.receipt_line_count,
                    "updated_at": r.updated_at,
                }
                for r in reports
            ],
        }
    )


@bp.post("/api/import")
def api_import():
    upload = request.files.get("receipts")
    if upload is not None and upload.filename:
        receipts_csv = upload.read()
    else:
        receipts_csv = request.form.get("receipt_text", "").encode("utf-8")

    result = services.import_daily_report(
        _db_path(),
        store_code=request.form.get("store_code", ""),
        store_name=request.form.get("store_name", ""),
        business_date=request.form.get("business_date", ""),
        revision=request.form.get("revision", ""),
        complete_hours=request.form.get("complete_hours", ""),
        receipts_csv=receipts_csv,
    )
    return (
        jsonify(
            {
                "ok": True,
                "store_code": result.store.code,
                "store_name": result.store.name,
                "business_date": result.business_date,
                "revision": result.revision,
                "replaced_revision": result.replaced_revision,
                "complete_hours": sorted(result.complete_hours),
                "total_sales_cents": result.total_sales_cents,
                "receipt_line_count": result.receipt_line_count,
            }
        ),
        201,
    )
