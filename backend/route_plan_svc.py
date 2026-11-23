"""冷链线路计划与承运排程窗闸。

窗闸门钉死在写路径：
- 新建线路窗须自身合法；
- 改线（PUT 窗）不得与该线既有正式批次窗撞车，否则拒且零改；
- 窗状态一律取自 window_svc，线路页不自行判窗。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from models import RouteAdjustIn, RouteIn
from window_svc import (
    SHADOW_STATUS,
    VOID_STATUS,
    board_row,
    parse_window,
    schedule_board,
    windows_overlap,
)

router = APIRouter(tags=["routes"])


@router.get("/api/routes")
def list_routes(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        # 承运视图与排程板同源：直接取 window_svc 的计算结果
        board = schedule_board(conn)
        by_id = {r["id"]: r for r in board["routes"]}
        rows = conn.execute(
            "SELECT * FROM route_plans ORDER BY id"
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            extra = by_id.get(d["id"], {})
            d["formal_occupancy"] = extra.get("formal_occupancy", 0)
            d["shadow_pending"] = extra.get("shadow_pending", 0)
            d["window_conflict"] = extra.get("window_conflict", False)
            items.append(d)
        return {"items": items}


@router.get("/api/routes/{route_id}")
def get_route(route_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM route_plans WHERE id=?", (route_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "线路不存在")
        return board_row(conn, row)


@router.post("/api/routes")
def create_route(body: RouteIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    has_win = bool(body.window_start or body.window_end)
    window_ok = True
    if has_win:
        parse_window(body.window_start, body.window_end)  # 非法窗 422 拒写
    with connect() as conn:
        if conn.execute(
            "SELECT id FROM route_plans WHERE route_code=?", (body.route_code,)
        ).fetchone():
            raise HTTPException(409, "线路编码已存在")
        cur = conn.execute(
            """INSERT INTO route_plans(route_code,origin_region,dest_region,cold_hours,
               vehicle_type,status,window_start,window_end,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                body.route_code,
                body.origin_region,
                body.dest_region,
                body.cold_hours,
                body.vehicle_type,
                "启用",
                body.window_start if has_win else None,
                body.window_end if has_win else None,
                now_iso(),
            ),
        )
        write_audit(
            conn,
            user,
            "route_create",
            body.route_code,
            {"id": cur.lastrowid, "window_ok": window_ok},
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "route_code": body.route_code,
            "status": "启用",
            "window_start": body.window_start if has_win else None,
            "window_end": body.window_end if has_win else None,
            "window_ok": window_ok,
        }


@router.put("/api/routes/{route_id}")
def adjust_route_window(route_id: int, body: RouteAdjustIn, authorization: AuthHeader = None):
    """改线消冲突：只动排程窗；新窗不得撞该线既有正式批次。"""
    user = require_writer(authorization)
    new_s, new_e = parse_window(body.window_start, body.window_end)
    with connect() as conn:
        route = conn.execute(
            "SELECT * FROM route_plans WHERE id=?", (route_id,)
        ).fetchone()
        if not route:
            raise HTTPException(404, "线路不存在")
        formal = conn.execute(
            """SELECT * FROM temp_batches
               WHERE route_id=? AND status NOT IN (?, ?) ORDER BY id""",
            (route_id, SHADOW_STATUS, VOID_STATUS),
        ).fetchall()
        for b in formal:
            if not b["window_start"] or not b["window_end"]:
                continue
            bs, be = parse_window(b["window_start"], b["window_end"])
            if windows_overlap(new_s, new_e, bs, be):
                raise HTTPException(
                    409,
                    f"改线后仍与正式批次 {b['batch_code']} 温控窗撞车，拒绝零改",
                )
        conn.execute(
            "UPDATE route_plans SET window_start=?, window_end=? WHERE id=?",
            (body.window_start, body.window_end, route_id),
        )
        write_audit(
            conn,
            user,
            "route_window_adjust",
            route["route_code"],
            {"window_start": body.window_start, "window_end": body.window_end},
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM route_plans WHERE id=?", (route_id,)
        ).fetchone()
        return board_row(conn, row)
