from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import rows_to_list, row_to_dict, read_tx, write_tx
from app.services.common import current_shift, now_str
from app.services import ledger

router = APIRouter(tags=["封袋"])


class BagOpenIn(BaseModel):
    口门编号: int
    备注: str = ""


class BagAddIn(BaseModel):
    扫描编号: int


@router.get("/封袋")
def list_bags(班次编号: int | None = None):
    with read_tx() as conn:
        if 班次编号 is not None:
            rows = conn.execute(
                """
                SELECT b.*, c.code AS 口门码,
                  (SELECT COUNT(*) FROM scans s WHERE s.bag_id=b.id) AS 件数
                FROM bags b JOIN chutes c ON c.id=b.chute_id
                WHERE b.shift_id=? ORDER BY b.id DESC LIMIT 100
                """,
                (班次编号,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT b.*, c.code AS 口门码,
                  (SELECT COUNT(*) FROM scans s WHERE s.bag_id=b.id) AS 件数
                FROM bags b JOIN chutes c ON c.id=b.chute_id
                ORDER BY b.id DESC LIMIT 100
                """
            ).fetchall()
        return rows_to_list(rows)


def _open_bag(conn, chute_id: int, note: str, shift_id: int) -> dict:
    chute = conn.execute("SELECT * FROM chutes WHERE id=?", (chute_id,)).fetchone()
    if not chute:
        raise HTTPException(404, "口门不存在")
    code = f"袋-{chute['code']}-{shift_id}-{now_str().replace(':', '').replace(' ', '')[-10:]}"
    conn.execute(
        "INSERT INTO bags(shift_id, chute_id, code, status, note) VALUES (?, ?, ?, '集包中', ?)",
        (shift_id, chute_id, code, note),
    )
    bag_id = int(conn.execute("SELECT id FROM bags WHERE code=?", (code,)).fetchone()["id"])
    prev = ledger.last_event_id(conn, bag_id=bag_id)
    ledger.append_event(
        conn, shift_id=shift_id, kind=ledger.开袋, bag_id=bag_id, chute_id=chute_id,
        note=code, actor="封袋岗", dedup_key=f"开袋:{bag_id}:{prev}",
    )
    return row_to_dict(conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone())


@router.post("/封袋/开袋")
def open_bag(body: BagOpenIn):
    with write_tx() as conn:
        shift = current_shift(conn)
        if shift["sealed"]:
            raise HTTPException(400, "班次已封班，禁止写口")
        return _open_bag(conn, body.口门编号, body.备注, shift["id"])


def _add_to_bag(conn, bag_id: int, scan_id: int, shift_id: int) -> dict:
    bag = conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone()
    if not bag:
        raise HTTPException(404, "袋不存在")
    if bag["shift_id"] != shift_id:
        raise HTTPException(400, "袋不属于当前班次")
    if bag["status"] != "集包中":
        raise HTTPException(400, "袋已封，禁止再入")
    scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not scan:
        raise HTTPException(404, "扫描不存在")
    if scan["shift_id"] != shift_id:
        raise HTTPException(400, "扫描不属于当前班次")
    if scan["status"] != "已路由":
        raise HTTPException(400, "仅已路由件可入袋")
    if scan["chute_id"] != bag["chute_id"]:
        raise HTTPException(400, "扫描口门与袋口门不一致")
    prev = ledger.last_event_id(conn, scan_id=scan_id)
    ledger.append_event(
        conn, shift_id=shift_id, kind=ledger.入袋, scan_id=scan_id, bag_id=bag_id,
        chute_id=bag["chute_id"], note=str(scan_id), actor="封袋岗",
        dedup_key=f"入袋:{scan_id}:{prev}",
    )
    ledger.rebuild_scan_cache(conn, [scan_id])
    return row_to_dict(conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone())


@router.post("/封袋/{bag_id}/入袋")
def add_to_bag(bag_id: int, body: BagAddIn):
    with write_tx() as conn:
        shift = current_shift(conn)
        if shift["sealed"]:
            raise HTTPException(400, "班次已封班，禁止写口")
        return _add_to_bag(conn, bag_id, body.扫描编号, shift["id"])


def _seal_bag(conn, bag_id: int, shift_id: int) -> dict:
    bag = conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone()
    if not bag:
        raise HTTPException(404, "袋不存在")
    if bag["shift_id"] != shift_id:
        raise HTTPException(400, "袋不属于当前班次")
    if bag["status"] != "集包中":
        raise HTTPException(400, "袋状态不可封口")
    n = conn.execute("SELECT COUNT(*) c FROM scans WHERE bag_id=?", (bag_id,)).fetchone()["c"]
    if n == 0:
        raise HTTPException(400, "空袋不可封口")
    conn.execute(
        "UPDATE bags SET status='已封袋', sealed_at=? WHERE id=?", (now_str(), bag_id)
    )
    prev = ledger.last_event_id(conn, bag_id=bag_id)
    ledger.append_event(
        conn, shift_id=shift_id, kind=ledger.封袋, bag_id=bag_id, chute_id=bag["chute_id"],
        note=f"件数{n}", actor="封袋岗", dedup_key=f"封袋:{bag_id}:{prev}",
    )
    # 封袋事件把整袋件推进已封袋格，四格与扫描册一起重算
    members = [r["id"] for r in conn.execute(
        "SELECT id FROM scans WHERE bag_id=?", (bag_id,)
    ).fetchall()]
    ledger.rebuild_scan_cache(conn, members)
    return row_to_dict(conn.execute("SELECT * FROM bags WHERE id=?", (bag_id,)).fetchone())


@router.post("/封袋/{bag_id}/封口")
def seal_bag(bag_id: int):
    with write_tx() as conn:
        shift = current_shift(conn)
        if shift["sealed"]:
            raise HTTPException(400, "班次已封班，禁止写口")
        return _seal_bag(conn, bag_id, shift["id"])
