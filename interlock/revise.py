"""修订：固定现有锁条，只在指定空位生成增锁/改锁/移锁候选并复验全部进路。

  * 增锁 add    —— 在空位新增一条锁条；
  * 改锁 change —— 保持杠杆对不变，仅改变某条未固定锁条的位置要求；
  * 移锁 move   —— 删除一条未固定锁条，把它的锁闭作用移到指定空位。
"""
from __future__ import annotations

from typing import Any

from .modelcheck import verify
from .schemas import LockingEntrySpec, ReviseRequest, Spec


def _entry_key(e: LockingEntrySpec) -> tuple:
    return (e.lever_a, e.pos_a, e.lever_b, e.pos_b)


def generate_candidates(spec: Spec, req: ReviseRequest) -> list[dict[str, Any]]:
    """生成候选规格（不验证）。每个候选携带新的完整锁条表。"""
    fixed = set(req.fixed_entry_ids)
    existing_keys = {_entry_key(e) for e in spec.locking}
    # 空位去重，且不与现有锁条重复
    slots: list[LockingEntrySpec] = []
    seen = set()
    for s in req.allowed_slots:
        k = _entry_key(s)
        if k not in existing_keys and k not in seen:
            seen.add(k)
            slots.append(s)

    movable_idx = [i for i, e in enumerate(spec.locking) if e.id not in fixed]
    cands: list[dict[str, Any]] = []

    def add(op: str, locking: list[LockingEntrySpec], note: str, changed: dict):
        cands.append({
            "op": op,
            "note": note,
            "changed": changed,
            "locking": [e.model_dump() for e in locking],
        })

    if "add" in req.ops:
        for s in slots:
            add("add", [*spec.locking, s],
                f"增锁：新增 {s.id}（{s.lever_a}={s.pos_a} 锁 {s.lever_b}@{s.pos_b}）",
                {"added": s.model_dump()})

    if "change" in req.ops:
        for i in movable_idx:
            e = spec.locking[i]
            for s in slots:
                # 改锁：同一杠杆对，仅位置要求不同
                if (e.lever_a, e.lever_b) != (s.lever_a, s.lever_b):
                    continue
                new = list(spec.locking)
                new[i] = e.model_copy(update={"pos_a": s.pos_a, "pos_b": s.pos_b,
                                              "slot": s.slot})
                add("change", new,
                    f"改锁：{e.id} 改为 {s.lever_a}={s.pos_a} 锁 "
                    f"{s.lever_b}@{s.pos_b}",
                    {"entry": e.id, "to": s.model_dump()})

    if "move" in req.ops:
        for i in movable_idx:
            e = spec.locking[i]
            for s in slots:
                # 移锁：杠杆对发生变化，净条数不变
                if (e.lever_a, e.lever_b) == (s.lever_a, s.lever_b):
                    continue
                new = [x for j, x in enumerate(spec.locking) if j != i]
                new.append(s)
                add("move", new,
                    f"移锁：删除 {e.id}，移至空位新增 {s.id}"
                    f"（{s.lever_a}={s.pos_a} 锁 {s.lever_b}@{s.pos_b}）",
                    {"removed": e.id, "added": s.model_dump()})

    return cands[: req.max_candidates]


def revise(spec: Spec, req: ReviseRequest) -> dict:
    """生成候选并逐一复验全部进路，按结果排序返回。"""
    cands = generate_candidates(spec, req)
    limits = req.limits or spec.limits
    results = []
    for c in cands:
        new_spec = spec.model_copy(update={
            "locking": [LockingEntrySpec(**e) for e in c["locking"]],
            "limits": limits,
        })
        res = verify(new_spec)
        results.append({
            **{k: c[k] for k in ("op", "note", "changed", "locking")},
            "ok": res["ok"],
            "complete": res["complete"],
            "states": res["stats"]["states"],
            "violations": [
                {"category": v["category"], "message": v["message"], "depth": v["depth"]}
                for v in res["violations"]
            ],
        })
    # 通过且完整证明的候选排前，其次按违规数与状态数
    results.sort(key=lambda r: (not r["ok"], not r["complete"],
                                len(r["violations"]), r["states"]))
    return {
        "candidates": results,
        "total": len(results),
        "fixed_entry_ids": req.fixed_entry_ids,
        "note": "候选仅由指定空位生成；ok=true 且 complete=true 才构成完整证明。",
    }
