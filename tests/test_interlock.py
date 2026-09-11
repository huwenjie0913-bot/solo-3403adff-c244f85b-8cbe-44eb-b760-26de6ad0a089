"""端到端测试：四类检查、最短反例、边界报告、修订、版本比较与导出。"""
from __future__ import annotations

import os
import tempfile

# 使用临时数据库，避免污染工作目录
_tmp = tempfile.mkdtemp()
os.environ["INTERLOCK_DB"] = os.path.join(_tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from interlock.db import init_db  # noqa: E402
from interlock.main import app  # noqa: E402

init_db()  # TestClient 非上下文管理器用法不触发 lifespan，这里显式建表
client = TestClient(app)


# ---------------------------------------------------------------- 规格构造

def base_spec():
    """单进路：接近区段 A1 → 区段 G1，道岔 P1（定位 N），FPL F1，信号 S1。"""
    return {
        "sections": [{"id": "A1"}, {"id": "G1"}],
        "levers": [
            {"id": "S1", "kind": "signal"},
            {"id": "P", "kind": "point"},
            {"id": "F", "kind": "fpl"},
        ],
        "points": [{"id": "P1", "lever_id": "P"}],
        "fpls": [{"id": "F1", "point_id": "P1", "lever_id": "F"}],
        "signals": [{"id": "S1", "lever_id": "S1"}],
        "locking": [],
        "track_locks": [],
        "routes": [{
            "id": "R1", "signal_id": "S1", "approach_section": "A1",
            "sections": ["G1"],
            "points": [{"point_id": "P1", "position": "N"}],
        }],
        "rules": {"conflicting_routes": []},
        "limits": {"max_states": 200000, "max_depth": 300},
    }


def good_spec():
    """正确的联锁：F 先锁闭才能开放 S1；F 锁 P；占用期间区段锁闭 P、F。"""
    spec = base_spec()
    spec["locking"] = [
        {"id": "E1", "lever_a": "S1", "pos_a": "R", "lever_b": "F", "pos_b": "R"},
        {"id": "E2", "lever_a": "F", "pos_a": "R", "lever_b": "P", "pos_b": "N"},
    ]
    spec["track_locks"] = [
        {"id": "T1", "section": "A1", "lever": "F", "pos": "R"},
        {"id": "T2", "section": "G1", "lever": "F", "pos": "R"},
        {"id": "T3", "section": "A1", "lever": "P", "pos": None},
        {"id": "T4", "section": "G1", "lever": "P", "pos": None},
    ]
    return spec


def conflict_spec():
    """两条冲突进路，只有两把信号杠杆，无任何锁条。"""
    return {
        "sections": [{"id": "G1"}, {"id": "G2"}],
        "levers": [{"id": "S1", "kind": "signal"}, {"id": "S2", "kind": "signal"}],
        "points": [], "fpls": [],
        "signals": [{"id": "S1", "lever_id": "S1"}, {"id": "S2", "lever_id": "S2"}],
        "locking": [], "track_locks": [],
        "routes": [
            {"id": "R1", "signal_id": "S1", "sections": ["G1"]},
            {"id": "R2", "signal_id": "S2", "sections": ["G2"]},
        ],
        "rules": {"conflicting_routes": [["R1", "R2"]]},
        "limits": {"max_states": 200000, "max_depth": 300},
    }


def deadlock_spec():
    """锁条互相卡死：S1=N 锁 P@N，P=R 锁 S1@N —— 进路要求 P=R，永远排不出。"""
    spec = base_spec()
    spec["routes"][0]["points"] = [{"point_id": "P1", "position": "R"}]
    spec["locking"] = [
        {"id": "E1", "lever_a": "S1", "pos_a": "N", "lever_b": "P", "pos_b": "N"},
        {"id": "E2", "lever_a": "P", "pos_a": "R", "lever_b": "S1", "pos_b": "N"},
    ]
    return spec


def create_and_verify(spec, name=""):
    r = client.post("/versions", json={"name": name, "spec": spec})
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    r = client.post(f"/versions/{vid}/verify")
    assert r.status_code == 201, r.text
    return vid, r.json()["result"]


# ---------------------------------------------------------------- 四类检查

def test_switch_unlocked_after_clear():
    """空锁条表：信号开放后 FPL 未锁闭 → SWITCH_UNLOCKED，轨迹最短。"""
    _, res = create_and_verify(base_spec(), "unlocked")
    cats = [v["category"] for v in res["violations"]]
    assert "SWITCH_UNLOCKED_AFTER_CLEAR" in cats
    v = next(v for v in res["violations"]
             if v["category"] == "SWITCH_UNLOCKED_AFTER_CLEAR")
    assert v["detail"]["reason"] == "fpl_not_engaged"
    # 最短轨迹：一步扳动 S1 即违规
    assert v["depth"] == 1
    last = v["trace"][-1]
    assert last["levers"]["S1"] == "R"
    assert "violation" in last
    assert last["violation"]["category"] == "SWITCH_UNLOCKED_AFTER_CLEAR"
    # 轨迹逐步包含杠杆位置与区段占用
    assert all("levers" in s and "occupied" in s for s in v["trace"])


def test_conflict_simultaneous_clear():
    _, res = create_and_verify(conflict_spec(), "conflict")
    cats = [v["category"] for v in res["violations"]]
    assert "CONFLICT_SIMULTANEOUS_CLEAR" in cats
    v = next(v for v in res["violations"]
             if v["category"] == "CONFLICT_SIMULTANEOUS_CLEAR")
    assert v["depth"] == 2  # 先后扳动两把信号杠杆
    assert v["trace"][-1]["levers"] == {"S1": "R", "S2": "R"}


def test_premature_release():
    """无轨道锁闭：列车在进路内时信号员恢复信号并解锁 FPL → 提前释放。"""
    spec = base_spec()
    spec["locking"] = [
        {"id": "E1", "lever_a": "S1", "pos_a": "R", "lever_b": "F", "pos_b": "R"},
        {"id": "E2", "lever_a": "F", "pos_a": "R", "lever_b": "P", "pos_b": "N"},
    ]
    _, res = create_and_verify(spec, "premature")
    cats = [v["category"] for v in res["violations"]]
    assert "PREMATURE_RELEASE" in cats
    v = next(v for v in res["violations"] if v["category"] == "PREMATURE_RELEASE")
    # 最短反例：列车进入接近区段后直接扳动未锁闭的道岔/FPL 杠杆
    last = v["trace"][-1]
    assert last["action"]["type"] == "lever"
    assert last["action"]["lever"] in ("P", "F")
    assert v["depth"] == 2
    assert any(t["route"] == "R1" for t in last["trains"])
    assert last["violation"]["detail"]["lever"] == last["action"]["lever"]


def test_route_deadlocked():
    _, res = create_and_verify(deadlock_spec(), "deadlock")
    cats = [v["category"] for v in res["violations"]]
    assert "ROUTE_DEADLOCKED" in cats
    v = next(v for v in res["violations"] if v["category"] == "ROUTE_DEADLOCKED")
    assert v["detail"]["route"] == "R1"
    assert "P" in v["detail"]["blocked_levers"]
    assert res["routes"]["R1"]["goal_reachable"] is False


def test_good_spec_passes():
    """正确联锁：完整展开、无任何违规、进路目标可达。"""
    _, res = create_and_verify(good_spec(), "good")
    assert res["complete"] is True
    assert res["ok"] is True
    assert res["violations"] == []
    assert res["routes"]["R1"]["goal_reachable"] is True


# ---------------------------------------------------------------- 边界报告

def test_boundary_reported_when_state_space_truncated():
    spec = good_spec()
    spec["limits"]["max_states"] = 5
    _, res = create_and_verify(spec, "bounded")
    assert res["complete"] is False
    assert "boundary" in res
    assert res["boundary"]["states_checked"] == 5
    assert "不构成完整证明" in res["boundary"]["note"]


# ---------------------------------------------------------------- 修订

def test_revise_add_lock_fixes_conflict():
    """固定现有锁条，在指定空位生成增锁候选并复验。"""
    vid, res = create_and_verify(conflict_spec(), "to-fix")
    assert not res["ok"]
    body = {
        "fixed_entry_ids": [],
        "allowed_slots": [
            {"id": "X1", "lever_a": "S1", "pos_a": "R",
             "lever_b": "S2", "pos_b": "N", "slot": "7"},
            {"id": "X2", "lever_a": "S2", "pos_a": "R",
             "lever_b": "S1", "pos_b": "N", "slot": "8"},
        ],
        "ops": ["add"],
    }
    r = client.post(f"/versions/{vid}/revise", json=body)
    assert r.status_code == 200, r.text
    cands = r.json()["result"]["candidates"]
    assert len(cands) == 2
    # 单条锁条即可防止并放：啮合要求 + 锁闭两个方向都生效
    assert all(c["ok"] and c["complete"] for c in cands)
    assert all(c["op"] == "add" for c in cands)

    # 把两条都加上也应当通过：克隆版本验证
    spec = conflict_spec()
    spec["locking"] = [dict(s, slot=None) for s in
                       (body["allowed_slots"][0], body["allowed_slots"][1])]
    _, res2 = create_and_verify(spec, "fixed")
    assert res2["ok"] is True


def test_revise_change_and_move():
    """改锁/移锁候选只在指定空位生成，且不动被固定的锁条。"""
    spec = base_spec()
    spec["locking"] = [
        {"id": "E1", "lever_a": "S1", "pos_a": "R", "lever_b": "F", "pos_b": "N"},
    ]
    vid, _ = create_and_verify(spec, "to-change")
    body = {
        "fixed_entry_ids": ["E1"],
        "allowed_slots": [
            {"id": "X1", "lever_a": "S1", "pos_a": "R", "lever_b": "F", "pos_b": "R"},
            {"id": "X2", "lever_a": "F", "pos_a": "R", "lever_b": "P", "pos_b": "N"},
        ],
        "ops": ["add", "change", "move"],
    }
    r = client.post(f"/versions/{vid}/revise", json=body)
    assert r.status_code == 200, r.text
    cands = r.json()["result"]["candidates"]
    # E1 被固定：change/move 都不能动它 → 只剩 add 候选
    assert cands and all(c["op"] == "add" for c in cands)
    assert all(any(e["id"] == "E1" for e in c["locking"]) for c in cands)


# ---------------------------------------------------------------- 版本/比较/导出

def test_version_compare_and_exports():
    v1, res1 = create_and_verify(conflict_spec(), "v1-buggy")
    assert not res1["ok"]

    # 修复后的规格另存为新版本
    spec = conflict_spec()
    spec["locking"] = [
        {"id": "X1", "lever_a": "S1", "pos_a": "R", "lever_b": "S2", "pos_b": "N"},
        {"id": "X2", "lever_a": "S2", "pos_a": "R", "lever_b": "S1", "pos_b": "N"},
    ]
    r = client.post(f"/versions/{v1}/clone", json={"name": "v2-fixed", "spec": spec})
    assert r.status_code == 201, r.text
    v2 = r.json()["id"]
    assert r.json()["parent_id"] == v1
    r = client.post(f"/versions/{v2}/verify")
    assert r.json()["result"]["ok"] is True

    # 比较：冲突反例消失，可达状态数变化
    r = client.get(f"/compare?from={v1}&to={v2}")
    assert r.status_code == 200, r.text
    diff = r.json()["diff"]
    assert "CONFLICT_SIMULTANEOUS_CLEAR" in diff["violations_removed"]
    assert diff["violations_added"] == []
    assert diff["states_delta"] is not None

    # 导出 JSON 轨迹
    r = client.get(f"/versions/{v1}/export/traces")
    assert r.status_code == 200
    payload = r.json()
    assert payload["traces"], "应至少有一条反例轨迹"
    tr = payload["traces"][0]
    assert tr["category"] == "CONFLICT_SIMULTANEOUS_CLEAR"
    assert [s["step"] for s in tr["trace"]] == list(range(len(tr["trace"])))
    assert "violation" in tr["trace"][-1]

    # 导出按进路排列的联锁表
    r = client.get(f"/versions/{v2}/export/locking-table")
    assert r.status_code == 200
    table = r.json()
    assert {rt["route"] for rt in table["routes"]} == {"R1", "R2"}
    r1 = next(rt for rt in table["routes"] if rt["route"] == "R1")
    assert r1["conflicts_with"] == ["R2"]
    assert any(e["lever"] == "S1" for e in r1["required_levers"])
    assert len(r1["locking_entries"]) == 2

    # 版本列表与运行记录
    assert any(v["id"] == v1 for v in client.get("/versions").json())
    runs = client.get(f"/versions/{v1}/runs").json()
    assert runs and runs[0]["kind"] == "verify"
    run_id = runs[0]["id"]
    assert client.get(f"/runs/{run_id}").json()["id"] == run_id


def test_validation_rejects_bad_refs():
    spec = base_spec()
    spec["routes"][0]["signal_id"] = "NOPE"
    r = client.post("/versions", json={"name": "bad", "spec": spec})
    assert r.status_code == 422


def test_normally_reverse_lever():
    """常反位杠杆：常态为 R，初始状态即处于 R。"""
    spec = conflict_spec()
    spec["levers"][1]["normal_position"] = "R"  # S2 常反位 → S2 常态开放
    _, res = create_and_verify(spec, "normal-reverse")
    v = next(v for v in res["violations"]
             if v["category"] == "CONFLICT_SIMULTANEOUS_CLEAR")
    # 只需扳动 S1 一把杠杆即并放
    assert v["depth"] == 1
    assert v["trace"][0]["levers"]["S2"] == "R"
