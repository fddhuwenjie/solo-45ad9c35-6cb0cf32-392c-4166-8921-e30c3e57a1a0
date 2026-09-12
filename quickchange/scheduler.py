"""快速换装排程引擎。

计入：动作先后（脱外层→脱内层→穿内层→穿外层→取道具）、人员并发
（演员/服装师同一时刻只能做一个动作）、换装位容量、步行时间、
服装复用（一件服装被多人依次使用）以及清洁/维修状态。
已锁任务位置固定，不得移动。
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

    acts = [{"kind": "walk", "label": "退场→换装位", "dur": walk_sec(exit_pt, pos_pt)}]
    for it in doff:
        acts.append({"kind": "doff", "label": f"脱·{it['name']}", "dur": it["doff_sec"],
                     "item_id": it["id"], "needs_dresser": it["kind"] != "prop"})
    for it in don_wear:
        acts.append({"kind": "don", "label": f"穿·{it['name']}", "dur": it["don_sec"],
                     "item_id": it["id"], "needs_dresser": True})
    for it in props:
        cart = carts.get(it["cart_id"]) if it["cart_id"] else None
        cart_pt = (cart["x"], cart["y"]) if cart else pos_pt
        acts.append({"kind": "fetch", "label": f"取道具·{it['name']}",
                     "dur": walk_sec(pos_pt, cart_pt) * 2 + PROP_HANDLE_SEC,
                     "item_id": it["id"], "needs_dresser": False})
    acts.append({"kind": "walk", "label": "换装位→上场口", "dur": walk_sec(pos_pt, exit_pt)})

    window = {
        "ready": from_sc["start_sec"] + from_sc["duration_sec"],  # 演员退出上一场
        "deadline": to_sc["start_sec"],                            # 下一场开场
    }
    return acts, window


def compute_schedule(state, overrides=None):
    """对全部任务排程。

    overrides: {task_id: {"start_sec":..,"position_id":..,"dresser_id":..}} 临时改动，
    用于“替代排法”试算，不写库。
    返回 {actions, windows, conflicts, unschedulable}
    """
    overrides = overrides or {}
    prime_caches(state)
    tasks = []
    for t in state["tasks"]:
        t = dict(t)
        ov = overrides.get(t["id"])
        if ov:
            t.update({k: v for k, v in ov.items() if v is not None})
        tasks.append(t)

    # 已锁任务先占位，其余按上一场结束时间排序
    tasks.sort(key=lambda t: (0 if t["locked"] else 1,
                              t["start_sec"] if t["locked"] and t["start_sec"] is not None else 0,
                              _scene_end(state, t["from_scene_id"]), t["id"]))

    actor_free = defaultdict(int)
    dresser_free = defaultdict(int)          # dresser_id -> time
    dresser_busy_by = {}                     # dresser_id -> task_id
    pos_intervals = defaultdict(list)        # position_id -> [(start,end,task_id)]
    item_free = defaultdict(list)            # item_id -> [可用时刻,...]（按 copies）
    item_blocker = {}
    for it in state["items"]:
        t0 = 0 if it["status"] == "ok" else it["available_at"]
        item_free[it["id"]] = [t0] * max(1, it["copies"])

    actions, windows, conflicts = [], {}, []

    for t in tasks:
        tid = t["id"]
        acts, win = build_actions(t, state)
        dur_total = sum(a["dur"] for a in acts)
        ready = win["ready"]
        deadline = win["deadline"]
        if t["locked"] and t["start_sec"] is not None:
            s0 = t["start_sec"]
        elif t["start_sec"] is not None:
            s0 = max(ready, t["start_sec"])
        else:
            s0 = ready

        placed = None
        trial = s0
        for _ in range(80):
            res = _place(t, acts, trial, ready, deadline, actor_free, dresser_free,
                         pos_intervals, item_free)
            if res["ok"]:
                placed = res
                break
            if res["reason"] == "late":
                placed = res  # 记录最晚尝试，供冲突定位
                break
            trial = res["retry_at"]
        if placed is None:
            placed = res

        # 提交资源占用
        if placed["ok"] or True:
            for a in placed["actions"]:
                if a.get("item_id") is not None and a["kind"] == "don":
                    frees = item_free[a["item_id"]]
                    idx = min(range(len(frees)), key=lambda i: frees[i])
                    frees[idx] = a["end"]  # 穿上后该件被占用，直到脱下动作更新
                if a.get("item_id") is not None and a["kind"] == "doff":
                    it = _item(state, a["item_id"])
                    frees = item_free[a["item_id"]]
                    idx = max(range(len(frees)), key=lambda i: frees[i])
                    extra = CLEAN_SEC if it and it["status"] == "cleaning" else 0
                    frees[idx] = a["end"] + extra
            actor_free[tid_actor(t)] = placed["end"]
            if t["dresser_id"]:
                dresser_free[t["dresser_id"]] = placed["end"]
                dresser_busy_by[t["dresser_id"]] = tid
            if t["position_id"]:
                pos_intervals[t["position_id"]].append(
                    (placed["occupy_start"], placed["occupy_end"], tid))

        for a in placed["actions"]:
            a["task_id"] = tid
        actions.extend(placed["actions"])
        windows[tid] = {"start": placed["start"], "end": placed["end"],
                        "ready": ready, "deadline": deadline, "ok": placed["ok"]}

        if t["needs_review"]:
            conflicts.append({"type": "review", "task_id": tid, "time": placed["start"],
                              "message": "关联场次/道具有变更，待复核"})
        if not placed["ok"]:
            bad = placed["fail_action"]
            conflicts.append({
                "type": "late", "task_id": tid, "time": bad["start"],
                "message": f"最早无法按时完成的动作：{bad['label']}"
                           f"（预计 {fmt(bad['end'])}，截止 {fmt(deadline)}）"})
        conflicts.extend(placed["notes"])

    actions.sort(key=lambda a: (a["start"], a["task_id"]))
    conflicts.sort(key=lambda c: (c["time"], c["task_id"]))
    return {"actions": actions, "windows": windows, "conflicts": conflicts}


def tid_actor(t):
    return t["actor_id"]


def _item(state, item_id):
    for it in state["items"]:
        if it["id"] == item_id:
            return it
    return None


def _scene_end(state, scene_id):
    for s in state["scenes"]:
        if s["id"] == scene_id:
            return s["start_sec"] + s["duration_sec"]
    return 0


def _place(t, acts, s0, ready, deadline, actor_free, dresser_free, pos_intervals, item_free):
    """尝试把任务放在开始时刻 s0。返回放置结果或失败原因与可重试时刻。"""
    actor_id = tid_actor(t)
    notes = []
    cur = max(s0, ready, actor_free[actor_id])
    if cur > s0:
        notes.append({"type": "actor", "task_id": t["id"], "time": cur,
                      "message": "演员上一动作未结束，顺延"})
    placed = []
    for a in acts:
        a = dict(a)
        a["start"] = cur
        # 服装师并发：整个任务期间独占一位服装师（简化：任务级占用）
        if a.get("item_id") is not None and a["kind"] == "don":
            frees = item_free[a["item_id"]]
            earliest = min(frees)
            if earliest > a["start"]:
                it = _item_cache(a["item_id"])
                why = "清洁/维修中" if it and it["status"] != "ok" else "复用等待（前一位演员尚未脱下）"
                notes.append({"type": "item", "task_id": t["id"], "time": earliest,
                              "message": f"缺件/复用冲突：{a['label']} 需等到 {fmt(earliest)}（{why}）"})
                a["start"] = earliest
        a["end"] = a["start"] + a["dur"]
        cur = a["end"]
        placed.append(a)

    # 服装师任务级占用
    if t["dresser_id"]:
        d_free = dresser_free[t["dresser_id"]]
        if d_free > placed[0]["start"]:
            shift = d_free - placed[0]["start"]
            notes.append({"type": "dresser", "task_id": t["id"], "time": d_free,
                          "message": f"服装师并发冲突：需等到 {fmt(d_free)}（正照看另一人）"})
            for a in placed:
                a["start"] += shift
                a["end"] += shift

    # 换装位容量：占用区间 = 到达换装位 → 离开换装位
    occupy_start = placed[0]["end"]            # 走完“退场→换装位”
    occupy_end = placed[-1]["start"]           # 开始走回上场口
    if t["position_id"]:
        cap = _pos_cap_cache(t["position_id"])
        intervals = pos_intervals[t["position_id"]]
        shift = _capacity_shift(intervals, cap, occupy_start, occupy_end)
        if shift > 0:
            notes.append({"type": "position", "task_id": t["id"], "time": occupy_start + shift,
                          "message": f"换装位容量不足，顺延 {shift}s"})
            for a in placed:
                a["start"] += shift
                a["end"] += shift
            occupy_start += shift
            occupy_end += shift

    # 截止检查：找出第一个超出截止时间的动作
    fail = None
    for a in placed:
        if a["end"] > deadline:
            fail = a
            break
    if fail:
        # 若整体顺延可解（资源占用导致），给出重试时刻；否则判定无法按时
        slack_needed = fail["end"] - deadline
        retry = s0 + max(1, slack_needed)
        if s0 <= ready and retry <= s0:
            retry = s0 + 5
        return {"ok": False, "reason": "late", "retry_at": retry,
                "actions": placed, "fail_action": fail,
                "start": placed[0]["start"], "end": placed[-1]["end"],
                "occupy_start": occupy_start, "occupy_end": occupy_end,
                "notes": notes}
    return {"ok": True, "actions": placed,
            "start": placed[0]["start"], "end": placed[-1]["end"],
            "occupy_start": occupy_start, "occupy_end": occupy_end,
            "notes": notes}


_ITEM_CACHE = {}
_POS_CACHE = {}


def _item_cache(item_id):
    return _ITEM_CACHE.get(item_id)


def _pos_cap_cache(pos_id):
    return _POS_CACHE.get(pos_id, 1)


def prime_caches(state):
    _ITEM_CACHE.clear()
    _ITEM_CACHE.update({i["id"]: i for i in state["items"]})
    _POS_CACHE.clear()
    _POS_CACHE.update({p["id"]: p["capacity"] for p in state["positions"]})


def _capacity_shift(intervals, cap, s, e):
    """若 [s,e) 内并发数超容量，返回需要顺延的秒数。"""
    for _ in range(40):
        overlap = [iv for iv in intervals if iv[0] < e and iv[1] > s]
        if len(overlap) < cap:
            return 0
        free_at = min(iv[1] for iv in overlap)
        shift = free_at - s
        if shift <= 0:
            shift = 1
        return shift if shift > 0 else 0
    return 0


def suggest(state, max_options=3):
    """针对冲突任务，试算改动较少的替代排法（不动已锁任务）。"""
    base = compute_schedule(state)
    base_n = len([c for c in base["conflicts"] if c["type"] != "review"])
    bad_tasks = sorted({c["task_id"] for c in base["conflicts"] if c["type"] in
                        ("late", "dresser", "position", "item")})
    if not bad_tasks:
        return []
    tasks = {t["id"]: t for t in state["tasks"]}
    suggestions = []
    for tid in bad_tasks:
        t = tasks[tid]
        if t["locked"]:
            continue
        best = None
        for pos in state["positions"]:
            for dr in ([None] + [d["id"] for d in state["dressers"]]):
                for shift in (0, -15, -30, 15, 30):
                    ov = {tid: {"position_id": pos["id"], "dresser_id": dr,
                                "start_sec": max(0, (t["start_sec"] or _scene_end(state, t["from_scene_id"])) + shift)}}
                    res = compute_schedule(state, overrides=ov)
                    n_conf = len([c for c in res["conflicts"] if c["type"] != "review"])
                    task_conf = len([c for c in res["conflicts"]
                                     if c["task_id"] == tid and c["type"] != "review"])
                    changes = int(pos["id"] != t["position_id"]) + int(dr != t["dresser_id"]) + int(shift != 0)
                    score = (n_conf, changes)
                    if best is None or score < best[0]:
                        best = (score, {"task_id": tid,
                                        "changes": {"position_id": pos["id"], "dresser_id": dr,
                                                    "start_sec": ov[tid]["start_sec"]},
                                        "remaining_conflicts": task_conf})
            if best and best[0][0] == 0 and best[0][1] <= 1:
                break
        if best and best[0][0] < base_n:
            suggestions.append(best[1])
        if len(suggestions) >= max_options:
            break
    return suggestions


def fmt(sec):
    sec = int(sec)
    return f"{sec//60:02d}:{sec % 60:02d}"
