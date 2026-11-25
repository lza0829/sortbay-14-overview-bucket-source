"""调度屏四格真源：只追加事件流 + 重放投影。

口径（全局唯一，禁止混用）：
  以「装载 → 裁决 → 回流 → 开袋 → 入袋 → 封袋」事件流滚动汇总为准。
  - 四格 = 事件流重放投影，可随时由选定班次+事件区间重放得到。
  - 口门在口 = 重放后仍处已路由/已入袋的件，按口门聚合；仅是同一真源的另一种投影，
    绝不反向用口门页面或扫描册里的可变状态纠偏。
  - 袋册封态 = 开袋/封袋事件投影。
  - 扫描册（scans.status 等）只是事件流的物化缓存，同事务内重算，供列表展示；
    任何缓存与重放不一致都会被 /调度屏/对账 报为拧花。
  - 手工改计一律拒绝：四格不接受任何非事件写入，也不得伪装成写口成功。

本模块不写 scans 以外的业务表；所有事件写入由调用方的 write_tx 事务包裹。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterable

from app.services.common import audit, now_str

# 事件类型
装载 = "装载"
裁决 = "裁决"
回流 = "回流"
开袋 = "开袋"
入袋 = "入袋"
封袋 = "封袋"

EVENT_KINDS = (装载, 裁决, 回流, 开袋, 入袋, 封袋)

# 事件 -> 审计 kind；审计由 append_event 统一产出，与事件流同事务
_AUDIT_KIND = {装载: "扫描", 裁决: "扫描", 回流: "回流", 开袋: "封袋", 入袋: "封袋", 封袋: "封袋"}

# 四格
格待路由 = "待路由"
格已路由 = "已路由"
格错分回流 = "错分回流"
格已封袋 = "已封袋"
格序 = (格待路由, 格已路由, 格错分回流, 格已封袋)


class LedgerError(Exception):
    """事件流写入被拒（调用方回滚整笔事务）。"""


@dataclass
class ScanState:
    scan_id: int
    status: str = 格待路由
    chute_id: int | None = None
    bag_id: int | None = None
    batch_id: int | None = None
    last_event_id: int = 0

    def cell(self) -> str:
        if self.status == 格待路由:
            return 格待路由
        if self.status in ("已路由", "已入袋"):
            return 格已路由
        if self.status == 格错分回流:
            return 格错分回流
        if self.status == 格已封袋:
            return 格已封袋
        # 未知状态一律按待路由兜底，绝不静默落进已路由格
        return 格待路由


@dataclass
class BagState:
    bag_id: int
    chute_id: int | None = None
    status: str = "集包中"
    sealed_at: str | None = None
    scan_ids: list[int] = field(default_factory=list)


@dataclass
class Replay:
    scans: dict[int, ScanState] = field(default_factory=dict)
    bags: dict[int, BagState] = field(default_factory=dict)
    applied: int = 0

    def cells(self) -> dict[str, int]:
        out = {k: 0 for k in 格序}
        for st in self.scans.values():
            out[st.cell()] += 1
        return out

    def chute_in_port(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for st in self.scans.values():
            if st.cell() == 格已路由 and st.chute_id is not None:
                out[st.chute_id] = out.get(st.chute_id, 0) + 1
        return out


def last_event_id(conn: sqlite3.Connection, *, scan_id: int | None = None, bag_id: int | None = None) -> int:
    """该件/该袋已提交的最大事件 id，作为「第几手」序列令牌用于去重键。"""
    if scan_id is not None:
        row = conn.execute("SELECT MAX(id) m FROM sort_events WHERE scan_id=?", (scan_id,)).fetchone()
    else:
        row = conn.execute("SELECT MAX(id) m FROM sort_events WHERE bag_id=?", (bag_id,)).fetchone()
    return int(row["m"] or 0)


def append_event(
    conn: sqlite3.Connection,
    *,
    shift_id: int,
    kind: str,
    scan_id: int | None = None,
    bag_id: int | None = None,
    batch_id: int | None = None,
    chute_id: int | None = None,
    note: str = "",
    actor: str = "系统",
    dedup_key: str | None = None,
) -> int:
    """追加一条事件，并同事务写入对应审计（审计与事件流永不脱节）。

    kind 非法或去重键冲突即抛 LedgerError，由外层整笔回滚。
    审计由本函数统一产出，写口路由不得再自行补事件相关审计，避免两套账。
    """
    if kind not in EVENT_KINDS:
        raise LedgerError(f"未知事件类型: {kind}")
    try:
        cur = conn.execute(
            """
            INSERT INTO sort_events(shift_id, kind, scan_id, bag_id, batch_id, chute_id,
                                    note, actor, dedup_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (shift_id, kind, scan_id, bag_id, batch_id, chute_id, note, actor, dedup_key, now_str()),
        )
    except sqlite3.IntegrityError as e:
        raise LedgerError(f"事件去重冲突: {kind}#{scan_id or bag_id or batch_id}: {e}")
    _audit_for(conn, kind=kind, scan_id=scan_id, bag_id=bag_id, batch_id=batch_id,
               chute_id=chute_id, note=note)
    return int(cur.lastrowid)


_AUDIT_ACTION = {
    装载: "过机装载", 裁决: "路由裁决", 回流: "建回流批次",
    开袋: "开袋", 入袋: "入袋", 封袋: "封口",
}


def _audit_for(conn, *, kind, scan_id, bag_id, batch_id, chute_id, note) -> None:
    audit(conn, _AUDIT_KIND[kind], _audit_ref_for(kind, scan_id, bag_id, batch_id),
          _AUDIT_ACTION[kind], note)


def _audit_ref_for(kind, scan_id, bag_id, batch_id) -> int | None:
    if kind == 入袋:
        return bag_id
    return scan_id or bag_id or batch_id


def _advance(rp: Replay, e: sqlite3.Row) -> list[tuple[int, str]]:
    """把一个事件折叠进重放状态；返回受影响扫描件 [(scan_id, 落格)]。

    袋级事件（开袋/封袋）对袋内每件都返回 (scan_id, 已封袋)，轨迹据此逐件归因。
    """
    kind = e["kind"]
    if kind == 开袋:
        b = rp.bags.get(e["bag_id"]) or BagState(bag_id=e["bag_id"])
        b.chute_id = e["chute_id"]
        b.status = "集包中"
        b.sealed_at = None
        rp.bags[e["bag_id"]] = b
        return []
    if kind == 封袋:
        # 袋级事件：把当前袋内所有件推到已封袋格，四格不依赖任何逐件写。
        # 若事件也带 scan_id（老库无袋件补记），同样把该件置为已封袋。
        b = rp.bags.get(e["bag_id"]) or BagState(bag_id=e["bag_id"])
        b.status = "已封袋"
        b.sealed_at = e["created_at"]
        rp.bags[e["bag_id"]] = b
        affected: list[tuple[int, str]] = []
        members = list(b.scan_ids)
        if e["scan_id"] is not None and e["scan_id"] not in members:
            members.append(e["scan_id"])
        for sid in members:
            st = rp.scans.get(sid) or ScanState(scan_id=sid)
            st.status = 格已封袋
            st.last_event_id = e["id"]
            rp.scans[sid] = st
            affected.append((sid, 格已封袋))
        return affected
    if e["scan_id"] is None:
        return []
    st = rp.scans.get(e["scan_id"]) or ScanState(scan_id=e["scan_id"])
    st.last_event_id = e["id"]
    if kind == 装载:
        st.status = 格待路由
        st.chute_id = None
        st.bag_id = None
        st.batch_id = None
    elif kind == 裁决:
        st.status = "已路由"
        st.chute_id = e["chute_id"]
        st.bag_id = None
        st.batch_id = None
    elif kind == 回流:
        # 错分回流把件从原袋摘出，袋内件数随之减少（避免封袋漏减已路由格）
        if st.bag_id is not None:
            old = rp.bags.get(st.bag_id)
            if old and e["scan_id"] in old.scan_ids:
                old.scan_ids.remove(e["scan_id"])
        st.status = 格错分回流
        st.batch_id = e["batch_id"]
        st.bag_id = None
    elif kind == 入袋:
        st.status = "已入袋"
        st.bag_id = e["bag_id"]
        if e["bag_id"] is not None:
            b = rp.bags.get(e["bag_id"]) or BagState(bag_id=e["bag_id"], chute_id=st.chute_id)
            if e["scan_id"] not in b.scan_ids:
                b.scan_ids.append(e["scan_id"])
            rp.bags[e["bag_id"]] = b
    rp.scans[e["scan_id"]] = st
    return [(e["scan_id"], st.cell())]


def _event_query(clauses: list[str]) -> str:
    sql = "SELECT * FROM sort_events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return sql + " ORDER BY id"


def replay(
    conn: sqlite3.Connection,
    *,
    shift_id: int | None = None,
    upto_event_id: int | None = None,
) -> Replay:
    """按事件 id 顺序重放。upto_event_id 用于「半截回执」对照：只看已提交前缀。"""
    clauses: list[str] = []
    params: list = []
    if shift_id is not None:
        clauses.append("shift_id=?")
        params.append(shift_id)
    if upto_event_id is not None:
        clauses.append("id<=?")
        params.append(upto_event_id)
    rp = Replay()
    for e in conn.execute(_event_query(clauses), params).fetchall():
        rp.applied += 1
        _advance(rp, e)
    return rp


def rebuild_scan_cache(conn: sqlite3.Connection, scan_ids: Iterable[int]) -> None:
    """在同一写事务内，用事件重放结果重写扫描册物化缓存。

    只改 scans.status/chute_id/bag_id/divert_batch_id，不碰路由规则或口门实物。
    仅重放受影响件所在班次的事件流，避免随历史增长而全表重放。
    """
    ids = sorted({int(i) for i in scan_ids})
    if not ids:
        return
    shifts: set[int] = set()
    for sid in ids:
        row = conn.execute("SELECT shift_id FROM scans WHERE id=?", (sid,)).fetchone()
        if row is not None:
            shifts.add(row["shift_id"])
    rp = Replay()
    for shift_id in sorted(shifts):
        part = replay(conn, shift_id=shift_id)
        rp.scans.update(part.scans)
        rp.bags.update(part.bags)
    for sid in ids:
        st = rp.scans.get(sid)
        if st is None:
            continue
        conn.execute(
            "UPDATE scans SET status=?, chute_id=?, bag_id=?, divert_batch_id=? WHERE id=?",
            (st.status, st.chute_id, st.bag_id, st.batch_id, sid),
        )


def project(conn: sqlite3.Connection, *, shift_id: int) -> dict:
    """调度屏投影：四格 + 口门在口 + 袋册封态，全部来自事件重放。"""
    rp = replay(conn, shift_id=shift_id)
    chutes = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM chutes").fetchall()}
    in_port = rp.chute_in_port()
    chute_rows = []
    for cid, c in chutes.items():
        chute_rows.append({
            "口门编号": cid,
            "口门码": c["code"],
            "标题": c["title"],
            "状态": c["status"],
            "在口件数": in_port.get(cid, 0),
        })
    chute_rows.sort(key=lambda x: x["口门码"])
    bags = []
    for bid, b in sorted(rp.bags.items()):
        bags.append({
            "袋编号": bid,
            "口门编号": b.chute_id,
            "状态": b.status,
            "件数": len(b.scan_ids),
        })
    return {
        "班次编号": shift_id,
        "四格": rp.cells(),
        "口门在口": chute_rows,
        "袋册": bags,
        "事件数": rp.applied,
    }


def trace(conn: sqlite3.Connection, *, shift_id: int, upto_event_id: int | None = None) -> list[dict]:
    """重放轨迹：逐事件给出受影响件所属格，便于对照四格如何滚出来。

    袋级封袋会为袋内每件各出一行（落格=已封袋），扫描编号为该件，
    否则逐件事件的轨迹无法解释「封袋减在口」究竟动了哪几件。
    """
    clauses = ["shift_id=?"]
    params: list = [shift_id]
    if upto_event_id is not None:
        clauses.append("id<=?")
        params.append(upto_event_id)
    rp = Replay()
    rows: list[dict] = []
    for e in conn.execute(_event_query(clauses), params).fetchall():
        rp.applied += 1
        affected = _advance(rp, e)
        if not affected:
            rows.append({
                "事件编号": e["id"], "类型": e["kind"], "扫描编号": e["scan_id"],
                "袋编号": e["bag_id"], "批次编号": e["batch_id"], "口门编号": e["chute_id"],
                "落格": None, "时间": e["created_at"], "备注": e["note"],
            })
            continue
        for sid, cell in affected:
            rows.append({
                "事件编号": e["id"], "类型": e["kind"], "扫描编号": sid,
                "袋编号": e["bag_id"], "批次编号": e["batch_id"], "口门编号": e["chute_id"],
                "落格": cell, "时间": e["created_at"], "备注": e["note"],
            })
    return rows


def verify(conn: sqlite3.Connection, *, shift_id: int) -> dict:
    """对账：事件重放 vs 扫描册缓存 vs 口门在口 vs 袋册 vs 审计。

    返回四格、各来源计数与全部差异项；差异为空才算对齐。
    """
    rp = replay(conn, shift_id=shift_id)
    cells = rp.cells()
    diffs: list[dict] = []

    # 1) 扫描册缓存应与重放一致
    cache_status: dict[int, str] = {}
    for r in conn.execute("SELECT id, status, chute_id, bag_id, divert_batch_id FROM scans WHERE shift_id=?", (shift_id,)):
        cache_status[r["id"]] = r["status"]
        st = rp.scans.get(r["id"])
        if st is None:
            # 有扫描件却无任何事件：装载事件缺失（回填未跑或半截写入）
            diffs.append({"来源": "扫描册", "扫描编号": r["id"], "问题": "缺少事件流", "缓存状态": r["status"]})
            continue
        if r["status"] != st.status:
            diffs.append({
                "来源": "扫描册", "扫描编号": r["id"], "问题": "状态拧花",
                "缓存状态": r["status"], "重放状态": st.status,
            })
        if (r["chute_id"] or None) != (st.chute_id or None):
            diffs.append({"来源": "扫描册", "扫描编号": r["id"], "问题": "口门拧花",
                          "缓存口门": r["chute_id"], "重放口门": st.chute_id})
        if (r["bag_id"] or None) != (st.bag_id or None):
            diffs.append({"来源": "扫描册", "扫描编号": r["id"], "问题": "袋拧花",
                          "缓存袋": r["bag_id"], "重放袋": st.bag_id})
    # 2) 重放里有事件但扫描册里没有该件
    for sid in rp.scans:
        if sid not in cache_status:
            diffs.append({"来源": "事件流", "扫描编号": sid, "问题": "重放件在扫描册缺失"})

    # 3) 袋册封态 vs 重放
    bag_cache = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM bags WHERE shift_id=?", (shift_id,))}
    for bid, b in rp.bags.items():
        if bid not in bag_cache:
            diffs.append({"来源": "袋册", "袋编号": bid, "问题": "重放袋在袋册缺失"})
        elif bag_cache[bid] != b.status:
            diffs.append({"来源": "袋册", "袋编号": bid, "问题": "封态拧花",
                          "袋册状态": bag_cache[bid], "重放状态": b.status})
    for bid in bag_cache:
        if bid not in rp.bags:
            diffs.append({"来源": "袋册", "袋编号": bid, "问题": "袋册袋在事件流缺失"})

    # 4) 口门在口 vs 调度屏口径（同一真源两投影）
    in_port = rp.chute_in_port()
    chute_rows = []
    for c in conn.execute("SELECT * FROM chutes ORDER BY code").fetchall():
        chute_rows.append({
            "口门编号": c["id"], "口门码": c["code"],
            "在口件数": in_port.get(c["id"], 0),
            "口门状态": c["status"],
        })

    # 5) 审计覆盖：每个写口事件都应有对应审计（kind/ref 与写口侧一致）
    audit_breaks = []
    for e in conn.execute(
        "SELECT * FROM sort_events WHERE shift_id=? ORDER BY id", (shift_id,)
    ).fetchall():
        n = conn.execute(
            "SELECT COUNT(*) c FROM audit_events WHERE kind=? AND ref_id=?",
            (_AUDIT_KIND[e["kind"]], _audit_ref_for(e["kind"], e["scan_id"], e["bag_id"], e["batch_id"])),
        ).fetchone()["c"]
        if n == 0:
            audit_breaks.append({"事件编号": e["id"], "类型": e["kind"]})

    scan_total = conn.execute("SELECT COUNT(*) c FROM scans WHERE shift_id=?", (shift_id,)).fetchone()["c"]
    alive_total = len(rp.scans)
    return {
        "班次编号": shift_id,
        "四格": cells,
        "重放件数": alive_total,
        "扫描册件数": scan_total,
        "口门在口": chute_rows,
        "袋册": [{"袋编号": k, "状态": v.status, "件数": len(v.scan_ids)} for k, v in sorted(rp.bags.items())],
        "差异": diffs,
        "审计缺口": audit_breaks,
        "对齐": not diffs and not audit_breaks,
    }

