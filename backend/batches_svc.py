"""温控批次入库：温度点序列 JSON。

写路径只有两条，入口分立、不得混用：
- 甲 POST /api/batches            正式建口：撞排程窗即零增拒建，线路零改；
- 乙 POST /api/batches/shadow-intent 挤窗影子：须真撞窗，落影子不占配额，
       待改线后 promote 整笔晋升，否则 void 作废。
同线同档以计划锁 plan:route:{id} 串行，争用仅留痕不写业务。
"""
from __future__ import annotations

import json
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, try_acquire
from models import BatchIn
from validators import summarize_points, validate_probe_online, validate_temp_point
from window_svc import (
    ACTIVE_SQL,
    FORMAL_INTENT,
    SHADOW_INTENT,
    SHADOW_STATUS,
    VOID_STATUS,
    enrich_batch,
    log_conflict,
    parse_window,
    route_window_collides,
)

router = APIRouter(tags=["batches"])


def _next_batch_code(conn, chamber_id: int) -> str:
    ch = conn.execute(
        "SELECT chamber_code FROM cold_chambers WHERE id=?", (chamber_id,)
    ).fetchone()
    prefix = ch["chamber_code"] if ch else f"CH-{chamber_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM temp_batches WHERE chamber_id=?",
        (chamber_id,),
    ).fetchone()["c"]
    return f"TB-{prefix}-{n + 1:03d}"


def _next_shadow_code(conn, chamber_id: int) -> str:
    ch = conn.execute(
        "SELECT chamber_code FROM cold_chambers WHERE id=?", (chamber_id,)
    ).fetchone()
    prefix = ch["chamber_code"] if ch else f"CH-{chamber_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM temp_batches WHERE chamber_id=?",
        (chamber_id,),
    ).fetchone()["c"]
    return f"SH-{prefix}-{n + 1:03d}"


@contextmanager
def _plan_lock(conn, route_id: int | None, holder: str):
    """计划锁：无线路批次不占档位；争用痕落库后抛 409，业务零写。"""
    if route_id is None:
        yield
        return
    key = f"plan:route:{route_id}"
    if not try_acquire(conn, key, holder):
        write_audit(conn, holder, "plan_lock_contention", key, {"ok": False})
        conn.commit()  # 争用痕当班封死，不随业务回滚
        raise HTTPException(409, "争用：后到只记争用，不写业务")
    try:
        yield
    finally:
        release(conn, key, holder)


def _load_chamber_probe(conn, body: BatchIn):
    ch = conn.execute(
        "SELECT * FROM cold_chambers WHERE id=?", (body.chamber_id,)
    ).fetchone()
    if not ch:
        raise HTTPException(404, "仓间不存在")
    probe = conn.execute(
        "SELECT * FROM probes WHERE id=?", (body.probe_id,)
    ).fetchone()
    if not probe or probe["chamber_id"] != body.chamber_id:
        raise HTTPException(422, "探头与仓间不匹配")
    validate_probe_online(probe)
    return ch, probe


def _validate_window_pair(body: BatchIn) -> None:
    if (body.window_start is None) != (body.window_end is None):
        raise HTTPException(422, "温控窗须同时给出起止时刻")
    if body.window_start and body.window_end:
        parse_window(body.window_start, body.window_end)
    if body.window_start and body.route_id is None:
        raise HTTPException(422, "温控窗须挂靠承运线路档位")


def _insert_row(conn, *, body, code, status, intent, points, tmin, tmax, promoted_from=None):
    now = now_iso()
    cur = conn.execute(
        """INSERT INTO temp_batches(batch_code,chamber_id,probe_id,cargo_ref,
           temp_points,generation,status,min_observed,max_observed,created_at,
           route_id,window_start,window_end,plan_intent,promoted_from)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            code,
            body.chamber_id,
            body.probe_id,
            body.cargo_ref,
            json.dumps(points, ensure_ascii=False),
            1,
            status,
            tmin if points else None,
            tmax if points else None,
            now,
            body.route_id,
            body.window_start,
            body.window_end,
            intent,
            promoted_from,
        ),
    )
    return cur.lastrowid


@router.get("/api/batches")
def list_batches(authorization: AuthHeader = None, status: str | None = None,
                 scope: str | None = None):
    require_user(authorization)
    with connect() as conn:
        where, params = [], []
        if scope == "shadow":
            where.append(f"b.status='{SHADOW_STATUS}'")
        elif scope == "all":
            pass
        else:  # 默认温控视图只见正式行，影子不混入
            where.append(f"b.status!='{SHADOW_STATUS}'")
        if status:
            where.append("b.status=?")
            params.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"""SELECT b.*, c.chamber_code, p.probe_code FROM temp_batches b
                JOIN cold_chambers c ON c.id=b.chamber_id
                JOIN probes p ON p.id=b.probe_id{clause}
                ORDER BY b.id DESC""",
            params,
        ).fetchall()
        items = []
        for r in rows:
            d = enrich_batch(conn, r)
            d["temp_points"] = json.loads(d["temp_points"] or "[]")
            items.append(d)
        return {"items": items}


@router.get("/api/batches/{batch_id}")
def get_batch(batch_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT b.*, c.name chamber_name, c.target_min, c.target_max, p.probe_code
               FROM temp_batches b
               JOIN cold_chambers c ON c.id=b.chamber_id
               JOIN probes p ON p.id=b.probe_id WHERE b.id=?""",
            (batch_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "批次不存在")
        data = enrich_batch(conn, row)
        data["temp_points"] = json.loads(data["temp_points"] or "[]")
        exc = conn.execute(
            "SELECT * FROM excursion_cursors WHERE batch_id=? ORDER BY id DESC",
            (batch_id,),
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM seal_snapshots WHERE batch_id=? ORDER BY id DESC",
            (batch_id,),
        ).fetchall()
        return {
            "batch": data,
            "excursions": [dict(e) for e in exc],
            "seals": [dict(s) for s in seals],
        }


def _create_formal(conn, user: str, body: BatchIn) -> dict:
    """甲路径：窗重叠则正式行零增拒建、线路占用零改。"""
    ch, _probe = _load_chamber_probe(conn, body)
    _validate_window_pair(body)
    active = conn.execute(
        f"SELECT COUNT(*) c FROM temp_batches WHERE chamber_id=? AND {ACTIVE_SQL}",
        (body.chamber_id,),
    ).fetchone()["c"]
    if active >= int(ch["quota_batches"]):
        raise HTTPException(409, "仓间在批配额已满")
    points = body.temp_points or []
    for pt in points:
        validate_temp_point(
            float(pt.get("c", pt.get("temp", 0))),
            float(ch["target_min"]),
            float(ch["target_max"]),
        )
    tmin, tmax = summarize_points(points)
    code = body.batch_code or _next_batch_code(conn, body.chamber_id)
    if conn.execute(
        "SELECT id FROM temp_batches WHERE batch_code=?", (code,)
    ).fetchone():
        raise HTTPException(409, "批次编码已存在")
    # 窗闸：与承运排程窗撞车即拒（纪行封死，线路零改）
    if body.route_id is not None:
        hit = route_window_collides(conn, body.route_id, body.window_start, body.window_end)
        if hit is not None:
            log_conflict(
                conn,
                route_id=hit["id"],
                route_code=hit["route_code"],
                batch_code=code,
                chamber_id=body.chamber_id,
                decision="拒建",
                detail=f"排程窗{hit['window_start']}~{hit['window_end']}撞温控窗"
                       f"{body.window_start}~{body.window_end}",
            )
            write_audit(
                conn, user, "batch_window_reject", code,
                {"route_code": hit["route_code"], "decision": "拒建"},
            )
            conn.commit()
            raise HTTPException(409, "承运排程窗与温控窗撞车，正式批次拒建且线路零改")
    status = "已入库" if points else "草稿"
    new_id = _insert_row(
        conn, body=body, code=code, status=status,
        intent=FORMAL_INTENT, points=points, tmin=tmin, tmax=tmax,
    )
    write_audit(
        conn, user, "batch_inbound", code,
        {"batch_id": new_id, "point_count": len(points), "plan_intent": FORMAL_INTENT},
    )
    conn.commit()
    return {
        "id": new_id,
        "batch_code": code,
        "status": status,
        "plan_intent": FORMAL_INTENT,
        "point_count": len(points),
    }


def _create_shadow(conn, user: str, body: BatchIn) -> dict:
    """乙路径：允许落影子的唯一前提是真撞窗；无撞属偏航，拒写。"""
    ch, _probe = _load_chamber_probe(conn, body)
    if body.route_id is None or not body.window_start or not body.window_end:
        log_conflict(
            conn, route_id=None, route_code="-",
            batch_code=body.batch_code or "(未命名)",
            chamber_id=body.chamber_id, decision="偏航",
            detail="挤窗意图缺少线路档位或温控窗",
        )
        write_audit(conn, user, "batch_window_drift", body.batch_code or "(未命名)",
                    {"decision": "偏航"})
        conn.commit()
        raise HTTPException(422, "挤窗意图须挂靠线路档位并给出温控窗")
    _validate_window_pair(body)
    points = body.temp_points or []
    for pt in points:
        validate_temp_point(
            float(pt.get("c", pt.get("temp", 0))),
            float(ch["target_min"]),
            float(ch["target_max"]),
        )
    tmin, tmax = summarize_points(points)
    code = body.batch_code or _next_shadow_code(conn, body.chamber_id)
    if conn.execute(
        "SELECT id FROM temp_batches WHERE batch_code=?", (code,)
    ).fetchone():
        raise HTTPException(409, "批次编码已存在")
    hit = route_window_collides(conn, body.route_id, body.window_start, body.window_end)
    if hit is None:
        # 无冲突却走乙路径：偏航，正式批次零改
        log_conflict(
            conn, route_id=body.route_id, route_code="(无撞)",
            batch_code=code, chamber_id=body.chamber_id, decision="偏航",
            detail="无窗冲突却走挤窗影子路径",
        )
        write_audit(conn, user, "batch_window_drift", code, {"decision": "偏航"})
        conn.commit()
        raise HTTPException(422, "无窗冲突不得落挤窗影子，请走正式建口")
    new_id = _insert_row(
        conn, body=body, code=code, status=SHADOW_STATUS,
        intent=SHADOW_INTENT, points=points, tmin=tmin, tmax=tmax,
    )
    log_conflict(
        conn, route_id=hit["id"], route_code=hit["route_code"],
        batch_code=code, chamber_id=body.chamber_id, decision="落影子",
        detail="挤窗意图待改线后整笔晋升",
    )
    write_audit(
        conn, user, "batch_shadow_intent", code,
        {"batch_id": new_id, "route_code": hit["route_code"]},
    )
    conn.commit()
    return {
        "id": new_id,
        "batch_code": code,
        "status": SHADOW_STATUS,
        "plan_intent": SHADOW_INTENT,
        "point_count": len(points),
    }


@router.post("/api/batches")
def create_batch(body: BatchIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"chamber:{body.chamber_id}"
    with connect() as conn:
        if not try_acquire(conn, lock_key, user):
            write_audit(conn, user, "chamber_lock_contention", lock_key, {"ok": False})
            conn.commit()
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        try:
            with _plan_lock(conn, body.route_id, user):
                return _create_formal(conn, user, body)
        finally:
            release(conn, lock_key, user)


@router.post("/api/batches/shadow-intent")
def create_shadow_intent(body: BatchIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"chamber:{body.chamber_id}"
    with connect() as conn:
        if not try_acquire(conn, lock_key, user):
            write_audit(conn, user, "chamber_lock_contention", lock_key, {"ok": False})
            conn.commit()
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        try:
            with _plan_lock(conn, body.route_id, user):
                return _create_shadow(conn, user, body)
        finally:
            release(conn, lock_key, user)


@router.post("/api/batches/{batch_id}/promote")
def promote_shadow(batch_id: int, authorization: AuthHeader = None):
    """乙路径收口：改线消冲突后整笔晋升；仍撞则零改拒晋升。"""
    user = require_writer(authorization)
    with connect() as conn:
        batch = conn.execute(
            "SELECT * FROM temp_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, "批次不存在")
        if batch["status"] == VOID_STATUS:
            raise HTTPException(409, "意图已作废，不得晋升")
        if batch["status"] != SHADOW_STATUS:
            raise HTTPException(409, "仅挤窗影子可晋升")
        with _plan_lock(conn, batch["route_id"], user):
            hit = route_window_collides(
                conn, batch["route_id"], batch["window_start"], batch["window_end"]
            )
            if hit is not None:
                log_conflict(
                    conn, route_id=hit["id"], route_code=hit["route_code"],
                    batch_code=batch["batch_code"], chamber_id=batch["chamber_id"],
                    decision="拒晋升", detail="改线后仍撞车，影子维持不生效",
                )
                write_audit(conn, user, "batch_promote_reject", batch["batch_code"],
                            {"route_code": hit["route_code"]})
                conn.commit()
                raise HTTPException(409, "排程窗仍撞温控窗，晋升拒绝且零改")
            points = json.loads(batch["temp_points"] or "[]")
            new_status = "已入库" if points else "草稿"
            conn.execute(
                """UPDATE temp_batches SET status=?, plan_intent=?,
                   promoted_from=id WHERE id=?""",
                (new_status, FORMAL_INTENT, batch_id),
            )
            log_conflict(
                conn, route_id=batch["route_id"], route_code=hit["route_code"]
                if hit else "(已消冲突)",
                batch_code=batch["batch_code"], chamber_id=batch["chamber_id"],
                decision="晋升", detail="改线消冲突，整笔晋升为正式批次",
            )
            write_audit(conn, user, "batch_promote", batch["batch_code"],
                        {"batch_id": batch_id, "status": new_status})
            conn.commit()
            return {
                "id": batch_id,
                "batch_code": batch["batch_code"],
                "status": new_status,
                "plan_intent": FORMAL_INTENT,
            }


@router.post("/api/batches/{batch_id}/void")
def void_shadow(batch_id: int, authorization: AuthHeader = None):
    """挤窗意图作废：仅影子可作废，正式批次零改。"""
    user = require_writer(authorization)
    with connect() as conn:
        batch = conn.execute(
            "SELECT * FROM temp_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, "批次不存在")
        if batch["status"] != SHADOW_STATUS:
            raise HTTPException(409, "仅挤窗影子可作废")
        conn.execute(
            "UPDATE temp_batches SET status=?, voided_at=? WHERE id=?",
            (VOID_STATUS, now_iso(), batch_id),
        )
        log_conflict(
            conn, route_id=batch["route_id"], route_code="-",
            batch_code=batch["batch_code"], chamber_id=batch["chamber_id"],
            decision="作废", detail="挤窗意图作废，未生效正式批次",
        )
        write_audit(conn, user, "batch_shadow_void", batch["batch_code"],
                    {"batch_id": batch_id})
        conn.commit()
        return {"id": batch_id, "batch_code": batch["batch_code"], "status": VOID_STATUS}
