# TSN 两跳相机触发帧送达核验服务（FastAPI + Docker Compose）

产线网管调整门控表（Gate Control List）前，用本服务核验：周期上报的相机触发帧
经过两跳 TSN 后，在给定的第二跳/第一跳时钟偏差范围内，是否**始终**能在统一时限内送达。

- 提交重复周期（整数 µs，`1..1000`）、最多 4 帧（首跳入队偏移 + 两跳发送耗时）
- 每跳一个按**本地时钟**配置、不跨周期、短于周期的开门窗
- 第二跳时钟减第一跳时钟的偏差范围限制在 `[-20, 20] µs`，对其中**每个整数偏差**
  从空队列开始连续推演
- 输出每个偏差下“是否始终按时”；若有误期，给出**首个**到期仍未送达的帧：
  编号、周期序号，以及到期时刻两跳已发生的入队 / 起发 / 发完时刻（未发生为 `null`）

## 1. 快速开始

```bash
docker compose up --build
```

- 接口文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 核验接口：`POST http://localhost:8000/api/v1/verify`（另有短路径 `POST /verify`）

端口冲突时可改 `docker-compose.yml` 的 `ports`，或：

```bash
BASE_URL=http://localhost:8000 bash examples/try.sh
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

## 5. 目录结构

```
app/
  simulator.py   # 纯 Python 核验引擎（门窗排程、周期状态检测、误期快照）
  schemas.py     # Pydantic 请求模型、别名与语义校验
  main.py        # FastAPI 路由 /health /docs /api/v1/verify
tests/
  test_simulator.py   # 手算边界用例
  test_crosscheck.py  # 120 组随机配置 × 5 偏差，与独立逐拍 FIFO 仿真器对照
  test_api.py         # HTTP/校验/响应结构测试
  tick_oracle.py      # 独立写法的逐拍参照仿真器
examples/        # 三个可直接 curl 的请求与 try.sh
Dockerfile
docker-compose.yml
requirements.txt
```

## 6. 本地开发与自测

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install pytest httpx          # 仅自测需要
uvicorn app.main:app --port 8000  # 然后 bash examples/try.sh
python -m pytest tests/ -q
```

### 实测结果（交付前）

- `python -m pytest tests/ -q`：**153 passed**。
  - 手算语义：恰好关门发完允许、恰好到期送达算按时、同刻按提交顺序、按入队偏移排序、
    偏差换算二跳本地时刻、帧比窗长永不起发、第 3 周期才因积压误期等；
  - 随机交叉验证：120 组随机配置 × 5 个偏差，引擎的每个起发/发完时刻和“首个误期”
    与独立逐拍仿真器完全一致；判 pass 的配置在参照仿真器 500 周期长程观察中无迟期；
  - API：健康检查、路由别名、字段别名、`include_trace`、各类 `422` 校验、误期响应结构。
- 已在本机用 uvicorn 实启服务并跑通 `examples/try.sh`（真实 HTTP 200）：
  - `request_pass.json`：41 个偏差全部 pass；
  - `request_offset_threshold.json`：偏差 `−20..15` pass（δ=15 恰在关门时刻发完），
    `16..20` miss，首个误期快照与手算一致；
  - `request_build_up_miss.json`：首个误期为 **3 号帧 / 第 3 周期**，
    到期 300 时一跳已 237 起发、249 发完，二跳 249 入队但要到 316 才起发，
    预计 340 发完，迟 40 µs。
- `docker-compose.yml` 已通过 YAML 解析检查。

## 7. 关键建模假设（如与现场口径不同，改 `app/simulator.py` 即可）

1. `deadline_us` 未传时取 T（从本帧入队起一个周期）；若现场“时限”另有固定值，显式传入即可。
2. 门窗 `close_us` 允许等于 `period_us`（在周期边界关门，不算跨周期）；`open=0,close=T`
   因窗长等于周期会被拒绝（违反“短于周期”）。
3. 门窗内剩余时间不足以发完整帧时不起发、不支持分片/抢占，等下一周期门窗。
4. 偏差 δ 指同一物理瞬间 `clock2 − clock1`，全程固定；评估区间内每个整数 δ 独立从空队列推演。
5. 耗时允许 0 µs（边界友好）；真实正值帧不受影响。

## 8. 待完成项 / 后续可选增强

- **本环境没有 Docker 守护进程/CLI，`docker compose up --build` 未在此处实际构建运行**；
  已用同一 `requirements.txt` 在 Python 3.11 虚拟环境实测应用与全部测试，
  并校验了 Compose 文件语法。需在装有 Docker 的交付环境执行一次 `docker compose up --build`
  做最终确认。
- 目前门窗为“每跳每周期一扇”，与题意一致；若现网 GCL 每跳有多扇门，可把
  `GateWindow` 扩成列表，排程函数已按“寻找最早可行窗”实现，扩展直接。
- 可按需增加批量核验、历史结果存储、按现网 GCL/拓扑自动生成请求等能力。
