from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import rows_to_list, row_to_dict, read_tx, write_tx
from app.services.common import current_shift, now_str
from app.services import ledger

router = APIRouter(tags=["过机扫描"])


class ScanIn(BaseModel):
    条码: str = Field(min_length=1)
    目的地码: str = ""
    分拣线编号: int | None = None
    备注: str = ""


@router.get("/扫描")
def list_scans(状态: str | None = None, 班次编号: int | None = None):
    with read_tx() as conn:
        sql = "SELECT * FROM scans"
        clauses = []
        params: list = []
        if 班次编号 is not None:
            clauses.append("shift_id=?")
            params.append(班次编号)
        if 状态:
            clauses.append("status=?")
            params.append(状态)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT 200"
        return rows_to_list(conn.execute(sql, params).fetchall())


def _load_scan(conn, barcode: str, dest: str, line_id: int | None, shift_id: int, note: str) -> int:
    """装载写口：查班次/线/去重后追加装载事件，缓存由事件重放同步。"""
    if line_id:
        line = conn.execute("SELECT * FROM lines WHERE id=?", (line_id,)).fetchone()
    else:
        line = conn.execute("SELECT * FROM lines WHERE active=1 ORDER BY id LIMIT 1").fetchone()
    if not line:
        raise HTTPException(400, "无可用分拣线")
    if not line["active"]:
        raise HTTPException(400, "分拣线已停用")
    dup = conn.execute(
        "SELECT id FROM scans WHERE barcode=? AND shift_id=?", (barcode, shift_id)
    ).fetchone()
    if dup:
        raise HTTPException(400, "本班次条码已过机")
    conn.execute(
        """
        INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, scanned_at, note)
        VALUES (?, ?, ?, ?, '待路由', ?, ?)
        """,
        (barcode, dest, shift_id, line["id"], now_str(), note),
    )
    scan_id = int(conn.execute(
        "SELECT id FROM scans WHERE barcode=? AND shift_id=?", (barcode, shift_id)
    ).fetchone()["id"])
    ledger.append_event(
        conn, shift_id=shift_id, kind=ledger.装载, scan_id=scan_id,
        note=barcode, actor="扫描岗", dedup_key=f"装载:{scan_id}",
    )
    ledger.rebuild_scan_cache(conn, [scan_id])
    return scan_id


@router.post("/扫描")
def create_scan(body: ScanIn):
    with write_tx() as conn:
        shift = current_shift(conn)
        if shift["sealed"]:
            raise HTTPException(400, "班次已封班，禁止写口")
        scan_id = _load_scan(conn, body.条码, body.目的地码, body.分拣线编号, shift["id"], body.备注)
        return row_to_dict(conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone())


def _route_scan(conn, scan_id: int, shift_id: int) -> int:
    """路由裁决写口：校验状态/目的地/口门后追加裁决事件。"""
    scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not scan:
        raise HTTPException(404, "扫描不存在")
    if scan["shift_id"] != shift_id:
        raise HTTPException(400, "扫描不属于当前班次")
    if scan["status"] not in ("待路由", "错分回流"):
        raise HTTPException(400, f"当前状态不可路由: {scan['status']}")
    if not scan["dest_code"]:
        raise HTTPException(400, "缺少目的地码")
    rule = conn.execute(
        "SELECT * FROM route_rules WHERE dest_code=?", (scan["dest_code"],)
    ).fetchone()
    if not rule:
        raise HTTPException(400, "无匹配路由")
    chute = conn.execute("SELECT * FROM chutes WHERE id=?", (rule["chute_id"],)).fetchone()
    if not chute:
        raise HTTPException(400, "口门不存在")
    if chute["status"] != "口门开":
        raise HTTPException(400, "口门已闭，禁止落入")
    prev = ledger.last_event_id(conn, scan_id=scan_id)
    ledger.append_event(
        conn, shift_id=shift_id, kind=ledger.裁决, scan_id=scan_id, chute_id=chute["id"],
        note=chute["code"], actor="路由岗", dedup_key=f"裁决:{scan_id}:{prev}",
    )
    ledger.rebuild_scan_cache(conn, [scan_id])
    return chute["id"]


@router.post("/扫描/{scan_id}/路由")
def route_scan(scan_id: int):
    with write_tx() as conn:
        shift = current_shift(conn)
        if shift["sealed"]:
            raise HTTPException(400, "班次已封班，禁止写口")
        _route_scan(conn, scan_id, shift["id"])
        return row_to_dict(conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone())
