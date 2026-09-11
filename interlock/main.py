"""机械杠杆联锁校核 REST API。

本机运行：  uvicorn interlock.main:app --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .db import get_db, init_db
from .modelcheck import verify
from .models import Run, Version
from .revise import revise
from .schemas import ReviseRequest, Spec, VersionClone, VersionCreate


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="机械杠杆联锁校核 API",
    version="0.1.0",
    description="把杠杆操作与列车占用展开为有限状态，校核锁条表是否满足"
                "信号开放后道岔锁闭、冲突信号不并放、列车通过前不提前释放、"
                "合法进路不被锁死；给出最短反例轨迹。",
    lifespan=_lifespan,
)


# ---------------------------------------------------------------- 工具

def _get_version(db: Session, vid: int) -> Version:
    v = db.get(Version, vid)
    if v is None:
        raise HTTPException(404, f"版本 {vid} 不存在")
    return v


def _latest_verify_run(db: Session, vid: int) -> Run | None:
    return (db.query(Run)
            .filter(Run.version_id == vid, Run.kind == "verify")
            .order_by(Run.id.desc())
            .first())


def _version_brief(v: Version) -> dict:
    return {"id": v.id, "name": v.name, "parent_id": v.parent_id,
            "created_at": v.created_at.isoformat()}


def _run_brief(r: Run) -> dict:
    return {"id": r.id, "version_id": r.version_id, "kind": r.kind, "ok": r.ok,
            "complete": r.complete, "states": r.states,
            "violations_count": r.violations_count,
            "created_at": r.created_at.isoformat()}


# ---------------------------------------------------------------- 版本

@app.post("/versions", status_code=201)
def create_version(body: VersionCreate, db: Session = Depends(get_db)):
    """保存一个版本（设备 + 规则快照）。"""
    v = Version(name=body.name, spec=body.spec.model_dump())
    db.add(v)
    db.commit()
    db.refresh(v)
    return _version_brief(v)


@app.get("/versions")
def list_versions(db: Session = Depends(get_db)):
    return [_version_brief(v) for v in db.query(Version).order_by(Version.id).all()]


@app.get("/versions/{vid}")
def get_version(vid: int, db: Session = Depends(get_db)):
    v = _get_version(db, vid)
    return {**_version_brief(v), "spec": v.spec}


@app.post("/versions/{vid}/clone", status_code=201)
def clone_version(vid: int, body: VersionClone, db: Session = Depends(get_db)):
    """从既有版本克隆（可整体替换规格），用于修订工作流。"""
    src = _get_version(db, vid)
    spec = body.spec.model_dump() if body.spec is not None else src.spec
    v = Version(name=body.name or f"{src.name}-副本", spec=spec, parent_id=src.id)
    db.add(v)
    db.commit()
    db.refresh(v)
    return _version_brief(v)


# ---------------------------------------------------------------- 校核

@app.post("/versions/{vid}/verify", status_code=201)
def verify_version(vid: int, db: Session = Depends(get_db)):
    """对版本做有限状态校核，保存结果并返回（含最短反例轨迹）。"""
    v = _get_version(db, vid)
    result = verify(Spec(**v.spec))
    run = Run(version_id=vid, kind="verify", ok=result["ok"],
              complete=result["complete"], states=result["stats"]["states"],
              violations_count=len(result["violations"]), result=result)
    db.add(run)
    db.commit()
    db.refresh(run)
    return {"run": _run_brief(run), "result": result}


@app.get("/versions/{vid}/runs")
def list_runs(vid: int, db: Session = Depends(get_db)):
    _get_version(db, vid)
    runs = (db.query(Run).filter(Run.version_id == vid)
            .order_by(Run.id.desc()).all())
    return [_run_brief(r) for r in runs]


@app.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    r = db.get(Run, run_id)
    if r is None:
        raise HTTPException(404, f"校核结果 {run_id} 不存在")
    return {**_run_brief(r), "result": r.result}


# ---------------------------------------------------------------- 修订

@app.post("/versions/{vid}/revise")
def revise_version(vid: int, req: ReviseRequest, db: Session = Depends(get_db)):
    """固定现有锁条，只在指定空位生成增锁/改锁/移锁候选并复验全部进路。"""
    v = _get_version(db, vid)
    result = revise(Spec(**v.spec), req)
    run = Run(version_id=vid, kind="revise",
              ok=any(c["ok"] and c["complete"] for c in result["candidates"]),
              complete=True, states=0,
              violations_count=sum(len(c["violations"]) for c in result["candidates"]),
              result=result)
    db.add(run)
    db.commit()
    db.refresh(run)
    return {"run": _run_brief(run), "result": result}


# ---------------------------------------------------------------- 比较

def _run_summary(run: Run | None) -> dict:
    if run is None:
        return {"run": None, "states": None, "violations": {}, "deadlocked": []}
    res = run.result
    return {
        "run": _run_brief(run),
        "states": res["stats"]["states"],
        "complete": res["complete"],
        "violations": {v["category"]: {"depth": v["depth"], "message": v["message"]}
                       for v in res["violations"]},
        "deadlocked": [rid for rid, r in res["routes"].items()
                       if not r["goal_reachable"]],
    }


@app.get("/compare")
def compare_versions(from_: int = Query(..., alias="from"),
                     to: int = Query(...),
                     db: Session = Depends(get_db)):
    """比较两个版本最近一次校核：可达状态数与反例变化。"""
    _get_version(db, from_)
    _get_version(db, to)
    a = _run_summary(_latest_verify_run(db, from_))
    b = _run_summary(_latest_verify_run(db, to))
    cats_a, cats_b = set(a["violations"]), set(b["violations"])
    common = cats_a & cats_b
    return {
        "from": {"version_id": from_, **a},
        "to": {"version_id": to, **b},
        "diff": {
            "states_delta": (None if a["states"] is None or b["states"] is None
                             else b["states"] - a["states"]),
            "violations_added": sorted(cats_b - cats_a),
            "violations_removed": sorted(cats_a - cats_b),
            "counterexample_depth_changes": {
                c: {"from": a["violations"][c]["depth"],
                    "to": b["violations"][c]["depth"]} for c in sorted(common)
            },
            "deadlocked_added": sorted(set(b["deadlocked"]) - set(a["deadlocked"])),
            "deadlocked_removed": sorted(set(a["deadlocked"]) - set(b["deadlocked"])),
        },
    }


# ---------------------------------------------------------------- 导出

@app.get("/versions/{vid}/export/traces")
def export_traces(vid: int, db: Session = Depends(get_db)):
    """导出最近一次校核的全部反例轨迹（JSON）。"""
    _get_version(db, vid)
    run = _latest_verify_run(db, vid)
    if run is None:
        raise HTTPException(404, f"版本 {vid} 尚无校核结果，请先调用 verify")
    return JSONResponse({
        "version_id": vid, "run_id": run.id,
        "complete": run.result["complete"],
        "boundary": run.result.get("boundary"),
        "traces": [
            {"category": v["category"], "message": v["message"],
             "depth": v["depth"], "trace": v["trace"]}
            for v in run.result["violations"]
        ],
    })


@app.get("/versions/{vid}/export/locking-table")
def export_locking_table(vid: int, db: Session = Depends(get_db)):
    """按进路排列的联锁表。"""
    v = _get_version(db, vid)
    spec = Spec(**v.spec)
    levers = {l.id: l for l in spec.levers}
    fpl_by_point = {f.point_id: f for f in spec.fpls}
    points = {p.id: p for p in spec.points}
    signals = {s.id: s for s in spec.signals}
    conflicts = {r.id: [] for r in spec.routes}
    for a, b in spec.rules.conflicting_routes:
        conflicts[a].append(b)
        conflicts[b].append(a)

    table = []
    for r in spec.routes:
        required = []
        sig = signals[r.signal_id]
        required.append({"lever": sig.lever_id, "position": sig.clear_position,
                         "device": f"信号机 {sig.id}"})
        for rp in r.points:
            p = points[rp.point_id]
            required.append({"lever": p.lever_id, "position": rp.position,
                             "device": f"道岔 {p.id}"})
            f = fpl_by_point.get(rp.point_id)
            if f:
                required.append({"lever": f.lever_id, "position": f.engage_position,
                                 "device": f"面对道岔锁 {f.id}"})
        involved = {e["lever"] for e in required}
        entries = [e.model_dump() for e in spec.locking
                   if e.lever_a in involved or e.lever_b in involved]
        tlocks = [t.model_dump() for t in spec.track_locks
                  if t.lever in involved]
        table.append({
            "route": r.id, "name": r.name, "signal": r.signal_id,
            "approach_section": r.approach_section, "sections": r.sections,
            "should_allow": r.should_allow,
            "required_levers": required,
            "locking_entries": entries,
            "track_locks": tlocks,
            "conflicts_with": sorted(conflicts[r.id]),
        })
    return {"version_id": vid, "levers": {l.id: levers[l.id].model_dump()
                                          for l in spec.levers},
            "routes": table}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
