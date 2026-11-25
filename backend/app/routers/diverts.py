from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import rows_to_list, row_to_dict, read_tx, write_tx
from app.services.common import current_shift, now_str
from app.services import ledger

router = APIRouter(tags=["错分回流"])


class DivertIn(BaseModel):
    扫描编号: int
    备注: str = ""


@router.get("/回流批次")
def list_batches(班次编号: int | None = None):
    with read_tx() as conn:
        if 班次编号 is not None:
            rows = conn.execute(
                "SELECT * FROM divert_batches WHERE shift_id=? ORDER BY id DESC LIMIT 100", (班次编号,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM divert_batches ORDER BY id DESC LIMIT 100").fetchall()
        return rows_to_list(rows)


def _divert(conn, scan_id: int, shift_id: int, note: str) -> int:
    """错分回流写口：把件从口门/袋摘出并追加回流事件；整笔原子。"""
    scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not scan:
        raise HTTPException(404, "扫描不存在")
    if scan["shift_id"] != shift_id:
        raise HTTPException(400, "扫描不属于当前班次")
    if scan["status"] not in ("已路由", "已入袋", "已封袋"):
        raise HTTPException(400, "仅已路由/已入袋/已封袋可标错分回流")
    if scan["status"] == "已封袋":
        raise HTTPException(400, "已封袋件不可回流，请走班次对账差异流程")
    code = f"回流-{shift_id}-{now_str().replace(' ', '').replace(':', '')[-8:]}"
    conn.execute(
        "INSERT INTO divert_batches(shift_id, code, status, created_at, note) VALUES (?, ?, '回流中', ?, ?)",
        (shift_id, code, now_str(), note),
    )
    batch_id = int(conn.execute(
        "SELECT id FROM divert_batches WHERE code=?", (code,)
    ).fetchone()["id"])
    if scan["chute_id"]:
        conn.execute(
            "INSERT INTO route_events(scan_id, chute_id, action, created_at, note) VALUES (?, ?, ?, ?, ?)",
            (scan_id, scan["chute_id"], "错分回流", now_str(), code),
        )
    prev = ledger.last_event_id(conn, scan_id=scan_id)
    ledger.append_event(
        conn, shift_id=shift_id, kind=ledger.回流, scan_id=scan_id, batch_id=batch_id,
        chute_id=scan["chute_id"], note=code, actor="回流岗",
        dedup_key=f"回流:{scan_id}:{prev}",
    )
    ledger.rebuild_scan_cache(conn, [scan_id])
    return batch_id


@router.post("/回流批次")
def create_divert(body: DivertIn):
    with write_tx() as conn:
        shift = current_shift(conn)
        if shift["sealed"]:
            raise HTTPException(400, "班次已封班，禁止写口")
        batch_id = _divert(conn, body.扫描编号, shift["id"], body.备注)
        return {
            "批次": row_to_dict(conn.execute("SELECT * FROM divert_batches WHERE id=?", (batch_id,)).fetchone()),
            "扫描": row_to_dict(conn.execute("SELECT * FROM scans WHERE id=?", (body.扫描编号,)).fetchone()),
        }
