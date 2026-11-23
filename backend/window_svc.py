"""排程窗 × 温控窗的唯一计算源。

五处读面（承运视图 /api/routes、排程视图 /api/schedule-board、
温控视图 /api/batches、封存视图 /api/seals、互证视图 /api/reconcile、
/api/proof）的窗状态一律取自本模块，禁止各页自行判窗。
写路径（线路窗闸 route_plan_svc、批次建口 batches_svc）同样只调本模块。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect, now_iso

router = APIRouter(tags=["window"])

SHADOW_STATUS = "影子"
VOID_STATUS = "作废"
SEALED_STATUS = "已封批"
FORMAL_INTENT = "正式"
SHADOW_INTENT = "挤窗"
# 对账/配额口径：已封批、作废、影子均不占在批配额
ACTIVE_EXCLUDE = ("已封批", "作废", "影子")
ACTIVE_SQL = "status NOT IN ('已封批','作废','影子')"


def parse_window(start: str | None, end: str | None) -> tuple[datetime, datetime]:
    if not start or not end:
        raise HTTPException(422, "温控窗/排程窗须同时给出起止时刻")
    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
    except ValueError as exc:
        raise HTTPException(422, "窗时刻格式非法，须 ISO8601（如 2026-12-01T08:00:00）") from exc
    if s >= e:
        raise HTTPException(422, "窗起须早于窗止")
    return s, e


def windows_overlap(s1: datetime, e1: datetime, s2: datetime, e2: datetime) -> bool:
    """半开区间撞车判定：[s,e) 相交即冲突。"""
    return s1 < e2 and s2 < e1


def route_window_collides(conn, route_id: int, start: str, end: str) -> dict | None:
    """温控窗与指定承运排程窗撞车时返回线路行，否则 None。"""
    route = conn.execute(
        "SELECT * FROM route_plans WHERE id=?", (route_id,)
    ).fetchone()
    if route is None:
        raise HTTPException(404, "线路不存在")
    rs, re_ = route["window_start"], route["window_end"]
    if not rs or not re_:
        return None
    s1, e1 = parse_window(start, end)
    s2, e2 = parse_window(rs, re_)
    return dict(route) if windows_overlap(s1, e1, s2, e2) else None


def log_conflict(
    conn,
    *,
    route_id: int | None,
    route_code: str,
    batch_code: str,
    chamber_id: int | None,
    decision: str,
    detail: str = "",
) -> None:
    """撞车窗纪行：当班封死，全仓只允许 INSERT，无改删口。"""
    conn.execute(
        """INSERT INTO window_conflict_log
           (route_id,route_code,batch_code,chamber_id,decision,detail,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (route_id, route_code, batch_code, chamber_id, decision, detail, now_iso()),
    )


def _batch_window_state(conn, row) -> dict:
    """单个批次行的窗状态，所有读面共用。"""
    d = dict(row)
    d["window_conflict"] = False
    if d.get("route_id") and d.get("window_start") and d.get("window_end"):
        route = conn.execute(
            "SELECT route_code,window_start,window_end FROM route_plans WHERE id=?",
            (d["route_id"],),
        ).fetchone()
        if route and route["window_start"] and route["window_end"]:
            try:
                s1, e1 = parse_window(d["window_start"], d["window_end"])
                s2, e2 = parse_window(route["window_start"], route["window_end"])
                d["window_conflict"] = windows_overlap(s1, e1, s2, e2)
                d["route_code"] = route["route_code"]
            except HTTPException:
                d["window_conflict"] = False
    return d


def board_row(conn, route) -> dict:
    """排程板单行：线路窗 + 占用批次（正式/影子）+ 同源撞车标记。

    撞车态有两个同源输入：在档批次窗实测重叠、window_conflict_log 当班纪行
    （拒建/落影子/拒晋升），任一命中即报冲突，五处读面共用此口径。
    """
    r = dict(route)
    batches = conn.execute(
        """SELECT id,batch_code,chamber_id,status,plan_intent,window_start,window_end,
                  route_id FROM temp_batches
           WHERE route_id=? AND status!=? ORDER BY id""",
        (route["id"], VOID_STATUS),
    ).fetchall()
    items = [_batch_window_state(conn, b) for b in batches]
    formal = [b for b in items if b["status"] != SHADOW_STATUS]
    shadows = [b for b in items if b["status"] == SHADOW_STATUS]
    logs = conn.execute(
        """SELECT batch_code, decision FROM window_conflict_log
           WHERE route_id=? ORDER BY id""",
        (route["id"],),
    ).fetchall()
    # 已晋升/已作废的批次其历史纪行不再计为当班冲突
    resolved = {
        b["batch_code"]
        for b in items
        if b["status"] not in (SHADOW_STATUS,) and not b["window_conflict"]
    }
    open_logs = [
        x for x in logs
        if x["decision"] in ("拒建", "落影子", "拒晋升", "偏航")
        and x["batch_code"] not in resolved
    ]
    log_codes = sorted({x["batch_code"] for x in open_logs})
    r["formal_occupancy"] = len(formal)
    r["shadow_pending"] = len(shadows)
    r["window_conflict"] = (
        any(b["window_conflict"] for b in items) or bool(open_logs)
    )
    r["formal_window_conflict"] = any(b["window_conflict"] for b in formal)
    r["conflict_log_codes"] = log_codes
    r["batches"] = [
        {
            "batch_code": b["batch_code"],
            "status": b["status"],
            "plan_intent": b["plan_intent"],
            "window_start": b["window_start"],
            "window_end": b["window_end"],
            "window_conflict": b["window_conflict"],
        }
        for b in items
    ]
    return r


def schedule_board(conn) -> dict:
    routes = conn.execute("SELECT * FROM route_plans ORDER BY id").fetchall()
    rows = [board_row(conn, r) for r in routes]
    return {
        "routes": rows,
        "summary": {
            "total": len(rows),
            "conflict_routes": sum(1 for r in rows if r["window_conflict"]),
        },
    }


def enrich_batch(conn, row) -> dict:
    """温控/封存/互证读面共用的批次窗补全。"""
    return _batch_window_state(conn, row)


@router.get("/api/schedule-board")
def get_board(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        return schedule_board(conn)


@router.get("/api/window-conflicts")
def list_conflicts(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM window_conflict_log ORDER BY id DESC LIMIT 300"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
