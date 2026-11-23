"""BUGFIX-09 撞车窗闸门三红测：甲拒建 / 乙影子晋升 / 同线同档争用。

红绿纪律：本文件先于实现落地，三个测试必须先在当前代码上打红，
且红因须分别对准「重叠仍落正式批」「挤窗意图未晋升却生效」「双人抢建」。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

ROUTE_WIN = ("2026-12-01T08:00:00", "2026-12-01T20:00:00")
OVERLAP_WIN = ("2026-12-01T18:00:00", "2026-12-01T22:00:00")
CLEAR_WIN = ("2026-12-02T08:00:00", "2026-12-02T10:00:00")


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _chamber_probe(c: TestClient, h: dict) -> tuple[int, int]:
    ch = next(
        x
        for x in c.get("/api/chambers", headers=h).json()["items"]
        if x["chamber_code"] == "CK-HN-03"
    )
    probe = c.get(
        f"/api/probes?chamber_id={ch['id']}", headers=h
    ).json()["items"][0]
    return ch["id"], probe["id"]


def _make_route(c: TestClient, h: dict, code: str = "RT-WIN-01") -> dict:
    r = c.post(
        "/api/routes",
        headers=h,
        json={
            "route_code": code,
            "origin_region": "华南",
            "dest_region": "华东",
            "cold_hours": 12.0,
            "vehicle_type": "冷藏车",
            "window_start": ROUTE_WIN[0],
            "window_end": ROUTE_WIN[1],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _formal_count() -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM temp_batches WHERE status NOT IN "
            "('已封批','作废','影子')"
        ).fetchone()["c"]


def _all_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) c FROM temp_batches").fetchone()["c"]


# ---- 失败测甲：排程窗覆盖温控窗，正式建口须零增拒建 ----------------------

def test_a_formal_create_rejected_on_window_overlap():
    c = TestClient(app)
    h = _auth(c)
    chamber_id, probe_id = _chamber_probe(c, h)
    route = _make_route(c, h)
    with connect() as conn:
        before_route_row = dict(conn.execute(
            "SELECT * FROM route_plans WHERE id=?", (route["id"],)
        ).fetchone())
    before_formal = _formal_count()
    before_all = _all_count()

    r = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": chamber_id,
            "probe_id": probe_id,
            "cargo_ref": "撞车甲货载",
            "batch_code": "TB-CRASH-A",
            "route_id": route["id"],
            "window_start": OVERLAP_WIN[0],
            "window_end": OVERLAP_WIN[1],
            "temp_points": [{"t": "18:00", "c": 3.5}],
        },
    )
    assert r.status_code == 409, r.text
    # 正式行零增、任何批次行零增
    assert _formal_count() == before_formal
    assert _all_count() == before_all
    # 线路占用零改：route_plans 原始行一字未动
    with connect() as conn:
        after_route_row = dict(conn.execute(
            "SELECT * FROM route_plans WHERE id=?", (route["id"],)
        ).fetchone())
    assert after_route_row == before_route_row
    # 撞车窗纪行当班封死：只增一条拒建
    conflicts = c.get("/api/window-conflicts", headers=h).json()["items"]
    mine = [x for x in conflicts if x["batch_code"] == "TB-CRASH-A"]
    assert len(mine) == 1
    assert mine[0]["decision"] == "拒建"
    assert mine[0]["route_code"] == "RT-WIN-01"
    # 排程视图同源报撞
    board = c.get("/api/schedule-board", headers=h).json()["routes"]
    row = next(x for x in board if x["route_code"] == "RT-WIN-01")
    assert row["window_conflict"] is True
    # 无重叠时同档正式批应可落（对照组在绿灯期生效）
    ok = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": chamber_id,
            "probe_id": probe_id,
            "cargo_ref": "合规货载",
            "batch_code": "TB-CLEAR-A",
            "route_id": route["id"],
            "window_start": CLEAR_WIN[0],
            "window_end": CLEAR_WIN[1],
            "temp_points": [{"t": "08:00", "c": 3.2}],
        },
    )
    assert ok.status_code == 200, ok.text
    assert _formal_count() == before_formal + 1


# ---- 失败测乙：挤窗影子未晋升不得生效，改线后整笔晋升 ---------------------

def test_b_shadow_intent_not_effective_until_promoted():
    c = TestClient(app)
    h = _auth(c)
    chamber_id, probe_id = _chamber_probe(c, h)
    route = _make_route(c, h)
    before_formal = _formal_count()
    before_all = _all_count()
    recon_before = c.get(
        "/api/reconcile/CK-HN-03", headers=h
    ).json()["active_batches"]

    r = c.post(
        "/api/batches/shadow-intent",
        headers=h,
        json={
            "chamber_id": chamber_id,
            "probe_id": probe_id,
            "cargo_ref": "挤窗乙货载",
            "batch_code": "TB-SHADOW-B",
            "route_id": route["id"],
            "window_start": OVERLAP_WIN[0],
            "window_end": OVERLAP_WIN[1],
            "temp_points": [{"t": "19:00", "c": 4.0}],
        },
    )
    assert r.status_code == 200, r.text
    shadow_id = r.json()["id"]
    assert r.json()["status"] == "影子"
    assert r.json()["plan_intent"] == "挤窗"
    # 正式行零改、配额不占
    assert _formal_count() == before_formal
    assert (
        c.get("/api/reconcile/CK-HN-03", headers=h).json()["active_batches"]
        == recon_before
    )
    formal_list = c.get("/api/batches", headers=h).json()["items"]
    assert all(b["batch_code"] != "TB-SHADOW-B" for b in formal_list)
    shadow_rows = c.get(
        "/api/batches?scope=shadow", headers=h
    ).json()["items"]
    assert any(b["batch_code"] == "TB-SHADOW-B" for b in shadow_rows)
    # 影子不得封批
    seal = c.post("/api/seals", headers=h, json={"batch_id": shadow_id})
    assert seal.status_code == 409
    # 未改线先晋升：拒且零改
    pr = c.post(f"/api/batches/{shadow_id}/promote", headers=h)
    assert pr.status_code == 409
    still = c.get(f"/api/batches/{shadow_id}", headers=h).json()["batch"]
    assert still["status"] == "影子"
    assert _formal_count() == before_formal
    # 无冲突却走乙路径属偏航，拒写
    drift = c.post(
        "/api/batches/shadow-intent",
        headers=h,
        json={
            "chamber_id": chamber_id,
            "probe_id": probe_id,
            "cargo_ref": "偏航货载",
            "batch_code": "TB-DRIFT-B",
            "route_id": route["id"],
            "window_start": CLEAR_WIN[0],
            "window_end": CLEAR_WIN[1],
            "temp_points": [],
        },
    )
    assert drift.status_code == 422
    # 偏航拒写：批次表零增（仅先前那笔影子）
    assert _all_count() == before_all + 1
    drift_log = c.get("/api/window-conflicts", headers=h).json()["items"]
    assert any(x["decision"] == "偏航" for x in drift_log)
    # 改线消冲突后整笔晋升
    adj = c.put(
        f"/api/routes/{route['id']}",
        headers=h,
        json={
            "window_start": "2026-12-03T08:00:00",
            "window_end": "2026-12-03T20:00:00",
        },
    )
    assert adj.status_code == 200, adj.text
    pr2 = c.post(f"/api/batches/{shadow_id}/promote", headers=h)
    assert pr2.status_code == 200, pr2.text
    assert pr2.json()["status"] == "已入库"
    assert pr2.json()["plan_intent"] == "正式"
    promoted = c.get(f"/api/batches/{shadow_id}", headers=h).json()["batch"]
    assert promoted["status"] == "已入库"
    assert _formal_count() == before_formal + 1
    decisions = [
        x["decision"]
        for x in c.get("/api/window-conflicts", headers=h).json()["items"]
        if x["batch_code"] == "TB-SHADOW-B"
    ]
    assert "落影子" in decisions and "晋升" in decisions
    # 作废支路：第二笔影子可作废
    r2 = c.post(
        "/api/batches/shadow-intent",
        headers=h,
        json={
            "chamber_id": chamber_id,
            "probe_id": probe_id,
            "cargo_ref": "挤窗待废",
            "batch_code": "TB-SHADOW-VOID",
            "route_id": route["id"],
            "window_start": "2026-12-03T18:00:00",
            "window_end": "2026-12-03T22:00:00",
            "temp_points": [],
        },
    )
    assert r2.status_code == 200, r2.text
    void_id = r2.json()["id"]
    v = c.post(f"/api/batches/{void_id}/void", headers=h)
    assert v.status_code == 200
    assert v.json()["status"] == "作废"


# ---- 失败测丙：两路调度同线同档抢建，计划锁串行仅存痕 ---------------------

def test_c_concurrent_same_route_slot_serialized(monkeypatch):
    # 测试侧给计划锁一个保持窗，稳定复现两路同时抢同线同档（不修改生产码）
    import time

    import batches_svc

    real_acquire = batches_svc.try_acquire

    def slow_plan_acquire(conn, lock_key, holder):
        ok = real_acquire(conn, lock_key, holder)
        if ok and lock_key.startswith("plan:route:"):
            time.sleep(0.4)
        return ok

    monkeypatch.setattr(batches_svc, "try_acquire", slow_plan_acquire)
    c = TestClient(app)
    h = _auth(c)
    # 两路调度各占不同仓间（仓间锁互不冲突），抢同一线路档位
    chs = c.get("/api/chambers", headers=h).json()["items"]
    ch1 = next(x for x in chs if x["chamber_code"] == "CK-HN-03")
    ch2 = next(x for x in chs if x["chamber_code"] == "CK-HD-01")
    pr1 = c.get(f"/api/probes?chamber_id={ch1['id']}", headers=h).json()["items"][0]
    pr2 = c.get(f"/api/probes?chamber_id={ch2['id']}", headers=h).json()["items"][0]
    route = _make_route(c, h)
    before_formal = _formal_count()
    barrier = threading.Barrier(2)
    results: list[dict] = []

    def _writer(user: str, code: str, chamber_id: int, probe_id: int,
                win: tuple[str, str], temp: float) -> None:
        cli = TestClient(app)
        hh = _auth(cli, user)
        barrier.wait()
        resp = cli.post(
            "/api/batches",
            headers=hh,
            json={
                "chamber_id": chamber_id,
                "probe_id": probe_id,
                "cargo_ref": f"争用-{user}",
                "batch_code": code,
                "route_id": route["id"],
                "window_start": win[0],
                "window_end": win[1],
                "temp_points": [{"t": "08:30", "c": temp}],
            },
        )
        results.append({"user": user, "status": resp.status_code})

    # 两个批次窗彼此不重叠且均不撞排程窗，争用只发生在计划锁
    t1 = threading.Thread(
        target=_writer,
        args=("admin", "TB-RACE-1", ch1["id"], pr1["id"],
              ("2026-12-02T08:00:00", "2026-12-02T09:00:00"), 3.1),
    )
    t2 = threading.Thread(
        target=_writer,
        args=("ops", "TB-RACE-2", ch2["id"], pr2["id"],
              ("2026-12-02T09:00:00", "2026-12-02T10:00:00"), -19.5),
    )
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert len(results) == 2
    statuses = sorted(r["status"] for r in results)
    assert statuses == [200, 409], results
    # 正式行恰好增一
    assert _formal_count() == before_formal + 1
    # 败者仅留争用痕
    cont = c.get("/api/contention", headers=h).json()["items"]
    assert any(
        x["lock_key"] == f"plan:route:{route['id']}" for x in cont
    ), cont
    winner = next(r["user"] for r in results if r["status"] == 200)
    loser = "ops" if winner == "admin" else "admin"
    trace = next(
        x
        for x in cont
        if x["lock_key"] == f"plan:route:{route['id']}"
    )
    assert trace["actor"] == loser
