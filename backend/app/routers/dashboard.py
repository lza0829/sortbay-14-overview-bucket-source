"""调度屏四格读口 + 重放/对账/轨迹。

口径：四格唯一真源是装载/裁决/回流/开袋/入袋/封袋事件流，读口一律重放投影。
本路由只读，不提供任何直接改四格或改扫描册状态的入口。
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Query
from app.db import read_tx
from app.services import ledger
from app.services.common import shift_or_404

router = APIRouter(tags=["调度屏"])


@router.get("/调度屏")
def dashboard(班次编号: int | None = None):
    with read_tx() as conn:
        shift = shift_or_404(conn, 班次编号)
        data = ledger.project(conn, shift_id=shift["id"])
        data["班次"] = dict(shift)
        data["口径"] = "事件流重放"
        return data


@router.get("/调度屏/重放")
def replay_cells(
    班次编号: int | None = None,
    截止事件编号: int | None = Query(default=None, description="只重放到该事件 id，用于半截回执对照"),
):
    with read_tx() as conn:
        shift = shift_or_404(conn, 班次编号)
        rp = ledger.replay(conn, shift_id=shift["id"], upto_event_id=截止事件编号)
        return {
            "班次编号": shift["id"],
            "口径": "事件流重放",
            "截止事件编号": 截止事件编号,
            "已重放事件数": rp.applied,
            "四格": rp.cells(),
            "口门在口件数": {str(k): v for k, v in rp.chute_in_port().items()},
            "袋册": [{"袋编号": k, "状态": v.status, "件数": len(v.scan_ids)} for k, v in sorted(rp.bags.items())],
        }


@router.get("/调度屏/轨迹")
def replay_trace(班次编号: int | None = None, 截止事件编号: int | None = None):
    with read_tx() as conn:
        shift = shift_or_404(conn, 班次编号)
        return {
            "班次编号": shift["id"],
            "口径": "事件流重放",
            "轨迹": ledger.trace(conn, shift_id=shift["id"], upto_event_id=截止事件编号),
        }


@router.get("/调度屏/对账")
def reconcile(班次编号: int | None = None):
    with read_tx() as conn:
        shift = shift_or_404(conn, 班次编号)
        data = ledger.verify(conn, shift_id=shift["id"])
        data["口径"] = "事件流重放"
        return data


@router.post("/调度屏/手工改计")
def manual_adjust():
    """手工改计口：一律拒绝。

    四格只能由事件流重放得到，任何「把四格数字改齐」的请求都不接受，
    更不允许伪装成写口成功。维护员需改账应走对应写口（装载/裁决/回流/入袋/封袋）
    或由差异审计定位后补事件，绝不直接改四格或扫描册状态。
    """
    raise HTTPException(
        409,
        "拒绝手工改计：调度屏四格仅由装载/裁决/回流/封袋事件流重放生成，"
        "不接受直接改数；如需纠偏请走对应写口或补记事件并经审计。",
    )
