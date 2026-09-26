# 纸本修复室 · 雾化管路配平系统

回湿脆化古画前，审核雾化管路能否把规定水量稳定送到每个分区：
**一处水源、2~4 个分区、0~4 个分流节点、4~10 条有向管路**，求整数流量方案。

- 水源流出**恰等于**水源总量；
- 每个分流节点流入 = 流出（不会凭空增减流量）；
- 每个分区流入**恰等于**精确需求（不会因某条支路有余量就让下游缺水）；
- 每条管路流量为整数且在 `[最小量, 最大量]` 内；
- 可行方案先最小化**相对优选量的绝对偏差和**，再按**管路录入顺序的流量序列字典序**稳定决胜；
- 不可行时给出逐点收支诊断（缺水点 / 积压点、缺口水量、可读原因）。

另有**韧性计划**：在现有草稿上选定 1~4 条可能临时停用的既有管路，
为每条填写可增设的非负整数上限与每单位增设成本，并给出全网备用增设
总量上限；服务端联合决定各管路的整数增设量，并对每一种所选管路单独
停用的情形重算配平（停用管流量固定为零，其余管路仍遵守既有范围、
节点守恒与分区精确需求）。多个计划可行时依次取**增设成本最低 →
总增设量最少 → 按管路录入顺序的增设序列字典序最小**；无解时明确说明
总量限制内无法覆盖的故障情形。

## 架构

| 组件 | 技术 | 说明 |
|---|---|---|
| `api` | Python 3.11 标准库（零第三方依赖） | 整数最小费用流求解 + HTTP API，端口 8000，`GET /healthz` 健康检查 |
| `web` | nginx + 原生静态页 | 录入草稿、发起配平/韧性计划、展示逐管流量与节点收支；反代 `/api/` 到 api，`GET /healthz` 健康检查 |
| `verify` | 一次性容器 | 单元测试 → 字节码构建核查 → API 业务冒烟 → Web/API 联调冒烟，跑完即退出，**退出码即验收结论** |

## 快速开始

```bash
# 宿主机端口可用环境变量配置（默认 web 8080 / api 8000）
WEB_PORT=8081 API_PORT=9000 docker compose up --build

# 浏览器打开 http://localhost:8081
```

只跑一次性验收（测试 + 构建 + 冒烟），并用 verify 的退出码报告结果：

```bash
docker compose build
docker compose up \
  --abort-on-container-exit \
  --exit-code-from verify verify
echo "验收退出码：$?"   # 0 通过，非 0 失败
```

> `--exit-code-from verify` 使整条 compose 命令返回 verify 容器的退出码；
> verify 依赖 api、web 健康检查通过后才开始冒烟，结束后自行退出，
> api/web 仍可按需要常驻（`docker compose up`）或由调用方停止。

## 端口配置

`docker-compose.yml` 读取宿主机环境变量，均有默认值：

| 变量 | 默认 | 含义 |
|---|---|---|
| `WEB_PORT` | `8080` | Web 页面宿主机端口 |
| `API_PORT` | `8000` | API 宿主机端口（一般只需经 Web 反代访问） |

可复制 `.env.example` 为 `.env` 后调整（`docker compose` 自动读取）。

## HTTP API

### `POST /api/balance`

请求体：

```json
{
  "source": {"id": "S"},
  "source_total": 10,
  "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
  "nodes": [{"id": "N"}],
  "pipes": [
    {"id": "p1", "from": "S", "to": "N", "min": 0, "max": 10, "preferred": 5},
    {"id": "p2", "from": "N", "to": "A", "min": 0, "max": 10, "preferred": 3},
    {"id": "p3", "from": "N", "to": "B", "min": 0, "max": 10, "preferred": 7},
    {"id": "p4", "from": "S", "to": "A", "min": 0, "max": 0,  "preferred": 0}
  ]
}
```

- 数量/连接方向/范围（`min ≤ preferred ≤ max`、非负整数）等校验失败返回 `400 {"error": ...}`。
- 校验通过返回 `200`，业务可行性由 `feasible` 表达：
  - 可行：`objective`（绝对偏差和）、`tie_sequence`（决胜流量序列）、
    `flows[]`（逐管流量与偏差）、`balances`（水源/节点/分区收支，守恒差额均为 0）；
  - 不可行：`infeasibility` 含总量与需求合计、已成立/缺口流量、
    `deficits`（进水不足点）、`surpluses`（来水积压点）与中文 `reasons`。

### `POST /api/plan`

在 `POST /api/balance` 的完整草稿字段之外，追加：

```json
{
  "total_budget": 10,
  "contingencies": [
    {"pipe_id": "p2", "add_max": 8, "unit_cost": 3},
    {"pipe_id": "p4", "add_max": 8, "unit_cost": 1}
  ]
}
```

- `contingencies`：1~4 条可能单独失效的既有管路（`pipe_id` 须在草稿中、
  不重复），`add_max`/`unit_cost`/`total_budget` 均为非负整数；
  校验失败返回 `400 {"error": ...}`。
- 校验通过返回 `200`，业务结论由 `feasible` 表达：
  - 可行：`plan` 含统一增设方案（`add_sequence` 按录入顺序的增设序列、
    `total_added`、`total_cost`、`budget_remaining`）；`contingencies[]`
    给出每个故障情形的重配平结果（逐管流量——停用管 `disabled=true`
    且流量为 0，加装管带 `added_capacity`/`effective_max`——与分区/节点
    收支，守恒差额均为 0）；
  - 不可行：`uncovered[]` 明确列出总量限制内无法覆盖的故障情形及原因
    （结构性加满仍不可行 / 单故障所需增设超总量上限 / 多故障竞争同一
    备用总量）。

### `GET /healthz`

返回 `200 {"status":"ok","service":"balance-api"}`。

## 算法（app/）

- `mincost.py`：连续最短路增广（SPFA/Bellman-Ford）的整数最小费用流。
  整数容量按整数瓶颈增广，流量必为整数。
- `balance.py`：以**优选量为初始预流**，再用超源/超汇调整：
  - 增大边费用 `P+q`、减小边费用 `P-q`，全部为正，无负费用环；
  - 每偏离优选量 1 单位，费用的主目标分量恰为 `P`；
    净变化携带录入顺序权重 `q`，故最小费用
    ⇔ 先最小化绝对偏差和、再最小化录入顺序字典序；
  - 超源/超汇两侧必须同时饱和才可行（总量 ≠ 需求合计时两侧总量不等，必然不可行）。
- `plan.py`：韧性计划。任一守恒网络都存在各管流量 ≤ `W+Σmax` 的可行流，
  故增设量有有限有效上界；对每个故障情形用"增设弧最小费用流"求
  总量/坐标/成本三类精确下界，指导下界剪枝的深度优先枚举，叶子用
  `balance.compute` 精确判定（停用管流量固定为 0）。
- 测试在随机小网络上对**全量整数解暴力枚举**逐例对照
  可行性、最优目标值与字典序决胜序列（`tests/test_balance.py`，400 例；
  `tests/test_plan.py` 对增设组合暴力枚举对照计划最优性）。

## 本地开发（无需 Docker）

```bash
python3 -m app.server                 # 起 API：http://localhost:8000
python3 -m unittest discover -s tests # 跑测试
BASE_URL=http://127.0.0.1:8000 python3 smoke/smoke.py
```

## 目录

```
app/                 API 与求解器（标准库）
web/index.html       前端单页（录入/配平/结果展示）
nginx/default.conf   静态托管 + /api/ 反代 + Web 健康检查
tests/               unittest 单元/随机对照测试
smoke/               API 冒烟、Web 联调、健康等待、verify 入口
Dockerfile           多阶段镜像：api / web / verify 三个 target（均含 HEALTHCHECK）
docker-compose.yml   api / web / verify 三服务
```
