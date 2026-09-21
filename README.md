# 乐谱安全翻页规划 API

提交按演奏顺序排列的谱面系统高度、相邻系统边界的休止秒数、页面可用高度与最低安全休止，
服务用动态规划生成**完整分页方案**：

- 每页容纳连续若干系统，总高度**不得超过页面可用高度**；
- 支持两种排版模式（`layout_mode`）：
  - `single`（默认）：单页逐页翻，只允许在休止秒数 **≥ 最低安全休止**的边界翻页
    （最后一页之后无需翻页）；
  - `spread`：双页谱架，相邻左、右页组成一组同时可见，**组内（左页→右页）边界不检查休止**；
    只有**翻过右页**进入下一组才是实体翻页，要求休止 ≥ 最低安全休止，
    且只有实体翻页参与最短翻页休止最大化；
- 优化目标按字典序依次进行：
  1. **最大化**全曲最短的一次（实体）翻页休止（瓶颈值，越大越安全）；
  2. 在达到该瓶颈的方案中，**最小化各页留白高度的平方和**；
  3. 仍并列时，**取页数更少**的方案。
- 可选的**人工断点约束**（系统下标数组，默认均为空）：
  - `forced_break_after`：对应系统之后**必须分页**。在 spread 模式下若断出的是左页，
    该边界仍是组内边界——**不检查休止、不算实体翻页**，只有断出右页后的断点才检查
    `min_safe_rest` 并参与瓶颈优化；
  - `forbidden_break_after`：对应系统之后**禁止分页**，只能由同一页跨越该边界。
  - 两组下标重叠、或下标指向末系统（其后没有边界）时返回 `422` 并给出具体冲突；
    约束本身合法但无可行分页方案时沿用原有的无解诊断。

无解时不保存任何部分方案，并在响应中指出**首个无法容纳的系统**或**首个无法安全完成的
（实体）翻页边界**。所有成功方案存入 SQLite，可随时查询（含新增字段的完整回放）。

## 快速启动

唯一需要的一条命令（依赖安装与建库建表均随启动自动完成）：

```bash
docker compose up --build
```

服务地址：**http://localhost:8000**

- 交互式接口文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

## 配置

通过环境变量配置（`docker-compose.yml` 中已给出默认值，均可不改）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `/data/plans.db`（容器内） | SQLite 数据库文件路径，Compose 已挂载卷 `plans-data` 持久化 |
| `PORT` | `8000` | 服务监听端口，Compose 映射到宿主机 8000 |

## 接口

### `POST /plans` — 生成并保存分页方案

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "system_heights": [120, 130, 110, 125, 115],
    "boundary_rests": [1.0, 4.0, 0.5, 6.0],
    "page_height": 300,
    "min_safe_rest": 2.0
  }'
```

请求参数：

- `system_heights`：按顺序排列的各谱面系统高度（正数，至少 1 个）；
- `boundary_rests`：相邻系统之间边界的休止秒数，长度 = 系统数 − 1（非负）；
- `page_height`：页面可用高度（正数）；
- `min_safe_rest`：最低安全休止秒数（非负）；
- `layout_mode`：排版模式，`single`（默认）或 `spread`；
- `spread_start_side`：`left`（默认）或 `right`，spread 模式下第 1 页所在的一侧，
  之后左右交替；single 模式下忽略；
- `forced_break_after`：系统下标数组（默认 `[]`），对应系统之后**必须分页**；
- `forbidden_break_after`：系统下标数组（默认 `[]`），对应系统之后**禁止分页**，
  该边界只能由同一页跨越。下标从 0 起、取值范围 `0 .. 系统数 − 2`
  （末系统下标 `系统数 − 1` 指向全曲结束，不允许指定）；重复下标会自动去重排序。

成功返回 `201`（single 模式示例）：

```json
{
  "id": 1,
  "layout_mode": "single",
  "spread_start_side": "left",
  "forced_break_after": [],
  "forbidden_break_after": [],
  "page_count": 3,
  "min_turn_rest": 4.0,
  "total_slack_squared": 40950.0,
  "pages": [
    {"page": 1, "start_system": 0, "end_system": 1, "used_height": 250.0, "slack": 50.0, "turn_rest_after": 4.0, "side": null, "physical_turn_after": true},
    {"page": 2, "start_system": 2, "end_system": 3, "used_height": 235.0, "slack": 65.0, "turn_rest_after": 6.0, "side": null, "physical_turn_after": true},
    {"page": 3, "start_system": 4, "end_system": 4, "used_height": 115.0, "slack": 185.0, "turn_rest_after": null, "side": null, "physical_turn_after": false}
  ]
}
```

每页字段：

- `turn_rest_after`：该页之后（实体）翻页处的休止秒数；最后一页、以及 spread 组内
  （左页→右页）无需翻页的边界为 `null`；
- `side`：spread 模式下为 `"left"` / `"right"`；single 模式不分左右页，为 `null`；
- `physical_turn_after`：该页之后是否需要实体翻页。single 模式每次翻页都是实体翻页
  （最后一页除外）；spread 模式只有右页之后才是实体翻页。

`min_turn_rest` 为全曲最短（实体）翻页休止（无需实体翻页时为 `null`）。

#### spread 双页谱架示例

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "system_heights": [6, 6, 6],
    "boundary_rests": [0.2, 8.0],
    "page_height": 10,
    "min_safe_rest": 1.0,
    "layout_mode": "spread",
    "spread_start_side": "left"
  }'
```

边界 0 的休止 0.2s 低于最低安全休止，single 模式下无解；spread 模式下它位于
组内（页 1 左 → 页 2 右），不检查休止，只有页 2（右页）之后的实体翻页需要达标：

```json
{
  "layout_mode": "spread",
  "spread_start_side": "left",
  "page_count": 3,
  "min_turn_rest": 8.0,
  "pages": [
    {"page": 1, "start_system": 0, "end_system": 0, "side": "left",  "physical_turn_after": false, "turn_rest_after": null, "...": "..."},
    {"page": 2, "start_system": 1, "end_system": 1, "side": "right", "physical_turn_after": true,  "turn_rest_after": 8.0,  "...": "..."},
    {"page": 3, "start_system": 2, "end_system": 2, "side": "left",  "physical_turn_after": false, "turn_rest_after": null, "...": "..."}
  ]
}
```

#### 人工断点约束示例

```bash
curl -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "system_heights": [5, 5, 5],
    "boundary_rests": [5.0, 3.0],
    "page_height": 10,
    "min_safe_rest": 1.0,
    "forced_break_after": [1],
    "forbidden_break_after": []
  }'
```

无约束时 DP 会选择 `[0]|[1,2]`（瓶颈 5s）；`forced_break_after: [1]` 要求系统 1 之后
必须分页，方案变为 `[0,1]|[2]`（瓶颈 3s）——人工约束在 DP 转移阶段直接生效，
但不改变三级优化目标。成功响应原样回显两组约束，并随方案存入 SQLite：

```json
{
  "forced_break_after": [1],
  "forbidden_break_after": [],
  "page_count": 2,
  "min_turn_rest": 3.0,
  "pages": [
    {"page": 1, "start_system": 0, "end_system": 1, "physical_turn_after": true,  "turn_rest_after": 3.0, "...": "..."},
    {"page": 2, "start_system": 2, "end_system": 2, "physical_turn_after": false, "turn_rest_after": null,  "...": "..."}
  ]
}
```

`forbidden_break_after` 则相反：下标所在边界不会出现任何分页。在 spread 模式下，
`forced_break_after` 断出左页时仍是**组内**边界——不检查休止、`turn_rest_after` 为
`null`、`physical_turn_after` 为 `false`，也不参与瓶颈；只有断出右页、需要实体翻页时
才检查 `min_safe_rest` 并参与瓶颈最大化。

约束本身冲突时返回 `422`（`error` 为 `invalid_break_constraints`），`conflicts` 数组
逐项给出冲突类型与具体下标，且**不保存**：

```json
{
  "detail": {
    "error": "invalid_break_constraints",
    "message": "人工断点约束存在冲突，无法执行分页规划",
    "conflicts": [
      {
        "type": "overlap",
        "system_index": 1,
        "fields": ["forced_break_after", "forbidden_break_after"],
        "message": "系统下标 1 同时出现在 forced_break_after 与 forbidden_break_after 中：该边界不能既要求分页又禁止分页"
      }
    ]
  }
}
```

冲突类型：

- `overlap`：同一系统下标同时出现在两组约束中；
- `points_to_last_system`：下标指向末系统（末系统之后没有边界，无法分页）；
- `index_out_of_range`：下标为负数或超出系统范围（负数在请求校验阶段即被拒绝）。

约束合法但无可行分页时（例如 `forbidden_break_after` 两侧系统同页必然超高，或
`forced_break_after` 落在休止不足的实体翻页边界），沿用下一节的无解诊断
（`error` 为 `no_feasible_pagination`），并在诊断中附带 `forced_break_after` /
`forbidden_break_after` 指明相关下标，同样不保存。

无解返回 `422`，指出首个问题区间，且**不保存任何部分方案**：

```json
{
  "detail": {
    "error": "no_feasible_pagination",
    "reason": "unsafe_boundary",
    "boundary_index": 1,
    "between_systems": [1, 2],
    "rest_seconds": 0.2,
    "min_safe_rest": 1.0,
    "physical_turn": true,
    "message": "边界 1（系统 1 与系统 2 之间）是首个无法安全完成的实体翻页边界：此处必然结束一页右页、需要实体翻页，但休止 0.2s 低于最低安全休止 1.0s"
  }
}
```

`reason` 取值：

- `system_exceeds_page_height`：首个高度超过页面可用高度的系统（附 `system_index`），任何页面都无法容纳；
- `unsafe_boundary`：首个无法安全断开的边界（附 `boundary_index` 与相邻系统下标）。
  spread 模式下该边界必为实体翻页边界（附 `physical_turn: true`）——若该页能落在左页，
  组内边界本无需检查休止。

### `GET /plans/{id}` — 查询完整方案

```bash
curl http://localhost:8000/plans/1
```

不存在时返回 `404`。响应完整回放保存时的请求与结果，包括 `layout_mode`、
`spread_start_side`、`forced_break_after`、`forbidden_break_after` 及逐页的
`side`、`physical_turn_after`。

### `GET /plans` — 列出方案摘要

```bash
curl 'http://localhost:8000/plans?limit=50&offset=0'
```

摘要含 `layout_mode`，便于区分两种排版模式的方案。

## 本地开发（可选）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试（需额外安装 `pytest` 与 `httpx`）：

```bash
python3 -m pytest tests/ -q
```

## 实现说明

- 规划核心为两阶段精确动态规划（`app/planner.py`）：
  1. widest-path DP 求最大可行瓶颈（最短实体翻页休止的最大值），同时得到各位置可达性；
  2. 仅保留休止 ≥ 瓶颈的实体翻页边，做（留白平方和, 页数）字典序最小的 DP 并回溯切分点。
- 人工断点约束在两阶段 DP 的**转移枚举边**时统一生效：边 `(i,j)` 若跨越
  `forced_break_after` 中的边界则禁止（任何从起点到终点的路径因此必经过该页末位置）；
  以 `forbidden_break_after` 中的边界作为（非末页）页末的边禁止，只能由同页跨越。
  spread 模式下约束断出左页时仍是组内边界，不检查休止、不参与瓶颈。
- spread 模式下 DP 状态额外携带**已用页数奇偶**：第 1 页位于 `spread_start_side`，
  之后左右交替，页码奇偶因此决定该页之后是否为实体翻页（右页之后才是）。
  组内（左页→右页）边界不参与休止检查与瓶颈计算。
- 无解诊断：先检查首个超高的系统；否则沿阶段一的全部可行边标记每个边界被
  “同页跨越”或“安全断开”，取首个两种方式都不可行的边界——forbidden 边界不可跨越时
  报告该禁止断点，否则报告无法安全完成的（实体）翻页边界（spread 模式附
  `physical_turn: true`）。
- 存储使用 SQLite（`app/storage.py`），请求与结果以 JSON 整体保存，
  只有成功生成的完整方案才会入库；新增字段（含两组人工断点约束）随 JSON 自动持久化
  并可回放，旧记录缺失字段时按空数组 / single 默认值回放。
