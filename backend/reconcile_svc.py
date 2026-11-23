"""仓间配额 vs 在批差额对账 + 互证只读。

窗状态与批次建口同源（window_svc）；影子意图不占在批配额。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect
from window_svc import ACTIVE_SQL, enrich_batch

router = APIRouter(tags=["reconcile"])


def _chamber_recon(conn, chamber) -> dict:
    cid = chamber["id"]
    quota = int(chamber["quota_batches"])
    active = conn.execute(
        f"SELECT COUNT(*) c FROM temp_batches WHERE chamber_id=? AND {ACTIVE_SQL}",
        (cid,),
    ).fetchone()["c"]
    sealed = conn.execute(
        """SELECT COUNT(*) c FROM temp_batches
           WHERE chamber_id=? AND status='已封批'""",
        (cid,),
    ).fetchone()["c"]
    shadow = conn.execute(
        "SELECT COUNT(*) c FROM temp_batches WHERE chamber_id=? AND status='影子'",
        (cid,),
    ).fetchone()["c"]
    conflict_rows = conn.execute(
        """SELECT b.* FROM temp_batches b WHERE b.chamber_id=?
           AND b.status NOT IN ('作废') ORDER BY b.id""",
        (cid,),
    ).fetchall()
    window_conflicts = [
        b["batch_code"] for b in (enrich_batch(conn, r) for r in conflict_rows)
        if b["window_conflict"]
    ]
    gap = quota - active
    ok = active <= quota
    return {
        "chamber_id": cid,
        "chamber_code": chamber["chamber_code"],
        "region": chamber["region"],
        "quota_batches": quota,
        "active_batches": active,
        "sealed_batches": sealed,
        "shadow_pending": shadow,
        "quota_left": gap,
        "balanced": ok,
        "window_conflict_codes": window_conflicts,
        "window_conflict": bool(window_conflicts),
        "proof": {
            "formula": "active_batches <= quota_batches and no window conflict",
            "left": active,
            "right": quota,
            "window_conflict_batches": window_conflicts,
        },
    }


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        chambers = conn.execute("SELECT * FROM cold_chambers ORDER BY id").fetchall()
        items = [_chamber_recon(conn, c) for c in chambers]
        bad = [i for i in items if not i["balanced"]]
        return {
            "items": items,
            "summary": {
                "total": len(items),
                "balanced": len(items) - len(bad),
                "unbalanced": len(bad),
            },
        }


@router.get("/api/reconcile/{chamber_code}")
def reconcile_one(chamber_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        ch = conn.execute(
            "SELECT * FROM cold_chambers WHERE chamber_code=?", (chamber_code,)
        ).fetchone()
        if not ch:
            raise HTTPException(404, "仓间不存在")
        return _chamber_recon(conn, ch)


@router.get("/api/proof/batch/{batch_code}")
def batch_proof(batch_code: str, authorization: AuthHeader = None):
    """批次互证：封批代 + 温谱导出 + 开放游标。"""
    require_user(authorization)
    with connect() as conn:
        batch = conn.execute(
            "SELECT * FROM temp_batches WHERE batch_code=?", (batch_code,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, "批次不存在")
        open_exc = conn.execute(
            """SELECT COUNT(*) c FROM excursion_cursors
               WHERE batch_id=? AND status='开放'""",
            (batch["id"],),
        ).fetchone()["c"]
        exports = conn.execute(
            """SELECT COUNT(*) c FROM spectrum_exports
               WHERE batch_id=? AND status='已导出'""",
            (batch["id"],),
        ).fetchone()["c"]
        sealed = batch["status"] == "已封批"
        spectrum_ok = (exports >= 1) if sealed else True
        bdict = enrich_batch(conn, batch)
        return {
            "batch_code": batch_code,
            "status": batch["status"],
            "generation": batch["generation"],
            "plan_intent": batch["plan_intent"],
            "route_id": batch["route_id"],
            "window_start": batch["window_start"],
            "window_end": batch["window_end"],
            "window_conflict": bdict["window_conflict"],
            "open_excursions": open_exc,
            "spectrum_exports": exports,
            "sealed": sealed,
            "excursion_closed": open_exc == 0,
            "spectrum_matches_seal": spectrum_ok,
            "proof": {
                "formula": "sealed => spectrum>=1 and open_excursions==0 and no window conflict",
                "sealed": sealed,
                "open_excursions": open_exc,
                "exports": exports,
                "window_conflict": bdict["window_conflict"],
            },
        }