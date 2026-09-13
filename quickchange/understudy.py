# -*- coding: utf-8 -*-
"""替演推演：午场前临时换角的副本适配与换装重排。

核心规则：
- 换角后候补**沿用原角色在对应场次的造型**（克隆原角 looks，不要求候补另建
  个人造型），以此为各造型的每件穿着类服装匹配实物副本；
- 逐部位关键尺寸判定「直接合身 / 需改衣 / 越界 / 尺寸缺失」，边界尺寸、
  人工改派与需改衣均须备注；按尺寸偏差与闭合件类型调整穿/脱时长；
- 副本一旦在适配阶段选定（fit_rows），排程与冻结都严格使用该件：自动匹配
  的副本等待该件复用，人工改派的副本被并发占用即冲突；开场前已穿着的段落
  （init 段）同样固定到选定副本；
- 汇总硬冲突（尺寸缺失、适配越界、演员场次重叠、副本并发占用、改衣赶不上
  开场、无法按时完成）与需备注决定；任一存在时 can_confirm=False。
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
    """item_id -> [copy_row,...]（按 copy_no）；copy_id -> {dim: fit_row}。"""
    by_item, fits = {}, {}
    for c in copies_rows or []:
        by_item.setdefault(c["item_id"], []).append(c)
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


def dims_for(item):
    return SHOE_DIMS if item.get("kind") == "shoes" else tuple(
        d for d in DIM_KEYS if d != "foot")


def wear_segments_for(state, tasks):
    """与 scheduler._wear_segments 同模型，但按 state 当前造型（含克隆给候补的
    原角造型）与传入任务的**当前演员**计算，避免换角后段落仍挂在原角名下。"""
    by_look = scheduler.look_items_map(state)
    scenes = _index(state["scenes"])
    look_of = {(l["actor_id"], l["scene_id"]): l["id"] for l in state["looks"]}
    actor_scenes = defaultdict(list)
    for (aid, sid) in look_of:
        actor_scenes[aid].append(sid)
    tasks_by_actor = defaultdict(list)
    for t in tasks:
        tasks_by_actor[t["actor_id"]].append(t)
    segs = defaultdict(list)

    def has_item_change(t, iid, want_don):
        """该任务在换场时是否真的穿/脱 item：比较两场造型清单。"""
        def items_in(sid):
            lid = look_of.get((t["actor_id"], sid))
            return {li.get("item_id") for li in by_look.get(lid, [])
                    if li.get("item_id") is not None}
        a, b = items_in(t["from_scene_id"]), items_in(t["to_scene_id"])
        return iid in (b - a) if want_don else iid in (a - b)

    def segment_tasks(aid, first_sid, last_sid, iid):
        """定位本段真正穿上/脱下该件的任务：
        - don：to_scene=段首场、且任务确实把该件穿入（前一场没有）的任务；
        - doff：from_scene=段末场、且任务确实把该件脱下（下一场没有）的任务。"""
        don = next((t["id"] for t in tasks_by_actor[aid]
                    if t["to_scene_id"] == first_sid
                    and has_item_change(t, iid, True)), None)
        doff = next((t["id"] for t in tasks_by_actor[aid]
                     if t["from_scene_id"] == last_sid
                     and has_item_change(t, iid, False)), None)
        return don, doff

    for aid, sids in actor_scenes.items():
        sids.sort(key=lambda sid: (scenes[sid]["start_sec"], scenes[sid]["seq"]))
        item_ids = set()
        for sid in sids:
            item_ids.update(i["id"] for i in by_look.get(look_of[(aid, sid)], []))
        for iid in item_ids:
            present_seq = [any(i["id"] == iid
                               for i in by_look.get(look_of[(aid, sid)], []))
                           for sid in sids]
            run = None
            for k, (sid, present) in enumerate(zip(sids, present_seq)):
                if present:
                    if run is None:
                        # 序列首场没有进入任务（开场前已穿）；否则按进入任务判定
                        don = None
                        if k > 0:
                            don, _ = segment_tasks(aid, sid, sid, iid)
                        run = {"item_id": iid, "start_scene": sid, "end_scene": sid,
                               "scenes": [sid], "don_task": don, "doff_task": None}
                    else:
                        run["end_scene"] = sid
                        run["scenes"].append(sid)
                elif run is not None:
                    _, doff = segment_tasks(aid, run["start_scene"], run["end_scene"], iid)
                    run["doff_task"] = doff
                    segs[(aid, iid)].append(run)
                    run = None
            if run is not None:
                _, doff = segment_tasks(aid, run["start_scene"], run["end_scene"], iid)
                run["doff_task"] = doff
                segs[(aid, iid)].append(run)
    return segs


def dim_cn(d):
    return dict(DIMS).get(d, d)


def duration_override(item, actor_measures, original_measures, copy_row, kind):
    """尺寸偏差（相对原角）+ 闭合件 → 单件穿/脱用时（秒）。"""
    base = item["don_sec"] if kind == "don" else item["doff_sec"]
    dims = dims_for(item)
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


def _check_cast_overlap(base, under_ids, scenes, hard):
    """同一候补被排在两个时间相交的场次（按其在场造型，同场不重）。"""
    if not under_ids:
        return
    intervals = defaultdict(dict)   # actor -> {scene_id: (start,end,tid,name)}
    for t in base["tasks"]:
        if t["actor_id"] not in under_ids:
            continue
        for sid in (t["from_scene_id"], t["to_scene_id"]):
            if not any(l["actor_id"] == t["actor_id"] and l["scene_id"] == sid
                       for l in base["looks"]):
                continue
            sc = scenes[sid]
            intervals[t["actor_id"]][sid] = (
                sc["start_sec"], sc["start_sec"] + sc["duration_sec"], t["id"], sc["name"])
    for aid, uniq in intervals.items():
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


# ---------------- 分支构建 ----------------

def build_branch(state, cast, assigns=None, notes=None, alter_start_sec=0):
    """在 state（基准快照+当前替演资料）上按 cast 推演。

    cast: {task_id: under_actor_id}（只含被拖换的任务）
    assigns: {"<task_id>:<item_id>": item_copies.id} 人工改派（don 任务键；
             init 段用其对应 don_task 键）
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
    base.pop("_pin_copy", None)
    dur_ov, pin_copy = {}, {}
    decisions, hard, fit_rows_out = [], [], []

    def add_hard(t, tid, msg, action_idx=0, kind="fit"):
        hard.append({"time": int(t), "task_id": tid, "action_idx": action_idx,
                     "type": kind, "message": msg})

    # 1) 拖换卡司：保留换装位/分工/出入口，记录原角
    cast = {int(k): int(v) for k, v in cast.items() if int(v)}
    swapped = set(cast)
    orig_of = {t["id"]: t["actor_id"] for t in base["tasks"] if t["id"] in swapped}
    for t in base["tasks"]:
        if t["id"] in cast:
            t["actor_id"] = cast[t["id"]]

    # 2) 换角后候补沿用原角色造型：先把原角在相关场次的造型克隆给候补
    #    （新 id、actor_id=候补），候补本人原有的同名场次造型不参与。克隆完成后，
    #    若某「角色×场次」的所有任务都被替掉，再移除原角在该场的造型，使副本
    #    日历只保留候补的穿着段落；同角色仍有未替任务的场次则保留原角。
    under_ids = set(cast.values())
    max_look = max((l["id"] for l in base["looks"]), default=0)
    max_li = max((li.get("ord", 0) for li in base.get("look_items", [])), default=0)
    cloned_pairs = set()   # (under, scene_id) 已克隆
    dropped_under_look_ids = set()
    new_look_items = []    # 克隆产生的造型明细（避免与待删明细混淆）
    for t in sorted(base["tasks"], key=lambda x: x["id"]):
        if t["id"] not in swapped:
            continue
        under = t["actor_id"]
        for sid in (t["from_scene_id"], t["to_scene_id"]):
            if (under, sid) in cloned_pairs:
                continue
            src = next((l for l in base["looks"]
                        if l["actor_id"] == orig_of[t["id"]] and l["scene_id"] == sid), None)
            # 该场候补本人原有的造型让位于「沿用原角」：记录待移除
            dropped_under_look_ids |= {l["id"] for l in base["looks"]
                                       if l["actor_id"] == under and l["scene_id"] == sid}
            if src is None:
                cloned_pairs.add((under, sid))
                continue
            max_look += 1
            new_look = {**src, "id": max_look, "actor_id": under}
            base["looks"].append(new_look)
            for li in [li for li in base.get("look_items", [])
                       if li["look_id"] == src["id"]]:
                max_li += 1
                new_look_items.append(
                    {**li, "look_id": max_look, "ord": li.get("ord", max_li)})
            cloned_pairs.add((under, sid))
    base["look_items"] = [li for li in base.get("look_items", [])
                          if li["look_id"] not in dropped_under_look_ids]
    base["look_items"].extend(new_look_items)
    # 移除候补在被克隆场次的本人旧造型（不动其未涉及场次的角色造型）
    base["looks"] = [l for l in base["looks"] if l["id"] not in dropped_under_look_ids]

    # 被完全替掉的「原角×场次」：候选为所有拖换任务涉及的端场；
    # 若仍有未拖换任务让该原角在该场登台，则保留。只删克隆前的原角色造型，
    # 不能按 (演员,场次) 匹配到刚克隆给候补的新造型。
    pre_clone_look_ids = {l["id"] for l in state["looks"]}
    replaced = defaultdict(set)
    for tid in swapped:
        rid = orig_of[tid]
        ot = next(t for t in state["tasks"] if t["id"] == tid)
        replaced[rid].add(ot["from_scene_id"])
        replaced[rid].add(ot["to_scene_id"])
    for t in state["tasks"]:
        if t["id"] in swapped:
            continue
        replaced.get(t["actor_id"], set()).discard(t["from_scene_id"])
        replaced.get(t["actor_id"], set()).discard(t["to_scene_id"])
    drop_look_ids = {l["id"] for l in base["looks"]
                     if l["id"] in pre_clone_look_ids
                     and l["scene_id"] in replaced.get(l["actor_id"], set())}
    base["looks"] = [l for l in base["looks"] if l["id"] not in drop_look_ids]
    base["look_items"] = [li for li in base.get("look_items", [])
                          if li["look_id"] not in drop_look_ids]

    # 目标场（to）候补无任何造型可沿用 → 无法推演该任务
    for t in base["tasks"]:
        if t["id"] not in swapped:
            continue
        if not any(l["actor_id"] == t["actor_id"]
                   and l["scene_id"] == t["to_scene_id"] for l in base["looks"]):
            add_hard(scenes[t["to_scene_id"]]["start_sec"], t["id"],
                     f"原角色在「{scenes[t['to_scene_id']]['name']}」没有造型登记，"
                     f"候补{actors.get(t['actor_id'], {}).get('name', '')}无造型可沿用",
                     kind="no_look")

    # 3) 场次重叠预检（克隆造型后，候补在哪些场次在场已确定）
    _check_cast_overlap(base, set(cast.values()), scenes, hard)

    # 4) 穿着段落：按克隆后的造型给出 (候补,服装) 的每段穿着。
    #    每个替演相关段落都要固定一件实物副本（fit_rows 决定，排程也必须用它）。
    segs = wear_segments_for(base, base["tasks"])
    tasks_by_id = _index(base["tasks"])

    def seg_start(g):
        sc = scenes.get(g["start_scene"])
        return sc["start_sec"] if sc else 0

    # 段落 -> 关联的拖换任务：直接用段自身的 don_task/doff_task（在 swapped 内）；
    # 开场前已穿且仅被某拖换任务触及（其 from/to 场落在覆盖场次内）时取该任务。
    seg_tasks = defaultdict(list)
    for (aid, iid), lst in segs.items():
        for g in lst:
            key = (aid, iid, g["start_scene"])
            for ttid, role in ((g.get("don_task"), "don"), (g.get("doff_task"), "doff")):
                if ttid in swapped:
                    seg_tasks[key].append((g, ttid, role))
            if seg_tasks[key]:
                continue
            covered = set(g.get("scenes") or [g["start_scene"], g["end_scene"]])
            for t in base["tasks"]:
                if t["id"] not in swapped or t["actor_id"] != aid:
                    continue
                if t["to_scene_id"] in covered or t["from_scene_id"] in covered:
                    seg_tasks[key].append((g, t["id"], "touch"))

    seg_list = []
    for (aid, iid, _scid), pairs in seg_tasks.items():
        g = pairs[0][0]
        # 穿上任务代表该段；否则脱下任务；都没有（连续穿着）取最小任务号
        role_rank = {"don": 0, "doff": 1, "touch": 2}
        best = min(pairs, key=lambda z: (role_rank[z[2]], z[1]))
        seg_list.append((g, best[1], aid, iid, best[2]))
    seg_list.sort(key=lambda z: (seg_start(z[0]), z[1]))

    chosen = {}          # (tid, iid) -> copy_row（fit_rows / 排程共用）
    chosen_init = {}     # (aid, iid, start_scene) -> copy_row（init 段固定）
    manual_keys = set()  # 人工改派的 (tid,iid)：排程严格固定，只等这一件
    reserved = defaultdict(set)   # iid -> {copy_no} 本分支已固定

    def evaluate(tid, aid, iid):
        it = items[iid]
        dims = dims_for(it)
        am, om = measures.get(aid), measures.get(orig_of.get(tid))
        scored = [(c, eval_copy(it, c, copy_fits.get(c["id"]), am, dims))
                  for c in copies_by_item.get(iid, [])]
        return it, dims, am, om, scored

    # 4a) 人工改派先固定（严格占用：排程只用该件，不换副本；可等复用）
    for g, tid, aid, iid, _role in seg_list:
        key = f"{tid}:{iid}"
        mid = assigns.get(key)
        if mid is None:
            continue
        it, dims, am, om, scored = evaluate(tid, aid, iid)
        mc = next((c for c, _ in scored if c["id"] == int(mid)), None)
        if not scored or mc is None:
            add_hard(seg_start(g), tid,
                     f"{it['name']}：没有实物副本资料或人工改派的副本#{mid}不属于该服装",
                     kind="manual" if mc is None else "no_copy")
            continue
        chosen[(tid, iid)] = mc
        chosen_init[(aid, iid, g["start_scene"])] = mc
        manual_keys.add((tid, iid))
        reserved[iid].add(mc["copy_no"])
        pin_copy[(tid, iid)] = mc["copy_no"] - 1
        decisions.append({
            "task_id": tid, "item_id": iid, "kind": "manual",
            "reason": f"人工改派副本：{it['name']}→第{mc['copy_no']}件",
            "need_note": True,
            "noted": bool((notes.get(key) or "").strip())})

    # 4b) 自动匹配：合身 → 需改衣 → 缺尺寸/越界；同档取余量最大的未占副本。
    #     自动选择作为分支内偏好（不同任务优先不同件），日历占用仍由 scheduler
    #     按常规分配/复用等待裁决；只有人工改派才严格钉死某一件。
    def grp(ev):
        if ev["out"]:
            return 3
        if ev["missing"]:
            return 2
        return 1 if ev["alter"] else 0

    for g, tid, aid, iid, _role in seg_list:
        if (tid, iid) in chosen:
            continue
        it, dims, am, om, scored = evaluate(tid, aid, iid)
        t0 = seg_start(g)
        if not scored:
            add_hard(t0, tid, f"{it['name']}：没有可用实物副本资料（请先同步副本）",
                     kind="no_copy")
            continue
        free = [(c, ev) for c, ev in scored if c["copy_no"] not in reserved[iid]]
        pool = free or scored
        c0, ev0 = min(pool, key=lambda ce: (
            grp(ce[1]), -_slack(copy_fits.get(ce[0]["id"]), am, dims), ce[0]["copy_no"]))
        chosen[(tid, iid)] = c0
        chosen_init[(aid, iid, g["start_scene"])] = c0
        if c0["copy_no"] not in reserved[iid]:
            reserved[iid].add(c0["copy_no"])

    # 5) 适配判定 → 硬冲突 / 备注决定 / fit_rows / 用时覆盖
    for g, tid, aid, iid, _role in seg_list:
        c = chosen.get((tid, iid))
        if c is None:
            continue
        it, dims, am, om, scored = evaluate(tid, aid, iid)
        ev = eval_copy(it, c, copy_fits.get(c["id"]), am, dims)
        key = f"{tid}:{iid}"
        t0 = seg_start(g)
        aname = actors.get(aid, {}).get("name", f"#{aid}")
        constrained = {d for cc, _ in scored for d in copy_fits.get(cc["id"], {})}
        missing_dims = [d for d in dims
                        if d in constrained and (am is None or am.get(d) is None)]
        if missing_dims:
            add_hard(t0, tid,
                     f"尺寸缺失：候补{aname}未登记{'/'.join(dim_cn(d) for d in missing_dims)}，"
                     f"无法为{it['name']}判定适配", kind="measure")
        if ev["out"]:
            add_hard(t0, tid,
                     f"适配越界：{aname}的{'/'.join(dim_cn(d) for d in ev['out'])}"
                     f"超出{it['name']}第{c['copy_no']}件可穿范围且不可调", kind="fit")
        if ev["boundary"]:
            decisions.append({
                "task_id": tid, "item_id": iid, "kind": "boundary",
                "reason": f"边界尺寸：{it['name']}第{c['copy_no']}件 "
                          f"{'/'.join(dim_cn(d) for d in ev['boundary'])}恰在适配端点",
                "need_note": True,
                "noted": bool((notes.get(key) or "").strip())})
        if ev["alter"]:
            decisions.append({
                "task_id": tid, "item_id": iid, "kind": "alter",
                "reason": f"需改衣：{it['name']}第{c['copy_no']}件 "
                          f"{'/'.join(dim_cn(d) for d in ev['alter'])}"
                          f"（最长 {ev['max_alter_sec']}s）",
                "need_note": True, "alter_sec": ev["max_alter_sec"],
                "noted": bool((notes.get(key) or "").strip())})
        # 穿上与脱下时长：穿上按选定件；脱下按同一候补穿着该件时的选定件
        don_dur, _d1, _c1 = duration_override(it, am, om, c, "don")
        dur_ov[(tid, iid, "don")] = don_dur
        doff_c = c
        if g.get("don_task") and g["don_task"] != tid:
            doff_c = chosen.get((g["don_task"], iid), c)
        dur_ov[(tid, iid, "doff")] = duration_override(it, am, om, doff_c, "doff")[0]
        fit_rows_out.append({
            "task_id": tid, "item_id": iid, "copy_no": c["copy_no"],
            "copy_id": c["id"],
            "manual": (tid, iid) in manual_keys,
            "status": "越界" if ev["out"] else "需改衣" if ev["alter"] else "合身",
            "direct": ev["direct"], "alter": ev["alter"], "out": ev["out"],
            "boundary": ev["boundary"], "missing": ev["missing"],
            "actor_id": aid, "actor_name": aname,
            "pre_show": g.get("don_task") is None,
            "note": notes.get(key, "")})

    # 6) 开场前已穿着段落（无 don_task）：仅人工改派的段落钉死选定副本；
    #    自动匹配的开场前穿着仍由 scheduler 自由选件（fit_rows 只是偏好）
    init_pins = {}   # (actor_id,item_id,start_scene) -> 副本下标(0起)
    for (aid, iid, start_scene), c in chosen_init.items():
        g_t = next(((gg, tt) for gg, tt, aa, ii, _rr in seg_list
                    if aa == aid and ii == iid and gg["start_scene"] == start_scene), None)
        if not g_t or g_t[0].get("don_task") is not None:
            continue
        if (g_t[1], iid) in manual_keys:
            init_pins[(aid, iid, start_scene)] = c["copy_no"] - 1

    base["_duration_ov"] = dur_ov
    base["_pin_copy"] = pin_copy
    base["_init_pins"] = init_pins

    # 7) 重排（动作分工、侧台走位、副本日历、人员/换装位并发）
    sched = scheduler.compute_schedule(base)

    # 8) 改衣赶不上开场：改衣须在首次穿上前完成（init 段按场次开场）
    for d in decisions:
        if d["kind"] != "alter":
            continue
        tid, iid = d["task_id"], d["item_id"]
        a = next((x for x in sched["actions"]
                  if x["task_id"] == tid and x["kind"] == "don"
                  and x.get("item_id") == iid), None)
        first_don = a["start"] if a else next(
            (t0 for gg, tt, aa, ii, _rr in seg_list
             if tt == tid and ii == iid for t0 in [seg_start(gg)]), 0)
        d["first_don"] = first_don
        d["latest_alter_start"] = first_don - d["alter_sec"]
        if first_don - d["alter_sec"] < int(alter_start_sec or 0):
            add_hard(first_don, tid,
                     f"改衣赶不上开场：{items[iid]['name']}需 {d['alter_sec']}s 改衣，"
                     f"最早 {scheduler.fmt(int(alter_start_sec or 0))} 开工则 "
                     f"{scheduler.fmt(int(alter_start_sec or 0) + d['alter_sec'])} 才好，"
                     f"穿上 {scheduler.fmt(first_don)}",
                     action_idx=a["action_idx"] if a else 0, kind="alter_late")

    # 9) 排程冲突（替演任务相关；待复核类不阻断）
    for c in sched["conflicts"]:
        if c["type"] in ("review", "action_review"):
            continue
        if c["task_id"] in swapped or c["type"] == "copy_pin":
            hard.append(dict(c))

    # 回填：自动匹配的副本最终以 scheduler 实际日历占用为准，保证 fit_rows
    # 与冻结计划一致；找不到实际占用（缺件未分配）时保留适配偏好件并标注。
    swapped_copies = _slim_copies(sched["copies"], swapped)
    actual_by_task = defaultdict(set)
    for cc in swapped_copies:
        if cc["task_id"] is not None:
            actual_by_task[(cc["task_id"], cc["item_id"])].add(cc["copy_no"])
    # 开场前穿着段（pre_show，task_id=None）按演员+服装反查
    pre_actual = {}
    actor_by_task = {tid: cast.get(tid) for tid in swapped}
    for cc in swapped_copies:
        if cc.get("pre_show") and cc["task_id"] is None:
            # 该 init 段属于谁：用副本区间起点对应场次匹配
            for g, tid, aid, iid, _role in seg_list:
                if iid == cc["item_id"] and g.get("don_task") is None and \
                        seg_start(g) == cc["start"]:
                    pre_actual[(tid, iid)] = cc["copy_no"]
    copy_rows_by_item, _cf = copy_fit_maps(state.get("item_copies"),
                                           state.get("copy_fit"))
    for fr in fit_rows_out:
        key = (fr["task_id"], fr["item_id"])
        if fr.get("manual"):
            fr["allocated"] = fr["copy_no"]
            continue
        actual = actual_by_task.get(key)
        no = sorted(actual)[0] if actual else pre_actual.get(key)
        if no:
            fr["copy_no"] = no
            cr = next((c for c in copy_rows_by_item.get(fr["item_id"], [])
                       if c["copy_no"] == no), None)
            if cr:
                fr["copy_id"] = cr["id"]
                # 脱下闭合件加时按实际落位副本重算（穿上时长在重排前已定）
                new_doff = duration_override(items[fr["item_id"]],
                                             measures.get(fr["actor_id"]),
                                             measures.get(orig_of.get(fr["task_id"])),
                                             cr, "doff")[0]
                fr["doff_dur"] = new_doff
            fr["allocated"] = no
        else:
            fr["allocated"] = None   # 未分配（缺件/超时）
    # 把脱下动作时长对齐到实际副本
    doff_dur_map = {(fr["task_id"], fr["item_id"]): fr.get("doff_dur")
                    for fr in fit_rows_out if fr.get("doff_dur")}
    for a in sched["actions"]:
        if a["kind"] == "doff" and (a["task_id"], a.get("item_id")) in doff_dur_map:
            a["dur"] = doff_dur_map[(a["task_id"], a.get("item_id"))]
            a["end"] = a["start"] + a["dur"]
    # 未备注的备注类决定也阻断确认
    for d in decisions:
        if d.get("need_note") and not d.get("noted"):
            t0 = d.get("first_don") or sched["windows"].get(
                d["task_id"], {}).get("start", 0)
            add_hard(t0, d["task_id"], f"需备注后确认：{d['reason']}", kind="need_note")
    hard.sort(key=lambda c: (c["time"], c["task_id"], c.get("action_idx", 0)))

    swapped_copies = _slim_copies(sched["copies"], swapped)
    return {
        "cast": {str(k): v for k, v in cast.items()},
        "assigns": assigns,
        "notes": notes,
        "alter_start_sec": int(alter_start_sec or 0),
        "actions": [_slim_action(a) for a in sched["actions"]],
        "windows": {str(tid): w for tid, w in sched["windows"].items()},
        "conflicts": hard,
        "decisions": decisions,
        "copies": swapped_copies,
        "fit_rows": fit_rows_out,
        "swapped": sorted(swapped),
        "can_confirm": not hard,
        "n_blocking": len([c for c in hard if c["type"] != "need_note"]),
        "earliest": hard[0] if hard else None,
    }


def _slim_copies(sched_copies, swapped):
    """每个替演穿上/开场前占用实际落在的实物副本编号。"""
    out = []
    for iid, cps in sched_copies.items():
        for ci, ivs in enumerate(cps):
            for iv in ivs:
                label = iv[2]
                tid = None
                if isinstance(label, str) and label.startswith("task:"):
                    tid = int(label.split(":")[1])
                elif isinstance(label, str) and label.startswith("init:"):
                    pass
                else:
                    continue
                if tid is not None and tid not in swapped:
                    continue
                out.append({"task_id": tid, "item_id": iid, "copy_no": ci + 1,
                            "start": iv[0], "end": iv[1],
                            "pre_show": isinstance(label, str) and label.startswith("init:")})
    out.sort(key=lambda x: (x["start"], x["task_id"] or 0))
    return out


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
