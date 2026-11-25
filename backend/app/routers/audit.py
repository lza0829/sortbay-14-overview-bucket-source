from __future__ import annotations
from fastapi import APIRouter
from app.db import rows_to_list, read_tx

router = APIRouter(tags=["审计"])


@router.get("/审计")
def list_audit(班次编号: int | None = None):
    """审计流。班次编号给定时追加该班事件流轨迹的事件编号映射，便于重放对账。"""
    with read_tx() as conn:
        rows = rows_to_list(conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT 200"
        ).fetchall())
        if 班次编号 is not None:
            ev = rows_to_list(conn.execute(
                "SELECT id, kind, scan_id, bag_id, batch_id, chute_id, note, actor, created_at "
                "FROM sort_events WHERE shift_id=? ORDER BY id",
                (班次编号,),
            ).fetchall())
            return {"审计": rows, "事件流": ev, "口径": "事件流重放"}
        return rows
