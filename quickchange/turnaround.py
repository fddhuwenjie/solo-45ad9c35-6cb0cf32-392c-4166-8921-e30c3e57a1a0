# -*- coding: utf-8 -*-
"""场间复位工作区：逐副本养护路线生成与复位排程。

来源（二选一）：
- run：选定连排的**实际穿用记录**——以冻结基准计划重建副本日历，再用
  打点登记的现场 item_id/copy_id 识别真实穿用的实物副本；
- revision：已确认方案（修订快照）——按快照排程的副本日历生成。

路线按服装属性与异常记录展开有序工序 clean→dry→mend→press→load，
就位期限 = 该副本在晚场（当前方案经 evening_offset 平移）被换装任务引用的
最早时刻。排程同时约束：
- 步骤次序（同一路线严格串行，前序完工后一序才能开始）；
- 设备/工位容量（区间日历，capacity 并发）与冷却间隔（占用结束后的禁用间隔）；
- 人员技能（资源要求技能，人员须具备）与人员独占（同一时刻只参与一个工序，
  且不与其换装动作日历冲突）；
- 晚场造型需求（deadline 与引用任务，无法按时归位标出最早卡点）。

完工（done）工序一经锁定即固定在实测时段，重排不再挪动；退回重做（return）
使该工序及后序回到待排；报废（scrap）取消整条路线。人员/工位/设备改派只
重算后续安排，已锁工序不动。
"""
import json

import db
import rehearsal
import scheduler

# 工序：(kind, 中文名, 默认用时秒)
STEP_KINDS = (
    ("clean", "清洁去渍", 120),
    ("dry", "烘干", 180),
    ("mend", "缝补", 150),
    ("press", "整烫", 90),
    ("load", "装车", 30),
)
STEP_CN = {k: cn for k, cn, _ in STEP_KINDS}
STEP_DEFAULT_DUR = {k: d for k, _, d in STEP_KINDS}
STEP_ORDER = [k for k, _, _ in STEP_KINDS]
EQUIPMENT_KINDS = {"clean", "dry"}          # 设备
STATION_KINDS = {"mend", "press", "load"}   # 工位
DRY_COOL_DOWN = 120                         # 烘干后部件冷却：后序最小间隔秒
EVENT_KINDS = ("start", "done", "return", "scrap")
# 连排异常打点中触发缝补工序的关键词
MEND_KEYWORDS = ("开线", "裂", "破", "撕", "脱线", "断线", "钩破", "挂破")


def _index(lst):
    return {r["id"]: r for r in lst}


# ---------------- 穿用记录抽取 ----------------

def _run_base_state(plan, run=None):
    """连排冻结基准重建排程用 state：scenes/tasks/items 取基准修订完整快照，
    辅助表取连排冻结 plan（修订快照优先）。"""
    items = plan["items"]
    scenes, tasks = plan["scenes"], plan["tasks"]
    if run is not None:
        rev = db.get_revision(run["revision_id"])
        if rev:
            snap = json.loads(rev["snapshot"])
            scenes, tasks, items = snap["scenes"], snap["tasks"], snap["items"]
    state = {
        "scenes": scenes, "tasks": tasks, "items": items,
        "looks": plan.get("looks", []), "look_items": plan.get("look_items", []),
        "actors": plan["actors"], "dressers": plan["dressers"],
        "positions": plan["positions"], "carts": plan.get("carts", []),
        "skills": plan.get("skills", []),
        "dresser_skills": plan.get("dresser_skills", []),
        "dresser_sides": plan.get("dresser_sides", []),
        "dresser_unavailable": plan.get("dresser_unavailable", []),
        "action_specs": plan.get("action_specs", []),
        "action_staff": plan.get("action_staff", []),
        "action_reviews": [],
    }
    if run is not None:
        rev = db.get_revision(run["revision_id"])
        if rev:
            snap = json.loads(rev["snapshot"])
            for k in rehearsal.AUX_KEYS:
                if snap.get(k) is not None:
                    state[k] = snap[k]
    return state


def extract_wear_from_run(run, events, state):
    """从连排实际穿用记录抽取日场穿用。

    返回 {"items": {item_id: {copy_no: released_at}},
          "wearers": {(item_id, copy_no): {actor_id,...}}}。
    以冻结基准重建的副本日历为主（日场落幕后通常只对脱下环节打点）：每个
    task:/init: 占用即一件副本的一次穿用，释放时刻取对应脱下动作的有效
    done 打点（可登记现场 item_id/copy_id 覆盖身份），缺打点用基准区间结束。
    """
    plan = rehearsal.load_plan(run)
    base_state = _run_base_state(plan, run)
    sched = scheduler.compute_schedule(base_state)
    eff = rehearsal.effective_events(events)
    tasks = _index(plan["tasks"])

    doffs = {}
    for a in plan["actions"]:
        if a["kind"] != "doff" or a.get("item_id") is None:
            continue
        ev_d = eff.get((a["task_id"], a["idx"], "done"))
        if not ev_d:
            continue
        actor = tasks[a["task_id"]]["actor_id"]
        iid = ev_d["item_id"] if ev_d["item_id"] is not None else a["item_id"]
        doffs.setdefault((actor, iid), []).append(
            (a["start"], ev_d["at_sec"], ev_d["copy_id"]))
    for v in doffs.values():
        v.sort()

    worn, wearers = {}, {}

    def record(iid, copy_no, released, actor):
        slot = worn.setdefault(iid, {})
        slot[copy_no] = max(slot.get(copy_no, 0), released)
        if actor is not None:
            wearers.setdefault((iid, copy_no), set()).add(actor)

    # 先建 (item,copy) → 演员集合（只从换装任务区间取，init 段不记演员）
    copy_actors = {}
    for iid, cps in sched["copies"].items():
        for ci, ivs in enumerate(cps):
            aids = set()
            for iv in ivs:
                label = iv[2]
                if isinstance(label, str) and label.startswith("task:"):
                    t = tasks.get(int(label.split(":")[1]))
                    if t:
                        aids.add(t["actor_id"])
            copy_actors[(iid, ci + 1)] = aids

    for iid, cps in sched["copies"].items():
        for ci, ivs in enumerate(cps):
            no = ci + 1
            for iv in ivs:
                label = iv[2]
                if isinstance(label, str) and label.startswith("task:"):
                    tid = int(label.split(":")[1])
                elif isinstance(label, str) and label.startswith("init:"):
                    tid = None
                else:
                    continue
                actual_no = no
                released, actor = iv[1], None
                if tid is not None:
                    t = tasks.get(tid)
                    if t:
                        actor = t["actor_id"]
                        queue = doffs.get((actor, iid))
                        if queue:
                            _order, at_done, cp_no = queue.pop(0)
                            released = at_done
                            if cp_no:
                                actual_no = cp_no
                record(iid, actual_no, released, actor)
            # init 段本身没有任务标签：穿着者按该副本的任务区间演员补登
            for actor in copy_actors.get((iid, no), ()):
                wearers.setdefault((iid, no), set()).add(actor)
    return {"items": worn, "wearers": wearers}


def extract_wear_from_revision(rev, state):
    """已确认方案（修订快照）：快照排程副本日历 →
    {"items": {item_id:{copy_no:released_at}}, "wearers": {(iid,copy):{actor}}}。"""
    snap = json.loads(rev["snapshot"])
    base = dict(state)
    base["scenes"] = snap["scenes"]
    base["tasks"] = snap["tasks"]
    base["items"] = snap["items"]
    for k in rehearsal.AUX_KEYS:
        if snap.get(k) is not None:
            base[k] = snap[k]
    sched = scheduler.compute_schedule(base)
    tasks = _index(snap["tasks"])
    worn, wearers = {}, {}

    def record(iid, no, rel, actor):
        slot = worn.setdefault(iid, {})
        slot[no] = max(slot.get(no, 0), rel)
        if actor is not None:
            wearers.setdefault((iid, no), set()).add(actor)

    for iid, cps in sched["copies"].items():
        for ci, ivs in enumerate(cps):
            for iv in ivs:
                label = iv[2]
                tid = None
                if isinstance(label, str) and label.startswith("task:"):
                    tid = int(label.split(":")[1])
                elif not (isinstance(label, str) and label.startswith("init:")):
                    continue
                actor = tasks.get(tid, {}).get("actor_id") if tid else None
                record(iid, ci + 1, iv[1], actor)
    return {"items": worn, "wearers": wearers}


def extract_mend_items(run, events):
    """连排有效异常打点命中开线/破损关键词 → 需要缝补的服装集合（item_id）。
    打点未登记现场 item_id 时按基准计划动作反查。"""
    plan = rehearsal.load_plan(run)
    action_item = {(a["task_id"], a["idx"]): a.get("item_id")
                   for a in plan["actions"]}
    out = set()
    for e in rehearsal.effective_events(events).values():
        if e["kind"] != "exception" or \
                not any(k in (e["reason"] or "") for k in MEND_KEYWORDS):
            continue
        iid = e.get("item_id")
        if iid is None:
            iid = action_item.get((e["task_id"], e["action_idx"]))
        if iid is not None:
            out.add(iid)
    return out


# ---------------- 晚场需求（就位期限 + 引用任务） ----------------

def evening_needs(state, evening_offset):
    """当前方案平移 evening_offset 后的晚场副本日历。

    返回 {"copies": {item_id: {copy_no: [(start,end,task_id),...]}},
          "by_actor": {(actor,item_id): {copy_no: [(start,end,task_id)]}}}。
    init 段（开场前已穿）task_id=None，期限取场次开场。
    """
    sched = scheduler.compute_schedule(state)
    out, by_actor = {}, {}
    # 穿上区间 → (actor,item)：从动作表反查（sched.copies 区间标签只存 task）
    don_actor = {}
    for a in sched["actions"]:
        if a["kind"] == "don" and a.get("item_id") is not None:
            t = next((t for t in state["tasks"] if t["id"] == a["task_id"]), None)
            if t:
                don_actor[(a["task_id"], a["item_id"])] = t["actor_id"]
    for iid, cps in sched["copies"].items():
        for ci, ivs in enumerate(cps):
            lst, alst = [], []
            for iv in ivs:
                label = iv[2]
                tid = None
                if isinstance(label, str) and label.startswith("task:"):
                    tid = int(label.split(":")[1])
                elif not (isinstance(label, str) and label.startswith("init:")):
                    continue
                row = (iv[0] + evening_offset, iv[1] + evening_offset, tid)
                lst.append(row)
                if tid is not None:
                    actor = don_actor.get((tid, iid))
                    if actor is not None:
                        alst.append((actor, row))
            if lst:
                out.setdefault(iid, {})[ci + 1] = sorted(lst)
                for actor, row in alst:
                    by_actor.setdefault((actor, iid), {}) \
                        .setdefault(ci + 1, []).append(row)
    for d in by_actor.values():
        for v in d.values():
            v.sort()
    return {"copies": out, "by_actor": by_actor}


# ---------------- 路线生成 ----------------

def _route_kinds(item, mend=False):
    kinds = []
    if item.get("need_clean", 1):
        kinds.append("clean")
    if item.get("need_dry", 1):
        kinds.append("dry")
    if mend or item["status"] == "repair":
        kinds.append("mend")
    if item.get("need_press", 1):
        kinds.append("press")
    kinds.append("load")
    seen, out = set(), []
    for k in STEP_ORDER:
        if k in kinds and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def build_routes_spec(state, source_kind, source_id, evening_offset, pid=1):
    """生成逐副本路线规格与来源摘要。返回 (routes_spec, source_summary)。"""
    mend_items = set()
    if source_kind == "run":
        run = db.get_run(source_id)
        if not run:
            return None, None
        events = db.run_events(source_id)
        wear = extract_wear_from_run(run, events, state)
        mend_items = extract_mend_items(run, events)
        source_summary = {
            "kind": "run", "run_id": run["id"], "run_name": run["name"],
            "revision_id": run["revision_id"], "status": run["status"],
        }
    elif source_kind == "revision":
        rev = db.get_revision(source_id)
        if not rev:
            return None, None
        wear = extract_wear_from_revision(rev, state)
        source_summary = {
            "kind": "revision", "revision_id": rev["id"], "note": rev["note"],
            "created_at": rev["created_at"],
        }
    else:
        return None, None

    worn, wearers = wear["items"], wear["wearers"]
    items = _index(state["items"])
    needs = evening_needs(state, evening_offset)
    routes = []
    for iid, copies in worn.items():
        it = items.get(iid)
        if not it or it["kind"] == "prop":
            continue
        kinds = _route_kinds(it, mend=(iid in mend_items))
        for no, released in sorted(copies.items()):
            # 晚场同一穿着者再次使用同一件 → 该副本须就位；无穿着者信息的
            # 开场前穿着段不做编号猜测（编号分配可能不同），视为晚场不再引用
            actor_ivs = []
            for actor in wearers.get((iid, no), ()):
                actor_ivs += [iv for c in needs["by_actor"].get((actor, iid), {}).values()
                              for iv in c]
            future = sorted(iv for iv in actor_ivs if iv[0] >= released)
            deadline = future[0][0] if future else None
            ref_ids = sorted({iv[2] for iv in future if iv[2] is not None})
            routes.append({
                "item_id": iid, "copy_no": no, "item_name": it["name"],
                "released_at": int(released), "deadline_sec": deadline,
                "ref_task_ids": ref_ids,
                "kinds": [(k, STEP_DEFAULT_DUR[k]) for k in kinds],
                "sort_key": deadline if deadline is not None else 10 ** 9,
            })
    routes.sort(key=lambda r: (r["sort_key"], r["item_id"], r["copy_no"]))
    return routes, source_summary


# ---------------- 执行事件解释 ----------------

def effective_care_events(events):
    """每道工序的有效事件集合：start/done/return/scrap 各取最新一条。"""
    out = {}
    for e in sorted(events, key=lambda x: x["id"]):
        out.setdefault(e["step_id"], {})[e["kind"]] = e
    return out


def _returned(eff_by_step, step):
    """退回重做：最新事件为 return（之后无新的开工/完工/报废）。"""
    evs = eff_by_step.get(step["id"]) or eff_by_step.get(str(step["id"])) or {}
    ret = evs.get("return")
    if not ret:
        return False
    return all(not evs.get(k) or evs[k]["id"] < ret["id"]
               for k in ("start", "done", "scrap"))


# ---------------- 单槽位日历 ----------------

def _earliest_lane(cal, ready, dur, cooldown, fixed=None, limit=24 * 3600):
    """单条资源/人员「槽位」日历上，最早可放下 [s,s+dur) 的时刻。

    cal: [s,e,step_id,fixed]；fixed=1 为锚点（不可进入），其余为自动区间，
    相邻占用之间至少留 cooldown 秒冷却间隔。
    fixed 给定时只检验该钉死时刻是否可行（不可行返回 None）。
    """
    def feasible(s):
        e = s + dur
        for s0, e0, _sid, fx in cal:
            if fx:
                if s < e0 and e > s0:
                    return False
            elif s < e0 + (cooldown or 0) and e > s0:
                return False
        return True

    if fixed is not None:
        return int(fixed) if feasible(int(fixed)) else None

    cands = [ready]
    for s0, e0, _sid, fx in sorted(cal):
        cands.append(e0 if fx else e0 + (cooldown or 0))
    for s in sorted({max(ready, int(c)) for c in cands}):
        if s > limit:
            break
        if feasible(s):
            return s
    return max(ready, max((e for _s, e, _i, fx in cal if not fx), default=ready)
               + (cooldown or 0))


# ---------------- 复位排程 ----------------

def compute(state, tr, routes, steps, events):
    """对工作区全部工序排程。返回 {steps, routes, conflicts, earliest}。"""
    resources = _index(state.get("care_resources", []))
    dressers = _index(state["dressers"])
    skills = _index(state.get("skills", []))
    dresser_sk = {}
    for r in state.get("dresser_skills", []):
        dresser_sk.setdefault(r["dresser_id"], set()).add(r["skill_id"])
    offset = int(tr.get("evening_offset_sec") or 0)

    routes_by_id = _index(routes)
    steps_by_route = {}
    for s in steps:
        steps_by_route.setdefault(s["route_id"], []).append(s)
    for v in steps_by_route.values():
        v.sort(key=lambda x: (x["seq"], x["id"]))
    eff = effective_care_events(events)

    # capacity>1 的资源展开成多条独立 lane（各自计冷却间隔）
    res_lanes = {r["id"]: [[] for _ in range(max(1, r["capacity"]))]
                 for r in resources.values()}
    # 人员日历预置换装动作（日场原时刻 + 平移后的晚场时刻）
    person_cal = {d: [] for d in dressers}
    for a in state.get("schedule", {}).get("actions", []):
        for did in (a.get("staff", {}) or {}).get("ids", []):
            if did in person_cal:
                person_cal[did].append([a["start"], a["end"], None, 1])
                person_cal[did].append([a["start"] + offset, a["end"] + offset,
                                        None, 1])

    laid, conflicts, route_scrapped = {}, [], set()

    def add_conflict(c):
        conflicts.append(c)

    def commit(s, start, resource_id, dresser_id, fixed, status, lane=0):
        rec = {"id": s["id"], "start": int(start), "end": int(start + s["dur_sec"]),
               "resource_id": resource_id, "dresser_id": dresser_id,
               "fixed": 1 if fixed else 0, "status": status}
        laid[s["id"]] = rec
        if resource_id in res_lanes:
            res_lanes[resource_id][lane].append(
                [rec["start"], rec["end"], s["id"], 1 if fixed else 0])
        if dresser_id in person_cal:
            person_cal[dresser_id].append(
                [rec["start"], rec["end"], s["id"], 1 if fixed else 0])
        return rec

    # ---- 分类：报废 / 锚点（done/locked/人工时刻/已开工）/ 自动 ----
    # 最新事件优先级：scrap > return（退回待排）> done 锁定 > start 锚定
    anchors, autos = [], []
    for route in routes:
        for s in steps_by_route.get(route["id"], []):
            evs = eff.get(s["id"], {})
            if evs.get("scrap"):
                route_scrapped.add(route["id"])
                laid[s["id"]] = {"id": s["id"], "start": evs["scrap"]["at_sec"],
                                 "end": evs["scrap"]["at_sec"],
                                 "resource_id": s["resource_id"],
                                 "dresser_id": s["dresser_id"], "fixed": 1,
                                 "status": "scrapped"}
                continue
            if _returned(eff, s):
                autos.append((route, s))
                continue
            done, started = evs.get("done"), evs.get("start")
            if done:
                anchors.append((route, s, "done", done["at_sec"]))
            elif s.get("locked"):
                anchors.append((route, s, "done",
                                started["at_sec"] if started
                                else max(route["released_at"], s.get("start_sec") or 0)))
            elif s.get("start_sec") is not None:
                anchors.append((route, s, "manual", int(s["start_sec"])))
            elif started:
                anchors.append((route, s, "started", started["at_sec"]))
            else:
                autos.append((route, s))

    def skill_ok(rid, did):
        res = resources.get(rid)
        return not (res and res.get("skill_id")) or \
            res["skill_id"] in dresser_sk.get(did, set())

    # ---- 阶段 1：锚点提交（只检查，不移动） ----
    for route, s, why, atv in anchors:
        start, rid, did = atv, s["resource_id"], s["dresser_id"]
        status = {"done": "done", "started": "started"}.get(why, "manual")
        chain = steps_by_route[route["id"]]
        idx = next(i for i, x in enumerate(chain) if x["id"] == s["id"])
        if idx > 0:
            prev = chain[idx - 1]
            pl = laid.get(prev["id"])
            if pl is None or pl["status"] == "blocked" or _returned(eff, prev):
                add_conflict({"time": start, "route_id": route["id"],
                              "step_id": s["id"], "kind": s["kind"], "type": "order",
                              "message": f"{route['item_name']}第{route['copy_no']}件"
                                         f"「{STEP_CN[s['kind']]}」早于前序"
                                         f"「{STEP_CN[prev['kind']]}」完成"})
            elif start < pl["end"]:
                add_conflict({"time": start, "route_id": route["id"],
                              "step_id": s["id"], "kind": s["kind"], "type": "order",
                              "message": f"{route['item_name']}第{route['copy_no']}件"
                                         f"「{STEP_CN[s['kind']]}」开工 "
                                         f"{scheduler.fmt(start)} 早于前序完工 "
                                         f"{scheduler.fmt(pl['end'])}"})
        lane = 0
        if rid in res_lanes:
            free = [i for i, l in enumerate(res_lanes[rid])
                    if not any(start < e and start + s["dur_sec"] > s0
                               for s0, e, _x, fx in l if fx)]
            if not free:
                add_conflict({"time": start, "route_id": route["id"],
                              "step_id": s["id"], "kind": s["kind"],
                              "type": "capacity",
                              "message": f"设备/工位「{resources[rid]['name']}」容量超限："
                                         f"{STEP_CN[s['kind']]}@{scheduler.fmt(start)}"})
            else:
                lane = free[0]
        if did:
            d = dressers.get(did)
            if rid and not skill_ok(rid, did):
                res = resources.get(rid)
                sk = skills.get(res["skill_id"]) if res else None
                add_conflict({"time": start, "route_id": route["id"],
                              "step_id": s["id"], "kind": s["kind"], "type": "skill",
                              "message": f"{d['name'] if d else '#'+str(did)}"
                                         f"不具备技能「{sk['name'] if sk else '?'}」"})
            if any(start < iv[1] and start + s["dur_sec"] > iv[0]
                   for iv in person_cal.get(did, [])):
                add_conflict({"time": start, "route_id": route["id"],
                              "step_id": s["id"], "kind": s["kind"], "type": "person",
                              "message": f"{d['name'] if d else '#'+str(did)}时间冲突："
                                         f"{STEP_CN[s['kind']]}与其它安排重叠"})
        commit(s, start, rid, did, fixed=True, status=status, lane=lane)

    # ---- 阶段 2：自动布局（按就位期限 × 路线 × 次序） ----
    order = sorted(autos, key=lambda rs: (rs[0]["sort_key"], rs[0]["id"],
                                          rs[1]["seq"]))
    for route, s in order:
        chain = steps_by_route[route["id"]]
        idx = next(i for i, x in enumerate(chain) if x["id"] == s["id"])
        ready = route["released_at"]
        if idx > 0:
            pl = laid.get(chain[idx - 1]["id"])
            if pl:
                ready = max(ready, pl["end"])
        if idx > 0 and chain[idx - 1]["kind"] == "dry":
            ready += DRY_COOL_DOWN
        kind = s["kind"]
        cands_res = [r for r in resources.values() if r["kind"] == kind]
        if s.get("resource_id") and s["resource_id"] in resources and \
                resources[s["resource_id"]]["kind"] == kind:
            cands_res = [resources[s["resource_id"]]]
        if s.get("dresser_id") in dressers:
            cands_persons = [dressers[s["dresser_id"]]]
        else:
            cands_persons = list(dressers.values())
        best = None
        for r in cands_res:
            cd = int(r.get("cool_down_sec") or 0)
            for li, lane in enumerate(res_lanes[r["id"]]):
                rs = _earliest_lane(lane, ready, s["dur_sec"], cd)
                for d in cands_persons:
                    req_sk = r["skill_id"]
                    if req_sk and req_sk not in dresser_sk.get(d["id"], set()):
                        continue
                    ps = _earliest_lane(person_cal[d["id"]], rs, s["dur_sec"], 0)
                    if best is None or ps < best[0]:
                        best = (ps, r["id"], li, d["id"])
        if best is None:
            no_skill = bool(cands_res) and not any(
                (not r["skill_id"] or r["skill_id"] in dresser_sk.get(d["id"], set()))
                for r in cands_res for d in dressers.values())
            add_conflict({"time": ready, "route_id": route["id"], "step_id": s["id"],
                          "kind": kind, "type": "skill" if no_skill else "no_resource",
                          "message": ("无合格人员：没有服装师具备该工序所需技能"
                                      if no_skill
                                      else f"缺少{STEP_CN.get(kind, kind)}设备/工位")})
            laid[s["id"]] = {"id": s["id"], "start": ready,
                             "end": ready + s["dur_sec"], "resource_id": None,
                             "dresser_id": None, "fixed": 0, "status": "blocked"}
            continue
        st, rid, li, did = best
        rec = commit(s, st, rid, did, fixed=False, status="planned", lane=li)
        dl = route["deadline_sec"]
        if dl is not None and rec["end"] > dl:
            add_conflict({"time": rec["end"], "route_id": route["id"],
                          "step_id": s["id"], "kind": kind, "type": "late",
                          "message": f"{route['item_name']}第{route['copy_no']}件"
                                     f"「{STEP_CN[kind]}」最早 {scheduler.fmt(rec['end'])} "
                                     f"才完成，晚场就位期限 {scheduler.fmt(dl)}"})

    # ---- 路线汇总 ----
    route_status = {}
    for route in routes:
        chain = steps_by_route.get(route["id"], [])
        if route["id"] in route_scrapped:
            rstatus = "scrapped"
        elif chain and all(laid.get(x["id"], {}).get("status") == "done" for x in chain):
            rstatus = "done"
        elif any(laid.get(x["id"], {}).get("status") == "started" for x in chain):
            rstatus = "running"
        else:
            rstatus = "planned"
        recs = [laid[x["id"]] for x in chain if x["id"] in laid]
        end = max((r["end"] for r in recs), default=None)
        dl = route["deadline_sec"]
        route_status[route["id"]] = {
            "id": route["id"], "status": rstatus, "end": end,
            "on_time": (None if dl is None or end is None or rstatus == "scrapped"
                        else end <= dl)}

    # 报废：提醒重新核对晚场引用它的换装任务
    for route in routes:
        if route["id"] not in route_scrapped:
            continue
        refs = json.loads(route["ref_task_ids"] or "[]")
        add_conflict({"time": route["deadline_sec"] or route["released_at"],
                      "route_id": route["id"], "step_id": None, "kind": None,
                      "type": "scrap",
                      "message": f"{route['item_name']}第{route['copy_no']}件已报废："
                                 f"请重新核对晚场引用它的换装任务"
                                 + (f"（任务 #{'、#'.join(map(str, refs))}）"
                                    if refs else "")})

    conflicts.sort(key=lambda c: (c["time"], c["route_id"] or 0, c.get("step_id") or 0))
    step_out = []
    for s in steps:
        rec = laid.get(s["id"])
        evs = eff.get(s["id"], {})
        out_s = {"id": s["id"], "route_id": s["route_id"], "seq": s["seq"],
                 "kind": s["kind"], "label": STEP_CN[s["kind"]],
                 "dur_sec": s["dur_sec"], "resource_id": s["resource_id"],
                 "dresser_id": s["dresser_id"], "start_sec": s.get("start_sec"),
                 "locked": bool(s.get("locked")),
                 "note": s.get("note", ""),
                 "returned": _returned(evs, s)}
        if rec:
            out_s.update({"start": rec["start"], "end": rec["end"],
                          "res_resource_id": rec["resource_id"],
                          "res_dresser_id": rec["dresser_id"],
                          "fixed": rec["fixed"],
                          "status": "planned" if _returned(evs, s)
                          and rec["status"] != "blocked" else rec["status"]})
        step_out.append(out_s)
    earliest = next((c for c in conflicts
                     if c["type"] in ("late", "no_resource", "capacity", "skill",
                                      "person", "order", "scrap")), None)
    return {"steps": step_out,
            "routes": [route_status[r["id"]] for r in routes],
            "conflicts": conflicts, "earliest": earliest}


# ---------------- 工作区装配 ----------------

def detail(state, tr):
    routes = db.care_routes(tr["id"])
    steps = db.care_steps(tr["id"])
    events = db.care_events(tr["id"])
    result = compute(state, tr, routes, steps, events)
    status_by_id = {r["id"]: r for r in result["routes"]}
    route_out = []
    for r in routes:
        route_out.append({**status_by_id.get(r["id"], {}), **r,
                          "ref_task_ids": json.loads(r.get("ref_task_ids") or "[]")})
    return {
        "turnaround": {k: tr[k] for k in
                       ("id", "name", "status", "source_kind", "source_id",
                        "evening_offset_sec", "created_at", "archived_at")},
        "source": json.loads(tr.get("source_json") or "{}"),
        "routes": route_out,
        "steps": result["steps"],
        "events": events,
        "conflicts": result["conflicts"],
        "earliest": result["earliest"],
    }


def archive_pack(state, tr):
    """归档包：来源记录 + 人工计划 + 执行事件。"""
    d = detail(state, tr)
    return {
        "turnaround": d["turnaround"], "source": d["source"],
        "routes": d["routes"], "steps": d["steps"], "events": d["events"],
        "conflicts": d["conflicts"],
    }
