# -*- coding: utf-8 -*-
"""连排实测回放：基准计划冻结、打点事件、实测检查与多次连排汇总。

原则：实测记录只追加（run_events），基准方案（scenes/tasks/items）不被改写；
开启连排时把基准计划完整冻结进 runs.plan，之后的分析都以该快照为准。

检查项：时刻倒序、动作漏项、演员/服装师/换装位并发冲突、服装副本错用；
并定位首个偏差（实测与计划偏差超阈值或异常打点）及其后续等待链。
"""
import json
import math

import db
import scheduler

DEVIATION_SEC = 5     # 实测与计划偏差超过该值记为偏差
CHAIN_TOL = 3         # 等待链衔接容差（秒）
MAX_CHAIN = 25


def fmt(sec):
    return scheduler.fmt(sec)


# ---------------- 基准计划冻结 ----------------

def freeze_plan(state, snap):
    """以修订快照（scenes/tasks/items）为基准、当前辅助数据（造型/人员/位置）为配套，
    计算排程并冻结为连排基准计划。"""
    base = dict(state)
    base["scenes"] = snap["scenes"]
    base["tasks"] = snap["tasks"]
    base["items"] = snap["items"]
    sched = scheduler.compute_schedule(base)

    by_task = {}
    for a in sched["actions"]:
        by_task.setdefault(a["task_id"], []).append(a)
    actions = []
    for tid, acts in by_task.items():
        acts.sort(key=lambda a: (a["start"], a["end"]))
        for idx, a in enumerate(acts):
            actions.append({
                "task_id": tid, "idx": idx, "kind": a["kind"], "label": a["label"],
                "dur": a["dur"], "item_id": a.get("item_id"),
                "start": int(a["start"]), "end": int(a["end"]),
            })
    slim_items = [{"id": i["id"], "name": i["name"], "kind": i["kind"],
                   "copies": i["copies"], "don_sec": i["don_sec"],
                   "doff_sec": i["doff_sec"]} for i in snap["items"]]
    return {
        "scenes": snap["scenes"],
        "actors": [{"id": a["id"], "name": a["name"]} for a in state["actors"]],
        "dressers": [{"id": d["id"], "name": d["name"]} for d in state["dressers"]],
        "positions": [{"id": p["id"], "name": p["name"], "capacity": p["capacity"]}
                      for p in state["positions"]],
        "items": slim_items,
        "tasks": [{k: t[k] for k in ("id", "actor_id", "from_scene_id", "to_scene_id",
                                     "exit_side", "position_id", "dresser_id",
                                     "start_sec", "locked")} for t in snap["tasks"]],
        "actions": actions,
        "windows": {str(tid): w for tid, w in sched["windows"].items()},
    }


def load_plan(run):
    plan = json.loads(run["plan"])
    plan["windows"] = {int(k): v for k, v in plan["windows"].items()}
    return plan


# ---------------- 打点事件 ----------------

def effective_events(events):
    """每个 (任务,动作,类型) 的最新有效事件；被补正替代的旧事件保留但不生效。"""
    superseded = {e["supersedes"] for e in events if e["supersedes"]}
    eff = {}
    for e in events:  # 按 id 升序，后者覆盖前者（正常路径下旧事件已被 supersedes 标记）
        if e["id"] in superseded:
            continue
        eff[(e["task_id"], e["action_idx"], e["kind"])] = e
    return eff


def action_actuals(plan, eff):
    """(task_id, idx) -> {start,end,skipped,exceptions:[...]}"""
    out = {}
    for a in plan["actions"]:
        key = (a["task_id"], a["idx"])
        ev_s = eff.get((a["task_id"], a["idx"], "start"))
        ev_d = eff.get((a["task_id"], a["idx"], "done"))
        ev_k = eff.get((a["task_id"], a["idx"], "skip"))
        out[key] = {
            "start": ev_s["at_sec"] if ev_s else None,
            "end": ev_d["at_sec"] if ev_d else None,
            "skipped": ev_k is not None,
            "exceptions": [e for e in plan["_events"]
                           if e["task_id"] == a["task_id"] and e["action_idx"] == a["idx"]
                           and e["kind"] == "exception" and e["id"] in plan["_eff_ids"]],
        }
    return out


def task_intervals(plan, actuals):
    """任务实测占用区间：[首个开始, 最后完成]；无打点则无区间。"""
    iv = {}
    for t in plan["tasks"]:
        tid = t["id"]
        starts, ends = [], []
        for a in plan["actions"]:
            if a["task_id"] != tid:
                continue
            ac = actuals.get((tid, a["idx"]), {})
            if ac.get("start") is not None:
                starts.append(ac["start"])
                ends.append(ac["end"] if ac.get("end") is not None else ac["start"])
        if starts:
            iv[tid] = (min(starts), max(ends))
    return iv


# ---------------- 实测检查 ----------------

def analyze(run, events):
    """返回 {actuals, task_intervals, anomalies, first_deviation, chain}。"""
    plan = load_plan(run)
    plan["_events"] = events
    eff = effective_events(events)
    plan["_eff_ids"] = {e["id"] for e in eff.values()}
    actuals = action_actuals(plan, eff)
    closed = run["status"] == "done"
    anomalies = []

    tasks = {t["id"]: t for t in plan["tasks"]}
    acts_by_task = {}
    for a in plan["actions"]:
        acts_by_task.setdefault(a["task_id"], []).append(a)
    for v in acts_by_task.values():
        v.sort(key=lambda a: a["idx"])

    # 1) 逐动作：时刻倒序、动作漏项、跳过与打点并存
    for tid, acts in acts_by_task.items():
        prev_end, prev_label = None, None
        for a in acts:
            ac = actuals[(tid, a["idx"])]
            st, dn, sk = ac["start"], ac["end"], ac["skipped"]
            if sk and (st is not None or dn is not None):
                anomalies.append({"type": "missing", "task_id": tid, "time": st or dn,
                                  "message": f"动作{a['idx']}「{a['label']}」既被跳过又有打点记录"})
            if dn is not None and st is None and not sk:
                anomalies.append({"type": "missing", "task_id": tid, "time": dn,
                                  "message": f"动作{a['idx']}「{a['label']}」有完成打点但缺开始"})
            if st is not None and dn is None and not sk:
                anomalies.append({"type": "missing", "task_id": tid, "time": st,
                                  "message": f"动作{a['idx']}「{a['label']}」已开始，未报完成/跳过"})
            if st is not None and dn is not None and dn < st:
                anomalies.append({"type": "order", "task_id": tid, "time": dn,
                                  "message": f"时刻倒序：动作{a['idx']}「{a['label']}」"
                                             f"完成 {fmt(dn)} 早于开始 {fmt(st)}"})
            if st is not None and prev_end is not None and st < prev_end:
                anomalies.append({"type": "order", "task_id": tid, "time": st,
                                  "message": f"时刻倒序：动作{a['idx']}「{a['label']}」开始 {fmt(st)}"
                                             f" 早于上一动作「{prev_label}」完成 {fmt(prev_end)}"})
            if closed and st is None and dn is None and not sk:
                anomalies.append({"type": "missing", "task_id": tid,
                                  "time": a["start"],
                                  "message": f"动作漏打点：动作{a['idx']}「{a['label']}」无任何记录"})
            if st is not None:
                prev_end = dn if dn is not None else st
                prev_label = a["label"]

    # 2) 资源并发：演员/服装师独占、换装位容量
    intervals = task_intervals(plan, actuals)
    actors = {a["id"]: a for a in plan["actors"]}
    dressers = {d["id"]: d for d in plan["dressers"]}
    positions = {p["id"]: p for p in plan["positions"]}

    def overlap(iv1, iv2):
        return iv1[0] < iv2[1] and iv2[0] < iv1[1]

    tids = sorted(intervals)
    for i, t1 in enumerate(tids):
        for t2 in tids[i + 1:]:
            if not overlap(intervals[t1], intervals[t2]):
                continue
            a, b = tasks[t1], tasks[t2]
            if a["actor_id"] == b["actor_id"]:
                anomalies.append({"type": "concurrency", "task_id": t2,
                                  "time": max(intervals[t1][0], intervals[t2][0]),
                                  "message": f"演员并发冲突：{actors[a['actor_id']]['name']} "
                                             f"在任务#{t1}与#{t2}的实测区间重叠"})
            if a["dresser_id"] and a["dresser_id"] == b["dresser_id"]:
                anomalies.append({"type": "concurrency", "task_id": t2,
                                  "time": max(intervals[t1][0], intervals[t2][0]),
                                  "message": f"服装师并发冲突：{dressers[a['dresser_id']]['name']} "
                                             f"同时照看任务#{t1}与#{t2}"})
    for pid, pos in positions.items():
        pts = []
        for tid, (s, e) in intervals.items():
            if tasks[tid]["position_id"] == pid:
                pts.append((s, 1, tid))
                pts.append((e, -1, tid))
        pts.sort(key=lambda x: (x[0], x[1]))
        cur, cur_ids = 0, []
        for t, d, tid in pts:
            cur += d
            cur_ids = [x for x in cur_ids if x != tid] + ([tid] if d > 0 else [])
            if cur > pos["capacity"]:
                anomalies.append({"type": "concurrency", "task_id": tid, "time": t,
                                  "message": f"换装位容量超限：{pos['name']} 实测并发 {cur}"
                                             f" 超过容量 {pos['capacity']}"})

    # 3) 服装身份与副本：以打点记录的现场 item_id/copy_id 为准（NULL=按计划）
    items = {i["id"]: i for i in plan["items"]}
    scenes = {s["id"]: s for s in plan["scenes"]}

    def item_name(iid):
        return items[iid]["name"] if iid in items else f"#{iid}"

    # 3a) 身份与副本编号校验：现场使用的服装与基准不符、副本编号超出件数
    #     （开始/完成两个有效打点分别校验，同一动作同类问题只报一次）
    for a in plan["actions"]:
        if a["kind"] not in ("don", "doff") or a.get("item_id") is None:
            continue
        tid = a["task_id"]
        seen = set()
        for ev in (eff.get((tid, a["idx"], "start")), eff.get((tid, a["idx"], "done"))):
            if not ev:
                continue
            actual_iid = ev["item_id"] if ev["item_id"] is not None else a["item_id"]
            if actual_iid != a["item_id"] and ("item", actual_iid) not in seen:
                seen.add(("item", actual_iid))
                anomalies.append({"type": "item", "task_id": tid, "time": ev["at_sec"],
                                  "message": f"错用服装：动作{a['idx']}「{a['label']}」现场使用"
                                             f"「{item_name(actual_iid)}」，基准要求"
                                             f"「{item_name(a['item_id'])}」"})
            cp = ev["copy_id"]
            if cp is None or ("copy", cp) in seen:
                continue
            it = items.get(actual_iid)
            cap = it["copies"] if it else None
            if cp < 1 or (cap is not None and cp > cap):
                seen.add(("copy", cp))
                anomalies.append({"type": "item", "task_id": tid, "time": ev["at_sec"],
                                  "message": f"副本编号无效：「{item_name(actual_iid)}」"
                                             f"第 {cp} 件（基准共 {cap} 件）"})

    # 3b) 实测穿着区间按现场实际服装构建；脱下被跳过记错用
    dons, doffs = {}, {}
    for a in plan["actions"]:
        if a["kind"] not in ("don", "doff") or a.get("item_id") is None:
            continue
        tid = a["task_id"]
        ev_s = eff.get((tid, a["idx"], "start"))
        ev_d = eff.get((tid, a["idx"], "done"))
        ev_k = eff.get((tid, a["idx"], "skip"))
        order = plan["windows"].get(tid, {}).get("start", 0)
        actor = tasks[tid]["actor_id"]
        if a["kind"] == "don" and ev_s:
            iid = ev_s["item_id"] if ev_s["item_id"] is not None else a["item_id"]
            dons.setdefault((actor, iid), []).append(
                (order, ev_s["at_sec"], tid, tasks[tid]["to_scene_id"], ev_s["copy_id"]))
        if a["kind"] == "doff":
            if ev_d:
                iid = ev_d["item_id"] if ev_d["item_id"] is not None else a["item_id"]
                doffs.setdefault((actor, iid), []).append((order, ev_d["at_sec"]))
            elif ev_k:
                anomalies.append({"type": "item", "task_id": tid, "time": a["start"],
                                  "message": f"错用服装：「{item_name(a['item_id'])}」"
                                             f"脱下被跳过，副本未回收"})
    wear = {}   # iid -> [(start, end, task_id, copy_id|None)]
    for (actor, iid), dl in dons.items():
        dl.sort()
        dl_doff = sorted(doffs.get((actor, iid), []))
        for order, st, tid, to_sid, cp in dl:
            rel = None
            while dl_doff and dl_doff[0][0] <= order:
                dl_doff.pop(0)
            if dl_doff:
                rel = dl_doff.pop(0)[1]
            if rel is None:
                sc = scenes.get(to_sid)
                rel = (sc["start_sec"] + sc["duration_sec"]) if sc else st
            wear.setdefault(iid, []).append((st, max(rel, st), tid, cp))

    # 3c) 同一件副本被重叠使用（打点声明了副本编号的精确冲突）
    for iid, ivs in wear.items():
        by_copy = {}
        for s, e, tid, cp in ivs:
            if cp is not None:
                by_copy.setdefault(cp, []).append((s, e, tid))
        for cp, lst in by_copy.items():
            lst.sort()
            for x, y in zip(lst, lst[1:]):
                if y[0] < x[1]:
                    anomalies.append({"type": "item", "task_id": y[2], "time": y[0],
                                      "message": f"副本冲突：「{item_name(iid)}」第 {cp} 件"
                                                 f"在任务#{x[2]}与#{y[2]}的实测区间重叠"})

    # 3d) 总数超件数（存在未声明副本的区间时的兜底检查）
    for iid, ivs in wear.items():
        if all(cp is not None for _, _, _, cp in ivs):
            continue  # 全部声明了副本：由 3c 精确检查覆盖
        pts = []
        for s, e, tid, _ in ivs:
            pts.append((s, 1, tid))
            pts.append((e, -1, tid))
        pts.sort(key=lambda x: (x[0], x[1]))
        cur = 0
        cap = items[iid]["copies"] if iid in items else 1
        for t, d, tid in pts:
            cur += d
            if cur > cap:
                anomalies.append({"type": "item", "task_id": tid, "time": t,
                                  "message": f"错用服装：「{item_name(iid)}」实测并发穿着 "
                                             f"{cur} 件，超过副本数 {cap}"})

    anomalies.sort(key=lambda c: (c["time"], c["task_id"]))

    # 4) 首个偏差与后续等待链
    first, chain = _deviation_chain(plan, actuals, intervals, tasks, items,
                                    dressers, positions, actors, events)
    return {
        "actuals": {f"{k[0]}:{k[1]}": v for k, v in actuals.items()},
        "task_intervals": {str(k): list(v) for k, v in intervals.items()},
        "anomalies": anomalies,
        "first_deviation": first,
        "chain": chain,
    }


def _deviation_chain(plan, actuals, intervals, tasks, items, dressers, positions,
                     actors, events):
    """首个偏差 = 计划时刻最早且 |实测-计划| 超阈值的动作，或最早的异常打点；
    等待链 = 从首个偏差出发，沿同任务顺延、共享资源（演员/服装师/换装位）、
    服装副本回收三类边传播到的后续延误动作。"""
    delays = {}   # (tid, idx) -> 实测开始 - 计划开始（仅统计有实测开始的动作）
    for a in plan["actions"]:
        ac = actuals[(a["task_id"], a["idx"])]
        if ac["start"] is not None:
            delays[(a["task_id"], a["idx"])] = ac["start"] - a["start"]

    cands = [(a["start"], a["task_id"], a["idx"], delays[(a["task_id"], a["idx"])])
             for a in plan["actions"]
             if (a["task_id"], a["idx"]) in delays
             and abs(delays[(a["task_id"], a["idx"])]) > DEVIATION_SEC]
    exc = [(e["at_sec"], e["task_id"], e["action_idx"], None)
           for e in events if e["kind"] == "exception"]
    first = None
    if cands or exc:
        allc = sorted(cands + exc, key=lambda x: (x[0], x[1], x[2]))
        t0, tid0, idx0, d0 = allc[0]
        act0 = next((a for a in plan["actions"]
                     if a["task_id"] == tid0 and a["idx"] == idx0), None)
        if d0 is None:
            ev0 = next(e for e in events if e["kind"] == "exception"
                       and e["task_id"] == tid0 and e["action_idx"] == idx0
                       and e["at_sec"] == t0)
            first = {"task_id": tid0, "action_idx": idx0, "time": t0,
                     "kind": "exception",
                     "label": act0["label"] if act0 else f"动作{idx0}",
                     "message": f"异常打点：{ev0['reason']}"}
        else:
            first = {"task_id": tid0, "action_idx": idx0, "time": t0,
                     "kind": "late" if d0 > 0 else "early",
                     "label": act0["label"] if act0 else f"动作{idx0}",
                     "planned": act0["start"] if act0 else t0,
                     "actual": act0["start"] + d0 if act0 else t0,
                     "delay": d0,
                     "message": f"{'延误' if d0 > 0 else '提前'} {abs(d0)}s"}
    if first is None:
        return None, []

    # 等待链 BFS
    start_node = (first["task_id"], first["action_idx"])
    acts_by_task = {}
    for a in plan["actions"]:
        acts_by_task.setdefault(a["task_id"], []).append(a)
    for v in acts_by_task.values():
        v.sort(key=lambda a: a["idx"])
    act_map = {(a["task_id"], a["idx"]): a for a in plan["actions"]}

    # 资源 -> 任务
    res_of_task = {}
    for t in plan["tasks"]:
        rs = [("actor", t["actor_id"],
               f"演员{actors[t['actor_id']]['name']}")]
        if t["dresser_id"]:
            rs.append(("dresser", t["dresser_id"],
                       f"服装师{dressers[t['dresser_id']]['name']}"))
        if t["position_id"]:
            rs.append(("position", t["position_id"],
                       f"换装位{positions[t['position_id']]['name']}"))
        res_of_task[t["id"]] = rs

    def actual_start(node):
        return actuals.get(node, {}).get("start")

    visited = {start_node}
    queue = [start_node]
    chain = []
    while queue and len(chain) < MAX_CHAIN:
        node = queue.pop(0)
        tid, idx = node
        # 边 1：同任务下一延误动作顺延（中间被跳过/未打点的动作不妨碍传播）
        for a_later in acts_by_task.get(tid, []):
            if a_later["idx"] <= idx:
                continue
            nxt = (tid, a_later["idx"])
            if nxt in visited:
                continue
            if delays.get(nxt, 0) > DEVIATION_SEC:
                a = act_map[nxt]
                visited.add(nxt)
                queue.append(nxt)
                chain.append({"time": actual_start(nxt), "task_id": tid,
                              "message": f"任务#{tid} 动作{a['idx']}「{a['label']}」同任务顺延："
                                         f"实测 {fmt(actual_start(nxt))}，计划 {fmt(a['start'])}"
                                         f"（+{delays[nxt]}s）"})
                break
            if actuals.get(nxt, {}).get("start") is not None or \
                    actuals.get(nxt, {}).get("skipped"):
                continue   # 该动作按时或被跳过：顺延到此为止
            break           # 无记录：无法判断，停止
        # 边 2：共享资源的其它任务被本任务实测占用拖延
        if tid in intervals:
            my_end = intervals[tid][1]
            for kind, rid, rname in res_of_task.get(tid, []):
                for t2 in plan["tasks"]:
                    t2id = t2["id"]
                    if t2id == tid or t2id not in intervals:
                        continue
                    if not any(k == kind and r == rid for k, r, _ in res_of_task.get(t2id, [])):
                        continue
                    first_act = acts_by_task[t2id][0]
                    node2 = (t2id, first_act["idx"])
                    if node2 in visited or delays.get(node2, 0) <= DEVIATION_SEC:
                        continue
                    st2 = actual_start(node2)
                    if my_end > first_act["start"] and st2 >= my_end - CHAIN_TOL:
                        visited.add(node2)
                        queue.append(node2)
                        chain.append({"time": st2, "task_id": t2id,
                                      "message": f"任务#{t2id} 等待{rname}：任务#{tid} 实测 "
                                                 f"{fmt(my_end)} 才释放 → 实测开始 {fmt(st2)}"
                                                 f"，计划 {fmt(first_act['start'])}"
                                                 f"（+{delays[node2]}s）"})
        # 边 3：服装副本回收拖延（本任务的脱下 → 其它任务的穿上）
        a_here = act_map.get(node)
        if a_here and a_here["kind"] == "doff" and a_here.get("item_id") is not None:
            ac_here = actuals.get(node, {})
            doff_end = ac_here.get("end")
            if doff_end is not None:
                for a2 in plan["actions"]:
                    if a2["kind"] != "don" or a2.get("item_id") != a_here["item_id"]:
                        continue
                    node2 = (a2["task_id"], a2["idx"])
                    if node2 in visited or delays.get(node2, 0) <= DEVIATION_SEC:
                        continue
                    st2 = actual_start(node2)
                    if st2 is None:
                        continue
                    if doff_end > a2["start"] and st2 >= doff_end - CHAIN_TOL:
                        iname = items[a_here["item_id"]]["name"] \
                            if a_here["item_id"] in items else "服装"
                        visited.add(node2)
                        queue.append(node2)
                        chain.append({"time": st2, "task_id": a2["task_id"],
                                      "message": f"任务#{a2['task_id']} 等待服装「{iname}」副本："
                                                 f"任务#{tid} {fmt(doff_end)} 才脱下 → "
                                                 f"实测 {fmt(st2)}，计划 {fmt(a2['start'])}"
                                                 f"（+{delays[node2]}s）"})
    chain.sort(key=lambda c: c["time"])
    return first, chain


# ---------------- 多次连排汇总与时长建议 ----------------

def _p75(samples):
    s = sorted(samples)
    return s[max(0, math.ceil(0.75 * len(s)) - 1)]


def summarize_suggestions(state, production_id=1, revision_id=None):
    """按服装动作（件×穿/脱）与完整人员配置（具体服装师/自助）汇总已结束连排的
    实测时长，以 P75 为建议值。revision_id 给定时只汇总基于该基准修订的连排，
    且「基准值」取自该修订的冻结服装用时。"""
    runs = [r for r in db.list_runs(production_id) if r["status"] == "done"]
    if revision_id is not None:
        runs = [r for r in runs if r["revision_id"] == revision_id]
    groups = {}          # (item_id, kind, dresser_id|None) -> [dur]
    dresser_names = {}
    for r in runs:
        plan = load_plan(db.get_run(r["id"]))
        events = db.run_events(r["id"])
        eff = effective_events(events)
        tasks = {t["id"]: t for t in plan["tasks"]}
        for d in plan["dressers"]:
            dresser_names[d["id"]] = d["name"]
        for a in plan["actions"]:
            if a["kind"] not in ("don", "doff") or a.get("item_id") is None:
                continue
            ev_s = eff.get((a["task_id"], a["idx"], "start"))
            ev_d = eff.get((a["task_id"], a["idx"], "done"))
            if not ev_s or not ev_d:
                continue
            dur = ev_d["at_sec"] - ev_s["at_sec"]
            if not 0 < dur < 900:
                continue
            dr = tasks[a["task_id"]]["dresser_id"]   # 完整人员配置：具体服装师/自助
            groups.setdefault((a["item_id"], a["kind"], dr), []).append(dur)

    if revision_id is not None:
        rev = db.get_revision(revision_id)
        snap_items = json.loads(rev["snapshot"])["items"] if rev else []
        items = {i["id"]: i for i in snap_items}
    else:
        items = {i["id"]: i for i in state["items"]}
    out = []
    for (iid, kind, dr), samples in sorted(groups.items(),
                                           key=lambda x: (x[0][0], x[0][1], x[0][2] or 0)):
        it = items.get(iid)
        if not it:
            continue
        field = "don_sec" if kind == "don" else "doff_sec"
        current = it[field]
        suggested = int(math.ceil(_p75(samples)))
        if suggested == current:
            continue
        out.append({
            "key": f"{iid}:{kind}:{dr or 0}",
            "item_id": iid, "item_name": it["name"],
            "action": kind, "dresser_id": dr,
            "dresser_name": dresser_names.get(dr) if dr else None,
            "staffed": dr is not None,
            "current": current, "suggested": suggested,
            "n": len(samples), "min": min(samples), "max": max(samples),
        })
    return out


def derive_revision(run_id, accepted_keys, production_id=1):
    """从所选连排的基准修订派生新修订：建议写回冻结服装用时 → 在冻结方案上
    重排 → 只把受影响任务（窗口开始变化）的开始时刻写回快照，锁定节点不动。
    全程不读写当前可变方案（scenes/tasks/items 表），产物仅为一个新修订。"""
    run = db.get_run(run_id)
    if not run:
        return None
    rev = db.get_revision(run["revision_id"])
    if not rev:
        return None
    snap = json.loads(rev["snapshot"])          # 冻结基准：scenes/tasks/items
    state = db.load_state(production_id)        # 仅取辅助数据（造型/人员/位置/服装车）
    sugg = summarize_suggestions(state, production_id, revision_id=rev["id"])
    chosen = [s for s in sugg if s["key"] in set(accepted_keys)]
    if not chosen:
        return None
    # 同一服装动作的多个人员配置组被同时勾选：以样本最多组的建议值为准
    best = {}
    for s in chosen:
        k = (s["item_id"], s["action"])
        if k not in best or s["n"] > best[k]["n"]:
            best[k] = s

    def frozen_state(items):
        st = dict(state)
        st["scenes"], st["tasks"], st["items"] = snap["scenes"], snap["tasks"], items
        return st

    old = scheduler.compute_schedule(frozen_state(snap["items"]))
    new_items = [dict(i) for i in snap["items"]]
    for s in best.values():
        field = "don_sec" if s["action"] == "don" else "doff_sec"
        for i in new_items:
            if i["id"] == s["item_id"]:
                i[field] = s["suggested"]
    new = scheduler.compute_schedule(frozen_state(new_items))

    moved = 0
    new_tasks = []
    for t in snap["tasks"]:
        t = dict(t)
        w_old, w_new = old["windows"].get(t["id"]), new["windows"].get(t["id"])
        if not t["locked"] and w_old and w_new \
                and int(w_old["start"]) != int(w_new["start"]):
            t["start_sec"] = int(w_new["start"])
            moved += 1
        new_tasks.append(t)
    names = "、".join(s["item_name"] for s in best.values())
    note = (f"连排#{run_id} 基准派生（修订#{rev['id']}）：调整 {len(best)} 项服装用时"
            f"（{names}），重排 {moved} 个受影响任务（已锁未动）")
    new_snap = {"scenes": snap["scenes"], "tasks": new_tasks, "items": new_items}
    rev_id = db.save_snapshot_revision(new_snap, note, production_id)
    return {"revision_id": rev_id, "moved": moved, "note": note,
            "applied": list(best.values())}
