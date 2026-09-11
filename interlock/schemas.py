"""设备、规则与校核规格的 Pydantic 模型。

位置约定：杠杆只有两个位置 —— "N"（定位）与 "R"（反位）。
常反位杠杆通过 LeverSpec.normal_position = "R" 表示。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

Pos = Literal["N", "R"]


# ---------------------------------------------------------------- 设备

class SectionSpec(BaseModel):
    """轨道电路区段（线路拓扑的基本单元）。"""
    id: str
    name: str = ""


class LeverSpec(BaseModel):
    """杠杆。kind 仅作标注；normal_position="R" 即常反位杠杆。"""
    id: str
    name: str = ""
    kind: Literal["signal", "point", "fpl", "bar"] = "bar"
    normal_position: Pos = "N"


class PointSpec(BaseModel):
    """道岔，由一把杠杆操纵，位置即杠杆位置。"""
    id: str
    name: str = ""
    lever_id: str


class FPLSpec(BaseModel):
    """面对道岔锁（FPL）：锁闭某组道岔，由杠杆操纵。"""
    id: str
    point_id: str
    lever_id: str
    engage_position: Pos = "R"  # 杠杆处于该位置时锁闭生效


class SignalSpec(BaseModel):
    """信号机，由杠杆操纵。"""
    id: str
    name: str = ""
    lever_id: str
    clear_position: Pos = "R"  # 杠杆处于该位置时信号开放


class LockingEntrySpec(BaseModel):
    """锁条表条目：当 lever_a 处于 pos_a 时，lever_b 被锁在 pos_b。

    机械含义（双向都成立）：
      * lever_a 已处于 pos_a 时，lever_b 不得离开 pos_b；
      * lever_a 要扳到 pos_a 时，lever_b 必须已经处于 pos_b（否则锁闭无法啮合）。
    slot 为锁条上的物理空位编号，供修订接口引用。
    """
    id: str
    lever_a: str
    pos_a: Pos
    lever_b: str
    pos_b: Pos
    slot: Optional[str] = None


class TrackLockSpec(BaseModel):
    """轨道电路锁闭：区段被占用期间，杠杆被设备强制锁闭。

    pos 给出时锁在该位置；pos 为 null 时锁在占用发生时的当前位置
    （区段锁闭的通常含义）。这是接近锁闭/区段锁闭的物理基础，
    校核器把它当作不可违反的设备约束；缺少它时，
    “列车通过前提前释放”检查就会给出反例。
    """
    id: str
    section: str
    lever: str
    pos: Optional[Pos] = None


# ---------------------------------------------------------------- 进路

class RoutePointSpec(BaseModel):
    """进路对一组道岔的位置要求。"""
    point_id: str
    position: Pos
    protects_section: Optional[str] = None  # 缺省取进路中同序号的区段


class RouteSpec(BaseModel):
    """一条应允许的进路。"""
    id: str
    name: str = ""
    signal_id: str                     # 防护该进路的进站/调车信号机
    sections: list[str] = Field(min_length=1)   # 进路内区段，按列车运行方向排序
    approach_section: Optional[str] = None      # 接近区段（接近锁闭用）
    points: list[RoutePointSpec] = []
    should_allow: bool = True          # 该进路应当能够排列并开放信号


# ---------------------------------------------------------------- 规则与限制

class RulesSpec(BaseModel):
    """校核规则配置。"""
    conflicting_routes: list[tuple[str, str]] = []  # 冲突进路对
    approach_locking: bool = True      # 启用接近锁闭检查（列车接近时进路元件不得解锁）
    section_release: bool = True       # 启用区段释放检查（列车通过前不得提前释放）
    max_trains_per_route: int = 1      # 每条进路同时允许的最大列车数


class LimitsSpec(BaseModel):
    """状态空间展开限制：超限即报告已检查边界，不作完整证明。"""
    max_states: int = 200_000
    max_depth: int = 300


class Spec(BaseModel):
    """一个版本的完整联锁规格。"""
    sections: list[SectionSpec] = []
    levers: list[LeverSpec] = []
    points: list[PointSpec] = []
    fpls: list[FPLSpec] = []
    signals: list[SignalSpec] = []
    locking: list[LockingEntrySpec] = []   # 锁条表
    track_locks: list[TrackLockSpec] = []  # 轨道电路占用锁闭
    routes: list[RouteSpec] = []
    rules: RulesSpec = RulesSpec()
    limits: LimitsSpec = LimitsSpec()

    @model_validator(mode="after")
    def _check_refs(self) -> "Spec":
        lever_ids = {l.id for l in self.levers}
        sec_ids = {s.id for s in self.sections}
        point_ids = {p.id for p in self.points}
        sig_ids = {s.id for s in self.signals}
        route_ids = {r.id for r in self.routes}

        def need(cond: bool, msg: str) -> None:
            if not cond:
                raise ValueError(msg)

        need(len(lever_ids) == len(self.levers), "杠杆 id 重复")
        need(len(sec_ids) == len(self.sections), "区段 id 重复")
        need(len(route_ids) == len(self.routes), "进路 id 重复")
        for p in self.points:
            need(p.lever_id in lever_ids, f"道岔 {p.id} 引用了不存在的杠杆 {p.lever_id}")
        for f in self.fpls:
            need(f.point_id in point_ids, f"FPL {f.id} 引用了不存在的道岔 {f.point_id}")
            need(f.lever_id in lever_ids, f"FPL {f.id} 引用了不存在的杠杆 {f.lever_id}")
        for s in self.signals:
            need(s.lever_id in lever_ids, f"信号机 {s.id} 引用了不存在的杠杆 {s.lever_id}")
        for e in self.locking:
            need(e.lever_a in lever_ids, f"锁条 {e.id} 引用了不存在的杠杆 {e.lever_a}")
            need(e.lever_b in lever_ids, f"锁条 {e.id} 引用了不存在的杠杆 {e.lever_b}")
            need(e.lever_a != e.lever_b, f"锁条 {e.id} 不能自锁")
        for t in self.track_locks:
            need(t.section in sec_ids, f"轨道锁闭 {t.id} 引用了不存在的区段 {t.section}")
            need(t.lever in lever_ids, f"轨道锁闭 {t.id} 引用了不存在的杠杆 {t.lever}")
        for r in self.routes:
            need(r.signal_id in sig_ids, f"进路 {r.id} 引用了不存在的信号机 {r.signal_id}")
            for sec in r.sections:
                need(sec in sec_ids, f"进路 {r.id} 引用了不存在的区段 {sec}")
            if r.approach_section is not None:
                need(r.approach_section in sec_ids,
                     f"进路 {r.id} 引用了不存在的接近区段 {r.approach_section}")
            for rp in r.points:
                need(rp.point_id in point_ids, f"进路 {r.id} 引用了不存在的道岔 {rp.point_id}")
                if rp.protects_section is not None:
                    need(rp.protects_section in r.sections,
                         f"进路 {r.id} 的防护区段 {rp.protects_section} 不在进路内")
        for a, b in self.rules.conflicting_routes:
            need(a in route_ids and b in route_ids, f"冲突进路对 ({a},{b}) 引用了不存在的进路")
        return self


# ---------------------------------------------------------------- 修订

class ReviseRequest(BaseModel):
    """修订请求：固定现有锁条，只在指定空位生成候选。"""
    fixed_entry_ids: list[str] = []            # 不得改动的现有锁条 id
    allowed_slots: list[LockingEntrySpec] = []  # 允许使用的空位（候选条目内容）
    ops: list[Literal["add", "change", "move"]] = ["add", "change", "move"]
    max_candidates: int = 100
    limits: Optional[LimitsSpec] = None        # 复验用的状态限制，缺省沿用版本规格


# ---------------------------------------------------------------- 版本

class VersionCreate(BaseModel):
    name: str = ""
    spec: Spec


class VersionClone(BaseModel):
    """从既有版本克隆并覆盖部分内容（修订工作流）。"""
    name: str = ""
    spec: Optional[Spec] = None                # 整体替换；缺省沿用原规格
