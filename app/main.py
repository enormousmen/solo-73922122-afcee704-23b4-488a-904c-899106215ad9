"""植被调查离线修订合并网页：FastAPI 入口。"""

import os
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db
from .csvio import dump_csv, parse_csv
from .merge import FIELD_LABELS, FIELDS, build_final_actions, compute_plan
from .uniqueness import DuplicateError

BASE_DIR = Path(__file__).resolve().parent
SEED_CSV = os.environ.get("INITIAL_CSV", str(BASE_DIR.parent / "data" / "initial_records.csv"))

app = FastAPI(title="植被调查离线修订合并")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["FIELD_LABELS"] = FIELD_LABELS
templates.env.globals["WEB_INSTANCE"] = os.environ.get("WEB_INSTANCE", "")


@app.on_event("startup")
def startup():
    db.init_db(SEED_CSV)


def redirect(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        sep = "&" if "?" in url else "?"
        from urllib.parse import quote

        url = f"{url}{sep}msg={quote(msg)}"
    return RedirectResponse(url, status_code=303)


def parse_count(raw: str) -> int:
    try:
        count = int(raw)
    except (TypeError, ValueError):
        raise ValueError("株数必须是非负整数")
    if count < 0:
        raise ValueError("株数必须是非负整数")
    return count


# ---------------- 首页／当前记录 ----------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str = ""):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "records": db.get_records(),
            "baselines": db.list_baselines(),
            "previews": db.list_previews(),
            "enabled": db.is_enabled(),
            "msg": msg,
        },
    )


@app.post("/records/add")
def record_add(rid: str = Form(...), quadrat: str = Form(...), species: str = Form(...), count: str = Form(...)):
    rid, quadrat, species = rid.strip(), quadrat.strip(), species.strip()
    try:
        n = parse_count(count)
        if not rid or not quadrat or not species:
            raise ValueError("ID、样方、物种均不能为空")
        db.add_record(rid, quadrat, species, n)
        return redirect("/", f"已新增记录 {rid}")
    except DuplicateError as e:
        return redirect("/", str(e))
    except Exception as e:
        return redirect("/", f"新增失败：{e}")


@app.post("/records/{rid}/update")
def record_update(rid: str, quadrat: str = Form(...), species: str = Form(...), count: str = Form(...)):
    try:
        n = parse_count(count)
        if not quadrat.strip() or not species.strip():
            raise ValueError("样方、物种不能为空")
        if not db.update_record(rid, quadrat.strip(), species.strip(), n):
            raise ValueError(f"记录 {rid} 不存在")
        return redirect("/", f"已更新记录 {rid}")
    except DuplicateError as e:
        return redirect("/", str(e))
    except Exception as e:
        return redirect("/", f"更新失败：{e}")


@app.post("/records/batch-update")
async def records_batch_update(request: Request):
    """整批人工修改：单事务提交，按整批最终状态做唯一性检查。

    允许同批交换两条记录的物种（中间重复、最终不重复）；
    最终状态重复则整批拒绝、不改变任何记录，回首页展示相关 ID。
    """
    form = await request.form()
    try:
        only = form.get("single")  # “仅保存本行”：只处理该 ID
        changes = {}
        # 字段名形如 r_<ID>_quadrat / r_<ID>_species / r_<ID>_count；
        # 用前缀+后缀切分，兼容 ID 本身含下划线的情况
        for key in form:
            if not key.startswith("r_"):
                continue
            for field in ("quadrat", "species", "count"):
                suffix = f"_{field}"
                if key.endswith(suffix):
                    rid = key[2 : -len(suffix)]
                    break
            else:
                continue
            if only and rid != only:
                continue
            changes.setdefault(rid, {})[field] = form[key]
        if not changes:
            return redirect("/", "没有需要保存的修改")
        normalized = {}
        for rid, vals in changes.items():
            if "quadrat" not in vals or "species" not in vals or "count" not in vals:
                raise ValueError(f"记录 {rid} 的修改数据不完整")
            quadrat, species = str(vals["quadrat"]).strip(), str(vals["species"]).strip()
            if not quadrat or not species:
                raise ValueError(f"记录 {rid} 的样方、物种不能为空")
            normalized[rid] = {
                "quadrat": quadrat,
                "species": species,
                "count": parse_count(vals["count"]),
            }
        db.batch_update_records(normalized)
        return redirect("/", f"已整批保存 {len(normalized)} 条修改")
    except DuplicateError as e:
        return redirect("/", str(e))
    except Exception as e:
        return redirect("/", f"整批保存失败：{e}")


@app.post("/records/{rid}/delete")
def record_delete(rid: str):
    if db.delete_record(rid):
        return redirect("/", f"已删除记录 {rid}")
    return redirect("/", f"记录 {rid} 不存在")


# ---------------- 唯一性检查与启用 ----------------


@app.get("/uniqueness", response_class=HTMLResponse)
def uniqueness_page(request: Request, msg: str = ""):
    enabled = db.is_enabled()
    # 页面检查走只读快照：不持 SQLite 写锁，期间维护与合并照常进行
    groups = [] if enabled else db.snapshot_duplicate_groups()
    return templates.TemplateResponse(
        request,
        "uniqueness.html",
        {
            "request": request,
            "enabled": enabled,
            "enabled_at": db.enabled_at(),
            "groups": groups,
            "msg": msg,
        },
    )


@app.post("/uniqueness/enable")
def uniqueness_enable():
    result = db.enable_uniqueness()
    if result["ok"]:
        if result["already"]:
            return redirect("/uniqueness", "唯一性约束此前已启用，本次为重复操作，状态不变")
        return redirect("/uniqueness", "唯一性约束已启用：此后人工增改与离线合并均按整批最终状态检查")
    # 有重复：未启用，数据与状态均未改变
    return redirect(
        "/uniqueness",
        "启用被拒绝：以下重复组必须先整理为同一样方+同一物种仅一条汇总记录，"
        "启用状态与数据均未改变",
    )


# ---------------- 导出 / 基线 ----------------


@app.get("/export.csv")
def export_current():
    return PlainTextResponse(
        dump_csv(db.get_records()),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=current_records.csv"},
    )


@app.post("/baselines")
def baseline_create(name: str = Form("")):
    records = db.get_records()
    bid = db.save_baseline(name.strip() or f"基线（{db.now()}）", records)
    return redirect(f"/baselines/{bid}", "基线已保存，可下载对应 CSV 交给外业队")


@app.get("/baselines/{bid}", response_class=HTMLResponse)
def baseline_detail(request: Request, bid: int, msg: str = ""):
    baseline = db.get_baseline(bid)
    if not baseline:
        return redirect("/", "基线不存在")
    return templates.TemplateResponse(
        request, "baseline.html", {"request": request, "baseline": baseline, "msg": msg}
    )


@app.get("/baselines/{bid}/csv")
def baseline_csv(bid: int):
    baseline = db.get_baseline(bid)
    if not baseline:
        return redirect("/", "基线不存在")
    return PlainTextResponse(
        dump_csv(baseline["data"]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=baseline_{bid}.csv"},
    )


# ---------------- 上传修订 / 合并预览 ----------------


@app.post("/merge/upload")
async def merge_upload(baseline_id: int = Form(...), file: UploadFile = File(...)):
    baseline = db.get_baseline(baseline_id)
    if not baseline:
        return redirect("/", "所选基线不存在")
    raw = (await file.read()).decode("utf-8-sig")
    try:
        revision = parse_csv(raw)
    except ValueError as e:
        return redirect("/", f"修订文件解析失败：{e}")

    current = db.get_records()
    plan = compute_plan(baseline["data"], revision, current)
    snapshot = db.get_records_by_ids(plan["involved"])
    token = uuid.uuid4().hex[:12]
    db.save_preview(token, baseline_id, revision, snapshot, plan)
    return redirect(f"/previews/{token}")


@app.get("/previews/{token}", response_class=HTMLResponse)
def preview_detail(request: Request, token: str, msg: str = ""):
    pv = db.get_preview(token)
    if not pv:
        return redirect("/", "预览不存在")
    baseline = db.get_baseline(pv["baseline_id"])
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "request": request,
            "pv": pv,
            "baseline": baseline,
            "plan": pv["plan"],
            "FIELDS": FIELDS,
            "enabled": db.is_enabled(),
            "msg": msg,
        },
    )


@app.post("/previews/{token}/discard")
def preview_discard(token: str):
    pv = db.get_preview(token)
    if pv and pv["status"] == "pending":
        db.set_preview_status(token, "discarded", "已放弃")
    return redirect("/", "已放弃该合并预览")


@app.post("/previews/{token}/confirm")
async def preview_confirm(token: str, request: Request):
    pv = db.get_preview(token)
    if not pv:
        return redirect("/", "预览不存在")
    if pv["status"] != "pending":
        return redirect("/", "该预览已处理，不能重复确认")

    # 1) 过期检测：仅核对计划触及的记录；无关记录变化不影响确认
    fresh = db.get_records_by_ids(pv["plan"]["involved"])
    if fresh != pv["snapshot"]:
        baseline = db.get_baseline(pv["baseline_id"])
        current = db.get_records()
        plan = compute_plan(baseline["data"], pv["revision"], current)
        snapshot = db.get_records_by_ids(plan["involved"])
        db.update_preview_plan(
            token, snapshot, plan, "拟改记录在预览后又被修改或删除，旧预览已作废并按最新当前记录重新比较，请重新核对。"
        )
        return redirect(f"/previews/{token}")

    # 2) 汇总人工裁决
    form = await request.form()
    resolutions = {}
    for conf in pv["plan"]["conflicts"]:
        rid = conf["id"]
        if conf["ctype"] == "fields":
            sel = {}
            for fc in conf["fields"]:
                v = form.get(f"c_{rid}_{fc['field']}")
                if v in ("rev", "cur"):
                    sel[fc["field"]] = v
            if sel:
                resolutions[rid] = sel
        else:
            v = form.get(f"c_{rid}__")
            if v in ("rev", "cur"):
                resolutions[rid] = {"_": v}

    # 3) 合成最终动作
    try:
        actions = build_final_actions(pv["plan"], resolutions)
    except ValueError as e:
        return redirect(f"/previews/{token}", str(e))

    # 4) 单事务应用；启用唯一性时按整批最终状态检查，
    #    重复则整体回滚、预览保持“待确认”，不标为已应用
    try:
        db.apply_actions(actions)
    except DuplicateError as e:
        return redirect(f"/previews/{token}", str(e))
    except Exception as e:
        return redirect(f"/previews/{token}", f"应用失败，当前数据未改变：{e}")

    db.set_preview_status(token, "applied", f"已应用 {len(actions)} 项离线改动")
    return redirect("/", f"合并完成：已应用 {len(actions)} 项离线改动")
