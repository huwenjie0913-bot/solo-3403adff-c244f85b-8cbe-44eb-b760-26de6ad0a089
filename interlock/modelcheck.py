"""有限状态模型检查器。

把杠杆操作与列车占用展开为有限状态空间（BFS），检查四类性质：

  * SWITCH_UNLOCKED_AFTER_CLEAR  信号开放后道岔解锁
  * CONFLICT_SIMULTANEOUS_CLEAR  冲突信号同时开放
  * PREMATURE_RELEASE            列车通过前提前释放（区段释放/接近锁闭）
  * ROUTE_DEADLOCKED             合法进路被错误锁死

BFS 保证返回的反例轨迹最短；超出状态/深度限制时明确报告已检查边界，
不把局部结果当作完整证明。
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Optional

from .schemas import Spec

POS = ("N", "R")

# 违规类别
SWITCH_UNLOCKED = "SWITCH_UNLOCKED_AFTER_CLEAR"
CONFLICT_CLEAR = "CONFLICT_SIMULTANEOUS_CLEAR"
PREMATURE_RELEASE = "PREMATURE_RELEASE"
ROUTE_DEADLOCKED = "ROUTE_DEADLOCKED"


class _Ctx:
    """把 Spec 编译成便于展开的内部结构。"""

    def __init__(self, spec: Spec):
        self.spec = spec
        self.lever_ids = [l.id for l in spec.levers]
        self.lidx = {lid: i for i, lid in enumerate(self.lever_ids)}
        self.n_levers = len(self.lever_ids)
        self.init_levers = tuple(0 if l.normal_position == "N" else 1 for l in spec.levers)

        # 锁条表索引：by_b[b] = [(a, pos_a, pos_b, entry_id)]  用于“b 能否离开当前位置”
        #              by_a[a] = [(pos_a, b, pos_b, entry_id)]  用于“a 能否进入目标位置”
        self.by_b: list[list[tuple[int, int, int, str]]] = [[] for _ in range(self.n_levers)]
        self.by_a: list[list[tuple[int, int, int, str]]] = [[] for _ in range(self.n_levers)]
        for e in spec.locking:
            a, b = self.lidx[e.lever_a], self.lidx[e.lever_b]
            pa, pb = (0 if e.pos_a == "N" else 1), (0 if e.pos_b == "N" else 1)
            self.by_b[b].append((a, pa, pb, e.id))
            self.by_a[a].append((pa, b, pb, e.id))

        # 轨道电路占用锁闭：tl_by_lever[b] = [(section, pos|None, id)]
        self.tl_by_lever: dict[int, list[tuple[str, Optional[int], str]]] = {}
        for t in spec.track_locks:
            pos = None if t.pos is None else (0 if t.pos == "N" else 1)
            self.tl_by_lever.setdefault(self.lidx[t.lever], []).append(
                (t.section, pos, t.id))

        self.point_lever = {p.id: self.lidx[p.lever_id] for p in spec.points}
        self.fpl_by_point = {f.point_id: f for f in spec.fpls}
        self.sig_lever = {s.id: self.lidx[s.lever_id] for s in spec.signals}
        self.sig_clear = {s.id: (0 if s.clear_position == "N" else 1) for s in spec.signals}

        # 编译进路
        self.routes: list[dict[str, Any]] = []
        self.ridx: dict[str, int] = {}
        for ri, r in enumerate(spec.routes):
            self.ridx[r.id] = ri
            positions = ([r.approach_section] if r.approach_section else []) + list(r.sections)
            pts = []
            for i, rp in enumerate(r.points):
                fpl = self.fpl_by_point.get(rp.point_id)
                sec = rp.protects_section or (
                    r.sections[min(i, len(r.sections) - 1)] if r.sections else None
                )
                pts.append({
                    "point_id": rp.point_id,
                    "lever": self.point_lever[rp.point_id],
                    "want": 0 if rp.position == "N" else 1,
                    "fpl_lever": self.lidx[fpl.lever_id] if fpl else None,
                    "fpl_pos": (0 if fpl.engage_position == "N" else 1) if fpl else None,
                    "protect_idx": positions.index(sec) if sec in positions else None,
                })
            self.routes.append({
                "id": r.id,
                "sig_lever": self.sig_lever[r.signal_id],
                "clear_pos": self.sig_clear[r.signal_id],
                "positions": positions,          # 列车依次经过的区段（含接近区段）
                "points": pts,
                "should_allow": r.should_allow,
            })

        self.conflicts = [
            (self.ridx[a], self.ridx[b]) for a, b in spec.rules.conflicting_routes
        ]

        # 杠杆 -> 受它防护的 (进路, 道岔, protect_idx, 类型, FPL锁闭位)
        self.guarded_by: dict[int, list[tuple[int, str, Optional[int], str, Optional[int]]]] = {}
        for ri, r in enumerate(self.routes):
            for pt in r["points"]:
                self.guarded_by.setdefault(pt["lever"], []).append(
                    (ri, pt["point_id"], pt["protect_idx"], "point", None))
                if pt["fpl_lever"] is not None:
                    self.guarded_by.setdefault(pt["fpl_lever"], []).append(
                        (ri, pt["point_id"], pt["protect_idx"], "fpl", pt["fpl_pos"]))

    # ---------------------------------------------------------- 锁闭语义

    def movable(self, L: tuple[int, ...], b: int, occ: frozenset) -> bool:
        """b 当前是否可以离开其所在位置（锁条与轨道电路锁闭都不生效）。"""
        p = L[b]
        for a, pa, pb, _ in self.by_b[b]:
            if L[a] == pa and pb == p:
                return False
        for sec, pos, _ in self.tl_by_lever.get(b, []):
            if sec in occ and (pos is None or pos == p):
                return False
        return True

    def can_move(self, L: tuple[int, ...], b: int, occ: frozenset) -> bool:
        """b 能否扳到另一位置：离开不被锁，且进入目标位置时啮合要求已满足。"""
        if not self.movable(L, b, occ):
            return False
        target = 1 - L[b]
        for pa, bb, pb, _ in self.by_a[b]:
            if pa == target and L[bb] != pb:
                return False
        return True

    def active_locks(self, L: tuple[int, ...], occ: frozenset) -> list[str]:
        """当前生效的锁条与轨道锁闭（可读文本）。"""
        out = []
        for e in self.spec.locking:
            a = self.lidx[e.lever_a]
            if L[a] == (0 if e.pos_a == "N" else 1):
                out.append(f"{e.id}: {e.lever_a}={e.pos_a} ⇒ 锁 {e.lever_b}@{e.pos_b}")
        for t in self.spec.track_locks:
            if t.section in occ:
                where = f"@{t.pos}" if t.pos else "（当前位置）"
                out.append(f"{t.id}: 区段 {t.section} 占用 ⇒ 锁 {t.lever}{where}")
        return out

    # ---------------------------------------------------------- 派生量

    def occupancy(self, T: tuple[int, ...]) -> list[str]:
        return [self.routes[ri]["positions"][st]
                for ri, st in enumerate(T) if st >= 0]

    def signal_clear(self, L: tuple[int, ...], ri: int) -> bool:
        r = self.routes[ri]
        return L[r["sig_lever"]] == r["clear_pos"]

    def route_set(self, L: tuple[int, ...], ri: int) -> bool:
        return all(L[pt["lever"]] == pt["want"] for pt in self.routes[ri]["points"])

    # ---------------------------------------------------------- 状态检查

    def state_violations(self, L: tuple[int, ...], occ: frozenset) -> list[dict]:
        """状态不变量：信号开放后道岔解锁、冲突信号同时开放。"""
        out = []
        for ri, r in enumerate(self.routes):
            if not self.signal_clear(L, ri):
                continue
            for pt in r["points"]:
                if pt["fpl_lever"] is not None and L[pt["fpl_lever"]] != pt["fpl_pos"]:
                    out.append({
                        "category": SWITCH_UNLOCKED,
                        "message": f"信号开放但道岔 {pt['point_id']} 的面对道岔锁未锁闭",
                        "detail": {"route": r["id"], "point": pt["point_id"],
                                   "reason": "fpl_not_engaged"},
                    })
                elif self.movable(L, pt["lever"], occ):
                    out.append({
                        "category": SWITCH_UNLOCKED,
                        "message": f"信号开放但道岔 {pt['point_id']} 的杠杆仍可扳动（未锁闭）",
                        "detail": {"route": r["id"], "point": pt["point_id"],
                                   "reason": "point_lever_movable"},
                    })
        for a, b in self.conflicts:
            if self.signal_clear(L, a) and self.signal_clear(L, b):
                out.append({
                    "category": CONFLICT_CLEAR,
                    "message": f"冲突进路 {self.routes[a]['id']} 与 "
                               f"{self.routes[b]['id']} 的信号同时开放",
                    "detail": {"routes": [self.routes[a]["id"], self.routes[b]["id"]]},
                })
        return out

    def transition_violation(self, L: tuple[int, ...], T: tuple[int, ...],
                             lever: int) -> Optional[dict]:
        """提前释放：列车尚未通过某道岔防护的区段，就解除其道岔锁闭。

        道岔杠杆的任何扳动都算释放；FPL 杠杆只有“离开锁闭位”
        （解锁方向）才算，扳向锁闭位是安全操作。
        """
        if not (self.spec.rules.section_release or self.spec.rules.approach_locking):
            return None
        for ri, point_id, protect_idx, kind, fpl_pos in self.guarded_by.get(lever, []):
            st = T[ri]
            if st < 0:
                continue
            if protect_idx is not None and st > protect_idx:
                continue  # 列车已越过该道岔防护的区段
            if kind == "fpl" and L[lever] != fpl_pos:
                continue  # FPL 当前未锁闭，扳动是锁闭方向而非释放
            r = self.routes[ri]
            at = r["positions"][st]
            return {
                "category": PREMATURE_RELEASE,
                "message": f"列车在区段 {at}（进路 {r['id']}）时，"
                           f"道岔 {point_id} 的锁闭被提前释放",
                "detail": {"route": r["id"], "point": point_id,
                           "train_at": at, "lever": self.lever_ids[lever]},
            }
        return None

    # ---------------------------------------------------------- 后继展开

    def successors(self, state):
        L, T = state
        occ = frozenset(self.occupancy(T))
        for b in range(self.n_levers):
            if self.can_move(L, b, occ):
                L2 = list(L)
                L2[b] ^= 1
                yield (tuple(L2), T), ("lever", b)
        for ri, r in enumerate(self.routes):
            pos = r["positions"]
            if not pos:
                continue
            st = T[ri]
            if st == -1:
                if pos[0] not in occ:
                    T2 = list(T)
                    T2[ri] = 0
                    yield (L, tuple(T2)), ("train", ri, "enter", pos[0])
            elif st == len(pos) - 1:
                T2 = list(T)
                T2[ri] = -1
                yield (L, tuple(T2)), ("train", ri, "leave", pos[st])
            else:
                nxt = pos[st + 1]
                if nxt not in occ:
                    T2 = list(T)
                    T2[ri] = st + 1
                    yield (L, tuple(T2)), ("train", ri, "advance", pos[st], nxt)

    # ---------------------------------------------------------- 轨迹重构

    def rules_hit_for(self, L: tuple[int, ...], occ: frozenset, action) -> list[str]:
        """本步杠杆动作检查/满足的锁条与轨道锁闭条目（可读文本）。"""
        if not action or action[0] != "lever":
            return []
        b = action[1]
        lid = self.lever_ids[b]
        target = 1 - L[b]
        hits = []
        for a, pa, pb, eid in self.by_b[b]:
            state = "生效" if L[a] == pa else "未生效"
            hits.append(f"检查锁条 {eid}（{self.lever_ids[a]}={POS[pa]} 时锁 "
                        f"{lid}@{POS[pb]}）：{state}")
        for sec, pos, tid in self.tl_by_lever.get(b, []):
            active = sec in occ and (pos is None or pos == L[b])
            where = POS[pos] if pos is not None else "当前位置"
            hits.append(f"检查轨道锁闭 {tid}（{sec} 占用时锁 {lid}@{where}）："
                        f"{'生效' if active else '未生效'}")
        for pa, bb, pb, eid in self.by_a[b]:
            if pa == target:
                ok = "满足" if L[bb] == pb else "不满足"
                hits.append(f"啮合要求 {eid}（{lid} 扳至 {POS[target]} 需 "
                            f"{self.lever_ids[bb]}={POS[pb]}）：{ok}")
        return hits

    def build_trace(self, parent, state, violation, final=None) -> list[dict]:
        """从父指针重构最短轨迹，逐步列出杠杆位置、区段占用、命中规则。

        final=(nxt, action) 时，轨迹在 state 之后再追加一步违规迁移
        （用于“提前释放”这类迁移违规，其目标状态可能早已被访问）。
        """
        chain = []
        s = state
        while s is not None:
            prev, action = parent[s]
            chain.append((s, prev, action))
            s = prev
        chain.reverse()
        if final is not None:
            chain.append((final[0], state, final[1]))

        steps = []
        for i, (st, prev, action) in enumerate(chain):
            L, T = st
            occ_list = self.occupancy(T)
            pre_L = prev[0] if prev else L
            pre_occ = frozenset(self.occupancy(prev[1])) if prev else frozenset()
            step = {
                "step": i,
                "action": self._describe_action(prev, action),
                "levers": {self.lever_ids[j]: POS[L[j]] for j in range(self.n_levers)},
                "occupied": occ_list,
                "trains": [
                    {"route": self.routes[ri]["id"],
                     "at": self.routes[ri]["positions"][t]}
                    for ri, t in enumerate(T) if t >= 0
                ],
                "active_locks": self.active_locks(L, frozenset(occ_list)),
                "rules_hit": self.rules_hit_for(pre_L, pre_occ, action) if prev else [],
            }
            steps.append(step)
        if steps:
            steps[-1]["violation"] = {
                "category": violation["category"],
                "message": violation["message"],
                "detail": violation["detail"],
            }
        return steps

    def _describe_action(self, prev_state, action) -> dict:
        if action is None:
            return {"type": "init", "note": "初始状态：全部杠杆处于常态位置，无列车"}
        if action[0] == "lever":
            b = action[1]
            frm = POS[prev_state[0][b]]
            to = POS[1 - prev_state[0][b]]
            return {"type": "lever", "lever": self.lever_ids[b],
                    "from": frm, "to": to,
                    "note": f"扳动杠杆 {self.lever_ids[b]}：{frm} → {to}"}
        _, ri, kind, *rest = action
        rid = self.routes[ri]["id"]
        if kind == "enter":
            note = f"列车进入进路 {rid} 的 {rest[0]}"
        elif kind == "leave":
            note = f"列车驶离进路 {rid}（出清 {rest[0]}）"
        else:
            note = f"列车在进路 {rid} 上由 {rest[0]} 推进到 {rest[1]}"
        return {"type": "train", "route": rid, "move": kind, "note": note}


# ---------------------------------------------------------------- 主入口

def verify(spec: Spec) -> dict:
    """对规格做有限状态校核，返回可 JSON 化的结果。"""
    ctx = _Ctx(spec)
    t0 = time.perf_counter()

    init = (ctx.init_levers, tuple(-1 for _ in ctx.routes))
    parent: dict = {init: (None, None)}
    depth_of = {init: 0}
    q = deque([init])

    violations: dict[str, dict] = {}          # 每类只保留首个（最短）反例
    goal_found: set[int] = set()              # 可排列并开放信号的进路
    best: dict[int, tuple[int, Any]] = {}     # 进路 -> (满足要求数, 状态) 用于锁死见证
    n_states = 0
    max_depth_seen = 0
    complete = True
    stop_reason = ""

    limits = spec.limits
    while q:
        state = q.popleft()
        L, T = state
        d = depth_of[state]
        n_states += 1
        max_depth_seen = max(max_depth_seen, d)
        occ = frozenset(ctx.occupancy(T))

        # 状态不变量
        for v in ctx.state_violations(L, occ):
            if v["category"] not in violations:
                violations[v["category"]] = {**v, "depth": d, "_state": state}

        # 进路目标：道岔位置正确、信号开放、本进路无车
        for ri, r in enumerate(ctx.routes):
            if not r["should_allow"] or ri in goal_found:
                continue
            sat = sum(1 for pt in r["points"] if L[pt["lever"]] == pt["want"])
            if ri not in best or sat > best[ri][0]:
                best[ri] = (sat, state)
            if (ctx.route_set(L, ri) and ctx.signal_clear(L, ri)
                    and T[ri] == -1):
                goal_found.add(ri)

        # 展开限制：报告边界，不作完整证明
        if n_states >= limits.max_states:
            complete, stop_reason = False, f"达到状态数上限 {limits.max_states}"
            break
        if d >= limits.max_depth:
            complete = False
            stop_reason = f"达到深度上限 {limits.max_depth}"
            continue

        for nxt, action in ctx.successors(state):
            # 提前释放是“迁移”违规：与目标状态是否已访问无关，先判定
            if action[0] == "lever" and PREMATURE_RELEASE not in violations:
                tv = ctx.transition_violation(L, T, action[1])
                if tv is not None:
                    violations[PREMATURE_RELEASE] = {
                        **tv, "depth": d + 1,
                        "_state": state, "_final": (nxt, action),
                    }
            if nxt in parent:
                continue
            parent[nxt] = (state, action)
            depth_of[nxt] = d + 1
            q.append(nxt)

    # 合法进路被错误锁死（只有完整展开时才是定论）
    for ri, r in enumerate(ctx.routes):
        if r["should_allow"] and ri not in goal_found:
            sat, st = best.get(ri, (0, init))
            want = {ctx.lever_ids[pt["lever"]]: POS[pt["want"]] for pt in r["points"]}
            got = {ctx.lever_ids[pt["lever"]]: POS[st[0][pt["lever"]]]
                   for pt in r["points"]}
            unsat = [k for k in want if want[k] != got[k]]
            v = {
                "category": ROUTE_DEADLOCKED,
                "message": f"进路 {r['id']} 应当允许，但无法排列并开放信号"
                           f"（杠杆 {', '.join(unsat) or '—'} 无法到位）",
                "detail": {"route": r["id"], "required": want,
                           "blocked_levers": unsat,
                           "complete": complete},
                "depth": depth_of[st],
                "_state": st,
            }
            violations.setdefault(ROUTE_DEADLOCKED, v)

    # 输出
    out_violations = []
    for cat in (SWITCH_UNLOCKED, CONFLICT_CLEAR, PREMATURE_RELEASE, ROUTE_DEADLOCKED):
        if cat not in violations:
            continue
        v = violations[cat]
        st = v.pop("_state")
        final = v.pop("_final", None)
        v["trace"] = ctx.build_trace(parent, st, v, final=final)
        out_violations.append(v)

    result = {
        "complete": complete,
        "ok": not out_violations,
        "stats": {
            "states": n_states,
            "depth": max_depth_seen,
            "levers": ctx.n_levers,
            "routes": len(ctx.routes),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        },
        "routes": {ctx.routes[ri]["id"]: {"goal_reachable": ri in goal_found}
                   for ri in range(len(ctx.routes))},
        "violations": out_violations,
    }
    if not complete:
        result["boundary"] = {
            "reason": stop_reason,
            "states_checked": n_states,
            "depth_reached": max_depth_seen,
            "max_states": limits.max_states,
            "max_depth": limits.max_depth,
            "note": "状态空间未穷尽：以上仅为已检查边界内的部分结果，"
                    "不构成完整证明；无反例不代表性质成立。",
        }
    return result
