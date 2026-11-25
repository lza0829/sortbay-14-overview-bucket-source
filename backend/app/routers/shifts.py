from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import rows_to_list, row_to_dict, read_tx, write_tx
from app.services import ledger
from app.services.common import audit, now_str

router = APIRouter(tags=["班次"])


class ShiftIn(BaseModel):
    营业日: str = Field(min_length=8)
    班次名: str = Field(min_length=1)


@router.get("/班次")
def list_shifts():
    with read_tx() as conn:
        return rows_to_list(conn.execute("SELECT * FROM shifts ORDER BY id DESC").fetchall())


@router.get("/班次/对账")
def reconcile(shift_id: int | None = None):
    """班次对账与调度屏同源：四格由事件重放给出，另附扫描册/袋册旁证与差异。"""
    with read_tx() as conn:
        if shift_id is None:
            shift = conn.execute("SELECT * FROM shifts ORDER BY id DESC LIMIT 1").fetchone()
        else:
            shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
        if not shift:
            raise HTTPException(404, "班次不存在")
        sid = shift["id"]
        ver = ledger.verify(conn, shift_id=sid)
        cells = ver["四格"]
        return {
            "班次": row_to_dict(shift),
            "口径": "事件流重放",
            "四格": cells,
            "扫描总数": ver["扫描册件数"],
            "重放件数": ver["重放件数"],
            "已封袋数": cells[ledger.格已封袋],
            "未清件数": cells[ledger.格待路由] + cells[ledger.格错分回流] + cells[ledger.格已路由],
            "差异": ver["差异"],
            "审计缺口": ver["审计缺口"],
            "对齐": ver["对齐"],
        }


@router.post("/班次")
def create_shift(body: ShiftIn):
    with write_tx() as conn:
        open_one = conn.execute("SELECT id FROM shifts WHERE sealed=0").fetchone()
        if open_one:
            raise HTTPException(400, "仍有未封班班次，先封班再开新班")
        try:
            conn.execute(
                "INSERT INTO shifts(business_day, name, sealed) VALUES (?, ?, 0)",
                (body.营业日, body.班次名),
            )
        except Exception as e:
            raise HTTPException(400, f"开班失败: {e}")
        row = conn.execute(
            "SELECT * FROM shifts WHERE business_day=? AND name=?", (body.营业日, body.班次名)
        ).fetchone()
        audit(conn, "班次", row["id"], "开班", body.班次名)
        return row_to_dict(row)


@router.post("/班次/{shift_id}/封班")
def seal_shift(shift_id: int):
    with write_tx() as conn:
        shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
        if not shift:
            raise HTTPException(404, "班次不存在")
        if shift["sealed"]:
            raise HTTPException(400, "已封班")
        # 封班闸门用重放口径，不再信任扫描册可变状态
        rp = ledger.replay(conn, shift_id=shift_id)
        pending = [sid for sid, st in rp.scans.items() if st.cell() != ledger.格已封袋]
        if pending:
            raise HTTPException(400, f"仍有未封袋件 {len(pending)}，禁止封班")
        conn.execute(
            "UPDATE shifts SET sealed=1, sealed_at=? WHERE id=?", (now_str(), shift_id)
        )
        audit(conn, "班次", shift_id, "封班", "")
        return row_to_dict(conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone())
