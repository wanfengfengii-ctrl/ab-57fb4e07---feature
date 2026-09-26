"""韧性计划求解器单元测试（标准库 unittest）。

覆盖：输入校验、可行计划的目标层级（成本→总量→录入顺序字典序）、
无解情形的最大覆盖与未覆盖报告，以及随机小网与暴力枚举逐例对照。
"""

import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.balance import ValidationError, solve, validate_and_build  # noqa: E402
from app.plan import solve_plan  # noqa: E402


def pipe(pid, u, v, lo, hi, pref):
    return {"id": pid, "from": u, "to": v,
            "min": lo, "max": hi, "preferred": pref}


def base_payload(pipes, total=10, demands=(6, 4), nodes=("N",)):
    return {
        "source": {"id": "S"},
        "source_total": total,
        "zones": [{"id": "A", "demand": demands[0]},
                  {"id": "B", "demand": demands[1]}],
        "nodes": [{"id": n} for n in nodes],
        "pipes": pipes,
    }


def cand(pid, add_max, cost):
    return {"pipe_id": pid, "add_max": add_max, "unit_cost": cost}


class TestPlanValidation(unittest.TestCase):
    def draft(self):
        p = base_payload([
            pipe("p1", "S", "N", 0, 10, 5),
            pipe("p2", "N", "A", 0, 10, 3),
            pipe("p3", "N", "B", 0, 10, 7),
            pipe("p4", "S", "A", 0, 5, 0),
        ])
        p["reserve_total"] = 10
        p["candidates"] = [cand("p1", 5, 1), cand("p4", 5, 1)]
        return p

    def test_candidate_count_bounds(self):
        p = self.draft()
        p["candidates"] = []
        with self.assertRaises(ValidationError):
            solve_plan(p)
        # 5 条候选 > 上限 4（草稿补一条 p5 以便凑足 5 条既有管路）
        p = self.draft()
        p["pipes"].append(pipe("p5", "S", "B", 0, 5, 0))
        p["candidates"] = [cand(x, 1, 1) for x in ("p1", "p2", "p3", "p4", "p5")]
        with self.assertRaises(ValidationError):
            solve_plan(p)

    def test_unknown_and_duplicate_pipe(self):
        p = self.draft()
        p["candidates"] = [cand("ghost", 1, 1)]
        with self.assertRaises(ValidationError):
            solve_plan(p)
        p = self.draft()
        p["candidates"] = [cand("p1", 1, 1), cand("p1", 2, 2)]
        with self.assertRaises(ValidationError):
            solve_plan(p)

    def test_non_int_and_negative(self):
        p = self.draft()
        p["reserve_total"] = -1
        with self.assertRaises(ValidationError):
            solve_plan(p)
        p = self.draft()
        p["reserve_total"] = 2.5
        with self.assertRaises(ValidationError):
            solve_plan(p)
        p = self.draft()
        p["candidates"] = [cand("p1", -3, 1)]
        with self.assertRaises(ValidationError):
            solve_plan(p)
        p = self.draft()
        p["candidates"] = [cand("p1", 3, -1)]
        with self.assertRaises(ValidationError):
            solve_plan(p)
        p = self.draft()
        p["candidates"] = [cand("p1", 3, True)]  # bool 不是整数
        with self.assertRaises(ValidationError):
            solve_plan(p)

    def test_missing_fields(self):
        p = self.draft()
        del p["reserve_total"]
        with self.assertRaises(ValidationError):
            solve_plan(p)
        p = self.draft()
        p["candidates"] = [{"pipe_id": "p1", "add_max": 3}]  # 缺 unit_cost
        with self.assertRaises(ValidationError):
            solve_plan(p)
        p = self.draft()
        p["candidates"] = [{"pipe_id": "p1", "unit_cost": 1}]  # 缺 add_max
        with self.assertRaises(ValidationError):
            solve_plan(p)

    def test_underlying_draft_still_validated(self):
        p = self.draft()
        p["source_total"] = 7  # 与需求合计 10 不符：草稿本身非法？不——
        # 总量≠需求合计是业务不可行而非校验错误，草稿校验应通过
        # 这里改为真正的校验错误：管路 min>max
        p = self.draft()
        p["pipes"][0]["min"] = 99
        with self.assertRaises(ValidationError):
            solve_plan(p)


class TestPlanFeasible(unittest.TestCase):
    def test_zero_addition_when_redundant(self):
        # 已有容量足够冗余：任何单管停用都无需增设
        p = base_payload([
            pipe("p1", "S", "N", 0, 10, 5),
            pipe("p2", "N", "A", 0, 10, 3),
            pipe("p3", "N", "B", 0, 10, 7),
            pipe("p4", "S", "A", 0, 10, 0),
            pipe("p5", "S", "B", 0, 10, 0),
        ])
        p["reserve_total"] = 20
        p["candidates"] = [cand(x, 20, 1) for x in ("p1", "p2", "p3", "p4")]
        r = solve_plan(p)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["total_added"], 0)
        self.assertEqual(r["total_cost"], 0)
        self.assertEqual(r["addition_sequence"], [0, 0, 0, 0, 0])
        self.assertEqual(len(r["scenarios"]), 4)
        self.assertTrue(all(s["feasible"] for s in r["scenarios"]))
        # 停用管流量必须为 0
        for s in r["scenarios"]:
            pid = s["disabled_pipe_id"]
            row = next(f for f in s["flows"] if f["pipe_id"] == pid)
            self.assertEqual(row["flow"], 0)
            self.assertEqual(row["max"], 0)

    def test_forced_addition_and_cost_min(self):
        # p3 是 B 的唯一进水，p1 是 N 的唯一进水：
        # p1 停用 → 只能经 p4(S→A)+p5(S→B) 直达，需 p4、p5 扩容
        p = base_payload([
            pipe("p1", "S", "N", 0, 10, 5),
            pipe("p2", "N", "A", 0, 10, 3),
            pipe("p3", "N", "B", 0, 10, 7),
            pipe("p4", "S", "A", 0, 0, 0),
            pipe("p5", "S", "B", 0, 0, 0),
        ])
        p["reserve_total"] = 20
        p["candidates"] = [cand("p1", 20, 5), cand("p4", 20, 1),
                           cand("p5", 20, 1)]
        r = solve_plan(p)
        self.assertTrue(r["feasible"])
        amounts = {a["pipe_id"]: a["amount"] for a in r["additions"]}
        # p1 停用：A 需 6 经 p4、B 需 4 经 p5；p4/p5 停用同理互为备份
        self.assertEqual(amounts["p4"], 6)
        self.assertEqual(amounts["p5"], 4)
        self.assertEqual(amounts["p1"], 0)
        self.assertEqual(r["total_cost"], 6 * 1 + 4 * 1)

    def test_lexicographic_tie_break_by_entry_order(self):
        # 两条并联备用管 b、c 都能顶替 a：成本相同、总量相同，
        # 应选录入顺序更靠前（序号更小）的管少装……即装在序号靠前者之后，
        # 字典序最小 = 靠前的管装得少。
        p = {
            "source": {"id": "S"}, "source_total": 2,
            "zones": [{"id": "X", "demand": 2}, {"id": "Y", "demand": 0}],
            "nodes": [],
            "pipes": [
                pipe("a", "S", "X", 0, 2, 2),   # 主管
                pipe("b", "S", "X", 0, 0, 0),   # 备用 1（录入靠前）
                pipe("c", "S", "X", 0, 0, 0),   # 备用 2
                pipe("d", "S", "Y", 0, 0, 0),   # 凑数
            ],
            "reserve_total": 5,
            "candidates": [cand("a", 5, 1), cand("b", 5, 1), cand("c", 5, 1)],
        }
        r = solve_plan(p)
        self.assertTrue(r["feasible"])
        amounts = {x["pipe_id"]: x["amount"] for x in r["additions"]}
        # a 停用需 b 或 c 补 2；字典序最小 → b=0、c=2？不：
        # 序列按录入顺序 [a,b,c,d]，(0,0,2) < (0,2,0)，故应 c=2、b=0
        self.assertEqual(amounts["b"], 0)
        self.assertEqual(amounts["c"], 2)
        self.assertEqual(amounts["a"], 0)

    def test_cost_dominates_amount(self):
        # 方案甲：便宜管装 2（成本 2×1=2，总量 2）
        # 方案乙：贵管装 1（成本 1×5=5，总量 1）——成本优先，选甲
        p = {
            "source": {"id": "S"}, "source_total": 2,
            "zones": [{"id": "X", "demand": 2}, {"id": "Y", "demand": 0}],
            "nodes": [],
            "pipes": [
                pipe("a", "S", "X", 0, 2, 2),
                pipe("cheap", "S", "X", 0, 0, 0),
                pipe("exp", "S", "X", 0, 0, 0),
                pipe("d", "S", "Y", 0, 0, 0),
            ],
            "reserve_total": 5,
            # a 停用需补 2：cheap 装 2（成本2）或 exp 装 2（成本10）
            # 或 cheap1+exp1（成本 1+5=6）。最低成本是 cheap 装 2。
            "candidates": [cand("a", 5, 1), cand("cheap", 5, 1),
                           cand("exp", 5, 5)],
        }
        r = solve_plan(p)
        self.assertTrue(r["feasible"])
        amounts = {x["pipe_id"]: x["amount"] for x in r["additions"]}
        self.assertEqual(amounts["cheap"], 2)
        self.assertEqual(amounts["exp"], 0)
        self.assertEqual(r["total_cost"], 2)


class TestPlanInfeasible(unittest.TestCase):
    def test_structurally_uncoverable(self):
        # p1 是进入 N 的唯一管路、p3 是进入 B 的唯一管路：
        # 二者停用无法靠增设他管弥补（B 没有别的来路）
        p = base_payload([
            pipe("p1", "S", "N", 0, 10, 5),
            pipe("p2", "N", "A", 0, 10, 3),
            pipe("p3", "N", "B", 0, 10, 7),
            pipe("p4", "S", "A", 0, 5, 0),
        ])
        p["reserve_total"] = 20
        p["candidates"] = [cand("p1", 20, 1), cand("p3", 20, 1),
                           cand("p4", 20, 1)]
        r = solve_plan(p)
        self.assertFalse(r["feasible"])
        self.assertIn("p1", r["uncovered_scenarios"])
        self.assertIn("p3", r["uncovered_scenarios"])
        self.assertNotIn("p4", r["uncovered_scenarios"])
        self.assertTrue(r["reasons"])
        # 报告应明确指出无法覆盖的情形
        joined = "".join(r["reasons"])
        self.assertIn("p1", joined)
        self.assertIn("p3", joined)

    def test_budget_too_small(self):
        # 需要 6 单位增设才能覆盖，但全网备用总量上限只有 3
        p = base_payload([
            pipe("p1", "S", "N", 0, 10, 5),
            pipe("p2", "N", "A", 0, 10, 3),
            pipe("p3", "N", "B", 0, 10, 7),
            pipe("p4", "S", "A", 0, 0, 0),
        ])
        p["reserve_total"] = 3
        p["candidates"] = [cand("p2", 20, 1), cand("p4", 20, 1)]
        r = solve_plan(p)
        # p2 停用需 p4 补 6，超过上限 3 → p2 情形无法覆盖
        self.assertFalse(r["feasible"])
        self.assertIn("p2", r["uncovered_scenarios"])
        # 上限放宽到 6 即可行
        p["reserve_total"] = 6
        r2 = solve_plan(p)
        self.assertTrue(r2["feasible"])
        amounts = {a["pipe_id"]: a["amount"] for a in r2["additions"]}
        self.assertEqual(amounts["p4"], 6)


def _scenario_ok(model, by_pipe, failed_idx):
    pipes = []
    for i, pp in enumerate(model["pipes"]):
        if i == failed_idx:
            pipes.append({"id": pp["id"], "from": pp["from"], "to": pp["to"],
                          "min": 0, "max": 0, "preferred": 0})
        else:
            pipes.append({"id": pp["id"], "from": pp["from"], "to": pp["to"],
                          "min": pp["min"],
                          "max": pp["max"] + by_pipe.get(i, 0),
                          "preferred": pp["preferred"]})
    payload = {
        "source": {"id": model["source_id"]},
        "source_total": model["total"],
        "zones": [{"id": z["id"], "demand": z["demand"]}
                  for z in model["zones"]],
        "nodes": [{"id": n} for n in model["nodes"]],
        "pipes": pipes,
    }
    return solve(payload)["feasible"]


class TestPlanAgainstBruteForce(unittest.TestCase):
    """随机小网上与暴力枚举全部整数装设向量逐例对照。"""

    def test_random(self):
        rng = random.Random(20260926)
        checked = 0
        feasible_checked = 0
        while checked < 60:
            n_nodes = rng.randrange(0, 3)
            nodes = [f"N{i}" for i in range(n_nodes)]
            pool = [("S", "A"), ("S", "B"), ("S", "A"), ("S", "B")]
            for n in nodes:
                pool += [("S", n), (n, "A"), (n, "B"), ("S", n)]
            rng.shuffle(pool)
            chosen = pool[:rng.randrange(4, min(7, len(pool)) + 1)]
            if len(chosen) < 4:
                continue
            total = rng.randrange(3, 9)
            pipes = []
            for i, (u, v) in enumerate(chosen):
                lo = rng.randrange(0, 2)
                hi = lo + rng.randrange(0, 6)
                pipes.append(pipe(f"p{i+1}", u, v, lo, hi,
                                  rng.randrange(lo, hi + 1)))
            d1 = rng.randrange(0, total + 1)
            payload = {
                "source": {"id": "S"}, "source_total": total,
                "zones": [{"id": "A", "demand": d1},
                          {"id": "B", "demand": total - d1}],
                "nodes": [{"id": n} for n in nodes],
                "pipes": pipes,
            }
            try:
                model = validate_and_build(payload)
            except ValidationError:
                continue
            if not solve(payload)["feasible"]:
                continue
            k_n = rng.randrange(1, min(4, len(pipes)) + 1)
            cand_idx = rng.sample(range(len(pipes)), k_n)
            reserve = rng.randrange(0, 6)
            cands = [cand(pipes[ci]["id"], rng.randrange(0, 6),
                          rng.randrange(0, 5)) for ci in cand_idx]
            payload["reserve_total"] = reserve
            payload["candidates"] = cands

            r = solve_plan(payload)
            # 暴力枚举
            feas = []
            cov_of = {}
            for vals in itertools.product(
                    *[range(c["add_max"] + 1) for c in cands]):
                if sum(vals) > reserve:
                    continue
                by_pipe = {cand_idx[i]: vals[i] for i in range(k_n)}
                flags = tuple(
                    _scenario_ok(model, by_pipe, cand_idx[i])
                    for i in range(k_n))
                cov_of[vals] = flags
                if all(flags):
                    feas.append((sum(cands[i]["unit_cost"] * vals[i]
                                     for i in range(k_n)),
                                 sum(vals), vals))
            order_idx = sorted(range(k_n), key=lambda i: cand_idx[i])
            got = {a["pipe_id"]: a["amount"] for a in r["additions"]}
            gv = tuple(got[cands[i]["pipe_id"]] for i in range(k_n))
            checked += 1
            if feas:
                feasible_checked += 1
                best = min(feas, key=lambda x: (x[0], x[1], x[2]))
                self.assertTrue(r["feasible"], msg=str(payload))
                self.assertEqual(
                    (r["total_cost"], r["total_added"]),
                    (best[0], best[1]), msg=str(payload))
                self.assertEqual(
                    tuple(gv[i] for i in order_idx),
                    tuple(best[2][i] for i in order_idx), msg=str(payload))
                self.assertTrue(all(s["feasible"] for s in r["scenarios"]))
            else:
                self.assertFalse(r["feasible"], msg=str(payload))
                max_cov = max(sum(f) for f in cov_of.values())
                self.assertEqual(
                    sum(1 for s in r["scenarios"] if s["feasible"]),
                    max_cov, msg=str(payload))
        self.assertGreater(feasible_checked, 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
