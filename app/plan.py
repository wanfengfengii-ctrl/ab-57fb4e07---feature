"""关键管路停用韧性计划：联合选择整数增设量，保证各单管停用情形仍可配平。

业务问题
--------
修复师在**当前完整草稿**上选择 1~4 条"可能失效"的既有管路，并为每条填写：
- 可增设通量的非负整数上限 ``add_max``；
- 每单位增设成本 ``unit_cost``；
另有全网备用总量上限 ``reserve_total``（所有增设量之和不得超过它）。

服务端联合决定各候选管的整数增设量 a_k（0 ≤ a_k ≤ add_max_k，
Σa_k ≤ reserve_total），并对"每条候选管单独停用"的情形分别重算配平：
停用管流量固定为 0，其余管路遵守既有 [min, max]（候选管 max 按增设量
抬高）、节点守恒与分区精确需求。

择优顺序（多个可行计划）
------------------------
1. 增设总成本 Σ c_k·a_k 最低；
2. 总增设量 Σ a_k 最少；
3. 按**管路录入顺序**的增设序列字典序最小。

精确求解
--------
- 单个故障情形的可行性即 balance.py 同一套"优选预流 + 超源/超汇"调整网：
  停用管在情形中完全不存在（不预流、无调整边，流量恒为 0）；其余候选管
  j≠k 在上调方向并联一条容量 = 已装量 a_j 的"增设边"。
- 这是两阶段决策（先装备用、各故障情形各自配平，装设量必须分别够每一种
  情形用，而非跨情形累加），属小型整数网络设计问题。候选管 K ≤ 4，用
  分支定界精确求解：
  * 有效取值域先用"情形调整总量"收紧（装设量超过它无意义），替代用户
    可能填写的超大上限；
  * 节点上把未固定管的增设边放宽到各自上界，对每一情形做最小费用流，
    任一情形不通即剪枝；
  * 各情形最小费用流的整数用量按管取峰值，即一个可行的整数完工解
    （上界，最小费用权重使它天然省量），直接作为现任最优；
  * 各情形单独满足时的最小费用之最大值为合法下界；
  * 按费用权重从大到小对自由管升序枚举装设量，并用
    "w_k·a_k ≥ 现任最优即剪枝"进一步压缩搜索。
- 费用权重三级整数大权（Python 大整数）：
  Q 按管路录入顺序递归（早录入管字典序主导后续一切差异），
  W2 压倒全部字典序差异（1 单位总量优先），
  W1 压倒一切（单位成本差 1 即最终优先）。

无解时枚举候选集合的全部子集（K ≤ 4，至多 15 个分支定界），报告可覆盖
故障数最多、再按成本/总量/字典序最优的方案，并明确列出总量限制内无法
覆盖的故障情形及其收支诊断。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .balance import ValidationError, _require_int, solve, validate_and_build
from .mincost import MinCostFlow

MIN_CANDIDATES, MAX_CANDIDATES = 1, 4


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
def _validate_candidates(payload: Dict[str, Any],
                         model: Dict[str, Any]) -> List[Dict[str, Any]]:
    """校验候选管路列表，附带其在草稿管路中的序号。"""
    raw = payload.get("candidates")
    if not isinstance(raw, list):
        raise ValidationError("candidates 必须是数组")
    if not (MIN_CANDIDATES <= len(raw) <= MAX_CANDIDATES):
        raise ValidationError(
            f"候选（可能失效）管路数量必须在 {MIN_CANDIDATES}~{MAX_CANDIDATES} 条之间")

    pipe_of_id = {p["id"]: i for i, p in enumerate(model["pipes"])}
    candidates: List[Dict[str, Any]] = []
    chosen: set[str] = set()
    for i, item in enumerate(raw):
        label = f"第 {i + 1} 条候选管路"
        if not isinstance(item, dict):
            raise ValidationError(f"{label}必须是对象")
        pid = item.get("pipe_id")
        if not isinstance(pid, str) or not pid.strip():
            raise ValidationError(f"{label}缺少 pipe_id")
        pid = pid.strip()
        if pid not in pipe_of_id:
            raise ValidationError(f"{label}（{pid}）不是草稿中已有的管路")
        if pid in chosen:
            raise ValidationError(f"候选管路重复：{pid}")
        chosen.add(pid)
        add_max = _require_int(item, "add_max", f"{label}（{pid}）可增设上限")
        unit_cost = _require_int(item, "unit_cost",
                                 f"{label}（{pid}）每单位增设成本")
        pipe_idx = pipe_of_id[pid]
        candidates.append({
            "pipe_id": pid,
            "pipe_idx": pipe_idx,
            "order": model["pipes"][pipe_idx]["order"],
            "add_max": add_max,
            "unit_cost": unit_cost,
        })
    return candidates


# --------------------------------------------------------------------------- #
# 单故障情形调整网（与 balance.solve 同构，仅范围/增设边按情形定制）
# --------------------------------------------------------------------------- #
def _build_scenario_mcf(
    model: Dict[str, Any],
    pipe_candidate: Dict[int, int],
    failed_idx: Optional[int],
    caps: Dict[int, int],
    weights: Dict[int, int],
) -> Dict[str, Any]:
    """构造一个单管停用情形的优选预流调整网。

    pipe_candidate: {管路序号: 候选序号}
    failed_idx     : 停用管路序号（None 表示不停用，仅内部测试用）
    caps           : {候选序号: 增设边上限}（停用管自身不放增设边）
    weights        : {候选序号: 增设边单位费用}
    """
    pipes = model["pipes"]
    source_id = model["source_id"]
    business = [source_id] + model["nodes"] + [z["id"] for z in model["zones"]]
    idx = {vid: i for i, vid in enumerate(business)}
    n_biz = len(business)
    ss, tt = n_biz, n_biz + 1
    mcf = MinCostFlow(n_biz + 2)

    balance = {vid: 0 for vid in business}
    reserve_refs: List[Tuple[int, int, int]] = []  # (候选序号, 起点, 边序号)
    for i, p in enumerate(pipes):
        u, v = idx[p["from"]], idx[p["to"]]
        if i == failed_idx:
            # 停用管在情形中不存在：不预流、无任何调整边（流量恒为 0）
            continue
        pref = p["preferred"]
        mcf.add_edge(u, v, p["max"] - pref, 0)       # 增大边
        mcf.add_edge(v, u, pref - p["min"], 0)       # 减小边
        ck = pipe_candidate.get(i)
        if ck is not None and ck in caps:
            ei = len(mcf.g[u])
            # 增设边：与增大边并联的 u→v 额外容量，容量 = 装设量
            mcf.add_edge(u, v, caps[ck], weights.get(ck, 0))
            reserve_refs.append((ck, u, ei))
        balance[p["from"]] -= pref
        balance[p["to"]] += pref

    required_in: Dict[str, int] = {source_id: -model["total"]}
    for n in model["nodes"]:
        required_in[n] = 0
    for z in model["zones"]:
        required_in[z["id"]] = z["demand"]

    ss_edges: List[Tuple[int, int]] = []
    tt_edges: List[Tuple[int, int]] = []
    ss_total = tt_total = 0
    for vid in business:
        delta = balance[vid] - required_in[vid]
        if delta > 0:
            ei = len(mcf.g[ss])
            mcf.add_edge(ss, idx[vid], delta, 0)
            ss_edges.append((ss, ei))
            ss_total += delta
        elif delta < 0:
            ei = len(mcf.g[idx[vid]])
            mcf.add_edge(idx[vid], tt, -delta, 0)
            tt_edges.append((idx[vid], ei))
            tt_total += -delta

    # 可行配平中总推量不超过两侧调整量，故任一增设边用量不会超过它；
    # 据此收紧放宽容量（只减不增，放宽仍然覆盖所有合规完工解）。
    adj_total = max(ss_total, tt_total)
    for _ck, _u, _ei in reserve_refs:
        _arc = mcf.g[_u][_ei]
        if _arc.cap > adj_total:
            _arc.cap = adj_total

    return {
        "mcf": mcf, "ss": ss, "tt": tt,
        "ss_edges": ss_edges, "tt_edges": tt_edges,
        "required_flow": max(ss_total, tt_total),
        "expect_flow": min(ss_total, tt_total),
        "reserve_refs": reserve_refs,
    }


def _run_scenario(net: Dict[str, Any]) -> Optional[Tuple[int, Dict[int, int]]]:
    """推流并检查超源/超汇双侧饱和。

    饱和返回 (最小费用, {候选序号: 增设边用量})；不通返回 None。
    """
    mcf = net["mcf"]
    pushed, cost = mcf.flow(net["ss"], net["tt"], net["required_flow"])
    saturated = all(mcf.g[a][e].cap == 0 for a, e in net["ss_edges"]) and all(
        mcf.g[a][e].cap == 0 for a, e in net["tt_edges"])
    if not saturated or pushed != net["expect_flow"]:
        return None
    used = {ck: mcf.used_flow(u, ei) for ck, u, ei in net["reserve_refs"]}
    return cost, used


# --------------------------------------------------------------------------- #
# 分支定界：求 required 子集合规时的最优增设向量
# --------------------------------------------------------------------------- #
def _adjust_total(model: Dict[str, Any], failed_idx: int) -> int:
    """停用某管情形下，优选预流调整网需要从 SS 推向 TT 的总量。

    停用管在情形中完全不存在（不预流）。任一可行配平中某条增设边
    上的流量不超过该总量（边流量不超过 SS 总推量），故可据此收紧
    各管装设量的取值域，替代用户可能填写的超大上限。
    """
    business = ([model["source_id"]] + model["nodes"]
                + [z["id"] for z in model["zones"]])
    bal = {vid: 0 for vid in business}
    for i, pp in enumerate(model["pipes"]):
        if i == failed_idx:
            continue
        bal[pp["from"]] -= pp["preferred"]
        bal[pp["to"]] += pp["preferred"]
    required_in: Dict[str, int] = {model["source_id"]: -model["total"]}
    for n in model["nodes"]:
        required_in[n] = 0
    for z in model["zones"]:
        required_in[z["id"]] = z["demand"]
    ss = tt = 0
    for vid in business:
        d = bal[vid] - required_in[vid]
        if d > 0:
            ss += d
        elif d < 0:
            tt += -d
    return max(ss, tt)


def _best_for_subset(
    model: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    required: List[int],
    reserve_total: int,
    upper: Dict[int, int],
    weights: Dict[int, int],
) -> Optional[Dict[int, int]]:
    """required 中的故障情形必须全部可配平；装设量可放在任意候选管上。

    不可行返回 None。未要求覆盖其停用情形的候选管仍可被增设（增设是
    全网共用的），只是不为它们生成故障情形。
    """
    k_all = len(candidates)
    all_idx = list(range(k_all))
    pipe_candidate = {candidates[k]["pipe_idx"]: k for k in all_idx}

    # 有效取值域：情形 f 中增设边流量不超过该情形的调整总量 adj[f]，
    # 而装设量 a_k 只需覆盖"用到它的各情形"中的最大用量，故
    # a_k ≤ max_{f≠k} adj[f]，超过它的装设无意义（替代超大上限）。
    adj = {f: _adjust_total(model, candidates[f]["pipe_idx"])
           for f in required}
    eff: Dict[int, int] = {}
    for k in all_idx:
        relevant = [adj[f] for f in required if f != k]
        eff[k] = min(upper[k], max(relevant) if relevant else 0)

    def evaluate(fixed: Dict[int, int], free_hi: Dict[int, int]
                 ) -> Optional[Tuple[int, Dict[int, int]]]:
        """未固定管放宽到 free_hi 后逐情形做最小费用流。

        返回 (合法下界, 各情形用量逐管取峰得到的完工向量)；
        任一情形不通即 None。峰值向量在各情形间彼此独立，
        因而是一个可直接验证的可行装设方案。
        """
        fixed_sum = sum(fixed.values())
        lb = 0
        peak: Dict[int, int] = dict(fixed)
        for f in required:
            caps: Dict[int, int] = {}
            for k in all_idx:
                if k == f:
                    continue
                if k in fixed:
                    caps[k] = fixed[k]
                else:
                    # 预算扣除已固定装设量
                    caps[k] = min(free_hi[k], eff[k],
                                  reserve_total - fixed_sum)
            net = _build_scenario_mcf(
                model, pipe_candidate, candidates[f]["pipe_idx"],
                caps, weights)
            outcome = _run_scenario(net)
            if outcome is None:
                return None
            cost, used = outcome
            if cost > lb:
                lb = cost
            for k, amount in used.items():
                if amount > peak.get(k, 0):
                    peak[k] = amount
        for k in all_idx:
            peak.setdefault(k, 0)
        return lb, peak

    root_hi = {k: eff[k] for k in all_idx}
    root = evaluate({}, root_hi)
    if root is None:
        return None

    best: Optional[Dict[int, int]] = None
    best_weight: Optional[int] = None

    def consider(vec: Dict[int, int]) -> bool:
        nonlocal best, best_weight
        if sum(vec.values()) > reserve_total:
            return False
        if any(vec[k] > upper[k] for k in all_idx):
            return False
        w = sum(weights[k] * vec[k] for k in all_idx)
        if best_weight is None or w < best_weight:
            best, best_weight = dict(vec), w
            return True
        return False

    consider(root[1])

    def branch(fixed: Dict[int, int], free: List[int],
               free_hi: Dict[int, int]) -> None:
        nonlocal best, best_weight
        # evaluate 对自由管放宽到 free_hi；free 为空时即检验精确装设量。
        # 返回的峰值向量逐管不超过放宽上限且各情形均可行，可直接采纳。
        evaluated = evaluate(fixed, free_hi)
        if evaluated is None:
            return
        lb, peak = evaluated
        if best_weight is not None and lb >= best_weight:
            return
        consider(peak)
        if not free:
            return
        if best_weight is not None and lb >= best_weight:
            return

        # 选费用权重最大（对目标影响最大）的自由管优先分支
        k = max(free, key=lambda j: weights[j])
        rest = [j for j in free if j != k]
        fixed_sum = sum(fixed.values())
        hi = min(free_hi[k], eff[k], reserve_total - fixed_sum)
        if best_weight is not None:
            # w_k·a_k ≥ best_weight 即不可能更优（其余管费用非负）
            hi = min(hi, (best_weight - 1) // weights[k])
        for a in range(0, hi + 1):
            nxt_fixed = dict(fixed)
            nxt_fixed[k] = a
            nxt_hi = dict(free_hi)
            nxt_hi[k] = a
            branch(nxt_fixed, rest, nxt_hi)
            if best_weight is not None and lb >= best_weight:
                return

    branch({}, list(all_idx), root_hi)
    return best

# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def _scenario_payload(model: Dict[str, Any], by_pipe: Dict[int, int],
                      failed_idx: int) -> Dict[str, Any]:
    """故障情形草稿：停用管范围固定 0，其余管 max 加装设量。"""
    pipes = []
    for i, p in enumerate(model["pipes"]):
        if i == failed_idx:
            lo = hi = pref = 0
        else:
            lo, pref = p["min"], p["preferred"]
            hi = p["max"] + by_pipe.get(i, 0)
        pipes.append({"id": p["id"], "from": p["from"], "to": p["to"],
                      "min": lo, "max": hi, "preferred": pref})
    return {
        "source": {"id": model["source_id"]},
        "source_total": model["total"],
        "zones": [{"id": z["id"], "demand": z["demand"]} for z in model["zones"]],
        "nodes": [{"id": n} for n in model["nodes"]],
        "pipes": pipes,
    }


def solve_plan(payload: Any) -> Dict[str, Any]:
    """求韧性计划。校验失败抛 ValidationError（调用方转 400）。"""
    model = validate_and_build(payload)
    reserve_total = _require_int(payload, "reserve_total", "全网备用总量上限")
    candidates = _validate_candidates(payload, model)
    k_n = len(candidates)

    # 三级权重：成本 W1 ≫ 总量 W2 ≫ 录入顺序字典序 Q
    upper = {k: min(candidates[k]["add_max"], reserve_total)
             for k in range(k_n)}
    ordered = sorted(range(k_n), key=lambda k: candidates[k]["order"])
    q_of: Dict[int, int] = {}
    weight = 0
    for k in reversed(ordered):
        qk = weight + 1
        q_of[k] = qk
        weight += qk * upper[k]
    q_first = q_of[ordered[0]]
    w2 = 1 + reserve_total * q_first
    w1 = 1 + reserve_total * (w2 + q_first)
    weights = {k: candidates[k]["unit_cost"] * w1 + w2 + q_of[k]
               for k in range(k_n)}

    # 枚举候选集合的全部非空子集（≤ 15 个），各自做分支定界
    records: List[Tuple[Tuple, Dict[int, int], int, int]] = []
    full_vec: Optional[Dict[int, int]] = None
    full_mask = (1 << k_n) - 1
    for mask in range(1, 1 << k_n):
        required = [k for k in range(k_n) if mask & (1 << k)]
        vec = _best_for_subset(model, candidates, required, reserve_total,
                               upper, weights)
        if vec is None:
            continue
        vec = {k: vec.get(k, 0) for k in range(k_n)}
        if mask == full_mask:
            full_vec = vec
        total_cost = sum(candidates[k]["unit_cost"] * vec[k]
                         for k in range(k_n))
        total_added = sum(vec.values())
        lex = tuple(vec[k] for k in ordered)
        records.append(((-mask.bit_count(), total_cost, total_added, lex),
                        vec, total_cost, total_added))

    if records:
        _, best_vec, best_cost, best_added = min(records, key=lambda r: r[0])
    else:  # 连任一单管停用都覆盖不了：以零装设量作为展示基准
        best_vec = {k: 0 for k in range(k_n)}
        best_cost = best_added = 0

    # 用最优装设向量逐故障情形重算配平（停用管固定为 0）
    by_pipe = {candidates[k]["pipe_idx"]: best_vec[k] for k in range(k_n)}
    scenarios: List[Dict[str, Any]] = []
    uncovered: List[str] = []
    for k in range(k_n):
        result = solve(_scenario_payload(
            model, by_pipe, candidates[k]["pipe_idx"]))
        scn: Dict[str, Any] = {
            "disabled_pipe_id": candidates[k]["pipe_id"],
            "order": candidates[k]["order"],
            "feasible": bool(result["feasible"]),
        }
        if result["feasible"]:
            scn["objective"] = result["objective"]
            scn["tie_sequence"] = result["tie_sequence"]
            scn["flows"] = result["flows"]
            scn["balances"] = result["balances"]
        else:
            scn["infeasibility"] = result["infeasibility"]
            uncovered.append(candidates[k]["pipe_id"])
        scenarios.append(scn)

    additions = [{
        "pipe_id": candidates[k]["pipe_id"],
        "order": candidates[k]["order"],
        "from": model["pipes"][candidates[k]["pipe_idx"]]["from"],
        "to": model["pipes"][candidates[k]["pipe_idx"]]["to"],
        "add_max": candidates[k]["add_max"],
        "unit_cost": candidates[k]["unit_cost"],
        "amount": best_vec[k],
        "line_cost": candidates[k]["unit_cost"] * best_vec[k],
    } for k in ordered]

    sequence = [0] * len(model["pipes"])
    for k in range(k_n):
        sequence[candidates[k]["order"]] = best_vec[k]

    base = {
        "reserve_total": reserve_total,
        "total_added": best_added,
        "total_cost": best_cost,
        "additions": additions,
        "addition_sequence": sequence,
        "scenarios": scenarios,
    }

    if full_vec is not None:
        return {"feasible": True,
                "uncovered_scenarios": [], "reasons": [], **base}

    reasons: List[str] = []
    if uncovered:
        reasons.append(
            f"在全网备用总量上限 {reserve_total} 内，以下 "
            f"{len(uncovered)} 种管路单独停用情形无法保证各分区定量供水："
            f"{'、'.join(uncovered)}")
        for scn in scenarios:
            if not scn["feasible"]:
                diag = scn["infeasibility"]
                first = diag["reasons"][0] if diag["reasons"] else \
                    "守恒/容量约束无法满足"
                reasons.append(f"停用 {scn['disabled_pipe_id']}：{first}")
    else:
        reasons.append("总量限制内未能同时覆盖全部故障情形，请检查备用上限设置")
    reasons.append(
        f"上方统一增设方案已在限制内覆盖最多故障情形"
        f"（总增设量 {best_added}、增设成本 {best_cost}）；"
        "可上调全网备用总量上限或有关管路的可增设上限后重试。")
    return {"feasible": False, "uncovered_scenarios": uncovered,
            "reasons": reasons, **base}
