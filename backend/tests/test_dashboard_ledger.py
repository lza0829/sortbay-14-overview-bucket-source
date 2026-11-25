"""调度屏四格真源回归夹具。

覆盖需求点名的四类缺陷：
  1. 手工改计假齐 —— 改计口必须拒绝，四格不得被非事件写入改齐。
  2. 封袋减在口漏已路由格 —— 封袋必须同时把件从已路由格移入已封袋格。
  3. 装载/封袋并发漂数 —— 并发写口后四格仍等于重放，点计不漂。
  4. 扫描册/口门页/调度屏长期拧花 —— 对账必须能报出并定位差异。
另覆盖：半截回执整笔回滚、选定口径重放、审计与事件流同刷。
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "sortbay.db"
    monkeypatch.setenv("SORTBAY_DB", str(db_path))
    # 让 app.config 与 db 以新路径重新加载
    for mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        del sys.modules[mod]
    main = importlib.import_module("app.main")
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c


def _cells(c, shift=None):
    params = {} if shift is None else {"班次编号": shift}
    return c.get("/api/调度屏", params=params).json()["四格"]


def _seed_pending(c) -> int:
    """种子库自带一笔待路由扫描，并发类断言需以此为基线。"""
    return _cells(c)["待路由"]


def _reconcile(c):
    return c.get("/api/调度屏/对账").json()


def _load_route(c, barcode, dest="110"):
    r = c.post("/api/扫描", json={"条码": barcode, "目的地码": dest})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert c.post(f"/api/扫描/{sid}/路由").status_code == 200
    return sid


def _open_bag(c, chute=1):
    r = c.post("/api/封袋/开袋", json={"口门编号": chute})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------------------------------------------------------------- 1. 手工改计假齐
def test_manual_adjust_is_rejected_and_cells_unchanged(client):
    _load_route(client, "MJ1")
    before = _cells(client)
    r = client.post("/api/调度屏/手工改计", json={"待路由": 0, "已路由": 99})
    assert r.status_code == 409, r.text
    assert "拒绝手工改计" in r.json()["detail"]
    assert _cells(client) == before, "改计被拒后四格不得变化"
    # 也不存在任何 POST 直接改四格的旁路
    assert _reconcile(client)["对齐"] is True


# ---------------------------------------------------------------- 2. 封袋减在口漏已路由格
def test_seal_moves_item_out_of_routed_cell(client):
    sid = _load_route(client, "SEAL1")
    bag = _open_bag(client, chute=1)
    assert client.post(f"/api/封袋/{bag}/入袋", json={"扫描编号": sid}).status_code == 200
    before = _cells(client)
    assert before["已路由"] == 1 and before["已封袋"] == 0
    assert client.post(f"/api/封袋/{bag}/封口").status_code == 200
    after = _cells(client)
    assert after["已路由"] == 0, "封袋必须把件移出已路由格"
    assert after["已封袋"] == 1
    # 口门在口同步减 1
    port = {r["口门码"]: r["在口件数"] for r in client.get("/api/调度屏").json()["口门在口"]}
    assert port["A01"] == 0
    assert _reconcile(client)["对齐"] is True


# ---------------------------------------------------------------- 3. 装载/封袋并发漂数
def test_concurrent_load_and_seal_does_not_drift(client):
    baseline_pending = _seed_pending(client)
    # 预备：10 件已入袋待封
    sids = [_load_route(client, f"CC{i}") for i in range(10)]
    bag = _open_bag(client, chute=1)
    for sid in sids:
        assert client.post(f"/api/封袋/{bag}/入袋", json={"扫描编号": sid}).status_code == 200

    errors: list[str] = []

    def seal_worker():
        try:
            r = client.post(f"/api/封袋/{bag}/封口")
            if r.status_code != 200:
                errors.append(r.text)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def load_worker(i):
        try:
            r = client.post("/api/扫描", json={"条码": f"NEW{i}", "目的地码": "210"})
            if r.status_code != 200:
                errors.append(r.text)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=seal_worker)]
    threads += [threading.Thread(target=load_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    cells = _cells(client)
    # 10 件封袋 + 10 件新装载待路由（外加种子那笔），点计不漂
    assert cells["已封袋"] == 10
    assert cells["待路由"] == baseline_pending + 10
    assert cells["已路由"] == 0
    rec = _reconcile(client)
    assert rec["对齐"] is True, rec
    # 四格必须能由事件流重放复算
    assert rec["四格"] == cells


# ---------------------------------------------------------------- 4. 册页与调度屏拧花可定位
def test_reconcile_detects_cache_drift(client):
    sid = _load_route(client, "DRIFT1")
    assert _reconcile(client)["对齐"] is True
    # 绕过写口直接篡改扫描册缓存，模拟「有人只改扫描状态集合」
    from app.config import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE scans SET status='已封袋' WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    rec = _reconcile(client)
    assert rec["对齐"] is False
    kinds = {d["问题"] for d in rec["差异"]}
    assert "状态拧花" in kinds
    # 调度屏仍按事件流给出真值（已路由），不被篡改的扫描册带偏
    assert _cells(client)["已路由"] == 1
    assert _cells(client)["已封袋"] == 0


# ---------------------------------------------------------------- 5. 半截回执整笔回滚
def test_half_receipt_rolls_back_atomically(client, monkeypatch):
    _load_route(client, "RB1")
    before = _cells(client)
    # 让事件追加在写缓存之后、审计之前失败，制造半截回执
    import app.services.ledger as ledger

    orig = ledger.append_event

    def boom(*a, **k):
        if k.get("kind") == ledger.装载:
            raise ledger.LedgerError("模拟半截写入")
        return orig(*a, **k)

    monkeypatch.setattr(ledger, "append_event", boom)
    r = client.post("/api/扫描", json={"条码": "RB2", "目的地码": "110"})
    assert r.status_code >= 500 or r.status_code == 400
    monkeypatch.setattr(ledger, "append_event", orig)
    # 四格与点计都不漂：回滚干净，没有孤立的 scans 行，也没有半截事件
    assert _cells(client) == before
    rec = _reconcile(client)
    assert rec["对齐"] is True, rec
    names = [s["barcode"] for s in client.get("/api/扫描").json()]
    assert "RB2" not in names, "半截回执不得留下扫描册残行"


# ---------------------------------------------------------------- 6. 重放口径可复算
def test_cells_reproducible_by_replay(client):
    sid = _load_route(client, "RP1")
    bag = _open_bag(client, chute=1)
    client.post(f"/api/封袋/{bag}/入袋", json={"扫描编号": sid})
    client.post(f"/api/封袋/{bag}/封口")
    board = client.get("/api/调度屏").json()
    replay = client.get("/api/调度屏/重放").json()
    assert board["四格"] == replay["四格"]
    trace = client.get("/api/调度屏/轨迹").json()["轨迹"]
    # 轨迹最后落格自洽：RP1 最后一手应落在已封袋
    last = [t for t in trace if t["扫描编号"] == sid][-1]
    assert last["落格"] == "已封袋"
    # 半截重放（截止事件）得到的是一个已提交前缀，四格总数不大于终态
    upto = trace[1]["事件编号"]
    prefix = client.get("/api/调度屏/重放", params={"截止事件编号": upto}).json()
    assert sum(prefix["四格"].values()) <= sum(board["四格"].values())


# ---------------------------------------------------------------- 7. 错分回流减在口
def test_divert_removes_from_port_and_bag(client):
    sid = _load_route(client, "DV1")
    bag = _open_bag(client, chute=1)
    client.post(f"/api/封袋/{bag}/入袋", json={"扫描编号": sid})
    assert client.post("/api/回流批次", json={"扫描编号": sid}).status_code == 200
    cells = _cells(client)
    assert cells["错分回流"] == 1
    assert cells["已路由"] == 0
    port = {r["口门码"]: r["在口件数"] for r in client.get("/api/调度屏").json()["口门在口"]}
    assert port["A01"] == 0
    assert _reconcile(client)["对齐"] is True


# ---------------------------------------------------------------- 8. 老库迁移对齐
def test_legacy_backfill_migrates_to_alignment(tmp_path, monkeypatch):
    """模拟无 sort_events 的老库：扫描册/袋册已推进但无事件，启动时补记后应对齐。"""
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("SORTBAY_DB", str(db_path))
    for mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        del sys.modules[mod]

    # 先只建 schema 与基础业务行（不带任何事件），复刻老库形态
    import app.db as dbmod
    dbmod.init_db()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO lines(name, active) VALUES ('一号皮带',1)")
    conn.execute("INSERT INTO shifts(business_day, name, sealed) VALUES ('2026-01-01','早班',0)")
    conn.execute("INSERT INTO chutes(line_id, code, title, status, soft_cap) VALUES (1,'A01','格','口门开',120)")
    conn.execute("INSERT INTO route_rules(dest_code, chute_id, note) VALUES ('110',1,'')")
    conn.execute("INSERT INTO bags(shift_id, chute_id, code, status, sealed_at) VALUES (1,1,'袋-OLD','已封袋','2026-01-01 01:00:00')")
    conn.execute("INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, chute_id, bag_id, scanned_at) VALUES ('OLD1','110',1,1,'已封袋',1,1,'2026-01-01 00:00:00')")
    conn.execute("INSERT INTO scans(barcode, dest_code, shift_id, line_id, status, scanned_at) VALUES ('OLD2','110',1,1,'待路由','2026-01-01 00:00:00')")
    conn.commit()
    conn.close()

    from app.seed import backfill_events
    backfill_events()

    from app.main import app as fastapi_app
    with TestClient(fastapi_app, raise_server_exceptions=False) as c:
        rec = c.get("/api/调度屏/对账").json()
        assert rec["对齐"] is True, rec
        assert rec["四格"] == {"待路由": 1, "已路由": 0, "错分回流": 0, "已封袋": 1}
        # 再次运行不应重复补记
        backfill_events()
        assert c.get("/api/调度屏/对账").json()["对齐"] is True

