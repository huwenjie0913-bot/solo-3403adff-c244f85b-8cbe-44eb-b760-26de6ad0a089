"""端到端演示：双进路共享道岔的小站，缺冲突锁 → 修订 → 复验 → 比较。"""
import json
import urllib.request

B = "http://127.0.0.1:8123"


def call(method, path, body=None):
    req = urllib.request.Request(
        B + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


# 站场：接近区段 A1/A2，道岔 P1 分歧到 G1/G2，两条进路冲突
spec = {
    "sections": [{"id": "A1"}, {"id": "A2"}, {"id": "G1"}, {"id": "G2"}],
    "levers": [
        {"id": "S1", "kind": "signal"}, {"id": "S2", "kind": "signal"},
        {"id": "P", "kind": "point"},
        {"id": "F1", "kind": "fpl"},
    ],
    "points": [{"id": "P1", "lever_id": "P"}],
    "fpls": [{"id": "FP1", "point_id": "P1", "lever_id": "F1"}],
    "signals": [{"id": "X1", "lever_id": "S1"}, {"id": "X2", "lever_id": "S2"}],
    "locking": [
        # FPL 锁闭后才能开放信号；FPL 锁道岔
        {"id": "E1", "lever_a": "S1", "pos_a": "R", "lever_b": "F1", "pos_b": "R"},
        {"id": "E2", "lever_a": "S2", "pos_a": "R", "lever_b": "F1", "pos_b": "R"},
        {"id": "E3", "lever_a": "F1", "pos_a": "R", "lever_b": "P", "pos_b": "N"},
        # 注意：缺少 S1 与 S2 的冲突锁！
    ],
    "track_locks": [
        {"id": "T1", "section": "A1", "lever": "P", "pos": None},
        {"id": "T2", "section": "A2", "lever": "P", "pos": None},
        {"id": "T3", "section": "G1", "lever": "P", "pos": None},
        {"id": "T4", "section": "G2", "lever": "P", "pos": None},
        {"id": "T5", "section": "A1", "lever": "F1", "pos": "R"},
        {"id": "T6", "section": "A2", "lever": "F1", "pos": "R"},
        {"id": "T7", "section": "G1", "lever": "F1", "pos": "R"},
        {"id": "T8", "section": "G2", "lever": "F1", "pos": "R"},
    ],
    "routes": [
        {"id": "R1", "signal_id": "X1", "approach_section": "A1",
         "sections": ["G1"], "points": [{"point_id": "P1", "position": "N"}]},
        {"id": "R2", "signal_id": "X2", "approach_section": "A2",
         "sections": ["G2"], "points": [{"point_id": "P1", "position": "N"}]},
    ],
    "rules": {"conflicting_routes": [["R1", "R2"]]},
    "limits": {"max_states": 200000, "max_depth": 300},
}

v1 = call("POST", "/versions", {"name": "缺冲突锁", "spec": spec})["id"]
res = call("POST", f"/versions/{v1}/verify")["result"]
print(f"[v{v1}] ok={res['ok']} complete={res['complete']} "
      f"states={res['stats']['states']} depth={res['stats']['depth']}")
for v in res["violations"]:
    print(f"  违规 {v['category']} (depth={v['depth']}): {v['message']}")
    for s in v["trace"]:
        print(f"    step{s['step']}: {s['action']['note']}"
              f" | 杠杆={s['levers']} | 占用={s['occupied']}")

# 修订：固定现有锁条，只在空位 7/8 生成候选
rev = call("POST", f"/versions/{v1}/revise", {
    "fixed_entry_ids": ["E1", "E2", "E3"],
    "allowed_slots": [
        {"id": "X1", "lever_a": "S1", "pos_a": "R", "lever_b": "S2",
         "pos_b": "N", "slot": "7"},
        {"id": "X2", "lever_a": "S2", "pos_a": "R", "lever_b": "S1",
         "pos_b": "N", "slot": "8"},
    ],
    "ops": ["add", "change", "move"],
})["result"]
print(f"\n修订候选 {rev['total']} 个：")
for c in rev["candidates"][:5]:
    print(f"  [{c['op']}] {c['note']} -> ok={c['ok']} complete={c['complete']}")

# 采用最优候选，克隆为新版本并复验
best = next(c for c in rev["candidates"] if c["ok"] and c["complete"])
spec2 = dict(spec, locking=best["locking"])
v2 = call("POST", f"/versions/{v1}/clone", {"name": "已修复", "spec": spec2})["id"]
res2 = call("POST", f"/versions/{v2}/verify")["result"]
print(f"\n[v{v2}] ok={res2['ok']} complete={res2['complete']} "
      f"states={res2['stats']['states']}")

cmp_ = call("GET", f"/compare?from={v1}&to={v2}")
print("比较：", json.dumps(cmp_["diff"], ensure_ascii=False))
