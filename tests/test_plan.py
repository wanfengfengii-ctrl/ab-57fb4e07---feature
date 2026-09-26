"""韧性计划求解器测试（标准库 unittest）。

- 与全量枚举增设组合的暴力解逐例对照（可行/无解、成本、总量、字典序）；
- 覆盖可行、无解（结构性 / 预算不足）、决胜规则与输入校验。
"""

import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.balance import ValidationError, validate_and_build  # noqa: E402
from app.plan import MAX_ADD_VALUE, solve_plan, validate_plan  # noqa: E402


def pipe(pid, u, v, lo, hi, pref=None):
    return {"id": pid, "from": u, "to": v, "min": lo, "max": hi,
            "preferred": pref if pref is not None else lo}


def base_draft():
    # A、B 各 3；N 为分流点；A 有三条进水管（p2/p3/p4）便于互相替代
    return {
        "source": {"id": "S"}, "source_total": 6,
        "zones": [{"id": "A", "demand": 3}, {"id": "B", "demand": 3}],
        "nodes": [{"id": "N"}],
        "pipes": [
            pipe("p1", "S", "N", 0, 6, 3),
            pipe("p2", "N", "A", 0, 3, 3),
            pipe("p3", "N", "A", 0, 1, 1),
            pipe("p4", "S", "A", 0, 1, 1),
            pipe("p5", "N", "B", 0, 3, 3),
            pipe("p6", "S", "B", 0, 3, 3),
        ],
    }


def make_plan(draft, pipe_specs, budget, order=None):
    """pipe_specs: [(pipe_id, add_max, unit_cost)]，按草稿录入序生成。"""
    p = {k: v for k, v in draft.items()}
    if order is None:
        order = [pid for pid, _, _ in pipe_specs]
    p["contingencies"] = [
        {"pipe_id": pid, "add_max": am, "unit_cost": uc}
        for pid, am, uc in pipe_specs
    ]
    p["total_budget"] = budget
    return p


class TestPlanFeasible(unittest.TestCase):
    def test_forced_uniform_additions(self):
        # p2 停用时 A 只能靠 p3(1)+p4(1)=2，缺 1：必须给 p3 或 p4 加装。
        # p3/p4 各自停用时 p2(3) 单独即可满足 A。
        draft = base_draft()
        payload = make_plan(draft,
                            [("p2", 2, 1), ("p3", 2, 1), ("p4", 2, 1)], 10)
        r = solve_plan(payload)
        self.assertTrue(r["feasible"])
        # 录入顺序 p2,p3,p4；p2 加装无用，第 2 位尽量小 → 加 p4：[0,0,1]
        self.assertEqual(r["plan"]["add_sequence"], [0, 0, 1])
        self.assertEqual((r["plan"]["total_added"],
                          r["plan"]["total_cost"]), (1, 1))
        # 每个故障情形都可行且分区精确闭合
        for c in r["contingencies"]:
            self.assertTrue(c["feasible"])
            self.assertEqual(len(c["flows"]), 6)
            disabled = [f for f in c["flows"] if f.get("disabled")]
            self.assertEqual(len(disabled), 1)
            self.assertEqual(disabled[0]["flow"], 0)
            for z in c["balances"]["zones"]:
                self.assertEqual(z["difference"], 0)
            for nrow in c["balances"]["nodes"]:
                self.assertEqual(nrow["difference"], 0)
            self.assertEqual(c["balances"]["source"]["difference"], 0)
        # p2 故障情形：p4 用到加装后的最大量 2
        c2 = next(c for c in r["contingencies"] if c["pipe_id"] == "p2")
        f4 = next(f for f in c2["flows"] if f["pipe_id"] == "p4")
        self.assertEqual(f4["flow"], 2)
        self.assertEqual(f4["effective_max"], 2)

    def test_cost_primary_then_amount_then_lex(self):
        draft = base_draft()

        # 成本主导：p3 单位成本 5，p4 单位 10 → 选加装 p3
        payload = make_plan(draft,
                            [("p2", 2, 1), ("p3", 2, 5), ("p4", 2, 10)], 10)
        r = solve_plan(payload)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["plan"]["add_sequence"], [0, 1, 0])
        self.assertEqual(r["plan"]["total_cost"], 5)

        # 总量决胜：两条路线总成本相同（4），加装 1 单位优于 2 单位。
        # p3 单位 4（加 1），p4 单位 2（需加 2）；p3 方案 [0,1,0] 总量 1。
        draft2 = base_draft()
        # 让 p4 必须加 2：把 A 经由 p4 的需求缺口拉大——p3 停用场景下
        # 走 p2 不需要加装，故只有 p2 故障约束；p4 加装 1 已足够（缺 1）。
        # 改为：p3 容量 0（管不存在容量意义）：用另一张网构造 2 单位缺口。
        draft2["pipes"][2]["max"] = 0
        draft2["pipes"][2]["preferred"] = 0
        draft2["pipes"][3]["max"] = 1
        # p2 故障：A 仅靠 p4=1 缺 2；只能加装 p4 → 至少 2 单位
        payload = make_plan(draft2,
                            [("p2", 2, 1), ("p4", 2, 2)], 10)
        r = solve_plan(payload)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["plan"]["add_sequence"], [0, 2])
        self.assertEqual((r["plan"]["total_added"],
                          r["plan"]["total_cost"]), (2, 4))

    def test_zero_addition_plan(self):
        # 管网本身对每条候选管故障都鲁棒：零增设零成本
        draft = base_draft()
        draft["pipes"][2]["max"] = 3  # p3 提到 3
        draft["pipes"][3]["max"] = 3  # p4 提到 3
        payload = make_plan(draft, [("p2", 3, 9), ("p3", 3, 9)], 5)
        r = solve_plan(payload)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["plan"]["add_sequence"], [0, 0])
        self.assertEqual((r["plan"]["total_cost"],
                          r["plan"]["total_added"]), (0, 0))
        self.assertEqual(r["plan"]["budget_remaining"], 5)

    def test_joint_addition_across_pipes(self):
        # 故障情形需要多条候选管联合增设：p3 停用后 A 由 p1(1+x1)+p2(1+x2)
        # 供水，需求 4 → x1+x2 ≥ 2，上限各 1 → 唯一解 [1,1,0]
        draft = {
            "source": {"id": "S"}, "source_total": 9,
            "zones": [{"id": "A", "demand": 4}, {"id": "B", "demand": 5}],
            "nodes": [],
            "pipes": [
                pipe("p1", "S", "A", 0, 1, 1),
                pipe("p2", "S", "A", 0, 1, 1),
                pipe("p3", "S", "A", 0, 3, 2),
                pipe("p4", "S", "B", 0, 10, 5),
            ],
        }
        payload = make_plan(draft,
                            [("p1", 1, 1), ("p2", 1, 1), ("p3", 5, 1)], 3)
        r = solve_plan(payload)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["plan"]["add_sequence"], [1, 1, 0])
        # 预算 1 时无解（x1+x2≥2 无法满足）
        payload["total_budget"] = 1
        r = solve_plan(payload)
        self.assertFalse(r["feasible"])
        self.assertTrue(any(u["pipe_id"] == "p3" for u in r["uncovered"]))


class TestPlanInfeasible(unittest.TestCase):
    def test_structural_no_alternate_path(self):
        # p1 是唯一进入 N 的管：它停用时 N 下游全部断水，加装谁都没用
        draft = base_draft()
        payload = make_plan(draft,
                            [("p1", 5, 1), ("p2", 5, 1)], 10)
        r = solve_plan(payload)
        self.assertFalse(r["feasible"])
        ids = [u["pipe_id"] for u in r["uncovered"]]
        self.assertIn("p1", ids)

    def test_add_max_too_small_is_structural(self):
        # 缺 1，但 p3/p4 允许增设上限都是 0 → 加满也救不了
        draft = base_draft()
        payload = make_plan(draft,
                            [("p2", 0, 1), ("p3", 0, 1), ("p4", 0, 1)], 10)
        r = solve_plan(payload)
        self.assertFalse(r["feasible"])
        self.assertTrue(any(u["pipe_id"] == "p2" for u in r["uncovered"]))
        for u in r["uncovered"]:
            self.assertIsNotNone(u["infeasibility"])

    def test_budget_blocks_otherwise_coverable(self):
        # p2 故障需要 +1（p3 或 p4），预算为 0 → 无解且非结构性
        draft = base_draft()
        payload = make_plan(draft,
                            [("p2", 2, 1), ("p3", 2, 1), ("p4", 2, 1)], 0)
        r = solve_plan(payload)
        self.assertFalse(r["feasible"])
        ids = [u["pipe_id"] for u in r["uncovered"]]
        self.assertIn("p2", ids)
        self.assertTrue(r["message"])

    def test_budget_shared_across_scenarios(self):
        # 两个互相独立的故障各需 +1，总量上限只给 1 → 统一方案不存在
        draft = base_draft()
        # B 侧也制造缺口：p6 容量降到 2，p5 故障时 B 缺 1 且 p6 非候选
        draft["pipes"][5]["max"] = 2
        draft["pipes"][5]["preferred"] = 2
        # 候选：p2（A 侧故障，需加 p3/p4）、p5（B 侧故障，需加 p6），
        # p3、p6 作为可加装的幸存候选
        payload = make_plan(
            draft,
            [("p2", 2, 1), ("p3", 2, 1), ("p5", 2, 1), ("p6", 2, 1)], 1)
        r = solve_plan(payload)
        self.assertFalse(r["feasible"])
        self.assertTrue(r["uncovered"])
        # 预算放宽到 2 即可同时覆盖
        payload["total_budget"] = 2
        r2 = solve_plan(payload)
        self.assertTrue(r2["feasible"])
        self.assertEqual(r2["plan"]["total_added"], 2)


class TestPlanValidation(unittest.TestCase):
    def _ok(self):
        return make_plan(base_draft(),
                         [("p2", 2, 1), ("p3", 2, 1)], 5)

    def test_counts(self):
        p = self._ok()
        p["contingencies"] = []
        with self.assertRaises(ValidationError):
            validate_plan(p)
        p = self._ok()
        p["contingencies"] = [
            {"pipe_id": f"p{i}", "add_max": 1, "unit_cost": 1}
            for i in range(1, 6)
        ]
        with self.assertRaises(ValidationError):  # 5 条 > 4
            validate_plan(p)

    def test_pipe_refs(self):
        p = self._ok()
        p["contingencies"][0]["pipe_id"] = "GHOST"
        with self.assertRaises(ValidationError):
            validate_plan(p)

        p = self._ok()
        p["contingencies"][1]["pipe_id"] = "p2"
        with self.assertRaises(ValidationError):
            validate_plan(p)

    def test_non_int_and_negative(self):
        for key in ("add_max", "unit_cost", "total_budget"):
            p = self._ok()
            if key == "total_budget":
                p[key] = -1
            else:
                p["contingencies"][0][key] = -1
            with self.assertRaises(ValidationError):
                validate_plan(p)

            p = self._ok()
            if key == "total_budget":
                p[key] = 1.5
            else:
                p["contingencies"][0][key] = 1.5
            with self.assertRaises(ValidationError):
                validate_plan(p)

            p = self._ok()
            if key == "total_budget":
                p[key] = True
            else:
                p["contingencies"][0][key] = True
            with self.assertRaises(ValidationError):
                validate_plan(p)

        p = self._ok()
        p["contingencies"] = "nope"
        with self.assertRaises(ValidationError):
            validate_plan(p)

        p = self._ok()
        p["contingencies"][0] = ["not", "obj"]
        with self.assertRaises(ValidationError):
            validate_plan(p)

    def test_too_large(self):
        p = self._ok()
        p["contingencies"][0]["add_max"] = MAX_ADD_VALUE + 1
        with self.assertRaises(ValidationError):
            validate_plan(p)

    def test_draft_still_validated(self):
        p = self._ok()
        p["source_total"] = "六"
        with self.assertRaises(ValidationError):
            validate_plan(p)
        p = self._ok()
        p["pipes"] = p["pipes"][:3]
        with self.assertRaises(ValidationError):  # 管路数不足 4
            validate_plan(p)


# ---- 暴力枚举对照 -------------------------------------------------------

def _brute_plan(model, candidates, add_maxes, costs, budget):
    """枚举全部增设向量，返回 (feasible, best_vec or None)。"""
    orders = [c["pipe_order"] for c in candidates]
    from app.balance import compute

    best = None
    for vec in itertools.product(*(range(a + 1) for a in add_maxes)):
        if sum(vec) > budget:
            continue
        add = {orders[k]: vec[k] for k in range(len(vec)) if vec[k] > 0}
        ok = all(compute(model, add=add, disabled=frozenset({orders[k]}))[
            "feasible"] for k in range(len(orders)))
        if not ok:
            continue
        cost = sum(costs[k] * vec[k] for k in range(len(vec)))
        key = (cost, sum(vec), tuple(vec))
        if best is None or key < best[0]:
            best = (key, list(vec))
    return (best is not None, best[1] if best else None)


def _random_plan_case(rng):
    """无环分层小网络：每个分区至少两条进水管，保证单管故障有替代路径。"""
    n_nodes = rng.randrange(0, 2)
    n_zones = rng.randrange(2, 4)
    nodes = [f"N{i}" for i in range(n_nodes)]
    zones = [f"Z{i}" for i in range(n_zones)]

    edges = []
    for i, ni in enumerate(nodes):
        edges.append(("S", ni))
        targets = [f"N{j}" for j in range(i + 1, n_nodes)] + zones
        edges.append((ni, rng.choice(targets)))
    # 无节点时无法给每个分区两条进水管（只有水源直连），跳过该情形
    if not nodes:
        return None
    for z in zones:
        edges.append(("S", z))
        edges.append((rng.choice(nodes), z))
    # 随机增补少量边（保持源/节点 → 节点/分区方向）
    pool = [("S", t) for t in nodes + zones]
    for i, ni in enumerate(nodes):
        for t in [f"N{j}" for j in range(i + 1, n_nodes)] + zones:
            pool.append((ni, t))
    rng.shuffle(pool)
    for e in pool[:rng.randrange(0, 3)]:
        edges.append(e)
    # 去重保序
    seen = set()
    edges = [e for e in edges if not (e in seen or seen.add(e))]
    if not (4 <= len(edges) <= 10):
        return None

    w = rng.randrange(4, 8)
    pipes = [pipe(f"e{i}", u, v, 0, rng.randrange(1, 5))
             for i, (u, v) in enumerate(edges)]
    for p in pipes:
        p["preferred"] = rng.randrange(0, p["max"] + 1)

    payload = {
        "source": {"id": "S"}, "source_total": w,
        "zones": [{"id": z, "demand": 0} for z in zones],
        "nodes": [{"id": n} for n in nodes],
        "pipes": pipes,
    }
    # 需求为总量 w 的一个随机非负整数划分（分区精确需求合计须等于总量）
    if n_zones == 1:
        demands = [w]
    else:
        cuts = sorted(rng.sample(range(1, w), n_zones - 1)) \
            if w - 1 >= n_zones - 1 else None
        if cuts is None:
            return None
        demands = []
        prev = 0
        for c in cuts:
            demands.append(c - prev)
            prev = c
        demands.append(w - prev)
    for z, d in zip(zones, demands):
        payload["zones"][[zz["id"] for zz in payload["zones"]].index(z)][
            "demand"] = d

    # 候选 1~3 条
    ids = [p["id"] for p in pipes]
    k = rng.randrange(1, min(3, len(ids)) + 1)
    chosen = rng.sample(ids, k)
    payload["contingencies"] = [
        {"pipe_id": cid,
         "add_max": rng.randrange(0, 4),
         "unit_cost": rng.randrange(0, 4)}
        for cid in chosen
    ]
    payload["total_budget"] = rng.randrange(0, 6)
    return payload


class TestPlanBruteForce(unittest.TestCase):
    def test_random_vs_brute(self):
        rng = random.Random(20260926)
        checked = feasible_n = infeasible_n = 0
        while checked < 250:
            payload = _random_plan_case(rng)
            if payload is None:
                continue
            checked += 1
            model = validate_and_build(payload)
            _, candidates, budget = validate_plan(payload)
            add_maxes = [c["add_max"] for c in candidates]
            costs = [c["unit_cost"] for c in candidates]
            bf_ok, bf_vec = _brute_plan(
                model, candidates, add_maxes, costs, budget)

            r = solve_plan(payload)
            self.assertEqual(r["feasible"], bf_ok, msg=str(payload))
            if bf_ok:
                feasible_n += 1
                self.assertEqual(r["plan"]["add_sequence"], bf_vec,
                                 msg=str(payload))
                self.assertEqual(r["plan"]["total_added"], sum(bf_vec))
                self.assertEqual(
                    r["plan"]["total_cost"],
                    sum(costs[k] * bf_vec[k] for k in range(len(bf_vec))))
                # 故障情形逐管自洽
                for c in r["contingencies"]:
                    self.assertTrue(c["feasible"])
                    for f in c["flows"]:
                        self.assertLessEqual(f["flow"],
                                             f.get("effective_max", f["max"]))
                    self.assertTrue(all(z["difference"] == 0
                                        for z in c["balances"]["zones"]))
            else:
                infeasible_n += 1
                self.assertIsNone(r["plan"])
                self.assertTrue(r["uncovered"])
        self.assertGreater(feasible_n, 30)
        self.assertGreater(infeasible_n, 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
