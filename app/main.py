"""CasePilot web API + UI.

Run:  uv run uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import bedrock, config, db, llm, scenarios
from app.agents import orchestrator
from app.sandbox import policy, store

logging.basicConfig(level = logging.INFO, format = "%(levelname)s %(name)s %(message)s")
MAX_UPLOAD = 10 * 1024 * 1024


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scenarios.ensure()
    yield


app = FastAPI(title = "CasePilot", lifespan = lifespan)
app.mount("/static", StaticFiles(directory = str(config.STATIC_DIR)), name = "static")


class Decision(BaseModel):
    approve: bool
    approver: str = "Supervisor"
    comment: str = ""


async def _save_uploads(case_id: int, files: list[UploadFile]) -> None:
    folder = config.UPLOAD_DIR / str(case_id)
    folder.mkdir(parents = True, exist_ok = True)
    for f in files:
        if not f.filename:
            continue
        data = await f.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, f"{f.filename} is larger than 10 MB")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", f.filename)[:120]
        path = folder / name
        path.write_bytes(data)
        db.add_attachment(case_id, f.filename, str(path), f.content_type or "application/octet-stream", len(data))


# --- Pages ------------------------------------------------------------------------------

@app.get("/")
def desk() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/portal")
def portal() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "portal.html")


@app.get("/api/status")
def status() -> dict:
    out = {"llm": llm.status(), "autopilot": config.AUTOPILOT,
           "limits": {"auto_refund": config.AUTO_REFUND_LIMIT, "auto_credit": config.AUTO_CREDIT_LIMIT}}
    if config.BEDROCK_ENABLED:
        out["bedrock_spend"] = bedrock.spend()
    return out


# --- Cases ------------------------------------------------------------------------------

@app.get("/api/cases")
def list_cases() -> list[dict]:
    rows = db.list_cases()
    for r in rows:
        r["running"] = orchestrator.is_running(r["id"])
        r["attachments"] = len(db.attachments(r["id"]))
    return rows


@app.post("/api/cases")
async def create_case(subject: str = Form(...), description: str = Form(...), customer_email: str = Form(...),
                      customer_name: str = Form(""), channel: str = Form("portal"),
                      files: list[UploadFile] = File(default = [])) -> dict:
    case = db.create_case(subject = subject, description = description, customer_email = customer_email.strip(),
                          customer_name = customer_name or customer_email.split("@")[0], channel = channel)
    db.add_message(case["id"], "customer", case["customer_name"], description)
    await _save_uploads(case["id"], files)
    if config.AUTOPILOT:
        orchestrator.start(case["id"])
    return db.get_case(case["id"])


@app.get("/api/cases/{cid}")
def get_case(cid: int, customer_view: bool = False) -> dict:
    case = db.get_case(cid)
    if not case:
        raise HTTPException(404, "case not found")
    verif = [e for e in db.events(cid) if e["type"] == "verification"]
    return {
        "case": case,
        "messages": db.messages(cid, include_internal = not customer_view),
        "attachments": db.attachments(cid),
        "proposal": None if customer_view else db.latest_proposal(cid),
        "verification": verif[-1]["payload"] if verif else None,
        "running": orchestrator.is_running(cid),
    }


@app.get("/api/cases/{cid}/events")
def case_events(cid: int, after: int = 0) -> list[dict]:
    return db.events(cid, after)


@app.post("/api/cases/{cid}/messages")
async def post_message(cid: int, body: str = Form(...), sender: str = Form("customer"), author: str = Form(""),
                       files: list[UploadFile] = File(default = [])) -> dict:
    case = db.get_case(cid)
    if not case:
        raise HTTPException(404, "case not found")
    internal = sender == "note"
    db.add_message(cid, "agent" if internal else sender, author or (case["customer_name"] if sender == "customer"
                                                                    else "Agent"), body, internal = internal)
    await _save_uploads(cid, files)
    resumed = orchestrator.customer_replied(cid) if sender == "customer" else False
    return {"ok": True, "resumed": resumed}


@app.post("/api/cases/{cid}/run")
def run_case(cid: int) -> dict:
    return {"started": orchestrator.start(cid)}


@app.post("/api/cases/{cid}/decision")
def decide(cid: int, body: Decision) -> dict:
    try:
        return orchestrator.decide(cid, body.approve, body.approver, body.comment)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/attachments/{aid}")
def attachment(aid: int) -> FileResponse:
    a = db.query_one("SELECT * FROM attachments WHERE id = ?", (aid,))
    if not a:
        raise HTTPException(404, "attachment not found")
    return FileResponse(a["path"], media_type = a["content_type"], filename = a["filename"])


# --- Demo cases -----------------------------------------------------------------------------

@app.get("/api/scenarios")
def list_scenarios() -> list[dict]:
    return [{"key": k, "label": v["label"], "customer": v["customer"][0], "subject": v["subject"],
             "shows": v["shows"], "has_reply": "reply" in v} for k, v in scenarios.scenarios().items()]


@app.post("/api/scenarios/{key}")
def run_scenario(key: str) -> dict:
    if key not in scenarios.scenarios():
        raise HTTPException(404, "unknown scenario")
    case = scenarios.file_case(key)
    if config.AUTOPILOT:
        orchestrator.start(case["id"])
    return case


@app.post("/api/cases/{cid}/scripted-reply")
def scripted_reply(cid: int) -> dict:
    case = db.get_case(cid)
    sc = scenarios.scenarios().get(case.get("scenario") or "", {})
    if "reply" not in sc:
        raise HTTPException(400, "no scripted reply for this case")
    db.add_message(cid, "customer", case["customer_name"], sc["reply"])
    return {"resumed": orchestrator.customer_replied(cid)}


@app.post("/api/reset")
def reset() -> dict:
    scenarios.reset_all()
    return {"ok": True}


# --- Enterprise systems (transparency) --------------------------------------------------------

@app.get("/api/customers")
def customers() -> list[dict]:
    return store.q("SELECT id, name, email, tier FROM customers ORDER BY id")


@app.get("/api/systems")
def systems() -> dict:
    return {
        "orders": store.q("SELECT o.*, c.name AS customer, p.refunded, p.captured FROM orders o "
                          "JOIN customers c ON c.id = o.customer_id JOIN payments p ON p.id = o.payment_id "
                          "ORDER BY o.id DESC"),
        "inventory": store.q("SELECT i.sku, p.name, i.warehouse_id, i.on_hand, i.reserved FROM inventory i "
                             "JOIN products p ON p.sku = i.sku ORDER BY i.sku, i.warehouse_id"),
        "refunds": store.q("SELECT * FROM refunds ORDER BY created_at DESC"),
        "shipments": store.q("SELECT * FROM shipments ORDER BY created_at DESC"),
        "store_credit": store.q("SELECT * FROM store_credit ORDER BY created_at DESC"),
        "world_events": store.q("SELECT * FROM world_events ORDER BY id DESC"),
        "audit": store.q("SELECT * FROM audit ORDER BY id DESC LIMIT 100"),
    }


@app.get("/api/knowledge")
def knowledge() -> dict:
    return {"policies": policy._docs(), "learned": db.list_kb()}
