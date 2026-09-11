# 机械杠杆联锁校核 API

供铁路信号修复人员校核机械杠杆联锁（锁条表）的 REST API。
本机运行，技术栈：Python + FastAPI + Pydantic + SQLAlchemy + SQLite。

系统把**杠杆操作**与**列车占用**展开为有限状态空间（BFS），检查：

| 类别 | 含义 |
|---|---|
| `SWITCH_UNLOCKED_AFTER_CLEAR` | 信号开放后道岔解锁（FPL 未锁闭或道岔杠杆仍可扳动） |
| `CONFLICT_SIMULTANEOUS_CLEAR` | 冲突进路的信号同时开放 |
| `PREMATURE_RELEASE` | 列车通过前提前释放（接近锁闭/区段释放） |
| `ROUTE_DEADLOCKED` | 应允许的进路被锁条错误锁死 |

发现问题时返回**最短反例轨迹**：逐步列出杠杆位置、区段占用、生效锁条、
命中规则和首个违规状态。状态空间超出限制时明确报告已检查边界
（`complete=false` + `boundary`），不把局部结果当作完整证明。

## 运行

```bash
pip install -r requirements.txt
uvicorn interlock.main:app --reload        # http://127.0.0.1:8000/docs
python -m pytest tests/                    # 运行测试
```

数据库默认写入当前目录 `interlock.db`，可用环境变量 `INTERLOCK_DB` 覆盖。

## 模型约定

- 杠杆只有定位 `N` / 反位 `R` 两个位置；`normal_position="R"` 表示**常反位杠杆**。
- 锁条表条目 `(a, pos_a, b, pos_b)`：`a` 处于 `pos_a` 时 `b` 被锁在 `pos_b`。
  双向生效：`a` 要扳到 `pos_a` 需 `b` 已在 `pos_b`（啮合要求），
  且 `a` 处于 `pos_a` 期间 `b` 不得离开 `pos_b`。
- 轨道电路锁闭 `track_locks`：区段占用期间杠杆被设备强制锁闭
  （`pos` 为 `null` 时锁在当前位置），是接近锁闭/区段锁闭的物理基础。
- 列车按进路 `approach_section → sections` 顺序占用/出清区段，
  每条进路同时至多一列（`rules.max_trains_per_route`）。

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/versions` | 保存版本（设备+规则快照） |
| GET | `/versions` / `/versions/{id}` | 列表 / 详情 |
| POST | `/versions/{id}/clone` | 克隆版本（可整体替换规格） |
| POST | `/versions/{id}/verify` | 有限状态校核，保存并返回结果（含最短反例轨迹） |
| GET | `/versions/{id}/runs` / `/runs/{id}` | 校核记录 |
| POST | `/versions/{id}/revise` | 固定现有锁条，只在指定空位生成增锁/改锁/移锁候选并复验全部进路 |
| GET | `/compare?from={a}&to={b}` | 比较两版本：可达状态数、反例增减、深度变化 |
| GET | `/versions/{id}/export/traces` | 导出全部反例轨迹（JSON） |
| GET | `/versions/{id}/export/locking-table` | 按进路排列的联锁表 |

## 规格示例

```json
{
  "sections": [{"id": "A1"}, {"id": "G1"}],
  "levers": [
    {"id": "S1", "kind": "signal"},
    {"id": "P",  "kind": "point"},
    {"id": "F",  "kind": "fpl"}
  ],
  "points":  [{"id": "P1", "lever_id": "P"}],
  "fpls":    [{"id": "F1", "point_id": "P1", "lever_id": "F"}],
  "signals": [{"id": "S1", "lever_id": "S1"}],
  "locking": [
    {"id": "E1", "lever_a": "S1", "pos_a": "R", "lever_b": "F", "pos_b": "R"},
    {"id": "E2", "lever_a": "F",  "pos_a": "R", "lever_b": "P", "pos_b": "N"}
  ],
  "track_locks": [
    {"id": "T1", "section": "A1", "lever": "F", "pos": "R"},
    {"id": "T2", "section": "G1", "lever": "P", "pos": null}
  ],
  "routes": [{
    "id": "R1", "signal_id": "S1", "approach_section": "A1",
    "sections": ["G1"], "points": [{"point_id": "P1", "position": "N"}]
  }],
  "rules":  {"conflicting_routes": [], "approach_locking": true, "section_release": true},
  "limits": {"max_states": 200000, "max_depth": 300}
}
```

## 修订工作流

1. `POST /versions/{id}/revise`：用 `fixed_entry_ids` 固定不动的锁条，
   用 `allowed_slots` 给出允许使用的空位；系统生成
   **增锁**（新增条目）、**改锁**（同杠杆对改位置）、**移锁**（删除旧条目、
   移到空位）候选，并对每个候选复验全部进路。
2. 取候选中的 `locking` 数组，通过 `POST /versions/{id}/clone` 存为新版本。
3. `POST /versions/{new}/verify` 复验，`GET /compare?from=..&to=..` 对比变化。

注意：`ok=true` 且 `complete=true` 才构成完整证明；
`complete=false` 时结果只覆盖已检查边界。
