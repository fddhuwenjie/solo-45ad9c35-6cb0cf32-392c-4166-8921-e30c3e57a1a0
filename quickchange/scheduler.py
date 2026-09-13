"""快速换装排程引擎。

计入：动作先后（脱外层→脱内层→穿内层→穿外层→取道具）、动作级人员并发
（演员/服装师在同一时刻只能参与一个动作）、双人同步（动作所需人数）、
动作交接（相邻动作可由不同服装师负责，含跨侧台步行）、换装位容量、
步行时间、技能/侧台资格、服装师不可用时段、服装复用（一件服装被多人
依次使用）以及清洁/维修状态。

资源模型：演员、服装师、换装位、服装副本均为「区间日历」。
任务按期望开始时刻的先后顺序排程；已锁任务保留在自己的固定时段，
只占用该时段内的资源，不会预先挤占更早任务的资源。

服装师分工按动作登记（action_staff）：每个穿脱动作可要求技能与人数、
指定固定负责人；人员需具备技能、支援动作所在侧台、且能从上一动作
（或同侧上场口待命点）步行赶到。到岗过晚、技能不符、人数不足等都会
定位到具体受阻动作。旧任务（无任何动作分工记录）退化为整段负责人
（tasks.dresser_id），保持既有行为。

服装副本自穿上动作起持续占用，直到对应的脱下动作结束；
若没有脱下记录，则占用到演员穿着该件的场次结束。
"""
import math
from collections import defaultdict

WALK_SPEED = 1.3          # 米/秒（侧台快走）
PLAN_W, PLAN_H = 40, 20   # 侧台平面图尺寸（米）
EXITS = {"L": (2.0, 10.0), "R": (38.0, 10.0)}  # 左/右上场口
PROP_HANDLE_SEC = 4       # 每件道具取用耗时
CLEAN_SEC = 90            # 复用前需要的简单清洁时间（仅当服装状态为 cleaning）


def walk_sec(a, b):
    return max(1, math.ceil(math.hypot(a[0] - b[0], a[1] - b[1]) / WALK_SPEED))


def _index(lst):
    return {r["id"]: r for r in lst}


def look_items_map(state):
    """look_id -> [item,...] 按层从内到外排序。"""
    items = _index(state["items"])
    by_look = defaultdict(list)
    for li in state["look_items"]:
        it = items.get(li["item_id"])
        if it:
            by_look[li["look_id"]].append(it)
    for v in by_look.values():
        v.sort(key=lambda i: (i["layer"], i["id"]))
    return by_look


def find_look(state, actor_id, scene_id):
    for l in state["looks"]:
        if l["actor_id"] == actor_id and l["scene_id"] == scene_id:
            return l
    return None


def build_actions(task, state):
    """把一个换装任务展开为有序动作列表。"""
    scenes = _index(state["scenes"])
    positions = _index(state["positions"])
    carts = _index(state["carts"])
    by_look = look_items_map(state)

    from_sc, to_sc = scenes[task["from_scene_id"]], scenes[task["to_scene_id"]]
    pos = positions.get(task["position_id"])
    exit_pt = EXITS.get(task["exit_side"], EXITS["L"])
    pos_pt = (pos["x"], pos["y"]) if pos else exit_pt

    prev_look = find_look(state, task["actor_id"], from_sc["id"])
    next_look = find_look(state, task["actor_id"], to_sc["id"])
    prev_items = by_look.get(prev_look["id"], []) if prev_look else []
    next_items = by_look.get(next_look["id"], []) if next_look else []
    prev_ids = {i["id"] for i in prev_items}
    next_ids = {i["id"] for i in next_items}

    doff = [i for i in sorted(prev_items, key=lambda x: -x["layer"]) if i["id"] not in next_ids]
    don = [i for i in next_items if i["id"] not in prev_ids]
    props = [i for i in don if i["kind"] == "prop"]
    don_wear = [i for i in don if i["kind"] != "prop"]

    acts = [{"kind": "walk", "label": "退场→换装位", "dur": walk_sec(exit_pt, pos_pt),
             "item_id": None, "seq": 0}]
    seq_n = defaultdict(int)
    for it in doff:
        acts.append({"kind": "doff", "label": f"脱·{it['name']}", "dur": it["doff_sec"],
                     "item_id": it["id"], "needs_dresser": it["kind"] != "prop",
                     "seq": seq_n["doff"]})
        seq_n["doff"] += 1
    for it in don_wear:
        acts.append({"kind": "don", "label": f"穿·{it['name']}", "dur": it["don_sec"],
                     "item_id": it["id"], "needs_dresser": True,
                     "seq": seq_n["don"]})
        seq_n["don"] += 1
    for it in props:
        cart = carts.get(it["cart_id"]) if it["cart_id"] else None
        cart_pt = (cart["x"], cart["y"]) if cart else pos_pt
        acts.append({"kind": "fetch", "label": f"取道具·{it['name']}",
                     "dur": walk_sec(pos_pt, cart_pt) * 2 + PROP_HANDLE_SEC,
                     "item_id": it["id"], "needs_dresser": False,
                     "seq": seq_n["fetch"]})
        seq_n["fetch"] += 1
    acts.append({"kind": "walk", "label": "换装位→上场口", "dur": walk_sec(pos_pt, exit_pt),
                 "item_id": None, "seq": 1})

    window = {
        "ready": from_sc["start_sec"] + from_sc["duration_sec"],  # 演员退出上一场
        "deadline": to_sc["start_sec"],                            # 下一场开场
    }
    return acts, window


def _scene_end(state, scene_id):
    for s in state["scenes"]:
        if s["id"] == scene_id:
            return s["start_sec"] + s["duration_sec"]
    return 0


# ---------------- 动作级分工 ----------------

def action_key(a):
    """动作定位键：(kind, item_id, seq)。walk/fetch 的 item_id 为 None。"""
    return (a["kind"], a.get("item_id"), a.get("seq", 0))


def _staffing_maps(state):
    """规格/分工/资格索引。返回 (spec_by, staff_by, dressers, skills,
    dresser_sk, dresser_sides, unavail)。"""
    skills = _index(state.get("skills", []))
    dressers = _index(state.get("dressers", []))
    dresser_sk = defaultdict(set)
    for r in state.get("dresser_skills", []):
        dresser_sk[r["dresser_id"]].add(r["skill_id"])
    dresser_sides = defaultdict(set)
    for r in state.get("dresser_sides", []):
        dresser_sides[r["dresser_id"]].add(r["side"])
    unavail = defaultdict(list)
    for r in state.get("dresser_unavailable", []):
        unavail[r["dresser_id"]].append((r["start_sec"], r["end_sec"]))
    for v in unavail.values():
        v.sort()
    spec_by = {}
    for sp in state.get("action_specs", []):
        spec_by[(sp["task_id"], sp["kind"], sp["item_id"], sp["seq"])] = sp
    staff_by = defaultdict(list)
    for st in state.get("action_staff", []):
        staff_by[(st["task_id"], st["kind"], st["item_id"], st["seq"])].append(st)
    for v in staff_by.values():
        v.sort(key=lambda s: (not s["is_lead"], s["id"]))
    review_by = {}
    for rv in state.get("action_reviews", []):
        review_by[(rv["task_id"], rv["kind"], rv["item_id"], rv["seq"])] = rv
    return (spec_by, staff_by, dressers, skills, dresser_sk,
            dresser_sides, unavail, review_by)


def action_requirement(state, t, a, maps):
    """某动作的 (所需技能id, 所需人数)：显式 action_specs 优先，
    其次服装默认技能与默认人数（穿/脱 1 人、步行/取道具 0 人）。"""
    spec_by, staff_by, dressers, skills, *_ = maps
    sp = spec_by.get((t["id"], a["kind"], a.get("item_id"), a.get("seq", 0)))
    if sp:
        return sp["skill_id"], max(1, sp["required_count"])
    skill = None
    if a["kind"] in ("don", "doff"):
        it = next((i for i in state["items"] if i["id"] == a.get("item_id")), None)
        if it:
            skill = it.get("skill_id")
    if a["kind"] in ("don", "doff") and a.get("needs_dresser", False):
        return skill, 1
    return skill, 0


def resolve_staffing(state, t, a, maps, overrides=None):
    """解析某动作的计划分工。

    返回 dict：{required_skill, required, ids(去重有序，负责人在前),
    lead_id, locked(分工已锁), legacy(旧任务按整段负责人回退)}。
    overrides: {action_key: [dresser_id,...]} 试算替代排法，不写库。
    """
    spec_by, staff_by, dressers, skills, *_ = maps
    overrides = overrides or {}
    key = action_key(a)
    required_skill, required = action_requirement(state, t, a, maps)
    rows = staff_by.get((t["id"],) + key, [])
    ids, staff_locked, legacy = [], any(s["locked"] for s in rows), False
    if key in overrides:
        seen = set()
        ids = [x for x in overrides[key]
               if x is not None and x in dressers and not (x in seen or seen.add(x))]
    elif rows:
        seen = set()
        for s in rows:
            if s["dresser_id"] in dressers and s["dresser_id"] not in seen:
                seen.add(s["dresser_id"])
                ids.append(s["dresser_id"])
    elif t.get("dresser_id") and a["kind"] in ("don", "doff"):
        # 旧任务：整段负责人照看全部穿/脱动作
        ids, legacy = [t["dresser_id"]], True
        if required == 0:
            required = 1
    if required == 0 and ids:
        required = len(ids)
    lead_id = next((s["dresser_id"] for s in rows if s["is_lead"]
                    and s["dresser_id"] in dressers), None)
    if lead_id is None and ids:
        lead_id = ids[0]
    return {"required_skill": required_skill, "required": required,
            "ids": ids, "lead_id": lead_id, "locked": staff_locked,
            "legacy": legacy}


def staffing_violations(state, t, a, staff, maps):
    """静态资格问题（不靠顺延解决）：技能不符 / 不支援该侧 / 人数不足 /
    重复登记。返回冲突描述列表。"""
    _, _, dressers, skills, dresser_sk, dresser_sides, _, _rev = maps
    out = []
    side = action_side(t, a, state)
    if len(staff["ids"]) != len(set(staff["ids"])):
        out.append("同一名服装师被重复登记")
    for did in staff["ids"]:
        d = dressers.get(did)
        if not d:
            out.append(f"服装师#{did} 不存在")
            continue
        sides = dresser_sides.get(did)
        if sides and side not in sides:
            out.append(f"{d['name']}不支援{('左' if side == 'L' else '右')}侧台")
        if staff["required_skill"] and staff["required_skill"] not in dresser_sk.get(did, set()):
            sk = skills.get(staff["required_skill"])
            out.append(f"{d['name']}不具备技能「{sk['name'] if sk else '#'+str(staff['required_skill'])}」")
    if staff["required"] and len(staff["ids"]) < staff["required"]:
        out.append(f"人数不足：需 {staff['required']} 人，实际 {len(staff['ids'])} 人")
    return out


def action_side(t, a, state):
    """动作所在侧台：换装位所在侧；未指定换装位时按退场口。"""
    pos = next((p for p in state["positions"] if p["id"] == t.get("position_id")), None)
    return pos["side"] if pos else t["exit_side"]


def point_of_action(t, a, state, positions):
    """动作发生点（米）：换装动作在换装位；walk=1 从换装位走向上场口，
    取上场口；fetch 往返取换装位。"""
    pos = positions.get(t.get("position_id"))
    if a["kind"] == "walk" and a.get("seq") == 1:
        return EXITS.get(t["exit_side"], EXITS["L"])
    return (pos["x"], pos["y"]) if pos else EXITS.get(t["exit_side"], EXITS["L"])


def dresser_ready(dr_id, start, dur, action_pt, busy, unavail, maps, limit=40):
    """服装师从 start 起最早可执行 [?, ?+dur) 的时刻。

    busy: 该服装师已提交区间 [(s,e,label,end_point),...]；
    unavail: 不可用时段 [(u0,u1),...]；
    衔接上一动作需从其结束点步行到本动作点（跨侧台按上场口间距离）。
    返回 (ready, blocker|None, prev_end)。
    """
    cur = start
    blocker = None
    prev_end = None
    for _ in range(limit):
        pushed = False
        for iv in busy:
            s, e = iv[0], iv[1]
            if s < cur + dur and e > cur:
                end_pt = iv[3] if len(iv) > 3 and iv[3] else None
                walk = walk_sec(end_pt, action_pt) if end_pt else 0
                nxt = e + walk
                if nxt > cur:
                    cur, blocker, prev_end = nxt, iv, e
                    pushed = True
                    break
        if pushed:
            continue
        for u0, u1 in unavail:
            if u0 < cur + dur and u1 > cur:
                cur, blocker, prev_end = max(cur, u1), ("unavail", u1, "不可用时段"), u1
                pushed = True
                break
        if not pushed:
            break
    return cur, blocker, prev_end


# ---------------- 服装穿着区间 ----------------

def _wear_segments(state, tasks):
    """(actor,item) -> [段落]：一件服装在一名演员身上连续穿着的场次区间。

    段落起点任务（don_task）= 进入首场的换装任务；没有则视为开场前已穿上（从 0 占用）。
    段落终点任务（doff_task）= 离开末场的换装任务；没有则占用到末场结束。
    """
    by_look = look_items_map(state)
    scenes = _index(state["scenes"])
    look_of = {(l["actor_id"], l["scene_id"]): l["id"] for l in state["looks"]}
    actor_scenes = defaultdict(list)
    for (aid, sid) in look_of:
        actor_scenes[aid].append(sid)
    don_task, doff_task = {}, {}
    for t in tasks:
        don_task[(t["actor_id"], t["to_scene_id"])] = t["id"]
        doff_task[(t["actor_id"], t["from_scene_id"])] = t["id"]
    segs = defaultdict(list)
    for aid, sids in actor_scenes.items():
        sids.sort(key=lambda sid: (scenes[sid]["start_sec"], scenes[sid]["seq"]))
        item_ids = set()
        for sid in sids:
            item_ids.update(i["id"] for i in by_look.get(look_of[(aid, sid)], []))
        for iid in item_ids:
            run = None
            for sid in sids:
                present = any(i["id"] == iid for i in by_look.get(look_of[(aid, sid)], []))
                if present:
                    if run is None:
                        run = {"item_id": iid, "start_scene": sid, "end_scene": sid,
                               "don_task": don_task.get((aid, sid)),
                               "doff_task": doff_task.get((aid, sid))}
                    else:
                        run["end_scene"] = sid
                        run["doff_task"] = doff_task.get((aid, sid))
                elif run is not None:
                    segs[(aid, iid)].append(run)
                    run = None
            if run is not None:
                segs[(aid, iid)].append(run)
    return segs


def _seg_release(seg, tasks_by_id, scenes, items, doff_actuals=None):
    """段落释放时刻：有脱下任务 → 该任务中脱下动作的结束时刻
    （优先取上一轮排程的实际值 doff_actuals；首轮按期望开始时刻估算）；
    没有脱下记录 → 演员穿着该件的末场结束时刻。"""
    it = items.get(seg["item_id"])
    extra = CLEAN_SEC if it and it["status"] == "cleaning" else 0
    dt = tasks_by_id.get(seg.get("doff_task"))
    if dt is not None:
        if doff_actuals and (dt["id"], seg["item_id"]) in doff_actuals:
            return doff_actuals[(dt["id"], seg["item_id"])] + extra
        cur = dt["_desired"]
        for a in dt["_acts"]:
            cur += a["dur"]
            if a["kind"] == "doff" and a.get("item_id") == seg["item_id"]:
                return cur + extra
    sc = scenes.get(seg["end_scene"])
    return (sc["start_sec"] + sc["duration_sec"]) if sc else 0


def _copy_gap_abs(cps, t, end):
    """最早 t'≥t 使某副本在 [t', end) 全程空闲；无可行副本返回 None。"""
    best = None
    for cp in cps:
        cur = t
        ok = True
        for iv in sorted(cp):
            if iv[1] <= cur:
                continue
            if iv[0] >= end:
                break
            cur = iv[1]          # 与 [cur,end) 相交 → 推到该区间结束之后
            if cur >= end:
                ok = False
                break
        if ok and (best is None or cur < best):
            best = cur
    return best


def _copy_gap_dur(cps, t, dur):
    """最早 t'≥t 使某副本在 [t', t'+dur) 空闲（仅保证穿上动作本身不重叠）。"""
    best = None
    for cp in cps:
        cur = t
        for iv in sorted(cp):
            if iv[1] <= cur:
                continue
            if iv[0] >= cur + dur:
                break
            cur = iv[1]
        if best is None or cur < best:
            best = cur
    return best if best is not None else t


def _pick_copy(cps, t, release):
    """在 t 时刻选一件副本：要求 [t, release) 不与该副本已有区间冲突。
    返回 (副本下标, 是否完全不冲突)；全部被占用返回 None。"""
    best = None
    for idx, cp in enumerate(cps):
        if any(iv[0] <= t < iv[1] for iv in cp):
            continue
        nxt = min((iv[0] for iv in cp if iv[0] > t), default=float("inf"))
        if nxt >= release:
            return (idx, True)
        if best is None or nxt > best[1]:
            best = (idx, nxt)
    return (best[0], False) if best else None


def _overlap(ivs, s, e):
    for iv in ivs:
        if iv[0] < e and iv[1] > s:
            return iv
    return None


def _capacity_shift(intervals, cap, s, e):
    """若 [s,e) 内并发数超容量，返回需要顺延的秒数。"""
    overlap = [iv for iv in intervals if iv[0] < e and iv[1] > s]
    if len(overlap) < cap:
        return 0
    return max(1, min(iv[1] for iv in overlap) - s)


# ---------------- 排程 ----------------

def compute_schedule(state, overrides=None):
    """对全部任务排程。

    overrides: {task_id: {"start_sec":..,"position_id":..,"dresser_id":..}} 临时改动，
    用于“替代排法”试算，不写库。
    返回 {actions, windows, conflicts}

    定点迭代：首轮按期望开始时刻估算副本释放点；随后每轮用上一轮的
    实际脱下结束时刻作为精确释放点重排，直到释放点收敛。资源冲突
    （如服装师争用）使脱下顺延的，副本占用随之延长到实际脱下结束，
    最终排程不会保留因估算提前释放造成的重复分配。
    """
    overrides = overrides or {}
    scenes = _index(state["scenes"])
    items = _index(state["items"])
    positions = _index(state["positions"])
    maps = _staffing_maps(state)
    staff_ov = overrides.get("__staff__", {})

    tasks = []
    for t in state["tasks"]:
        t = dict(t)
        ov = overrides.get(t["id"])
        if ov:
            t.update({k: v for k, v in ov.items()
                      if k in ("start_sec", "position_id", "dresser_id") and v is not None})
        acts, win = build_actions(t, state)
        # 试算替代分工：{action_key: [dresser_id,...]}（键按当前任务解析）
        t["_staff_ov"] = {k[1:]: v for k, v in staff_ov.items() if k and k[0] == t["id"]}
        t["_acts"] = acts
        t["_win"] = win
        if t["locked"] and t["start_sec"] is not None:
            t["_desired"] = t["start_sec"]          # 锁定任务：固定时段
        elif t["start_sec"] is not None:
            t["_desired"] = max(win["ready"], t["start_sec"])
        else:
            t["_desired"] = win["ready"]
        tasks.append(t)
    # 按期望开始时刻先后处理：较早任务先占用资源；
    # 锁定任务只保留自己的固定时段，不会预先挤占更早任务的服装师/换装位。
    tasks.sort(key=lambda t: (t["_desired"], 0 if t["locked"] else 1, t["id"]))
    tasks_by_id = {t["id"]: t for t in tasks}
    segs = _wear_segments(state, tasks)

    doff_actuals = None
    result = None
    for _ in range(12):
        result = _schedule_once(state, tasks, tasks_by_id, scenes, items,
                                positions, segs, doff_actuals, maps)
        new_actuals = {(a["task_id"], a["item_id"]): a["end"]
                       for a in result["actions"]
                       if a["kind"] == "doff" and a.get("item_id") is not None}
        converged = doff_actuals is not None and new_actuals == doff_actuals
        if converged and not _find_copy_overlaps(result["copies"]):
            break  # 释放点收敛且无交叠：本轮使用的即为实际脱下结束时刻
        if converged:
            break  # 收敛但仍有交叠：由返回前的修复兜底
        doff_actuals = new_actuals
    # 返回前保证：同一副本的实际占用区间不得交叠
    _repair_copy_overlaps(result, items)
    result["conflicts"].sort(key=lambda c: (c["time"], c["task_id"]))
    return result


def _find_copy_overlaps(copies):
    """列出所有同一副本上交叠的占用区间对。"""
    bad = []
    for iid, cps in copies.items():
        for ci, cp in enumerate(cps):
            ivs = sorted(cp, key=lambda x: (x[0], x[1]))
            for a, b in zip(ivs, ivs[1:]):
                if b[0] < a[1]:
                    bad.append((iid, ci, a, b))
    return bad


def _repair_copy_overlaps(result, items):
    """兜底：移除同一副本上较晚的交叠分配并记录冲突，保证返回结果无重复分配。"""
    for iid, cps in result["copies"].items():
        it = items.get(iid)
        name = it["name"] if it else f"#{iid}"
        for cp in cps:
            kept = []
            for iv in sorted(cp, key=lambda x: (x[0], x[1])):
                if kept and iv[0] < kept[-1][1]:
                    tid = _iv_task_id(iv)
                    result["conflicts"].append({
                        "type": "item", "task_id": tid, "time": iv[0],
                        "message": f"缺件：{name} 副本占用交叠，已取消较晚的重复分配"})
                    continue
                kept.append(iv)
            cp[:] = kept


def _iv_task_id(iv):
    try:
        return int(str(iv[2]).split(":")[1]) if str(iv[2]).startswith("task:") else 0
    except (IndexError, ValueError):
        return 0


def _schedule_once(state, tasks, tasks_by_id, scenes, items, positions, segs,
                   doff_actuals, maps):
    """单轮排程。doff_actuals: {(task_id,item_id): 实际脱下结束时刻}（上一轮结果）。

    人员占用按动作登记：dresser_busy[d] = [(s,e,task_id,end_point,action_key)]，
    下一动作者需从上一动作结束点步行赶到本动作点（跨侧台计入步行时间）。
    """
    (_, _, dressers, skills, dresser_sk, dresser_sides,
     unavail, _review_by) = maps
    actor_busy = defaultdict(list)     # actor_id -> [(s,e,task_id)]
    dresser_busy = defaultdict(list)   # dresser_id -> [(s,e,tid,end_pt,key)]
    pos_busy = defaultdict(list)       # position_id -> [(s,e,task_id)]

    # 服装副本日历：清洁/维修不可用区间 + 开场前已穿着的区间
    copies = {}
    for it in state["items"]:
        cps = [[] for _ in range(max(1, it["copies"]))]
        if it["status"] != "ok" and it["available_at"] > 0:
            for cp in cps:
                cp.append([0, it["available_at"], "status"])
        copies[it["id"]] = cps
    segs = segs if segs is not None else _wear_segments(state, tasks)
    actions, windows, conflicts = [], {}, []

    # 每个穿上动作在本轮的释放点（随定点迭代更新为实际值）
    for t in tasks:
        for a in t["_acts"]:
            if a["kind"] == "don" and a.get("item_id") is not None:
                seg = next((g for g in segs.get((t["actor_id"], a["item_id"]), [])
                            if g["start_scene"] == t["to_scene_id"]), None)
                a["_release"] = _seg_release(seg, tasks_by_id, scenes, items,
                                             doff_actuals) if seg \
                    else _scene_end(state, t["to_scene_id"])

    worn = {}                          # (actor,item) -> (副本下标, 区间)
    for (aid, iid), lst in segs.items():
        for seg in lst:
            if seg["don_task"] is None:    # 无穿上任务：从穿着首场的开场起占用
                rel = _seg_release(seg, tasks_by_id, scenes, items, doff_actuals)
                sc = scenes.get(seg["start_scene"])
                start0 = sc["start_sec"] if sc else 0
                pick = _pick_copy(copies[iid], start0, rel)
                if pick is None or not pick[1]:
                    it = items.get(iid)
                    conflicts.append({
                        "type": "item", "task_id": 0, "time": start0,
                        "message": f"缺件：{it['name'] if it else iid} 开场穿着无可用副本（副本不足）"})
                    continue
                idx = pick[0]
                iv = [start0, rel, f"init:a{aid}"]
                copies[iid][idx].append(iv)
                worn[(aid, iid)] = (idx, iv)

    for t in tasks:
        tid = t["id"]
        aid = t["actor_id"]
        win = t["_win"]
        s0 = t["_desired"]
        # 首次受阻根因：只记录第一次布局尝试时发现的问题。后续尝试通过顺延
        # 解决冲突（最终布局无冲突），但根因要保留下来定位「最早受阻动作」。
        root_staff_notes, root_item_notes, root_arrival_notes = [], [], []
        root_capacity = None
        laid = None
        for _try in range(80):
            # 1) 顺序布局：依次检查副本、演员、每位参与者的到岗/不可用/步行
            item_wait_notes, staff_arrival_notes = [], []
            static_notes, static_ok = [], True
            cur = s0
            laid = []
            max_shift = 0
            record_root = _try == 0   # 根因只取期望时刻的首轮布局
            for a0 in t["_acts"]:
                a = dict(a0)
                ai = len(laid)
                a["action_idx"] = ai
                key = action_key(a)
                a["start"] = cur
                if a["kind"] == "don" and a.get("item_id") is not None:
                    iid = a["item_id"]
                    it = items.get(iid)
                    rel = max(a.get("_release", 0), cur + a["dur"])
                    t1 = _copy_gap_abs(copies[iid], cur, rel)
                    if t1 is None:
                        # 穿着全程无可行副本：退化为仅保证穿上动作本身不重叠，
                        # 提交阶段若仍无法全程分配则拒绝（不产生重复分配）
                        t2 = _copy_gap_dur(copies[iid], cur, a["dur"])
                        item_wait_notes.append({
                            "type": "item", "task_id": tid, "action_idx": ai,
                            "time": t2, "message": f"缺件：{a['label']} 穿着期间无连续可用副本"})
                        a["start"] = t2
                        if record_root:
                            root_item_notes.append(item_wait_notes[-1])
                    elif t1 > cur:
                        why = "清洁/维修中" if it and it["status"] != "ok" \
                            else "复用等待（前一位演员尚未脱下）"
                        item_wait_notes.append({
                            "type": "item", "task_id": tid, "action_idx": ai,
                            "time": t1, "message": f"缺件/复用冲突：{a['label']} 需等到 "
                                                   f"{fmt(t1)}（{why}）"})
                        a["start"] = t1
                        if record_root:
                            root_item_notes.append(item_wait_notes[-1])
                cur = a["start"]
                a["end"] = cur + a["dur"]

                staff = resolve_staffing(state, t, a, maps, overrides=t.get("_staff_ov"))
                # 静态资格问题（不可顺延解决）：技能/侧台/人数
                vnotes = staffing_violations(state, t, a, staff, maps)
                for m in vnotes:
                    note = {
                        "type": "staffing", "task_id": tid,
                        "action_idx": a["action_idx"],
                        "time": a["start"], "action_label": a["label"],
                        "message": f"{a['label']}：{m}"}
                    static_notes.append(note)
                    if record_root:
                        root_staff_notes.append(note)
                if vnotes:
                    static_ok = False

                apt = point_of_action(t, a, state, positions)
                # 每位参与者：动作级占用 / 不可用时段 / 跨侧台步行 → 集体到岗时刻
                action_shift = 0
                arrival_info = None
                for did in staff["ids"]:
                    ready, blocker, prev_end = dresser_ready(
                        did, cur, a["dur"], apt, dresser_busy[did],
                        unavail.get(did, []), maps)
                    if ready > cur:
                        if ready - cur > action_shift:
                            action_shift = ready - cur
                            arrival_info = (did, blocker, prev_end, ready)
                if action_shift:
                    did, blocker, prev_end, ready = arrival_info
                    dname = dressers[did]["name"] if did in dressers else f"#{did}"
                    if blocker and blocker[0] == "unavail":
                        msg = (f"到岗过晚：{dname} 处于不可用时段至 {fmt(prev_end)}，"
                               f"{a['label']} 最早 {fmt(ready)} 开始")
                    elif blocker:
                        bt = tasks_by_id.get(blocker[2])
                        blabel = f"任务#{blocker[2]}"
                        if bt:
                            bkey = blocker[4] if len(blocker) > 4 else None
                            ba = next((x for x in bt["_acts"] if action_key(x) == bkey), None)
                            if ba:
                                blabel = f"「{ba['label']}」"
                        walk_n = ready - prev_end
                        cross = f"，跨侧台步行 {walk_n}s" if walk_n else ""
                        msg = (f"到岗过晚：{dname} 在 {fmt(prev_end)} 才结束{blabel}{cross}，"
                               f"{a['label']} 最早 {fmt(ready)} 开始")
                    else:
                        msg = f"到岗过晚：{dname} 最早 {fmt(ready)} 到岗"
                    staff_arrival_notes.append({
                        "type": "arrival", "task_id": tid, "action_idx": ai,
                        "time": ready, "action_label": a["label"], "message": msg})
                    if record_root:
                        root_arrival_notes.append(staff_arrival_notes[-1])
                    max_shift = max(max_shift, action_shift)
                a["staff"] = staff
                a["point"] = apt
                laid.append(a)
                cur = a["end"]

            start, end = laid[0]["start"], laid[-1]["end"]
            occ_s, occ_e = laid[0]["end"], laid[-1]["start"]
            if t["locked"]:
                # 锁定任务不移动：与其它占用的重叠只记录为冲突
                blk = _overlap(actor_busy[aid], start, end)
                if blk:
                    staff_arrival_notes.append({"type": "actor", "task_id": tid,
                        "action_idx": 0, "time": start,
                        "message": f"锁定任务与任务#{blk[2]}的演员时间重叠"})
                for a in laid:
                    for did in a["staff"]["ids"]:
                        blk = _overlap2(dresser_busy[did], start, end)
                        if blk:
                            dn = dressers[did]["name"] if did in dressers else f"#{did}"
                            staff_arrival_notes.append({
                                "type": "arrival", "task_id": tid,
                                "action_idx": a["action_idx"],
                                "time": start,
                                "message": f"锁定任务：{dn} 与任务#{blk[2]}的动作时间重叠"})
                break
            blk = _overlap(actor_busy[aid], start, end)
            if blk:
                s0 = max(s0 + 1, blk[1])
                continue
            if max_shift:
                s0 += max_shift
                continue
            if t["position_id"]:
                cap = positions[t["position_id"]]["capacity"] \
                    if t["position_id"] in positions else 1
                shift = _capacity_shift(pos_busy[t["position_id"]], cap, occ_s, occ_e)
                if shift:
                    note = {"type": "position", "task_id": tid,
                            "action_idx": 0, "time": occ_s + shift,
                            "message": f"换装位容量不足，顺延 {shift}s"}
                    staff_arrival_notes.append(note)
                    if record_root and root_capacity is None:
                        root_capacity = note
                    s0 += shift
                    continue
            break

        start, end = laid[0]["start"], laid[-1]["end"]
        occ_s, occ_e = laid[0]["end"], laid[-1]["start"]

        # 2) 提交资源占用
        actor_busy[aid].append((start, end, tid))
        if t["position_id"]:
            pos_busy[t["position_id"]].append((occ_s, occ_e, tid))
        item_commit_notes = []
        for a in laid:
            iid = a.get("item_id")
            # 服装师动作级日历（区间结束点用于下一动作的步行衔接）
            for did in a["staff"]["ids"]:
                dresser_busy[did].append(
                    (a["start"], a["end"], tid, a["point"], action_key(a)))
            if iid is None:
                continue
            if a["kind"] == "don":
                rel = max(a.get("_release", a["end"]), a["end"])
                pick = _pick_copy(copies[iid], a["start"], rel)
                if pick is None or not pick[1]:
                    # 无连续可用副本：拒绝分配，绝不放置交叠区间
                    item_commit_notes.append({
                        "type": "item", "task_id": tid, "action_idx": a["action_idx"],
                        "time": a["start"], "action_label": a["label"],
                        "message": f"缺件：{a['label']} 穿着期间无连续可用副本，未分配"})
                    continue
                idx = pick[0]
                iv = [a["start"], rel, f"task:{tid}"]
                copies[iid][idx].append(iv)
                worn[(aid, iid)] = (idx, iv)
            elif a["kind"] == "doff":
                it = items.get(iid)
                extra = CLEAN_SEC if it and it["status"] == "cleaning" else 0
                wkey = (aid, iid)
                if wkey in worn:
                    _, iv = worn.pop(wkey)
                    iv[1] = a["end"] + extra   # 副本实际释放时刻 = 脱下动作结束(+清洁)
                # 无对应穿着记录（数据缺造型或该次穿上未分配）：不产生区间

        # 3) 截止检查：第一个超出截止时间的动作
        fail = next((a for a in laid if a["end"] > win["deadline"]), None)
        action_review_notes = []
        review_by = maps[7]
        for a in laid:
            rv = review_by.get((tid, a["kind"], a.get("item_id"), a.get("seq", 0)))
            if rv:
                a["needs_review"] = True
                action_review_notes.append({
                    "type": "action_review", "task_id": tid,
                    "action_idx": a["action_idx"], "time": a["start"],
                    "action_label": a["label"],
                    "message": f"{a['label']}：{rv['reason'] or '相关资料变更，待复核'}"})
        for a in laid:
            a["task_id"] = tid
        actions.extend(laid)
        windows[tid] = {"start": start, "end": end, "ready": win["ready"],
                        "deadline": win["deadline"], "ok": fail is None}
        if t["needs_review"]:
            conflicts.append({"type": "review", "task_id": tid, "action_idx": 0,
                              "time": start, "message": "关联场次/道具有变更，待复核"})
        conflicts.extend(action_review_notes)
        if t["locked"]:
            # 锁定任务不移动：报本轮实际检测到的重叠
            conflicts.extend(staff_arrival_notes)
        else:
            # 可移动任务：报首次受阻根因（最终布局已顺延时当前轮无冲突）
            conflicts.extend(root_staff_notes)
            conflicts.extend(root_arrival_notes)
            if root_capacity:
                conflicts.append(root_capacity)
        conflicts.extend(root_item_notes)
        conflicts.extend(item_commit_notes)
        if fail:
            conflicts.append({
                "type": "late", "task_id": tid,
                "action_idx": fail["action_idx"], "time": fail["start"],
                "action_label": fail["label"],
                "message": f"最早无法按时完成的动作：{fail['label']}"
                           f"（预计 {fmt(fail['end'])}，截止 {fmt(win['deadline'])}）"})

    actions.sort(key=lambda a: (a["start"], a["task_id"], a["action_idx"]))
    conflicts.sort(key=lambda c: (c["time"], c["task_id"], c.get("action_idx", 0)))
    # 人员动作日历供泳道/交接/侧台图使用
    dresser_intervals = {d: sorted(v, key=lambda iv: iv[0])
                         for d, v in dresser_busy.items()}
    return {"actions": actions, "windows": windows, "conflicts": conflicts,
            "copies": copies, "dresser_intervals": dresser_intervals}


def _overlap2(ivs, s, e):
    """开放区间相交（[a,b) 与 [b,c) 不算冲突，允许动作交接背靠背）。"""
    for iv in ivs:
        if iv[0] < e and iv[1] > s:
            return iv
    return None


def suggest(state, max_options=3):
    """针对冲突，试算改动较少的替代排法（不动已锁任务/已锁分工）。

    优先处理最早受阻动作：
    - 换装位/开始时刻：枚举换装位与 ±15/30 秒顺延；
    - 动作级分工冲突（技能不符/侧台/人数不足/到岗过晚/人员重叠）：
      为受阻动作枚举合格服装师组合（含双人同步所需多人），
      只动最少的动作分工。
    """
    base = compute_schedule(state)
    blocking = [c for c in base["conflicts"]
                if c["type"] in ("late", "dresser", "position", "item",
                                 "staffing", "arrival")]
    if not blocking:
        return []
    base_n = len([c for c in base["conflicts"] if c["type"] != "review"])
    bad_tasks = sorted({c["task_id"] for c in blocking if c["task_id"]},
                       key=lambda tid: min(c["time"] for c in blocking
                                           if c["task_id"] == tid))
    tasks = {t["id"]: t for t in state["tasks"]}
    maps = _staffing_maps(state)
    suggestions = []
    for tid in bad_tasks:
        t = tasks[tid]
        tconf = [c for c in blocking if c["task_id"] == tid]
        earliest = min(tconf, key=lambda c: (c["time"], c.get("action_idx", 0)))
        best = None

        def consider(ov, changes_dict, change_n, staff_changes=None):
            nonlocal best
            res = compute_schedule(state, overrides=ov)
            n_conf = len([c for c in res["conflicts"] if c["type"] != "review"])
            task_conf = len([c for c in res["conflicts"]
                             if c["task_id"] == tid and c["type"] != "review"])
            score = (n_conf, change_n, task_conf)
            cand = {"task_id": tid, "changes": changes_dict,
                    "staff_changes": staff_changes or [],
                    "remaining_conflicts": task_conf,
                    "reason": (earliest["message"]
                               if earliest["type"] in ("staffing", "arrival")
                               else (earliest.get("action_label") or ""))}
            if best is None or score < best[0]:
                best = (score, cand)

        staff_types = {"staffing", "arrival", "dresser"}
        # 到岗/资格类受阻，或仅有超时（可能由跨侧台步行引起）：尝试换人
        if any(c["type"] in staff_types for c in tconf) or \
                all(c["type"] in ("late",) for c in tconf):
            acts, _win = build_actions(t, state)
            idxs = sorted({c.get("action_idx", 0) for c in tconf
                           if c["type"] in staff_types})
            if not idxs:
                # 仅超时时：尝试全部需人动作里最早的
                idxs = [i for i, a in enumerate(acts) if resolve_staffing(
                    state, t, a, maps)["ids"]]
            for bi in idxs[:2]:
                if bi >= len(acts):
                    continue
                a = acts[bi]
                staff = resolve_staffing(state, t, a, maps)
                if not staff["ids"] or staff["locked"]:
                    continue
                side = action_side(t, a, state)
                eligible = [d["id"] for d in state["dressers"]
                            if _eligible(d["id"], side, staff["required_skill"], maps)
                            and d["id"] not in staff["ids"]]
                cur = staff["ids"]
                need = max(staff["required"], len(cur), 1)
                # 换人优先：保留固定负责人与其余搭档，只替换受阻的人；
                # 其次枚举合格组合（人数不变，差异最小）。
                combos = _staff_combos(eligible, need, keep=staff["lead_id"],
                                       current=cur, prefer_current=True)
                for ids in combos[:10]:
                    if set(ids) == set(cur):
                        continue
                    ov = {"__staff__": {(tid,) + action_key(a): list(ids)}}
                    change_n = _staff_change_count(cur, ids)
                    consider(ov, {}, change_n,
                             [{"kind": a["kind"], "item_id": a.get("item_id"),
                               "seq": a.get("seq", 0), "dresser_ids": list(ids)}])
                if best and best[0][0] == 0:
                    break

        # 换装位/时刻维度（已锁任务不移动，只允许换人）
        if t["locked"]:
            if best:
                suggestions.append(best[1])
            if len(suggestions) >= max_options:
                break
            continue
        positions = state["positions"] or [None]
        for pos in positions:
            pid_ = pos["id"] if pos else None
            base_start = t["start_sec"] if t["start_sec"] is not None \
                else base["windows"].get(tid, {}).get("start") or \
                _scene_end(state, t["from_scene_id"])
            for shift in (0, -15, -30, 15, 30):
                new_start = max(0, int(base_start) + shift)
                changes = {"start_sec": new_start}
                change_n = int(shift != 0)
                if pid_ is not None and pid_ != t["position_id"]:
                    changes["position_id"] = pid_
                    change_n += 1
                ov = {tid: {"position_id": pid_, "start_sec": new_start}}
                consider(ov, changes, change_n)
        if best and best[0][0] < base_n:
            suggestions.append(best[1])
        elif best and not any(s["task_id"] == tid for s in suggestions):
            # 即便总冲突数没降（级联冲突），只要本任务冲突减少也给出
            if best[1]["remaining_conflicts"] < len(tconf):
                suggestions.append(best[1])
        if len(suggestions) >= max_options:
            break
    return suggestions


def _eligible(dr_id, side, skill_id, maps):
    _, _, dressers, skills, dresser_sk, dresser_sides, _, _rev = maps
    sides = dresser_sides.get(dr_id)
    if sides and side not in sides:
        return False
    if skill_id and skill_id not in dresser_sk.get(dr_id, set()):
        return False
    return dr_id in dressers


def _staff_combos(eligible, need, keep=None, current=None, prefer_current=False):
    """按「与现状差异最小」排序的合格组合。

    prefer_current=True（换人）：只从非当前人员中挑替换者，
    保留固定负责人与其余搭档；need 按当前人数补齐。
    否则：1) 现组合已合格；2) 保留固定负责人补齐；3) 其他合格人员。
    """
    import itertools
    current = [c for c in (current or [])]
    cur_set = set(current)
    out, seen = [], set()

    def add(ids):
        ids = tuple(dict.fromkeys(int(x) for x in ids))
        if len(ids) < need or ids in seen:
            return
        seen.add(ids)
        out.append(list(ids))

    if prefer_current:
        # 必须至少包含一名非现任者（真正换人）：保留 0..need-1 名现任，
        # 其余从合格者补齐；保留时负责人优先。
        free = list(dict.fromkeys(d for d in eligible if d not in cur_set))
        ordered_cur = ([keep] if keep in cur_set else []) + \
            [x for x in current if x != keep]
        for n_keep in range(min(len(ordered_cur), need - 1), -1, -1):
            for kept in itertools.combinations(ordered_cur, n_keep):
                n_free = need - len(kept)
                if n_free < 1 or len(free) < n_free:
                    continue
                for combo in itertools.combinations(free, n_free):
                    add(tuple(kept) + combo)
        out.sort(key=lambda ids: _staff_change_count(current, ids))
        return out

    eligible_all = list(dict.fromkeys(list(current) + list(eligible)))
    if len(cur_set) >= need:
        add(current[:need])
    if keep and keep in eligible_all:
        rest = [d for d in eligible_all if d != keep]
        for r in range(max(0, need - 1), min(len(rest), need - 1) + 1):
            for combo in itertools.combinations(rest, r):
                add((keep,) + combo)
    for r in range(need, min(len(eligible_all), need + 1) + 1):
        for combo in itertools.combinations(eligible_all, r):
            add(combo)
    out.sort(key=lambda ids: _staff_change_count(current, ids))
    return out


def _staff_change_count(old, new):
    old, new = set(old or []), set(new or [])
    return len(old.symmetric_difference(new))


def fmt(sec):
    sec = int(sec)
    return f"{sec//60:02d}:{sec % 60:02d}"
