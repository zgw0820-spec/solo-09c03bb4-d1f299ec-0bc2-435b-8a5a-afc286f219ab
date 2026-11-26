# TSN 两跳相机触发帧送达核验 / 诊断帧预约服务（FastAPI + Docker Compose）

产线网管调整门控表（Gate Control List）前，用本服务做两件事：

1. **核验**：周期上报的相机触发帧经过两跳 TSN 后，在给定的第二跳/第一跳
   时钟偏差范围内，是否**始终**能在统一时限内送达；
2. **预约演练**：用同一套两跳配置加**一个固定钟差**创建演练，配置核验通过后，
   在**从空队列起算的前 8 个周期**内为诊断帧寻找空档预约，**不改动相机触发帧
   的原定时序**。

- 提交重复周期（整数 µs，`1..1000`）、最多 4 帧（首跳入队偏移 + 两跳发送耗时）
- 每跳一个按**本地时钟**配置、不跨周期、短于周期的开门窗
- 第二跳时钟减第一跳时钟的偏差范围限制在 `[-20, 20] µs`，对其中**每个整数偏差**
  从空队列开始连续推演
- 输出每个偏差下“是否始终按时”；若有误期，给出**首个**到期仍未送达的帧：
  编号、周期序号，以及到期时刻两跳已发生的入队 / 起发 / 发完时刻（未发生为 `null`）
- 预约 API 在 8 周期窗口内同时避让**触发帧与已有预约**的发送时段，两跳都排得下
  才原子占位，排不下返回“无可用时段”

## 1. 快速开始

```bash
docker compose up --build
```

- 接口文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 核验接口：`POST http://localhost:8000/api/v1/verify`（另有短路径 `POST /verify`）
- 预约演练：`POST /api/v1/drills`、`POST /api/v1/drills/{id}/reservations`

端口冲突时可改 `docker-compose.yml` 的 `ports`，或：

```bash
BASE_URL=http://localhost:8000 bash examples/try.sh          # 核验 API
BASE_URL=http://localhost:8000 bash examples/try_drills.sh   # 预约 API
```

也可以直接逐个试：

```bash
curl -sS -X POST http://localhost:8000/api/v1/verify \
  -H 'Content-Type: application/json' \
  --data @examples/request_pass.json | python3 -m json.tool

curl -sS -X POST http://localhost:8000/api/v1/verify \
  -H 'Content-Type: application/json' \
  --data @examples/request_offset_threshold.json | python3 -m json.tool

curl -sS -X POST http://localhost:8000/api/v1/verify \
  -H 'Content-Type: application/json' \
  --data @examples/request_build_up_miss.json | python3 -m json.tool
```

三个示例的实测结论见下文“示例与实测结果”。

## 2. 请求字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `period_us` | 是 | 重复周期 T，整数，`1..1000` µs |
| `deadline_us` | 否 | 统一送达时限，从**本帧**首跳入队起算，`1..T`；缺省等于 T（最长一个周期） |
| `clock_offset_range_us` | 否 | `[min, max]`，第二跳时钟 − 第一跳时钟的偏差范围，端点均在 `[-20,20]`；范围内每个整数偏差都评估 |
| `clock_offset_us` | 否 | 单一固定偏差的简写（min=max） |
| `offset_min_us` / `offset_max_us` | 否 | 直接给范围端点（缺省均为 0） |
| `hop1_gate` / `hop2_gate` | 是 | `{"open_us": o, "close_us": c}`，各跳本地时钟的周期门窗，要求 `0 ≤ o < c ≤ T` 且 `c-o < T` |
| `frames` | 是 | `1..4` 帧，**数组顺序即提交顺序**（同时刻的决胜顺序） |
| `frames[].id` | 否 | 调用方编号/名称，原样回显 |
| `frames[].enqueue_offset_us` | 是 | 周期内首跳入队时刻，`0..T-1` |
| `frames[].hop1_tx_us` / `hop2_tx_us` | 是 | 两跳发送耗时（整数 µs，`≥0`） |
| `include_trace` | 否 | `true` 时附带每帧排程时间（调试用，最多 2000 条） |

> 为方便对接，常用别名字段也能识别，例如顶层 `period`/`period_duration_us`、
> `offset_range_us`/`offset_range`、`frame_configs`/`gate1`/`gate2`，门窗的
> `open/close`、`start/end`，帧的 `id/name`、`enqueue_us`、`tx1/tx2` 等；
> 也接受 `gates: {hop1, hop2}` 嵌套和常见中文键（`周期`、`偏差范围`、`帧`、
> `首跳入队时刻`、`一跳/二跳发送耗时`、`开门时刻/关门时刻` 等）。

请求示例：

```json
{
  "period_us": 1000,
  "deadline_us": 1000,
  "clock_offset_range_us": [-20, 20],
  "hop1_gate": { "open_us": 0, "close_us": 900 },
  "hop2_gate": { "open_us": 40, "close_us": 980 },
  "frames": [
    { "id": "cam-front", "enqueue_offset_us": 0, "hop1_tx_us": 90, "hop2_tx_us": 120 },
    { "id": "cam-left",  "enqueue_offset_us": 120, "hop1_tx_us": 90, "hop2_tx_us": 120 }
  ]
}
```

## 3. 时间与调度语义（实现口径）

所有计算都是**整数微秒**，无浮点。

1. **统一时间轴**：令第一跳时钟即统一时间轴（其读数 = 统一时刻 t）。
   固定偏差 `δ = clock2 − clock1` 后，统一时刻 t 在第二跳时钟读 `t + δ`；
   第二跳门窗相位 `[o2,c2]` 换算到统一时间轴为 `[o2−δ, c2−δ]`，仍按 T 周期重复。
2. **帧顺序**：实例按首跳入队绝对时刻 `kT + enqueue_offset` 排序，同刻按提交顺序。
   第一跳 FIFO 串行发送；第一跳发完的时刻才进入第二跳队列，因此第二跳顺序与之一致。
3. **尽早发送 + 整帧在窗内**：从“入队时刻/上一帧发完时刻”的较大值开始，找最早的
   整数起发点 s，使 `s + tx ≤ close`（**恰好关门发完允许**，等号成立）。
   当前窗剩余长度不够整帧时，不半截发送，等下一个周期门窗（与 802.1Qbv
   “门关闭前来不及发完整帧就不起发”的实际行为一致）。
4. **首跳发完才入第二跳**：`enqueue2 = finish1`（同一物理瞬间，第二跳本地读数为
   `finish1 + δ`）。
5. **时限判定**：第二跳发完（送达）时刻 `f2 ≤ enqueue1 + deadline_us` 为按时；
   `f2` 不存在（帧比门窗长，永远无法整帧入窗）或大于到期时刻即为误期。
6. **首个误期帧**：按时间顺序推演时遇到的第一个误期帧，就是到期时刻最早的未送达帧。

### 如何判定“始终”按时（而不是只看几个周期）

到达、门窗、排序全部以 T 为周期，调度是确定性的。每个周期算完后，记录该周期最后一帧
两跳“发完时刻相对本周期零点”的余数对 `(r1,r2)`。该状态唯一决定下一周期的全部排程：

- 若余数对在某个周期**重复**，之后的整张时刻表只会整体平移若干个 T，因此未来不会再出现新误期 → `verdict = "pass"`，并在 `cycle` 中给出周期编号证据；
- 从空队列出发，该余数序列分量单调不减；在“尚未误期”的轨迹上余数是有界整数
  （`r ∈ [最后一帧入队偏移, 该偏移+deadline]`，跨度 ≤ T），所以必然很快到达重复状态，
  数学上不需要无限推演，也不会漏判积压型误期。
- 若中途误期，立即停止并输出首个误期报告（含它实际是在第几个周期积压后才超时）。

## 4. 误期报告怎么读

`results[]` 中每个偏差一项：

```jsonc
{
  "offset_us": 16,
  "verdict": "deadline_miss",      // 或 "pass"
  "always_on_time": false,
  "first_miss": {
    "frame_no": 1,                 // 提交顺序编号（1 起）
    "frame_id": "cam-trigger",
    "period_no": 1,                // 第几个重复周期（1 起；另有 0 基 period_index）
    "deadline": { "timeline_us": 400, "hop1_local_us": 400, "hop2_local_us": 436 },
    "hop1": {
      "enqueue_at": { "timeline_us": 0,   "local_us": 0 },
      "start_at":   { "timeline_us": 0,   "local_us": 0 },
      "finish_at":  { "timeline_us": 100, "local_us": 100 }
    },
    "hop2": {
      "enqueue_at": { "timeline_us": 100, "local_us": 116 },
      "start_at": null,             // 到期(400)时还没起发 -> null
      "finish_at": null
    },
    "planned": { /* 完整排程，即使晚于到期也列出，便于定位门冲突 */ },
    "late_by_us": 684
  }
}
```

- `hop1/hop2` 下的 `enqueue_at/start_at/finish_at`：**仅当时刻 ≤ 到期时刻才给值**，
  否则为 `null`（恰好到期的事件算“已发生”，会列出）。
- 每个时刻同时给 `timeline_us`（统一时间轴 = 一跳时钟）和 `local_us`（该跳本地时钟；
  二跳 = 统一时刻 + δ）。
- `planned` 保留引擎为该帧排出的完整时刻（含到期之后的起发/发完，或因帧比窗长而为
  `null`），并给出机器可读 `reason`：
  `delivery_after_deadline` / `hop1_frame_longer_than_gate_window` /
  `hop2_frame_longer_than_gate_window` / `hop1_no_feasible_gate_slot` /
  `hop2_no_feasible_gate_slot`。

顶层汇总：`all_offsets_pass`、`pass_offsets_us`、`miss_offsets_us`、
`pass_count/miss_count` 等。

## 5. 诊断帧预约 API（演练）

目标：**保住相机触发帧的原定时序**，在触发帧留出的空档里插入诊断帧。全部时刻
都是整数 µs，输入与输出**统一用首跳时钟**（统一时间轴）；响应里另附二跳本地时钟
读数（统一时刻 + 固定钟差 δ）。

### 5.1 创建演练 `POST /api/v1/drills`

请求体与核验接口**完全相同的两跳配置**，但钟差必须是**一个固定值**
（`clock_offset_us`，或两端相等的 `clock_offset_range_us`）。

- 服务先在该固定偏差下跑核验：未通过（存在误期）直接 **HTTP 422** 拒绝，
  响应体带首个误期报告，不创建演练；
- 核验通过后，取从空队列起算的**前 8 个周期**作为可预约窗口
  （horizon = `8 × period_us`），并把触发帧在这 8 个周期内两跳的发送时段
  记为初始占用，随 `trigger_schedule` 返回；
- 返回 `drill_id`、钟差、周期、窗口上界、核验 cycle 证据、触发帧时刻表。

### 5.2 预约一帧 `POST /api/v1/drills/{drill_id}/reservations`

每次提交一帧的五个量（均为首跳时钟）：

| 字段 | 说明 |
| --- | --- |
| `hop1_earliest_start_us` | 首跳**最早起发时刻** a（含，`≥0` 且 `< horizon`） |
| `hop2_latest_finish_us` | 二跳**最晚发完时刻** b（含边界，`b > a` 且 `≤ horizon`） |
| `hop1_tx_us` / `hop2_tx_us` | 两跳**发送耗时**（整数 µs，`≥0`） |
| `max_relay_wait_us` | **中转等待上限** w：二跳起发 − 首跳发完 的最大值 |
| `frame_id` | 可选，调用方编号，原样回显 |

排程约束（全部满足才占位）：

1. **整帧在本地门窗内**：起发 `s` 满足 `s + tx ≤ close`，恰好关门发完允许；
   剩余窗长不够整帧则等下一周期门窗（与核验引擎同一口径）；
2. **避让占用**：任一跳的发送时段 `[s, s+tx)` 不得与该跳上触发帧或已有预约的
   发送时段重叠；仅首尾相接（`[..,f)` 与 `[f,..)`）不算重叠；
3. **二跳等首跳发完**：`s2 ≥ f1`；
4. **等候不超限**：`s2 − f1 ≤ max_relay_wait_us`；
5. `s1 ≥ a`、`f2 ≤ b`，且两跳都落在 `[0, 8T)` 窗口内（在窗口边界发完允许）。

返回**最早送达**（`f2` 最小，并列时 `s1` 最小）的一组起发/发完时刻：

```jsonc
{
  "drill_id": "...",
  "available": true,
  "reason": null,
  "reservation": {
    "reservation_id": "...",
    "frame_id": "diag-1",
    "period_no": 1,                       // 落在第几个周期（1 起）
    "hop1": { "start_timeline_us": 100, "finish_timeline_us": 130,
              "start_local_us": 100, "finish_local_us": 130 },
    "hop2": { "start_timeline_us": 200, "finish_timeline_us": 230,
              "start_local_us": 200, "finish_local_us": 230 },
    "relay_wait_us": 70,
    "request_constraints": { /* 回显本次提交的五个约束量 */ }
  }
}
```

排不下时返回 **HTTP 200**（业务失败而非请求错误），`available=false`、
`reservation=null`，且**不占用任何时段**，`reason` 取值：

- `relay_wait_limit_exceeded`：所有可行起点的中转等待都超上限；
- `hop2_latest_finish_unreachable`：最早送达仍晚于 `hop2_latest_finish_us`；
- `hop1_no_available_slot` / `hop2_no_available_slot`：窗口内该跳没有
  合门窗且不冲突的空档；
- `frame_longer_than_gate_window`：某跳耗时比所有门窗都长；
- `reservation_horizon_exceeded`：约束时刻超出 8 周期窗口。

### 5.3 查询接口

- `GET /api/v1/drills`：列出全部演练（id、钟差、周期、预约数）；
- `GET /api/v1/drills/{id}`：演练快照，含全部预约与两跳占用区间
  （`occupied_hop1_us` / `occupied_hop2_us`，统一时间轴的 `[start, finish)`）；
- `GET /api/v1/drills/{id}/reservations`：仅列预约。
- 未知 `drill_id` 一律 **HTTP 404**。

### 5.4 并发与占位原子性

演练状态保存在单进程内存中，由一把可重入锁保护：**“检查两跳 → 占位”是原子
操作**。并发请求不会相互穿插，因此同一跳的预约时段永不重叠；只有两跳都排得下
才写入占用，任一跳排不下就整体放弃并返回无可用时段。测试中用真实线程 / HTTP
并发验证了该不变量（见 `tests/test_booking.py`、`tests/test_booking_api.py`）。

### 5.5 排程算法口径（为什么返回的是最早送达）

- 首跳候选起点按升序枚举：每次取“不早于下界、整帧入当前/下一周期门窗、且不与
  占用区间重叠”的最早整数起点；遇到冲突就跳到冲突区间末尾，因此迭代次数由
  占用区间数与周期数界定（窗口最多 8000 µs、8 个周期）。
- 对每个首跳起点，二跳用同一规则在 `[f1, f1+w]` 内找最早空档。
- 中转等待关于 `s1` **不单调**（较早的 `f1` 可能撞上二跳占用而久等，更晚的
  `f1` 反而等待为 0），所以等待超限只淘汰当前候选、继续扫描下一个首跳起点；
  扫描到的第一个可行解即全局最早送达解（两个“最早可行起点”函数对下界单调）。

### 5.6 可直接试的请求

```bash
# 1) 创建演练（核验通过才返回 drill_id）
curl -sS -X POST http://localhost:8000/api/v1/drills \
  -H 'Content-Type: application/json' \
  --data @examples/drill_create.json | python3 -m json.tool

# 2) 用返回的 drill_id 预约（脚本会自动提取 id 并连跑 5 个场景）
BASE_URL=http://localhost:8000 bash examples/try_drills.sh
```

示例文件：`examples/drill_create.json`、`examples/drill_reservation.json`、
`examples/drill_reservation_no_slot.json`。预约请求同样接受中文键
（`首跳最早起发时刻`、`二跳最晚发完时刻`、`一跳/二跳发送耗时`、`中转等待上限` 等）。

## 6. 目录结构

```
app/
  simulator.py   # 纯 Python 核验引擎（门窗排程、周期状态检测、误期快照、定周期推演）
  booking.py     # 诊断帧预约引擎（占用区间、最早送达排程、线程安全演练仓库）
  schemas.py     # Pydantic 请求模型、别名与语义校验（核验 / 演练 / 预约）
  main.py        # FastAPI 路由 /health /docs /api/v1/verify 与 /api/v1/drills
tests/
  test_simulator.py     # 手算边界用例（核验引擎）
  test_crosscheck.py    # 120 组随机配置 × 5 偏差，与独立逐拍 FIFO 仿真器对照
  test_api.py           # 核验 API 的 HTTP/校验/响应结构测试
  test_booking.py       # 预约引擎手算用例（含并发原子性、非单调等待）
  test_booking_api.py   # 演练/预约 API 的 HTTP/校验/并发测试
  tick_oracle.py        # 独立写法的逐拍参照仿真器
examples/            # 可直接 curl 的请求、try.sh 与 try_drills.sh
Dockerfile
docker-compose.yml
requirements.txt
```

## 7. 本地开发与自测

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install pytest httpx          # 仅自测需要
uvicorn app.main:app --port 8000  # 然后 bash examples/try.sh examples/try_drills.sh
python -m pytest tests/ -q
```

### 实测结果（交付前）

- `python -m pytest tests/ -q`：**187 passed**。
  - 核验引擎原有用例（153 个）全部保持通过：恰好关门发完允许、恰好到期送达算按时、
    同刻按提交顺序、按入队偏移排序、偏差换算二跳本地时刻、帧比窗长永不起发、
    第 3 周期才因积压误期，以及 120 组随机配置 × 5 偏差与逐拍仿真器的交叉对照；
  - 新增预约引擎手算用例：触发帧占用播种、门窗+占用联合避让、首尾相接不算重叠、
    最早起发/最晚发完/等待上限约束、8 周期窗口边界（恰在边界发完允许）、
    排不下不占位、**中转等待关于 s1 非单调时继续扫描**、钟差换算本地时刻等；
  - 并发：真实线程下 20 个并发请求时段两两不重叠；人为构造“全窗口只剩一个空档”后
    4 个并发请求恰有 1 个成功、其余返回无可用时段；
  - API：演练创建（核验通过/拒绝、固定钟差校验）、预约成功与无可用时段（HTTP 200 +
    `available=false`）、404、422 校验、中文键、16 路真实 HTTP 并发零重叠。
- 已在本机用 uvicorn 实启服务并跑通两个脚本（真实 HTTP 200）：
  - `examples/try.sh`：核验 API 三个示例结论与历史一致（41 偏差全 pass、
    阈值偏差 `−20..15` pass / `16..20` miss、3 号帧第 3 周期误期）；
  - `examples/try_drills.sh`：创建演练 → 首帧预约（首跳 100..130、二跳 200..230、
    等待 70 µs）→ 同参数第二帧自动接在其后 → 过紧请求返回
    `hop2_latest_finish_unreachable` 且不占位 → 快照列出全部预约与两跳占用。
- `docker-compose.yml` 已通过 YAML 解析检查。

## 8. 关键建模假设（如与现场口径不同，改对应模块即可）

1. `deadline_us` 未传时取 T（从本帧入队起一个周期）；若现场“时限”另有固定值，显式传入即可。
2. 门窗 `close_us` 允许等于 `period_us`（在周期边界关门，不算跨周期）；`open=0,close=T`
   因窗长等于周期会被拒绝（违反“短于周期”）。
3. 门窗内剩余时间不足以发完整帧时不起发、不支持分片/抢占，等下一周期门窗。
4. 偏差 δ 指同一物理瞬间 `clock2 − clock1`，全程固定；核验时区间内每个整数 δ 独立
   从空队列推演；演练只接受一个固定 δ。
5. 耗时允许 0 µs（边界友好）；真实正值帧不受影响。
6. 预约为**单进程内存状态**，重启即清空；发送时段按半开区间 `[start, finish)` 判重叠，
   两跳各自独立判冲突，跨跳时段允许重叠（同一诊断帧的两跳本就先后发生）。
7. 可预约范围固定为从空队列起算前 8 个周期，时刻以首跳时钟为统一时间轴，
   窗口上界 `8T` 处发完算合法。

## 9. 待完成项 / 后续可选增强

- **本环境没有 Docker 守护进程/CLI，`docker compose up --build` 未在此处实际构建运行**；
  已用同一 `requirements.txt`（fastapi 0.165.0 / pydantic 2.14.4 / uvicorn 0.54.0）
  在 Python 3.11 实测应用与全部 187 个测试，并校验了 Compose 文件语法。
  需在装有 Docker 的交付环境执行一次 `docker compose up --build` 做最终确认。
- 预约状态目前只在单进程内存中，未做持久化与多副本共享；若要多实例部署或重启保留，
  需把 `DrillStore` 换成数据库 / 分布式锁实现（路由层与引擎无需改动）。
- 目前门窗为“每跳每周期一扇”，与题意一致；若现网 GCL 每跳有多扇门，可把
  `GateWindow` 扩成列表，排程函数已按“寻找最早可行窗”实现，扩展直接。
- 暂无取消 / 修改预约的接口；如需释放已占位时段，可在 `DrillStore` 增加按
  `reservation_id` 删除并重建占用区间的方法。
- 可按需增加批量核验、历史结果存储、按现网 GCL/拓扑自动生成请求等能力。
