from __future__ import annotations

import json
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from jinja2 import Environment, FileSystemLoader

from .aggregate import build_report, load_cache, save_cache
from .auth import (
    authenticate,
    ensure_users,
    get_current_user,
    is_production,
    new_session_secret,
    require_admin,
)
from .db import UPLOAD_DIR, ensure_dirs, init_db
from .importer import delete_month, ensure_month, guess_month, import_file, list_months

ROOT = Path(__file__).resolve().parent
# Python 3.14 + Jinja2 默认 cache key 含 dict，会报错；关闭 bytecode cache
TEMPLATES = Jinja2Templates(env=Environment(
    loader=FileSystemLoader(str(ROOT / "templates")),
    autoescape=True,
    cache_size=0,
))

app = FastAPI(title="双店月度经营报告")
app.add_middleware(
    SessionMiddleware,
    secret_key=new_session_secret(),
    session_cookie="dualstore_session",
    same_site="lax",
    https_only=is_production(),
    max_age=60 * 60 * 24 * 14,
)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.on_event("startup")
def on_startup() -> None:
    ensure_dirs()
    init_db()
    ensure_users()


def _user(request: Request):
    return get_current_user(request)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _user(request):
        return RedirectResponse("/", status_code=303)
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"error": None},
    )


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    user = authenticate(username, password)
    if not user:
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"error": "用户名或密码错误"},
            status_code=400,
        )
    request.session["user"] = {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
    }
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["role"] == "admin":
        return RedirectResponse("/admin", status_code=303)
    return RedirectResponse("/report", status_code=303)


@app.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        report = load_cache()
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(
            f"<h3>报告生成失败</h3><pre>{e}</pre><p><a href='/admin'>回后台</a>可先点「强制重算报告缓存」</p>",
            status_code=500,
        )
    return TEMPLATES.TemplateResponse(
        request,
        "report.html",
        {
            "user": user,
            "P_json": json.dumps(report, ensure_ascii=False),
            "period_label": report.get("period_label") or "暂无数据",
            "generated_at": report.get("generated_at") or "",
        },
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["role"] != "admin":
        return RedirectResponse("/report", status_code=303)
    months = list_months()
    return TEMPLATES.TemplateResponse(
        request,
        "admin.html",
        {
            "user": user,
            "months": months,
            "message": request.query_params.get("msg"),
            "error": request.query_params.get("err"),
        },
    )


@app.post("/admin/months")
async def admin_create_month(request: Request, ym: str = Form(...)):
    require_admin(request)
    ym = ym.strip()
    if not (len(ym) == 7 and ym[4] == "-"):
        return RedirectResponse("/admin?err=月份格式应为YYYY-MM", status_code=303)
    ensure_month(ym)
    return RedirectResponse(f"/admin?msg=已创建月份 {ym}", status_code=303)


@app.post("/admin/upload")
async def admin_upload(
    request: Request,
    ym: str = Form(...),
    store: str = Form(""),
    files: list[UploadFile] = File(...),
):
    require_admin(request)
    ym = ym.strip()
    ensure_month(ym)
    results = []
    errors = []
    for f in files:
        if not f.filename:
            continue
        original_name = Path(f.filename).name
        suffix = Path(original_name).suffix.lower()
        if suffix not in {".xlsx", ".xls"}:
            errors.append(f"{original_name}: 仅支持 Excel")
            continue
        raw = await f.read()
        # 用原文件名落盘，便于识别类型/门店；避免 Path.unlink(ignore_errors=...) 兼容问题
        tmp_dir = Path(tempfile.mkdtemp(prefix="dualstore_up_"))
        tmp_path = tmp_dir / original_name
        tmp_path.write_bytes(raw)
        try:
            store_arg = store.strip() or None
            if store_arg == "auto":
                store_arg = None
            info = import_file(
                tmp_path,
                ym=ym,
                store=store_arg,
                uploaded_by=_user(request)["username"],
                replace=True,
            )
            info["filename"] = original_name
            results.append(info)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{original_name}: {e}")
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            try:
                tmp_dir.rmdir()
            except OSError:
                pass

    if errors and not results:
        return RedirectResponse(
            "/admin?err=" + quote("; ".join(errors)[:200], safe=""),
            status_code=303,
        )
    msg = f"成功导入 {len(results)} 个文件"
    if errors:
        msg += "；部分失败: " + "; ".join(errors)
    for info in results:
        if info.get("warnings"):
            msg += "；" + "；".join(info["warnings"])
            break
    return RedirectResponse(
        "/admin?msg=" + quote(msg[:220], safe=""),
        status_code=303,
    )


@app.post("/admin/rebuild")
async def admin_rebuild_all(request: Request):
    require_admin(request)
    try:
        save_cache(build_report())
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(
            "/admin?err=" + quote(f"重算失败: {e}"[:200], safe=""),
            status_code=303,
        )
    return RedirectResponse(
        "/admin?msg=" + quote("已重算报告缓存", safe=""),
        status_code=303,
    )


@app.post("/admin/months/{ym}/rebuild")
async def admin_rebuild(request: Request, ym: str):
    require_admin(request)
    save_cache(build_report())
    return RedirectResponse(f"/admin?msg=已重算报告（含 {ym}）", status_code=303)


@app.post("/admin/months/{ym}/delete")
async def admin_delete(request: Request, ym: str):
    require_admin(request)
    delete_month(ym)
    return RedirectResponse(f"/admin?msg=已删除 {ym}", status_code=303)


@app.post("/admin/seed")
async def admin_seed(request: Request):
    require_admin(request)
    from .seed import seed_existing

    results = seed_existing()
    ok = sum(1 for r in results if "error" not in r)
    err = sum(1 for r in results if "error" in r)
    return RedirectResponse(
        f"/admin?msg=种子导入完成：成功 {ok}，失败 {err}",
        status_code=303,
    )


@app.get("/api/report.json")
async def api_report(request: Request):
    if not _user(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    return JSONResponse(load_cache())


@app.get("/export/html")
async def export_html(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    report = load_cache()
    html = TEMPLATES.get_template("report.html").render(
        request=request,
        user=user,
        P_json=json.dumps(report, ensure_ascii=False),
        period_label=report.get("period_label") or "",
        generated_at=report.get("generated_at") or "",
        export_mode=True,
    )
    filename = f"双店月度对比报告_{report.get('period_label', 'export')}.html".replace(
        " ", ""
    )
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@app.get("/api/guess-month")
async def api_guess_month(filename: str):
    return {"ym": guess_month(filename)}
