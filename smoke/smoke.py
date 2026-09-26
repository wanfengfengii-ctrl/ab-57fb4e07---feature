"""API 业务冒烟脚本（仅标准库）：在真实 HTTP 服务上端到端验证。

检查项：
1. GET /healthz 返回 200 且 status=ok；
2. 可行草稿返回 feasible=true，守恒/范围/目标值全部成立，流量为整数；
3. 不可行草稿返回 feasible=false 且给出收支诊断；
4. 非法草稿返回 HTTP 400；
5. 韧性计划：可行（含统一增设方案与各故障重配平）、无解（明确故障情形）、
   输入错误 400。

由容器内 exec 执行，默认访问本机 8000；可用 BASE_URL 覆盖。
任何断言失败即以非零退出码报告。
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def request(method, path, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def main():
    print(f"[smoke] 目标服务 {BASE}")

    status, body = request("GET", "/healthz")
    check(status == 200 and body.get("status") == "ok",
          f"健康检查 200/status=ok（实际 {status}, {body}）")

    feasible_payload = {
        "source": {"id": "S"},
        "source_total": 10,
        "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
        "nodes": [{"id": "N"}],
        "pipes": [
            {"id": "p1", "from": "S", "to": "N",
             "min": 0, "max": 10, "preferred": 5},
            {"id": "p2", "from": "N", "to": "A",
             "min": 0, "max": 10, "preferred": 3},
            {"id": "p3", "from": "N", "to": "B",
             "min": 0, "max": 10, "preferred": 7},
            {"id": "p4", "from": "S", "to": "A",
             "min": 0, "max": 0, "preferred": 0},
        ],
    }
    status, r = request("POST", "/api/balance", feasible_payload)
    check(status == 200, f"可行草稿 HTTP 200（实际 {status}）")
    check(r["feasible"] is True, "结论为可行")
    check(r["tie_sequence"] == [10, 6, 4, 0],
          f"流量序列 [10,6,4,0]（实际 {r['tie_sequence']}）")
    check(all(isinstance(x, int) for x in r["tie_sequence"]),
          "所有流量均为整数")
    check(r["objective"] == 11, f"绝对偏差和为 11（实际 {r['objective']}）")
    src = r["balances"]["source"]
    check(src["outflow"] == 10 and src["difference"] == 0,
          "水源流出恰等于总量 10")
    node = r["balances"]["nodes"][0]
    check(node["inflow"] == node["outflow"] == 10,
          "分流节点流入=流出=10")
    for zrow, need in zip(r["balances"]["zones"], (6, 4)):
        check(zrow["inflow"] == need and zrow["difference"] == 0,
              f"分区 {zrow['id']} 流入恰等于需求 {need}")
    for f in r["flows"]:
        check(f["min"] <= f["flow"] <= f["max"],
              f"管路 {f['pipe_id']} 流量 {f['flow']} 在范围内")

    infeasible_payload = json.loads(json.dumps(feasible_payload))
    # A 需求改成 60：总量与需求不等且容量不足，必不可行
    infeasible_payload["zones"][0]["demand"] = 60
    status, r = request("POST", "/api/balance", infeasible_payload)
    check(status == 200 and r["feasible"] is False,
          "超量需求判为不可行（HTTP 200 业务结论）")
    info = r["infeasibility"]
    check(info["total_demand"] == 64 and info["shortfall_flow"] > 0,
          "诊断含需求合计与流量缺口")
    check(bool(info["reasons"]), "给出可读的不可行原因")

    bad_payload = {"source": {"id": "S"}, "source_total": 1,
                   "zones": [{"id": "A", "demand": 1}],
                   "nodes": [], "pipes": []}
    status, r = request("POST", "/api/balance", bad_payload)
    check(status == 400 and "error" in r,
          f"非法草稿返回 400（实际 {status}）")

    # ---- 韧性计划 ----
    # 网络：S->N 10；N->A(p2)、N->B(p3)；另加 S->A 直连管 p4(0..0)。
    # p2 停用时 A 只能靠 p4，需给 p4 加装至少 6 才能继续满足 A=6。
    plan_payload = {
        "source": {"id": "S"},
        "source_total": 10,
        "zones": [{"id": "A", "demand": 6}, {"id": "B", "demand": 4}],
        "nodes": [{"id": "N"}],
        "pipes": [
            {"id": "p1", "from": "S", "to": "N",
             "min": 0, "max": 10, "preferred": 5},
            {"id": "p2", "from": "N", "to": "A",
             "min": 0, "max": 10, "preferred": 3},
            {"id": "p3", "from": "N", "to": "B",
             "min": 0, "max": 10, "preferred": 7},
            {"id": "p4", "from": "S", "to": "A",
             "min": 0, "max": 0, "preferred": 0},
        ],
        "total_budget": 10,
        "contingencies": [
            {"pipe_id": "p2", "add_max": 8, "unit_cost": 3},
            {"pipe_id": "p4", "add_max": 8, "unit_cost": 1},
        ],
    }
    status, r = request("POST", "/api/plan", plan_payload)
    check(status == 200, f"韧性计划 HTTP 200（实际 {status}）")
    check(r["feasible"] is True, "韧性计划结论为可行")
    check(r["plan"]["add_sequence"] == [0, 6],
          f"统一增设序列 [0,6]（实际 {r['plan']['add_sequence']}）")
    check(r["plan"]["total_cost"] == 6,
          f"增设成本 6（实际 {r['plan']['total_cost']}）")
    check(len(r["contingencies"]) == 2, "给出 2 个故障情形的重算结果")
    for c in r["contingencies"]:
        check(c["feasible"] is True, f"故障 {c['pipe_id']} 情形可行")
        disabled = [f for f in c["flows"] if f.get("disabled")]
        check(len(disabled) == 1 and disabled[0]["flow"] == 0,
              f"故障 {c['pipe_id']} 停用管流量固定为 0")
        for zrow in c["balances"]["zones"]:
            check(zrow["difference"] == 0,
                  f"故障 {c['pipe_id']} 分区 {zrow['id']} 精确满足需求")
        check(all(n["difference"] == 0
                  for n in c["balances"]["nodes"]),
              f"故障 {c['pipe_id']} 节点守恒")

    # 无解：总量上限只给 3，p2 故障时 p4 至少需加 6
    infeasible_plan = json.loads(json.dumps(plan_payload))
    infeasible_plan["total_budget"] = 3
    status, r = request("POST", "/api/plan", infeasible_plan)
    check(status == 200 and r["feasible"] is False,
          "预算不足判为计划无解（HTTP 200 业务结论）")
    check(bool(r["uncovered"]) and r["plan"] is None,
          "明确列出无法覆盖的故障情形")
    check(any(u["pipe_id"] == "p2" for u in r["uncovered"]),
          "无解故障为 p2 停用情形")

    # 输入错误：候选管不存在 / 数量超界 / 非整数
    bad_plan = json.loads(json.dumps(plan_payload))
    bad_plan["contingencies"][0]["pipe_id"] = "GHOST"
    status, r = request("POST", "/api/plan", bad_plan)
    check(status == 400 and "error" in r,
          f"候选管不存在返回 400（实际 {status}）")

    bad_plan = json.loads(json.dumps(plan_payload))
    bad_plan["contingencies"] = []
    status, r = request("POST", "/api/plan", bad_plan)
    check(status == 400, "候选为空返回 400")

    bad_plan = json.loads(json.dumps(plan_payload))
    bad_plan["total_budget"] = -2
    status, r = request("POST", "/api/plan", bad_plan)
    check(status == 400, "负总量上限返回 400")

    print("[smoke] 全部冒烟断言通过")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke] 失败：{exc}", file=sys.stderr)
        sys.exit(1)
