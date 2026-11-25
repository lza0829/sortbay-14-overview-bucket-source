from __future__ import annotations
from datetime import date, datetime

from app.db import write_tx, init_db
from app.services import ledger
from app.services.common import audit, now_str


def seed_if_empty() -> None:
    init_db()
    with write_tx() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM lines").fetchone()["c"]
        if n:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        day = date.today().isoformat()
        conn.execute("INSERT INTO lines(name, active) VALUES (?, 1)", ("一号皮带",))
        line_id = conn.execute("SELECT id FROM lines WHERE name=?", ("一号皮带",)).fetchone()["id"]
        conn.execute(
            "INSERT INTO shifts(business_day, name, sealed) VALUES (?, ?, 0)",
            (day, "早班",),
        )
        shift_id = conn.execute(
            "SELECT id FROM shifts WHERE business_day=? AND name=?", (day, "早班")
        ).fetchone()["id"]
        chutes = [
            ("A01", "华北快运格"),
            ("A02", "华东快运格"),
            ("B01", "西南快运格"),
            ("C01", "异常暂存格"),
        ]
        chute_ids = {}
        for code, title in chutes:
            conn.execute(
                "INSERT INTO chutes(line_id, code, title, status, soft_cap) VALUES (?, ?, ?, '口门开', 120)",
                (line_id, code, title),
            )
            chute_ids[code] = conn.execute(
                "SELECT id FROM chutes WHERE line_id=? AND code=?", (line_id, code)
            ).fetchone()["id"]
        routes = [
            ("110", "A01"),
            ("210", "A02"),
            ("510", "B01"),
            ("999", "C01"),
        ]
        for dest, chute in routes:
            conn.execute(
                "INSERT INTO route_rules(dest_code, chute_id, note) VALUES (?, ?, ?)",
                (dest, chute_ids[chute], "种子路由"),
            )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?), (?, ?)",
            ("口门软容量提示", "120", "回流自动再路由", "否"),
        )
        # 预置一笔待路由扫描，方便冒烟看见分布；装载事件与扫描册同事务落齐
        conn.execute(
            """
            INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, scanned_at, note)
            VALUES (?, ?, ?, ?, '待路由', ?, ?)
            """,
            ("YB202611240001", "110", shift_id, line_id, now, "种子待路由"),
        )
        scan_id = conn.execute(
            "SELECT id FROM scans WHERE barcode=? AND shift_id=?", ("YB202611240001", shift_id)
        ).fetchone()["id"]
        ledger.append_event(
            conn, shift_id=shift_id, kind=ledger.装载, scan_id=scan_id,
            note="YB202611240001", actor="种子", dedup_key=f"装载:{scan_id}",
        )
        audit(conn, "系统", None, "种子初始化", "分拣过机台")


def backfill_events() -> None:
    """老库迁移：给尚无事件流的扫描/袋补记事件，避免四格对账长期分叉。

    只在「该处完全没有对应事件」时补，绝不改写已存在的事件流。补记顺序必须
    符合在线生命周期：先袋（开袋）→ 再件（装载/裁决/入袋）→ 最后封袋，
    否则入袋事件会先于开袋、封袋会被漏补。补记在单写事务内完成，失败整笔回滚。
    """
    with write_tx() as conn:
        # 1) 袋：无开袋事件则补开袋；无封袋事件但袋册已封则补封袋
        bags = conn.execute(
            """
            SELECT b.* FROM bags b
            WHERE NOT EXISTS (SELECT 1 FROM sort_events e WHERE e.bag_id=b.id AND e.kind='开袋')
            ORDER BY b.id
            """
        ).fetchall()
        for b in bags:
            ledger.append_event(
                conn, shift_id=b["shift_id"], kind=ledger.开袋, bag_id=b["id"],
                chute_id=b["chute_id"], note=b["code"], actor="迁移补记",
            )
        sealed_bags = conn.execute(
            """
            SELECT b.* FROM bags b
            WHERE b.status='已封袋'
              AND NOT EXISTS (SELECT 1 FROM sort_events e WHERE e.bag_id=b.id AND e.kind='封袋')
            ORDER BY b.id
            """
        ).fetchall()
        # 2) 扫描件：无装载事件则按当前状态补装载，再按需补裁决/回流/入袋
        scans = conn.execute(
            """
            SELECT s.* FROM scans s
            WHERE NOT EXISTS (SELECT 1 FROM sort_events e WHERE e.scan_id=s.id)
            ORDER BY s.id
            """
        ).fetchall()
        for s in scans:
            ledger.append_event(
                conn, shift_id=s["shift_id"], kind=ledger.装载, scan_id=s["id"],
                note=s["barcode"], actor="迁移补记", dedup_key=f"装载:{s['id']}",
            )
            if s["status"] in ("已路由", "已入袋", "已封袋") and s["chute_id"]:
                ledger.append_event(
                    conn, shift_id=s["shift_id"], kind=ledger.裁决, scan_id=s["id"],
                    chute_id=s["chute_id"], note="迁移补记", actor="迁移补记",
                )
            if s["status"] == "错分回流":
                ledger.append_event(
                    conn, shift_id=s["shift_id"], kind=ledger.回流, scan_id=s["id"],
                    batch_id=s["divert_batch_id"], chute_id=s["chute_id"],
                    note="迁移补记", actor="迁移补记",
                )
            if s["status"] in ("已入袋", "已封袋") and s["bag_id"]:
                ledger.append_event(
                    conn, shift_id=s["shift_id"], kind=ledger.入袋, scan_id=s["id"],
                    bag_id=s["bag_id"], chute_id=s["chute_id"], note="迁移补记", actor="迁移补记",
                )
        # 3) 已封袋且封袋事件缺失的袋，最后补封袋（此时袋内件已入袋）
        for b in sealed_bags:
            ledger.append_event(
                conn, shift_id=b["shift_id"], kind=ledger.封袋, bag_id=b["id"],
                chute_id=b["chute_id"], note="迁移补记", actor="迁移补记",
            )
