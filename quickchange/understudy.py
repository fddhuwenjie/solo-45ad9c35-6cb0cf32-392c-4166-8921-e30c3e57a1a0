# -*- coding: utf-8 -*-
"""替演推演：午场前临时换角的副本适配与换装重排。

输入：基准修订快照（场次/任务/服装/造型/人员…）、候补顺位、演员关键尺寸、
实物副本的适配区间与可调部位、改衣耗时；外加一张逐任务的拖换卡司
{task_id: under_actor_id}。

计算：
1. 为各造型的每件穿着类服装在实物副本中挑选可用副本——按关键尺寸逐部位
   判定「直接合身 / 需改衣 / 越界」，尺寸缺失单独报出；
2. 按尺寸偏差与闭合件类型调整该演员该件的穿/脱时长（系带、盘扣更费时）；
3. 把人工改派的副本设为严格占用、自动分配在分支内互斥占位，交 scheduler
   重排，动作分工与侧台走位沿用既有引擎；
4. 汇总硬冲突（尺寸缺失、适配越界、演员场次重叠、副本并发占用、改衣赶不上
   开场、无法按时完成）与需备注决定（边界尺寸、人工改派、需改衣）。

硬冲突或未备注决定存在时 build_branch() 返回 can_confirm=False，页面禁止确认。
"""
import copy as _copy
import json
from collections import defaultdict

import scheduler

# 关键尺寸（厘米；foot 为脚长）
DIMS = (("height", "身高"), ("chest", "胸围"), ("waist", "腰围"),
        ("hip", "臀围"), ("shoulder", "肩宽"), ("foot", "脚长"))
DIM_KEYS = tuple(k for k, _ in DIMS)
SHOE_DIMS = ("foot",)
# 部位偏差每厘米增加的穿/脱秒数（替代演员与原角尺寸不同时，穿脱更费时）
DEV_SEC_PER_CM = 1.0
DEV_MAX_SEC = 12
# 闭合件 → (穿上额外秒, 脱下额外秒)；zip 用服装默认
CLOSURE_EXTRA = {
    "zip": (0, 0), "hook": (4, 3), "tie": (10, 8), "frog": (14, 10),
}
CLOSURE_CN = {"zip": "拉链", "hook": "钩扣", "tie": "系带", "frog": "盘扣"}


def _index(lst):
    return {r["id"]: r for r in lst}


def measures_map(rows):
    return {r["actor_id"]: r for r in (rows or [])}


def copy_fit_maps(copies_rows, fit_rows):
    """copy_id -> {dim: fit_row}；item_id -> [copy_row,...]（按 copy_no）。"""
    by_copy, by_item = {}, {}
    for c in copies_rows or []:
        by_item.setdefault(c["item_id"], []).append(c)
    fits = {}
    for f in fit_rows or []:
        fits.setdefault(f["copy_id"], {})[f["dim"]] = f
    for v in by_item.values():
        v.sort(key=lambda c: c["copy_no"])
    return by_item, fits


def closure_of(item, copy_row):
    c = (copy_row or {}).get("closure") or item.get("closure") or "zip"
    return c if c in CLOSURE_EXTRA else "zip"


def eval_copy(item, copy_row, fit_rows_of_copy, actor_measures, dims):
    """逐部位判定一件实物副本对该演员的适配。

    返回 {direct,alter,out,missing,boundary,max_alter_sec}：
    - 无适配区间的部位：该副本不受限，不计入判定；
    - 有区间但演员尺寸缺失：missing（硬冲突）；
    - 在 [lo,hi] 内：direct（恰在端点再标 boundary，需备注）；
    - 越界但该部位可调：alter；不可调：out（硬冲突）。
    """
    res = {"direct": [], "alter": [], "out": [], "missing": [],
           "boundary": [], "max_alter_sec": 0}
    m = actor_measures or {}
    for d in dims:
        f = (fit_rows_of_copy or {}).get(d)
        if f is None:
            continue
        val = m.get(d) if m else None
        if val is None:
            res["missing"].append(d)
            continue
        if f["lo"] <= val <= f["hi"]:
            res["direct"].append(d)
            if abs(val - f["lo"]) < 1e-9 or abs(val - f["hi"]) < 1e-9:
                res["boundary"].append(d)
        elif f.get("alterable"):
            res["alter"].append(d)
            res["max_alter_sec"] = max(res["max_alter_sec"], int(f.get("alter_sec") or 0))
        else:
            res["out"].append(d)
    return res


def dim_cn(d):
    return dict(DIMS).get(d, d)


def duration_override(item, actor_measures, original_measures, copy_row, kind):
    """尺寸偏差 + 闭合件 → 单件穿/脱用时（秒）。"""
    base = item["don_sec"] if kind == "don" else item["doff_sec"]
    dims = SHOE_DIMS if item.get("kind") == "shoes" else tuple(
        d for d in DIM_KEYS if d != "foot")
    extra_dev = 0
    am, om = actor_measures or {}, original_measures or {}
    if am and om:
        dev = 0
        for d in dims:
            if am.get(d) is not None and om.get(d) is not None:
                dev += abs(float(am[d]) - float(om[d]))
        extra_dev = min(DEV_MAX_SEC, int(round(dev * DEV_SEC_PER_CM)))
    extra_cl = CLOSURE_EXTRA.get(closure_of(item, copy_row), (0, 0))[
        0 if kind == "don" else 1]
    return int(base + extra_dev + extra_cl), extra_dev, extra_cl


def _slack(fits_of_copy, am, dims):
    """尺寸在各区间内的最小余量（厘米）；越大越宽裕。"""
    if not am:
        return -1e9
    worst = 1e9
    for d in dims:
        f = (fits_of_copy or {}).get(d)
        if not f or am.get(d) is None:
            continue
        worst = min(worst, min(am[d] - f["lo"], f["hi"] - am[d]))
    return worst if worst < 1e8 else 1e8


def _check_cast_overlap(base, cast, scenes, hard):
    """同一候补被排在两个时间相交的场次：按其在场造型（looks）判重。"""
    under_ids = set(cast.values())
    if not under_ids:
        return
    intervals = defaultdict(list)   # actor_id -> [(start,end,task_id,scene_name)]
    # 演员在某场有造型即视为在该场登台；任务的起、止场都计入
    for t in base["tasks"]:
        for sid in (t["from_scene_id"], t["to_scene_id"]):
            if not any(l["actor_id"] == t["actor_id"] and l["scene_id"] == sid
                       for l in base["looks"]):
                continue
            sc = scenes[sid]
            intervals[t["actor_id"]].append(
                (sc["start_sec"], sc["start_sec"] + sc["duration_sec"],
                 t["id"], sc["name"], sid))
    for aid, ivs in intervals.items():
        if aid not in under_ids:
            continue
        # 同一场（同 sid）只保留一条：同场连戏不是重叠
        uniq = {}
        for iv in ivs:
            uniq[iv[4]] = iv
        ivs = sorted(uniq.values())
        for x, y in zip(ivs, ivs[1:]):
            if y[0] < x[1]:
                hard.append({
                    "time": y[0], "task_id": y[2], "action_idx": 0,
                    "type": "cast_overlap",
                    "message": f"演员场次重叠：同一候补在「{x[3]}」未下场就要赶「{y[3]}」"})


def _slim_action(a):
    st = a.get("staff", {}) or {}
    return {
        "task_id": a["task_id"], "action_idx": a.get("action_idx", 0),
        "kind": a["kind"], "label": a["label"], "dur": a["dur"],
        "item_id": a.get("item_id"), "seq": a.get("seq", 0),
        "start": int(a["start"]), "end": int(a["end"]),
        "staff_ids": list(st.get("ids", [])),
    }


def _slim_copies(sched_copies, chosen, swapped):
    """输出每个替演穿上动作实际落在的实物副本编号。"""
    out = []
    for iid, cps in sched_copies.items():
        for ci, ivs in enumerate(cps):
            for iv in ivs:
                if isinstance(iv[2], str) and iv[2].startswith("task:"):
                    tid = int(iv[2].split(":")[1])
                    if tid in swapped:
                        out.append({"task_id": tid, "item_id": iid,
                                    "copy_no": ci + 1, "start": iv[0], "end": iv[1]})
    out.sort(key=lambda x: (x["start"], x["task_id"]))
    return out


def _fit_decisions(chosen, copy_fits, measures, cast, state, items):
    """每件替演服装的适配明细（供换装单列出尺寸与判定）。"""
    actors = _index(state["actors"])
    rows = []
    for (tid, iid), c in sorted(chosen.items()):
        if c is None:
            continue
        aid = cast[tid]
        ev = eval_copy(items[iid], c, copy_fits.get(c["id"]),
                       measures.get(aid),
                       SHOE_DIMS if items[iid]["kind"] == "shoes"
                       else tuple(d for d in DIM_KEYS if d != "foot"))
        status = "越界" if ev["out"] else "需改衣" if ev["alter"] else "合身"
        rows.append({"task_id": tid, "item_id": iid, "copy_no": c["copy_no"],
                     "copy_id": c["id"], "status": status,
                     "direct": ev["direct"], "alter": ev["alter"],
                     "out": ev["out"], "boundary": ev["boundary"],
                     "missing": ev["missing"],
                     "actor_name": actors.get(aid, {}).get("name", f"#{aid}")})
    return rows


# ---------------- 分支构建 ----------------

def build_branch(state, cast, assigns=None, notes=None, alter_start_sec=0):
    """在 state（基准快照+当前替演资料）上按 cast 推演。

    cast: {task_id: under_actor_id}（只含被拖换的任务）
    assigns: {"<task_id>:<item_id>": item_copies.id} 人工改派
    返回分支完整试算结果（写入 branches.plan_json）。
    """
    assigns = assigns or {}
    notes = notes or {}
    items = _index(state["items"])
    actors = _index(state["actors"])
    scenes = _index(state["scenes"])
    copies_by_item, copy_fits = copy_fit_maps(state.get("item_copies"),
                                             state.get("copy_fit"))
    measures = measures_map(state.get("actor_measures"))

    base = _copy.deepcopy(state)
    base.pop("_duration_ov", None)
    dur_ov, pin_copy = {}, {}
    decisions, hard = [], []

    def add_hard(t, tid, msg, action_idx=0, kind="fit"):
        hard.append({"time": int(t), "task_id": tid, "action_idx": action_idx,
                     "type": kind, "message": msg})

    # 1) 拖换卡司：复制任务改演员，保留换装位/分工/出入口
    cast = {int(k): int(v) for k, v in cast.items() if int(v)}
    swapped = set(cast)
    for t in base["tasks"]:
        if t["id"] in cast:
            t["actor_id"] = cast[t["id"]]
    orig_of = {tid: next((tt["actor_id"] for tt in state["tasks"] if tt["id"] == tid), None)
               for tid in swapped}

    # 2) 场次重叠预检：同一候补在两个时间相交的场次中同时在台
    _check_cast_overlap(base, cast, scenes, hard)

    # 3) 收集每个拖换任务的「待穿服装」需求
    by_look = scheduler.look_items_map(base)
    need = {}
    for t in base["tasks"]:
        tid = t["id"]
        if tid not in swapped:
            continue
        aid = t["actor_id"]
        am, om = measures.get(aid), measures.get(orig_of[tid])
        prev_look = scheduler.find_look(base, aid, t["from_scene_id"])
        next_look = scheduler.find_look(base, aid, t["to_scene_id"])
        if next_look is None and prev_look is None:
            add_hard(scenes[t["to_scene_id"]]["start_sec"], tid,
                     f"候补{actors.get(aid, {}).get('name', '#'+str(aid))}"
                     f"在「{scenes[t['to_scene_id']]['name']}」无角色造型登记",
                     kind="no_look")
            continue
        prev_ids = {i["id"] for i in by_look.get(prev_look["id"], [])} if prev_look else set()
        next_ids = {i["id"] for i in by_look.get(next_look["id"], [])} if next_look else set()
        to_start = scenes[t["to_scene_id"]]["start_sec"]
        for it in by_look.get(next_look["id"], []) if next_look else []:
            if it["kind"] == "prop" or it["id"] in prev_ids:
                continue
            iid = it["id"]
            dims = SHOE_DIMS if it["kind"] == "shoes" else tuple(
                d for d in DIM_KEYS if d != "foot")
            scored = [(c, eval_copy(it, c, copy_fits.get(c["id"]), am, dims))
                      for c in copies_by_item.get(iid, [])]
            need[(tid, iid)] = {
                "item": it, "am": am, "om": om, "dims": dims,
                "to_start": to_start, "scored": scored,
                "manual": assigns.get(f"{tid}:{iid}"), "phase": "don",
            }
        # 仅脱下（上一场穿、本场不穿）：只取闭合件用于脱下加时，不占副本日历
        if prev_look is not None:
            for it in by_look.get(prev_look["id"], []):
                iid = it["id"]
                if iid in next_ids or it["kind"] == "prop" or (tid, iid) in need:
                    continue
                manual = assigns.get(f"{tid}:{iid}")
                if manual is None:
                    continue   # 自动脱下沿用第一件默认闭合件即可，无需逐件评估
                dims = SHOE_DIMS if it["kind"] == "shoes" else tuple(
                    d for d in DIM_KEYS if d != "foot")
                scored = [(c, eval_copy(it, c, copy_fits.get(c["id"]), am, dims))
                          for c in copies_by_item.get(iid, [])]
                need[(tid, iid)] = {
                    "item": it, "am": am, "om": om, "dims": dims,
                    "to_start": to_start, "scored": scored,
                    "manual": manual, "phase": "doff",
                }

    # 4) 副本分配：人工改派先固定（穿上严格占用；仅脱下项只用于闭合件加时）；
    #    自动件按就绪先后占位，同一实物副本在本分支内只给一个穿上任务
    reserved = defaultdict(set)   # iid -> {copy_no} 本分支已固定/占位
    chosen = {}                   # (tid,iid) -> copy_row
    for (tid, iid), n in need.items():
        mid = n["manual"]
        if mid is None:
            continue
        mc = next((c for c, _ev in n["scored"] if c["id"] == int(mid)), None)
        if mc is None:
            add_hard(n["to_start"], tid,
                     f"{n['item']['name']}：人工改派的副本#{mid}不属于该服装", kind="manual")
            continue
        chosen[(tid, iid)] = mc
        if n["phase"] == "don":
            if mc["copy_no"] in reserved[iid]:
                other = next((k for k, v in chosen.items()
                              if k != (tid, iid) and v["id"] == mc["id"]), None)
                add_hard(n["to_start"], tid,
                         f"人工改派冲突：{n['item']['name']}第{mc['copy_no']}件"
                         f"已分配给任务#{other[0] if other else '?'}", kind="manual")
            reserved[iid].add(mc["copy_no"])
            pin_copy[(tid, iid)] = mc["copy_no"] - 1
        decisions.append({
            "task_id": tid, "item_id": iid, "kind": "manual",
            "reason": f"人工改派副本：{n['item']['name']}→第{mc['copy_no']}件"
                      + ("（脱下件）" if n["phase"] == "doff" else ""),
            "need_note": True,
            "noted": bool((notes.get(f"{tid}:{iid}") or "").strip())})

    for key in sorted((k for k in need if k not in chosen and need[k]["phase"] == "don"),
                      key=lambda k: (need[k]["to_start"], k[0])):
        tid, iid = key
        n = need[key]
        if not n["scored"]:
            add_hard(n["to_start"], tid,
                     f"{n['item']['name']}：没有可用实物副本资料（请先同步副本）",
                     kind="no_copy")
            continue

        def grp(ev):
            if ev["out"]:
                return 3
            if ev["missing"]:
                return 2
            return 1 if ev["alter"] else 0

        free = [(c, ev) for c, ev in n["scored"] if c["copy_no"] not in reserved[iid]]
        pool = free or n["scored"]   # 全被本分支占满：交给日历按复用等待/报缺件
        c0, ev0 = min(pool, key=lambda ce: (
            grp(ce[1]), -_slack(copy_fits.get(ce[0]["id"]), n["am"], n["dims"]),
            ce[0]["copy_no"]))
        chosen[key] = c0
        if c0["copy_no"] not in reserved[iid]:
            reserved[iid].add(c0["copy_no"])

    # 5) 适配判定 → 硬冲突、需备注决定、用时覆盖（仅脱下项只做闭合件加时）
    for (tid, iid), c in sorted(chosen.items()):
        n = need[(tid, iid)]
        it, am, om, dims = n["item"], n["am"], n["om"], n["dims"]
        ev = eval_copy(it, c, copy_fits.get(c["id"]), am, dims)
        note_key = f"{tid}:{iid}"
        if n["phase"] == "doff":
            new_dur, _d, _cl = duration_override(it, am, om, c, "doff")
            dur_ov[(tid, iid, "doff")] = new_dur
            continue
        aname = actors.get(cast[tid], {}).get("name", f"#{cast[tid]}")
        constrained_dims = {d for cc, _ in n["scored"] for d in copy_fits.get(cc["id"], {})}
        missing_dims = [d for d in dims
                        if d in constrained_dims and (am is None or am.get(d) is None)]
        if missing_dims:
            add_hard(n["to_start"], tid,
                     f"尺寸缺失：候补{aname}未登记{'/'.join(dim_cn(d) for d in missing_dims)}，"
                     f"无法为{it['name']}判定适配", kind="measure")
        if ev["out"]:
            add_hard(n["to_start"], tid,
                     f"适配越界：{aname}的{'/'.join(dim_cn(d) for d in ev['out'])}"
                     f"超出{it['name']}第{c['copy_no']}件可穿范围且不可调", kind="fit")
        if ev["boundary"]:
            decisions.append({
                "task_id": tid, "item_id": iid, "kind": "boundary",
                "reason": f"边界尺寸：{it['name']}第{c['copy_no']}件 "
                          f"{'/'.join(dim_cn(d) for d in ev['boundary'])}恰在适配端点",
                "need_note": True,
                "noted": bool((notes.get(note_key) or "").strip())})
        if ev["alter"]:
            decisions.append({
                "task_id": tid, "item_id": iid, "kind": "alter",
                "reason": f"需改衣：{it['name']}第{c['copy_no']}件 "
                          f"{'/'.join(dim_cn(d) for d in ev['alter'])}"
                          f"（最长 {ev['max_alter_sec']}s）",
                "need_note": True, "alter_sec": ev["max_alter_sec"],
                "noted": bool((notes.get(note_key) or "").strip())})
        for kind in ("don", "doff"):
            new_dur, _dev, _cl = duration_override(it, am, om, c, kind)
            dur_ov[(tid, iid, kind)] = new_dur

    base["_duration_ov"] = dur_ov
    base["_pin_copy"] = pin_copy

    # 6) 重排（含动作分工、侧台走位、副本日历、人员/换装位并发）
    sched = scheduler.compute_schedule(base)

    # 7) 改衣赶不上开场：最长改衣须在首次穿上前完成
    for d in decisions:
        if d["kind"] != "alter":
            continue
        tid, iid = d["task_id"], d["item_id"]
        a = next((x for x in sched["actions"]
                  if x["task_id"] == tid and x["kind"] == "don"
                  and x.get("item_id") == iid), None)
        if not a:
            continue
        d["first_don"] = a["start"]
        d["latest_alter_start"] = a["start"] - d["alter_sec"]
        if a["start"] - d["alter_sec"] < int(alter_start_sec or 0):
            add_hard(a["start"], tid,
                     f"改衣赶不上开场：{items[iid]['name']}需 {d['alter_sec']}s 改衣，"
                     f"最早 {scheduler.fmt(int(alter_start_sec or 0))} 开工则 "
                     f"{scheduler.fmt(int(alter_start_sec or 0) + d['alter_sec'])} 才好，"
                     f"穿上 {scheduler.fmt(a['start'])}",
                     action_idx=a["action_idx"], kind="alter_late")

    # 8) 汇总排程冲突（替演任务相关；待复核类不阻断）
    for c in sched["conflicts"]:
        if c["type"] in ("review", "action_review"):
            continue
        if c["task_id"] in swapped or c["type"] == "copy_pin":
            hard.append(dict(c))
    # 未备注的备注类决定也阻断确认（边界尺寸/人工改派/改衣必须备注）
    for d in decisions:
        if d.get("need_note") and not d.get("noted"):
            t0 = d.get("first_don") or sched["windows"].get(
                d["task_id"], {}).get("start", 0)
            add_hard(t0, d["task_id"], f"需备注后确认：{d['reason']}", kind="need_note")
    hard.sort(key=lambda c: (c["time"], c["task_id"], c.get("action_idx", 0)))

    return {
        "cast": {str(k): v for k, v in cast.items()},
        "assigns": assigns,
        "notes": notes,
        "alter_start_sec": int(alter_start_sec or 0),
        "actions": [_slim_action(a) for a in sched["actions"]],
        "windows": {str(tid): w for tid, w in sched["windows"].items()},
        "conflicts": hard,
        "decisions": decisions,
        "copies": _slim_copies(sched["copies"], chosen, swapped),
        "fit_rows": _fit_decisions(chosen, copy_fits, measures, cast, state, items),
        "swapped": sorted(swapped),
        "can_confirm": not hard,
        "n_blocking": len([c for c in hard if c["type"] != "need_note"]),
        "earliest": hard[0] if hard else None,
    }


# ---------------- 基准快照 → 推演 state ----------------

# 排程辅助状态：快照内冻结；替演资料（尺寸/候补/副本/适配）始终取当前登记
AUX_FROZEN_KEYS = ("looks", "look_items", "dressers", "positions", "carts",
                   "skills", "dresser_skills", "dresser_sides", "dresser_unavailable",
                   "action_specs", "action_staff", "action_reviews")


def base_state_for(state, revision):
    """用修订快照覆盖场次/任务/服装/造型/人员，尺寸与副本适配取当前值。"""
    snap = json.loads(revision["snapshot"])
    base = dict(state)
    base["scenes"] = snap["scenes"]
    base["tasks"] = snap["tasks"]
    base["items"] = snap["items"]
    base["actors"] = snap.get("actors", state["actors"])
    for k in AUX_FROZEN_KEYS:
        if snap.get(k) is not None:
            base[k] = snap[k]
    return base


def roster_for(state, role_actor_id):
    """原角的候补列表（按顺位）。"""
    actors = _index(state["actors"])
    out = []
    for r in state.get("understudy_roster", []):
        if r["role_actor_id"] == role_actor_id:
            a = actors.get(r["under_actor_id"])
            out.append({"priority": r["priority"], "actor_id": r["under_actor_id"],
                        "name": a["name"] if a else f"#{r['under_actor_id']}"})
    out.sort(key=lambda x: (x["priority"], x["actor_id"]))
    return out


def parse_plan(branch):
    if not branch or not branch.get("plan_json"):
        return None
    return json.loads(branch["plan_json"])
