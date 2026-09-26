"""关键管路韧性计划：选定 1~4 条可能单独失效的既有管路，预先加装
有限备用通量，使任一被选管路单独停用时全网仍可定量配平。

业务约束
--------
- 以当前**完整草稿**为基础（与配平接口同一套校验）；
- 每条候选管路填写：可增设的非负整数上限 ``add_max``、每单位增设成本
  ``unit_cost``；另填全网备用增设总量上限 ``total_budget``；
- 服务端联合决定各管路整数增设量 x_i（0 ≤ x_i ≤ add_max_i，
  Σx_i ≤ total_budget）；
- 对**每一条**所选管路单独停用的情形重算配平：停用管流量固定为 0，
  其余管路遵守（加装后的）既有范围、节点守恒与分区精确需求，全部可行
  才算计划可行；
- 增设只抬高管路最大量，不改变优选量/最小量。

目标（多个可行计划时依次）
--------------------------
1. 增设总成本 Σ unit_cost_i·x_i 最低；
2. 总增设量 Σ x_i 最少；
3. 按管路录入顺序的增设序列字典序最小。

求解
----
1. 增设量的**有效上界**：守恒网络中总存在各管流量 ≤ ``B = 水源总量 W +
   全网管路最大量之和`` 的可行流（把流量分解为"水源→分区"路径流与环流，
   环流仅由管路下界迫使存在；去掉环流后每管路径流 ≤ W，加回下界即得
   f_e ≤ W + max_e ≤ B）。故把某管最大量抬到 B 以上永远用不上，
   ``add_max`` 可按 B 截断，搜索域有限。
2. **最小增设费用流**：对某故障情形，把每条幸存候选管的"增设"建成
   容量 = min(有效上限, 剩余预算)、单位费用 = 权重的增设弧，叠加在按
   下界预流的可行性网络上跑最小费用流，饱和时的最小费用即"该情形至少
   需要的增设量/成本/某坐标取值"。由此得到三类精确下界用于剪枝：
   总量下界（预算可行性）、坐标下界（当前位至少增设多少）、成本下界
   （与当前最优方案比较）。
3. 在下界导引下做深度优先枚举（录入顺序、由小到大），叶子处用
   ``balance.compute`` 对每个故障情形做精确可行性判定；枚举总量受
   节点上限保护（病态输入时明确报错而非挂起）。

无解时区分：即使不加预算限制、把候选管全部加满仍无法覆盖的故障
（结构性无解），以及单看可覆盖但受 ``total_budget`` 联合限制而无法
同时满足的故障。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .balance import (
    ValidationError,
    _is_int,
    compute,
    validate_and_build,
)
from .mincost import MinCostFlow

# 候选故障管路 1~4 条
MIN_CANDIDATES, MAX_CANDIDATES = 1, 4
# 防御性数值上限（数量级远超任何业务水量，防止病态输入拖垮枚举）
MAX_ADD_VALUE = 1_000_000
# 枚举节点上限：正常用例远低于此；超过即判定输入规模超出计划能力
MAX_SEARCH_NODES = 20_000


class SearchSpaceTooLarge(Exception):
    """增设搜索空间超过保护上限。"""


def _candidate_int(obj: Any, key: str, label: str) -> int:
    if not isinstance(obj, dict):
        raise ValidationError("contingencies 的每一项必须是对象")
    if key not in obj:
        raise ValidationError(f"{label}缺失（字段 {key}）")
    v = obj[key]
    if not _is_int(v):
        raise ValidationError(f"{label}必须是非负整数")
    if v < 0:
        raise ValidationError(f"{label}不能为负数")
    if v > MAX_ADD_VALUE:
        raise ValidationError(f"{label}不能超过 {MAX_ADD_VALUE}")
    return v


def validate_plan(payload: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]], int]:
    """校验韧性计划请求，返回 (内部草稿模型, 候选列表[按录入序], 总量上限)。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    # 完整草稿部分复用配平校验（数量/连接/范围/优选量等全部一致）
    model = validate_and_build(payload)

    raw = payload.get("contingencies")
    if not isinstance(raw, list):
        raise ValidationError("contingencies 必须是数组（选定 1~4 条可能失效的管路）")
    if not (MIN_CANDIDATES <= len(raw) <= MAX_CANDIDATES):
        raise ValidationError(
            f"可能失效的管路必须选择 {MIN_CANDIDATES}~{MAX_CANDIDATES} 条")

    pipe_index = {p["id"]: i for i, p in enumerate(model["pipes"])}
    candidates: List[Dict[str, Any]] = []
    seen: set = set()
    for k, c in enumerate(raw):
        label = f"第 {k + 1} 个候选管路"
        if not isinstance(c, dict):
            raise ValidationError(f"{label}必须是对象")
        pid = c.get("pipe_id")
        if not isinstance(pid, str) or not pid.strip():
            raise ValidationError(f"{label}缺少 pipe_id")
        pid = pid.strip()
        if pid not in pipe_index:
            raise ValidationError(f"{label}（{pid}）不在草稿管路中")
        if pid in seen:
            raise ValidationError(f"候选管路重复：{pid}")
        seen.add(pid)

        add_max = _candidate_int(c, "add_max", f"{label}（{pid}）可增设上限")
        unit_cost = _candidate_int(c, "unit_cost", f"{label}（{pid}）单位增设成本")
        candidates.append({
            "pipe_id": pid,
            "pipe_order": pipe_index[pid],
            "add_max": add_max,
            "unit_cost": unit_cost,
        })

    # 按管路录入顺序排列：增设序列与字典序决胜均以此为准
    candidates.sort(key=lambda c: c["pipe_order"])

    total_budget = _candidate_int(payload, "total_budget", "全网备用总量上限")
    return model, candidates, total_budget


def _min_expansion_cost(model: Dict[str, Any], disabled_order: int,
                        fixed_add: Dict[int, int],
                        expansion: List[Tuple[int, int, int]]) -> Optional[int]:
    """某故障情形下达到可行所需的最小增设费用（费用=弧权重加权和）。

    - ``disabled_order``：被停用管路的录入序号（流量固定为 0，无下界）；
    - ``fixed_add``：{管路序号: 已确定增设量}，直接抬高该管最大量；
    - ``expansion``：[(管路序号, 增设弧容量, 单位权重)]，表示仍可加装的量。

    网络按管路**下界**预流（与 balance.compute 同构的超源/超汇可行性
    模型），幸存管基础容量费用 0、增设弧带权重；超源/超汇同时饱和时
    返回最小费用（SSP 对每个流量值都给出最小费用流），否则返回 None。
    """
    source_id = model["source_id"]
    total: int = model["total"]
    zones = model["zones"]
    nodes = model["nodes"]
    pipes = model["pipes"]
    zone_ids = [z["id"] for z in zones]

    business = [source_id] + nodes + zone_ids
    idx = {vid: i for i, vid in enumerate(business)}
    ss, tt = len(business), len(business) + 1
    mcf = MinCostFlow(tt + 1)

    exp_map = {po: (cap, w) for po, cap, w in expansion}
    balance = {vid: 0 for vid in business}
    for i, p in enumerate(pipes):
        if i == disabled_order:
            continue  # 停用管：流量固定为 0，无下界无容量
        u = idx[p["from"]]
        v = idx[p["to"]]
        lo = p["min"]
        hi = p["max"] + fixed_add.get(i, 0)
        if hi > lo:
            mcf.add_edge(u, v, hi - lo, 0)
        balance[p["from"]] -= lo
        balance[p["to"]] += lo
        if i in exp_map:
            cap, w = exp_map[i]
            if cap > 0:
                mcf.add_edge(u, v, cap, w)

    required_in: Dict[str, int] = {source_id: -total}
    for nn in nodes:
        required_in[nn] = 0
    for z in zones:
        required_in[z["id"]] = z["demand"]

    ss_edges: List[int] = []
    tt_edges: List[Tuple[int, int]] = []
    ss_total = tt_total = 0
    for vid in business:
        delta = balance[vid] - required_in[vid]
        if delta > 0:
            ei = len(mcf.g[ss])
            mcf.add_edge(ss, idx[vid], delta, 0)
            ss_edges.append(ei)
            ss_total += delta
        elif delta < 0:
            ei = len(mcf.g[idx[vid]])
            mcf.add_edge(idx[vid], tt, -delta, 0)
            tt_edges.append((idx[vid], ei))
            tt_total += -delta

    pushed, cost = mcf.flow(ss, tt, max(ss_total, tt_total))
    saturated = all(mcf.g[ss][ei].cap == 0 for ei in ss_edges) and all(
        mcf.g[v][ei].cap == 0 for v, ei in tt_edges)
    if not saturated:
        return None
    return cost


def _scenario_feasible(model: Dict[str, Any], add: Dict[int, int],
                       disabled_order: int) -> bool:
    return compute(model, add=add,
                   disabled=frozenset({disabled_order}))["feasible"]


class _Search:
    """联合最优增设搜索：在下界剪枝的 DFS 上求 (成本, 总量, 字典序) 最优。"""

    def __init__(self, model: Dict[str, Any], orders: List[int],
                 eff_caps: List[int], costs: List[int], budget: int):
        self.model = model
        self.orders = orders
        self.caps = eff_caps
        self.costs = costs
        self.budget = budget
        self.n = len(orders)
        self.best_x: Optional[List[int]] = None
        self.best_cost = 0
        self.best_amount = 0
        self.nodes = 0
        self._feas_cache: Dict[Tuple, bool] = {}

    # ---- 精确可行性（叶子判定，带缓存） ----
    def _scenario_ok(self, add: Dict[int, int], disabled_order: int) -> bool:
        key = (disabled_order,) + tuple(sorted(add.items()))
        cached = self._feas_cache.get(key)
        if cached is None:
            cached = _scenario_feasible(self.model, add, disabled_order)
            self._feas_cache[key] = cached
        return cached

    def _all_feasible(self, x: List[int]) -> bool:
        add = {self.orders[j]: xj for j, xj in enumerate(x) if xj > 0}
        return all(self._scenario_ok(add, self.orders[si])
                   for si in range(self.n))

    # ---- 下界 ----
    def _expansion(self, i: int, si: int, remain: int,
                   weight_of) -> List[Tuple[int, int, int]]:
        """未定坐标 i..n-1 的增设弧（跳过该情形的停用管）。"""
        arcs = []
        for j in range(i, self.n):
            if j == si:
                continue
            arcs.append((self.orders[j],
                         min(self.caps[j], remain), weight_of(j)))
        return arcs

    def _bounds(self, i: int, x: List[int], cur_cost: int, cur_amount: int,
                remain: int) -> Tuple[bool, int, Optional[str]]:
        """对分支 (前缀 x[0..i-1], 剩余预算 remain) 计算下界。

        返回 (是否可能可行, 第 i 位增设量下界, 剪枝原因)。
        原因 "amount"/"cost" 关于当前位取值单调（每多增设 1 单位，最小
        追加量/费用至多回落 1 单位/1 份单位成本），可用于循环提前终止。
        """
        fixed = {self.orders[j]: x[j] for j in range(i) if x[j] > 0}
        lo = 0
        for si in range(self.n):
            disabled = self.orders[si]
            # 总量下界：该情形至少还需多少增设量（单位权重）
            m = _min_expansion_cost(
                self.model, disabled, fixed,
                self._expansion(i, si, remain, lambda _j: 1))
            if m is None:
                return False, 0, "structural"
            if cur_amount + m > self.budget:
                return False, 0, "amount"
            # 坐标下界：第 i 位至少增设多少（该位权重 1，其余 0）
            if i != si:
                t = _min_expansion_cost(
                    self.model, disabled, fixed,
                    self._expansion(i, si, remain,
                                    lambda j: 1 if j == i else 0))
                if t is None:
                    return False, 0, "structural"
                if t > lo:
                    lo = t
            # 成本下界：与当前最优方案比较（单位成本权重）
            if self.best_x is not None:
                mc = _min_expansion_cost(
                    self.model, disabled, fixed,
                    self._expansion(i, si, remain,
                                    lambda j: self.costs[j]))
                if mc is None:
                    return False, 0, "structural"
                if cur_cost + mc > self.best_cost:
                    return False, 0, "cost"
        return True, lo, None

    # ---- 枚举 ----
    def _dfs(self, i: int, remain: int, cur_cost: int, cur_amount: int,
             x: List[int]) -> Optional[str]:
        """返回本节点被剪枝的原因（未剪枝则 None）。"""
        self.nodes += 1
        if self.nodes > MAX_SEARCH_NODES:
            raise SearchSpaceTooLarge
        if i == self.n:
            if self._all_feasible(x):
                self._consider(x, cur_cost, cur_amount)
            return None

        ok, lo, reason = self._bounds(i, x, cur_cost, cur_amount, remain)
        if not ok:
            return reason

        hi = min(self.caps[i], remain)
        cunit = self.costs[i]
        if self.best_x is not None and cunit > 0:
            hi = min(hi, (self.best_cost - cur_cost) // cunit)
        if lo > hi:
            # 坐标下界超过可取值：关于父层取值非单调（父层多增设可能降低
            # 本位需求），父层不得因此终止循环
            return "coord"

        xi = lo
        while xi <= hi:
            new_cost = cur_cost + cunit * xi
            new_amount = cur_amount + xi
            if self.best_x is not None:
                if new_cost > self.best_cost:
                    break  # xi 再增大，成本只升不降
                if new_cost == self.best_cost:
                    if new_amount > self.best_amount:
                        break  # 同理，总量只升不降
                    if (new_amount == self.best_amount
                            and x + [xi] > self.best_x[:i + 1]):
                        break  # 前缀字典序已更大，后续无法逆转
            child_reason = self._dfs(i + 1, remain - xi, new_cost,
                                     new_amount, x + [xi])
            # 总量/成本下界关于 xi 单调不减：子分支因此失败时不必再试更大值
            if child_reason in ("amount", "cost"):
                break
            xi += 1
        return None

    def _consider(self, x: List[int], cost: int, amount: int) -> None:
        bx = self.best_x
        if (bx is None or cost < self.best_cost
                or (cost == self.best_cost
                    and (amount < self.best_amount
                         or (amount == self.best_amount
                             and tuple(x) < tuple(bx))))):
            self.best_x = x[:]
            self.best_cost = cost
            self.best_amount = amount

    def _seed(self, x: List[int]) -> None:
        """用一个已知预算内的可行方案初始化最优界，加速剪枝。"""
        if sum(x) <= self.budget and all(0 <= xj <= self.caps[j]
                                         for j, xj in enumerate(x)):
            if self._all_feasible(x):
                self._consider(x,
                               sum(self.costs[j] * x[j] for j in range(self.n)),
                               sum(x))

    def solve(self) -> Optional[Tuple[List[int], int, int]]:
        # 贪心种子：把预算按序加满（容量最大的方案最可能可行）
        for order in (range(self.n), range(self.n - 1, -1, -1)):
            g = [0] * self.n
            remain = self.budget
            for j in order:
                g[j] = min(self.caps[j], remain)
                remain -= g[j]
            self._seed(g)
        self._seed([0] * self.n)

        self._dfs(0, self.budget, 0, 0, [])
        if self.best_x is None:
            return None
        return self.best_x, self.best_cost, self.best_amount


def solve_plan(payload: Any) -> Dict[str, Any]:
    """求韧性计划。校验失败由调用方转 400。"""
    model, candidates, total_budget = validate_plan(payload)

    total_water = model["total"]
    orders = [c["pipe_order"] for c in candidates]
    raw_caps = [c["add_max"] for c in candidates]
    costs = [c["unit_cost"] for c in candidates]
    n = len(candidates)

    # 有效增设上界：存在各管流量 ≤ B = W + Σmax 的可行流（环流仅由下界
    # 迫使），把某管最大量抬到 B 以上永远用不上，可安全截断搜索域。
    flow_bound = total_water + sum(p["max"] for p in model["pipes"])
    eff_caps = [
        min(raw_caps[k], max(0, flow_bound - model["pipes"][orders[k]]["max"]))
        for k in range(n)
    ]
    eff_budget = min(total_budget, sum(eff_caps))

    base_out = {
        "source_id": model["source_id"],
        "source_total": model["total"],
        "zones": model["zones"],
        "nodes": model["nodes"],
        "pipes": model["pipes"],
    }

    def candidate_rows(adds: List[int]) -> List[Dict[str, Any]]:
        rows = []
        for k, c in enumerate(candidates):
            rows.append({
                "pipe_id": c["pipe_id"],
                "pipe_order": c["pipe_order"],
                "add_max": c["add_max"],
                "unit_cost": c["unit_cost"],
                "added": adds[k],
                "added_cost": c["unit_cost"] * adds[k],
            })
        return rows

    # ---- 结构性无解诊断：不受预算限制、按有效上限加满仍覆盖不了的故障 ----
    structural: List[Dict[str, Any]] = []
    for k, c in enumerate(candidates):
        expansion = [(orders[j], eff_caps[j], 1) for j in range(n) if j != k]
        if _min_expansion_cost(model, c["pipe_order"], {}, expansion) is None:
            add = {orders[j]: eff_caps[j] for j in range(n)
                   if j != k and eff_caps[j] > 0}
            r = compute(model, add=add, disabled=frozenset({c["pipe_order"]}))
            structural.append({
                "pipe_id": c["pipe_id"],
                "pipe_order": c["pipe_order"],
                "reason": ("即使把其余候选管路按各自上限全部加满，停用该管后"
                           "仍无法满足节点守恒与各分区精确需求"
                           "（结构性容量/连通不足，与总量上限无关）"),
                "infeasibility": r["infeasibility"],
            })

    if structural:
        return {
            "feasible": False,
            "total_budget": total_budget,
            "candidates": candidate_rows([0] * n),
            "plan": None,
            "contingencies": None,
            "uncovered": structural,
            "message": ("存在即使加满备用通量也无法覆盖的故障情形，"
                        "韧性计划无解；下列管路停用后无法保证各分区定量供水"),
            "draft": base_out,
        }

    # ---- 联合最优搜索 ----
    search = _Search(model, orders, eff_caps, costs, eff_budget)
    try:
        found = search.solve()
    except SearchSpaceTooLarge:
        raise ValidationError(
            "满足约束的增设组合过多，超过求解保护上限：请收紧相关管路的"
            "可增设上限或全网备用总量上限后重试")

    if found is None:
        # 加满可覆盖、但受 total_budget 联合限制：逐故障判断在总量内
        # 单独能否覆盖（可把全部预算用于该故障的幸存管），明确报告。
        uncovered = []
        for k, c in enumerate(candidates):
            expansion = [(orders[j], min(eff_caps[j], eff_budget), 1)
                         for j in range(n) if j != k]
            need = _min_expansion_cost(model, c["pipe_order"], {}, expansion)
            if need is None or need > eff_budget:
                if need is None:
                    reason = (f"即使把全网备用总量 {total_budget} 全部用于该故障"
                              "所需的幸存管路，停用该管后仍无法配平")
                else:
                    reason = (f"停用该管至少需要在幸存管路上增设 {need} 单位，"
                              f"超过全网备用总量上限 {total_budget}")
                uncovered.append({
                    "pipe_id": c["pipe_id"],
                    "pipe_order": c["pipe_order"],
                    "reason": reason,
                    "infeasibility": None,
                })
        if not uncovered:
            # 每个故障单独都能覆盖，但共用同一总量上限无法同时满足
            for c in candidates:
                uncovered.append({
                    "pipe_id": c["pipe_id"],
                    "pipe_order": c["pipe_order"],
                    "reason": ("各故障情形单独看均可在总量上限内覆盖，但所需"
                               "增设相互竞争同一备用总量，无法找到统一增设方案"
                               "同时覆盖全部故障，请上调全网备用总量上限"),
                    "infeasibility": None,
                })
        return {
            "feasible": False,
            "total_budget": total_budget,
            "candidates": candidate_rows([0] * n),
            "plan": None,
            "contingencies": None,
            "uncovered": uncovered,
            "message": ("在全网备用总量上限内无法覆盖全部所选故障情形，"
                        "韧性计划无解；下列故障情形无法保证各分区定量供水"),
            "draft": base_out,
        }

    x, best_cost, best_amount = found
    add = {orders[k]: x[k] for k in range(n) if x[k] > 0}

    # 各故障情形按同一加装方案重算最优配平
    contingencies = []
    for k, c in enumerate(candidates):
        r = compute(model, add=add, disabled=frozenset({c["pipe_order"]}))
        contingencies.append({
            "pipe_id": c["pipe_id"],
            "pipe_order": c["pipe_order"],
            "feasible": True,
            "objective": r["objective"],
            "tie_sequence": r["tie_sequence"],
            "flows": r["flows"],
            "balances": r["balances"],
        })

    rows = candidate_rows(x)
    return {
        "feasible": True,
        "total_budget": total_budget,
        "candidates": rows,
        "plan": {
            "additions": rows,
            "add_sequence": x,
            "total_added": best_amount,
            "total_cost": best_cost,
            "budget_remaining": total_budget - best_amount,
        },
        "contingencies": contingencies,
        "uncovered": [],
        "message": "韧性计划可行：以下统一增设方案可覆盖任一所选管路单独停用",
        "draft": base_out,
    }
