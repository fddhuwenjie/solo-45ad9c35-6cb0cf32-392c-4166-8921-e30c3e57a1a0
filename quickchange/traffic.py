# -*- coding: utf-8 -*-
"""侧台通行推演：路网找路与占用仿真。

路网：通道节点（corridor/door 门洞）+ 路段（净宽/通行耗时/容量/单向）
+ 随场次封闭时段 + 禁行区多边形。换装位/服装车/上场口吸附到节点
（显式 node_id 优先，否则按坐标就近吸附）。

推演：为演员（退场→换装位→上场口）、服装师（动作间交接）、服装车
（换装位补给往返）分别找路——时间相关 Dijkstra（封闭时段内等待或绕行），
再按占用时段做容量仿真：窄段对向会车让行、门洞/窄门排队、容量超限顺延；
已锁任务的时空区间固定不动（他人让行，冲突照实报告）。人工拖改路径
（net_paths）与让行顺序（net_yields）参与推演。

输出：逐段时刻（每段进入/离开/等待）、最早受阻路段与相关任务、
回写给排程引擎的步行/交接耗时（_walk_ov / _transfer_ov）。
"""
import json
import math
from collections import defaultdict

WALK_SPEED = 1.3        # 米/秒（与 scheduler 一致）
CART_FACTOR = 1.4       # 服装车推行耗时倍率
CART_MIN_WIDTH = 0.9    # 服装车可通过的最小净宽（米）
PERSON_WIDTH = 0.6      # 容量推导：每名人员占用净宽
TWO_WAY_MIN = 1.2       # 净宽低于该值：对向不可同时通过（会车让行）
DOOR_DWELL = 2          # 门洞默认通过耗时（秒）
SNAP_MAX = 4.0          # 就近吸附半径（米）
EXITS = {"L": (2.0, 10.0), "R": (38.0, 10.0)}


def fmt(sec):
    sec = int(sec)
    return f"{sec//60:02d}:{sec % 60:02d}"


def _index(lst):
    return {r["id"]: r for r in lst}


# ---------------- 路网构建 ----------------

def _in_poly(x, y, poly):
    """射线法：点是否在多边形内。poly=[[x,y],...]"""
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def build_net(state):
    """组装可通行路网：节点/路段（剔除禁行区覆盖）、邻接表、封闭时段。"""
    zones = []
    for z in state.get("net_zones", []):
        try:
            pts = json.loads(z["points_json"])
        except (TypeError, ValueError):
            pts = []
        if len(pts) >= 3:
            zones.append((z["id"], z.get("name") or "禁行区", pts))

    nodes = {}
    for n in state.get("net_nodes", []):
        blocked = any(_in_poly(n["x"], n["y"], pts) for _, _, pts in zones)
        if not blocked:
            nodes[n["id"]] = dict(n)

    edges = {}
    adj = defaultdict(list)      # node_id -> [(edge_id, other_node, dir)]
    for e in state.get("net_edges", []):
        if e["a_node"] not in nodes or e["b_node"] not in nodes:
            continue
        mx = (nodes[e["a_node"]]["x"] + nodes[e["b_node"]]["x"]) / 2
        my = (nodes[e["a_node"]]["y"] + nodes[e["b_node"]]["y"]) / 2
        if any(_in_poly(mx, my, pts) for _, _, pts in zones):
            continue
        edges[e["id"]] = dict(e)
        adj[e["a_node"]].append((e["id"], e["b_node"], 1))
        if not e["oneway"]:
            adj[e["b_node"]].append((e["id"], e["a_node"], -1))

    scenes = _index(state.get("scenes", []))
    closures = defaultdict(list)   # edge_id -> [(s, e, label)]
    for c in state.get("net_closures", []):
        if c.get("scene_id") and c["scene_id"] in scenes:
            sc = scenes[c["scene_id"]]
            label = f"场次「{sc['name']}」" + (f"：{c['reason']}" if c.get("reason") else "")
            closures[c["edge_id"]].append(
                (sc["start_sec"], sc["start_sec"] + sc["duration_sec"], label))
        elif c.get("start_sec") is not None and c.get("end_sec") is not None:
            closures[c["edge_id"]].append(
                (int(c["start_sec"]), int(c["end_sec"]), c.get("reason") or "临时封路"))
    for v in closures.values():
        v.sort()

    return {"nodes": nodes, "edges": edges, "adj": adj, "closures": closures,
            "zones": zones}


def edge_capacity(e):
    if e.get("capacity"):
        return max(1, int(e["capacity"]))
    return max(1, int(e["width_m"] / PERSON_WIDTH))


def edge_traverse(e, profile):
    base = e["traverse_sec"] if e["traverse_sec"] > 0 else None
    if base is None:
        base = max(1, math.ceil(e["_len"] / WALK_SPEED))
    if profile == "cart":
        base = max(1, math.ceil(base * CART_FACTOR))
    return base


def node_dwell(n):
    if n.get("kind") == "door":
        return max(1, int(n.get("dwell_sec") or DOOR_DWELL))
    return max(0, int(n.get("dwell_sec") or 0))


def _closed_at(net, edge_id, t):
    """t 时刻路段是否封闭；封闭返回 (开放时间, 标签)，否则 None。"""
    for s, e, label in net["closures"].get(edge_id, []):
        if s <= t < e:
            return e, label
    return None


def _passable(net, e, profile):
    if profile == "cart" and e["width_m"] < CART_MIN_WIDTH:
        return False
    return True


def route(net, fr, to, t0, profile, ignore_closures=False):
    """时间相关 Dijkstra：最早到达。返回 {"steps":[...], "arrive":t} 或 None。

    steps: ("edge", edge_id, dir) 与 ("node", door_node_id) 交替；
    封闭路段：等待开放或绕行（等待计入到达时刻）。
    """
    if fr == to:
        return {"steps": [], "arrive": t0}
    if fr not in net["nodes"] or to not in net["nodes"]:
        return None
    dist = {fr: t0}
    prev = {}
    heap = [(t0, fr)]
    import heapq
    while heap:
        t, u = heapq.heappop(heap)
        if t > dist.get(u, float("inf")):
            continue
        if u == to:
            break
        for eid, v, d in net["adj"].get(u, []):
            e = net["edges"][eid]
            if not _passable(net, e, profile):
                continue
            arr = t
            if not ignore_closures:
                cl = _closed_at(net, eid, arr)
                if cl:
                    arr = cl[0]
            arr += edge_traverse(e, profile)
            if v != to:
                arr += node_dwell(net["nodes"][v])
            if arr < dist.get(v, float("inf")):
                dist[v] = arr
                prev[v] = (u, eid, d)
                heapq.heappush(heap, (arr, v))
    if to not in dist:
        return None
    # 回溯
    seq = []
    cur = to
    while cur != fr:
        u, eid, d = prev[cur]
        seq.append((cur, eid, d))
        cur = u
    seq.reverse()
    steps = []
    for node_id, eid, d in seq:
        steps.append(("edge", eid, d))
        if node_id != to and node_dwell(net["nodes"][node_id]) > 0:
            steps.append(("node", node_id))
    return {"steps": steps, "arrive": dist[to]}


def manual_steps(net, node_ids, profile):
    """人工拖改路径：节点序列 → steps；断点/逆行/净宽不足返回 None。"""
    if len(node_ids) < 2:
        return []
    steps = []
    for u, v in zip(node_ids, node_ids[1:]):
        cand = None
        for eid, other, d in net["adj"].get(u, []):
            if other != v:
                continue
            e = net["edges"][eid]
            if not _passable(net, e, profile):
                continue
            if cand is None or edge_traverse(e, profile) < edge_traverse(
                    net["edges"][cand[0]], profile):
                cand = (eid, d)
        if cand is None:
            return None
        steps.append(("edge", cand[0], cand[1]))
        if v != node_ids[-1] and node_dwell(net["nodes"][v]) > 0:
            steps.append(("node", v))
    return steps


# ---------------- 吸附 ----------------

def snap_maps(state, net):
    """点位 → 节点：positions/carts/exits 显式 node_id 优先，否则就近吸附。
    返回 (pos_node, cart_node, exit_node, nearest_fn)。"""
    nodes = net["nodes"]

    def nearest(x, y):
        best, bd = None, SNAP_MAX
        for n in nodes.values():
            d = math.hypot(n["x"] - x, n["y"] - y)
            if d <= bd:
                best, bd = n["id"], d
        return best

    pos_node, cart_node = {}, {}
    for p in state.get("positions", []):
        nid = p.get("node_id")
        pos_node[p["id"]] = nid if nid in nodes else nearest(p["x"], p["y"])
    for c in state.get("carts", []):
        nid = c.get("node_id")
        cart_node[c["id"]] = nid if nid in nodes else nearest(c["x"], c["y"])
    ex_bind = {r["side"]: r["node_id"] for r in state.get("net_exits", [])}
    exit_node = {}
    for side, (x, y) in EXITS.items():
        nid = ex_bind.get(side)
        exit_node[side] = nid if nid in nodes else nearest(x, y)
    return pos_node, cart_node, exit_node, nearest


def _node_label(net, nid):
    n = net["nodes"].get(nid)
    if not n:
        return f"节点#{nid}"
    return n["name"] or ("门洞" if n["kind"] == "door" else f"节点#{nid}")


def _edge_label(net, eid):
    e = net["edges"].get(eid)
    if not e:
        return f"路段#{eid}"
    return (f"路段#{eid}（{_node_label(net, e['a_node'])}↔"
            f"{_node_label(net, e['b_node'])}）")


# ---------------- 移动需求（legs） ----------------

def build_legs(state, sched, net):
    """从排程结果抽取移动需求：演员步行、服装师交接、服装车补给。"""
    positions = _index(state.get("positions", []))
    tasks = _index(state.get("tasks", []))
    items = _index(state.get("items", []))
    pos_node, cart_node, exit_node, nearest = snap_maps(state, net)
    legs = []
    lid = [0]

    def add(mover_kind, mover_id, mlabel, task_id, action_idx, leg_kind,
            fr, to, t0, deadline=None, locked=False, fr_pt=None, to_pt=None,
            seq=None):
        lid[0] += 1
        legs.append({
            "id": lid[0], "mover": (mover_kind, mover_id), "mover_label": mlabel,
            "task_id": task_id, "action_idx": action_idx, "leg_kind": leg_kind,
            "fr": fr, "to": to, "t0": int(t0), "deadline": deadline,
            "locked": bool(locked), "fr_pt": fr_pt, "to_pt": to_pt, "seq": seq,
        })

    actors = _index(state.get("actors", []))
    dressers = _index(state.get("dressers", []))
    windows = sched.get("windows", {})

    # 演员：walk 动作（seq0 退场→换装位，seq1 换装位→上场口）
    for a in sched.get("actions", []):
        if a["kind"] != "walk":
            continue
        t = tasks.get(a["task_id"])
        if not t:
            continue
        pos = positions.get(t.get("position_id"))
        ex_pt = EXITS.get(t["exit_side"], EXITS["L"])
        pos_pt = (pos["x"], pos["y"]) if pos else ex_pt
        fr_n = exit_node.get(t["exit_side"]) if a["seq"] == 0 \
            else (pos_node.get(pos["id"]) if pos else exit_node.get(t["exit_side"]))
        to_n = (pos_node.get(pos["id"]) if pos else exit_node.get(t["exit_side"])) \
            if a["seq"] == 0 else exit_node.get(t["exit_side"])
        dl = windows.get(a["task_id"], {}).get("deadline") if a["seq"] == 1 else None
        aname = actors.get(t["actor_id"], {}).get("name", f"#{t['actor_id']}")
        add("actor", t["actor_id"], f"演员{aname}", a["task_id"], a["action_idx"],
            "actor_walk", fr_n, to_n, a["start"], deadline=dl,
            locked=t["locked"],
            fr_pt=ex_pt if a["seq"] == 0 else pos_pt,
            to_pt=pos_pt if a["seq"] == 0 else ex_pt, seq=a["seq"])

    # 服装师：相邻动作间的交接（含跨台）
    for did, ivs in (sched.get("dresser_intervals") or {}).items():
        ivs = sorted(ivs, key=lambda iv: iv[0])
        for prev, nxt in zip(ivs, ivs[1:]):
            fr_pt, to_pt = prev[3], nxt[3]
            if not fr_pt or not to_pt:
                continue
            if abs(fr_pt[0] - to_pt[0]) < 1e-6 and abs(fr_pt[1] - to_pt[1]) < 1e-6:
                continue
            t2 = tasks.get(nxt[2])
            dname = dressers.get(did, {}).get("name", f"#{did}")
            add("dresser", did, f"服装师{dname}", nxt[2], None, "transfer",
                nearest(*fr_pt), nearest(*to_pt), prev[1], deadline=nxt[0],
                locked=bool(t2 and t2["locked"]), fr_pt=fr_pt, to_pt=to_pt)

    # 服装车：把本场要穿的服装从停靠点推到换装位（用后返回）
    for t in state.get("tasks", []):
        tid = t["id"]
        acts = [a for a in sched.get("actions", []) if a["task_id"] == tid]
        if not acts:
            continue
        pos = positions.get(t.get("position_id"))
        pn = pos_node.get(pos["id"]) if pos else None
        if pn is None:
            continue
        cart_need = {}
        for a in acts:
            if a["kind"] != "don" or a.get("item_id") is None:
                continue
            it = items.get(a["item_id"])
            if not it or not it.get("cart_id"):
                continue
            cid = it["cart_id"]
            if cid not in cart_need or a["start"] < cart_need[cid]:
                cart_need[cid] = a["start"]
        for cid, need_by in cart_need.items():
            cn = cart_node.get(cid)
            if cn is None or cn == pn:
                continue
            cname = next((c["name"] for c in state.get("carts", [])
                          if c["id"] == cid), f"#{cid}")
            add("cart", cid, f"服装车{cname}", tid, None, "cart_out",
                cn, pn, acts[0]["start"], deadline=need_by, locked=t["locked"])
            add("cart", cid, f"服装车{cname}", tid, None, "cart_back",
                pn, cn, acts[-1]["end"], locked=t["locked"])
    return legs


# ---------------- 容量占用 ----------------

def _feasible_enter(cal, cap, narrow, t, dur, direction, limit=120):
    """最早进入时刻：容量与对向会车约束。cal=[(s,e,mover,dir)]。"""
    for _ in range(limit):
        block_end = None
        # 并发数只在区间起点处变化：考察 t 与 [t,t+dur) 内各起点
        points = [t] + sorted(s for s, e, _, _ in cal if t < s < t + dur)
        for p in points:
            same = sum(1 for s, e, _, d2 in cal if s <= p < e and d2 == direction)
            opp = sum(1 for s, e, _, d2 in cal if s <= p < e and d2 != direction)
            if same + opp >= cap or (narrow and opp > 0):
                ends = [e for s, e, _, _ in cal if s <= p < e]
                block_end = min(ends) if ends else None
                break
        if block_end is None:
            return t
        t = max(t + 1, block_end)
    return t


def _capacity_ok(cal, cap, narrow, enter, exit_, direction):
    """已锁腿占用检测：放入后是否超容量/对向冲突。"""
    points = [enter] + sorted(s for s, e, _, _ in cal if enter < s < exit_)
    for p in points:
        same = sum(1 for s, e, _, d2 in cal if s <= p < e and d2 == direction)
        opp = sum(1 for s, e, _, d2 in cal if s <= p < e and d2 != direction)
        if same + opp + 1 > cap or (narrow and opp > 0):
            return False
    return True


# ---------------- 推演 ----------------

def simulate(state, sched):
    """全网通行推演。返回 {version, legs, conflicts, earliest}。"""
    net = build_net(state)
    for e in net["edges"].values():
        a, b = net["nodes"][e["a_node"]], net["nodes"][e["b_node"]]
        e["_len"] = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
    legs = build_legs(state, sched, net)
    conflicts = []
    manual = {}
    for r in state.get("net_paths", []):
        try:
            ns = json.loads(r["nodes_json"])
        except (TypeError, ValueError):
            continue
        if ns:
            manual[(r["task_id"], r["mover_kind"], r["mover_id"])] = ns

    # 1) 找路（人工路径优先，其次时间相关最短路）
    for leg in legs:
        leg["delay"] = 0
        leg["segments"] = []
        leg["arrive"] = None
        leg["manual"] = False
        leg["steps"] = None
        if leg["fr"] is None or leg["to"] is None:
            conflicts.append({
                "type": "net_snap", "task_id": leg["task_id"], "time": leg["t0"],
                "message": f"{leg['mover_label']}（任务#{leg['task_id']}）起止点"
                           f"未吸附到路网：请为换装位/服装车/上场口指定或就近放置节点"})
            continue
        profile = "cart" if leg["mover"][0] == "cart" else "person"
        mnodes = manual.get((leg["task_id"], leg["mover"][0], leg["mover"][1]))
        if mnodes and mnodes[0] == leg["fr"] and mnodes[-1] == leg["to"]:
            steps = manual_steps(net, mnodes, profile)
            if steps is None:
                conflicts.append({
                    "type": "net_manual", "task_id": leg["task_id"],
                    "time": leg["t0"],
                    "message": f"人工路径断开或逆行/净宽不足：{leg['mover_label']}"
                               f"（任务#{leg['task_id']}）的指定路径不可用，已回退自动找路"})
            else:
                leg["steps"] = steps
                leg["manual"] = True
        if leg["steps"] is None:
            res = route(net, leg["fr"], leg["to"], leg["t0"], profile)
            if res is None:
                # 区分：封路导致（忽略封闭可通）还是路网本身断开
                free = route(net, leg["fr"], leg["to"], leg["t0"], profile,
                             ignore_closures=True)
                if free is not None:
                    # 封闭-free 路径上最早封闭的受阻路段
                    hit_e, hit_t, hit_lab = None, None, ""
                    t = leg["t0"]
                    for stp in free["steps"]:
                        if stp[0] != "edge":
                            continue
                        cl = _closed_at(net, stp[1], t)
                        if cl and (hit_t is None or True):
                            hit_e, hit_t, hit_lab = stp[1], t, cl[1]
                            break
                        t += edge_traverse(net["edges"][stp[1]], profile)
                    conflicts.append({
                        "type": "net_closed", "task_id": leg["task_id"],
                        "time": leg["t0"], "edge_id": hit_e,
                        "message": f"封路绕行失败：{leg['mover_label']}（任务#{leg['task_id']}）"
                                   f"唯一通道 {_edge_label(net, hit_e)} 于 {fmt(leg['t0'])} "
                                   f"封闭（{hit_lab}），无可行绕行"})
                else:
                    conflicts.append({
                        "type": "net_disconnected", "task_id": leg["task_id"],
                        "time": leg["t0"],
                        "message": f"路径断开：{leg['mover_label']}（任务#{leg['task_id']}）"
                                   f"从{_node_label(net, leg['fr'])}到"
                                   f"{_node_label(net, leg['to'])}无可通路径"
                                   f"（检查禁行区/单向段/净宽）"})
                continue
            leg["steps"] = res["steps"]

    # 2) 占用仿真：已锁腿先行（时空区间固定），让行规则优先，其余按时刻
    yields = {(y["edge_id"], y["mover_kind"], y["mover_id"])
              for y in state.get("net_yields", [])}

    def boosted(leg):
        return any(s[0] == "edge" and (s[1], leg["mover"][0], leg["mover"][1]) in yields
                   for s in (leg["steps"] or []))

    order = sorted(legs, key=lambda l: (0 if l["locked"] else 1,
                                        0 if boosted(l) else 1,
                                        l["t0"], l["id"]))
    edge_cal = defaultdict(list)   # edge_id -> [(enter, exit, mover, dir)]
    node_cal = defaultdict(list)   # node_id -> [(enter, exit, mover, 0)]
    mover_free = {}
    capacity_hits = []

    for leg in order:
        if leg["steps"] is None:
            continue
        profile = "cart" if leg["mover"][0] == "cart" else "person"
        t = leg["t0"] if leg["locked"] else max(leg["t0"],
                                                mover_free.get(leg["mover"], 0))
        base_t = t
        segs = []
        ok = True
        for stp in leg["steps"]:
            if stp[0] == "edge":
                eid, direction = stp[1], stp[2]
                e = net["edges"][eid]
                dur = edge_traverse(e, profile)
                cap, narrow = edge_capacity(e), e["width_m"] < TWO_WAY_MIN
                if leg["locked"]:
                    cl = _closed_at(net, eid, t)
                    if cl:
                        conflicts.append({
                            "type": "net_locked", "task_id": leg["task_id"],
                            "time": t, "edge_id": eid,
                            "message": f"已锁动作的时空区间不得移动：{leg['mover_label']}"
                                       f"（任务#{leg['task_id']}）{fmt(t)} 经过"
                                       f"{_edge_label(net, eid)}，但该路段封闭至 "
                                       f"{fmt(cl[0])}（{cl[1]}）"})
                        ok = False
                    if not _capacity_ok(edge_cal[eid], cap, narrow, t, t + dur,
                                        direction):
                        conflicts.append({
                            "type": "net_locked", "task_id": leg["task_id"],
                            "time": t, "edge_id": eid,
                            "message": f"已锁动作的时空区间不得移动：{_edge_label(net, eid)}"
                                       f"在 {fmt(t)} 容量/会车冲突（{leg['mover_label']}，"
                                       f"任务#{leg['task_id']}），需调整让行或解锁"})
                        ok = False
                    enter, exit_ = t, t + dur
                    edge_cal[eid].append((enter, exit_, leg["mover"], direction))
                    wait = 0
                else:
                    cl = _closed_at(net, eid, t)
                    wait_closed = 0
                    if cl:
                        wait_closed = cl[0] - t
                        t = cl[0]
                    enter = _feasible_enter(edge_cal[eid], cap, narrow, t, dur,
                                            direction)
                    wait = enter - t + wait_closed
                    if enter > t or wait_closed:
                        capacity_hits.append({
                            "type": "net_capacity", "task_id": leg["task_id"],
                            "time": enter, "edge_id": eid,
                            "message": (
                                f"封路等待：{leg['mover_label']}（任务#{leg['task_id']}）"
                                f"在{_edge_label(net, eid)}前等待 {wait}s"
                                f"（{cl[1]}）" if wait_closed else
                                f"窄段排队/会车让行：{leg['mover_label']}"
                                f"（任务#{leg['task_id']}）在{_edge_label(net, eid)}"
                                f"前等待 {wait}s（净宽 {e['width_m']}m，容量 {cap}）")})
                    exit_ = enter + dur
                    edge_cal[eid].append((enter, exit_, leg["mover"], direction))
                segs.append({"kind": "edge", "id": eid, "enter": enter,
                             "exit": exit_, "wait": wait})
                t = exit_
            else:
                nid = stp[1]
                n = net["nodes"][nid]
                dwell = node_dwell(n)
                if dwell <= 0:
                    continue
                if leg["locked"]:
                    if not _capacity_ok(node_cal[nid], 1, True, t, t + dwell, 0):
                        conflicts.append({
                            "type": "net_locked", "task_id": leg["task_id"],
                            "time": t, "node_id": nid,
                            "message": f"已锁动作的时空区间不得移动：门洞"
                                       f"{_node_label(net, nid)}在 {fmt(t)} 被占用"
                                       f"（{leg['mover_label']}，任务#{leg['task_id']}）"})
                        ok = False
                    enter, exit_ = t, t + dwell
                    node_cal[nid].append((enter, exit_, leg["mover"], 0))
                    wait = 0
                else:
                    enter = _feasible_enter(node_cal[nid], 1, True, t, dwell, 0)
                    wait = enter - t
                    if wait > 0:
                        capacity_hits.append({
                            "type": "net_capacity", "task_id": leg["task_id"],
                            "time": enter, "node_id": nid,
                            "message": f"窄门排队：{leg['mover_label']}"
                                       f"（任务#{leg['task_id']}）在门洞"
                                       f"{_node_label(net, nid)}前等待 {wait}s"})
                    exit_ = enter + dwell
                    node_cal[nid].append((enter, exit_, leg["mover"], 0))
                segs.append({"kind": "node", "id": nid, "enter": enter,
                             "exit": exit_, "wait": wait})
                t = exit_
        leg["segments"] = segs
        leg["arrive"] = t
        leg["delay"] = t - base_t - sum(
            edge_traverse(net["edges"][s["id"]], profile) if s["kind"] == "edge"
            else node_dwell(net["nodes"][s["id"]]) for s in segs)
        leg["delay"] = max(0, leg["delay"])
        if not ok:
            leg["infeasible"] = True
        if not leg["locked"]:
            mover_free[leg["mover"]] = t

    # 3) 赶不到动作/开场：到达晚于期限
    for leg in legs:
        if leg["arrive"] is None or leg.get("deadline") is None:
            continue
        if leg["arrive"] > leg["deadline"]:
            last_edge = next((s["id"] for s in reversed(leg["segments"])
                              if s["kind"] == "edge"), None)
            kind_cn = {"actor_walk": "演员赶不到上场口",
                       "transfer": "服装师赶不到动作",
                       "cart_out": "服装车赶不到换装位"}.get(leg["leg_kind"], "赶不到")
            conflicts.append({
                "type": "net_late", "task_id": leg["task_id"], "time": leg["arrive"],
                "edge_id": last_edge,
                "message": f"{kind_cn}：{leg['mover_label']}（任务#{leg['task_id']}）"
                           f"预计 {fmt(leg['arrive'])} 到达，晚于期限 "
                           f"{fmt(leg['deadline'])}"})

    conflicts.extend(capacity_hits)
    conflicts.sort(key=lambda c: (c["time"], c.get("task_id") or 0))
    earliest = next((c for c in conflicts
                     if c["type"] in ("net_disconnected", "net_closed", "net_late",
                                      "net_locked", "net_snap")), None)
    return {"version": state.get("net_version", 0), "net": net, "legs": legs,
            "conflicts": conflicts, "earliest": earliest}


# ---------------- 回写排程的耗时 ----------------

def overrides_from_sim(sim):
    """(walk_ov, transfer_ov)：把逐段推演耗时回写给排程引擎。
    已锁任务不回写（时空区间固定，冲突另行报告）。"""
    walk_ov, transfer_ov = {}, {}
    for leg in sim["legs"]:
        if leg["arrive"] is None or leg["locked"]:
            continue
        dur = max(1, int(math.ceil(leg["arrive"] - leg["t0"])))
        if leg["leg_kind"] == "actor_walk":
            # walk 动作 seq：退场=0 / 上场=1（由 action 的 seq 决定）
            seq = 0 if leg["action_idx"] == 0 else 1
            walk_ov[(leg["task_id"], seq)] = dur
        elif leg["leg_kind"] == "transfer" and leg.get("fr_pt") and leg.get("to_pt"):
            k = (round(leg["fr_pt"][0], 1), round(leg["fr_pt"][1], 1),
                 round(leg["to_pt"][0], 1), round(leg["to_pt"][1], 1))
            transfer_ov[k] = max(transfer_ov.get(k, 0), dur)
    return walk_ov, transfer_ov


def slim_legs(sim):
    """留存/导出用：逐段时刻精简表。"""
    out = []
    for leg in sim["legs"]:
        out.append({
            "task_id": leg["task_id"], "mover_kind": leg["mover"][0],
            "mover_id": leg["mover"][1], "mover_label": leg["mover_label"],
            "leg_kind": leg["leg_kind"], "t0": leg["t0"], "arrive": leg["arrive"],
            "delay": leg["delay"], "manual": leg["manual"], "locked": leg["locked"],
            "edges": [s["id"] for s in leg["segments"] if s["kind"] == "edge"],
            "nodes": ([leg["fr"]] if leg["fr"] else []) +
                     [s["id"] for s in leg["segments"] if s["kind"] == "node"] +
                     ([leg["to"]] if leg["to"] else []),
            "segments": leg["segments"],
        })
    return out


def public(sim):
    """挂到排程结果上供前端/导出使用的部分。"""
    net = sim["net"]
    return {
        "version": sim["version"],
        "legs": [{
            "id": l["id"], "mover_kind": l["mover"][0], "mover_id": l["mover"][1],
            "mover_label": l["mover_label"], "task_id": l["task_id"],
            "leg_kind": l["leg_kind"], "t0": l["t0"], "arrive": l["arrive"],
            "deadline": l["deadline"], "delay": l["delay"],
            "locked": l["locked"], "manual": l["manual"],
            "fr": l["fr"], "to": l["to"],
            "path": _leg_path(net, l),
            "segments": [dict(s, label=(
                _edge_label(net, s["id"]) if s["kind"] == "edge"
                else "门洞" + _node_label(net, s["id"])))
                for s in l["segments"]],
        } for l in sim["legs"]],
        "conflicts": sim["conflicts"],
        "earliest": sim["earliest"],
    }


def _leg_path(net, leg):
    """腿的空间路径：节点坐标序列（含起止）。"""
    pts = []
    if leg["fr"] in net["nodes"]:
        n = net["nodes"][leg["fr"]]
        pts.append({"id": leg["fr"], "x": n["x"], "y": n["y"]})
    cur = leg["fr"]
    for s in leg["segments"]:
        if s["kind"] != "edge":
            continue
        e = net["edges"][s["id"]]
        nxt = e["b_node"] if e["a_node"] == cur else e["a_node"]
        n = net["nodes"].get(nxt)
        if n:
            pts.append({"id": nxt, "x": n["x"], "y": n["y"]})
        cur = nxt
    return pts


# ---------------- 增量重算定位 ----------------

def affected_tasks(state, sched, changed_nodes=None, changed_edges=None):
    """路网变化后，途经相关路段/节点的任务集合。

    旧路径取 net_sim 留存的逐段时刻（变化前），新路径现场重算；
    两者并集即受影响任务（只重算/标记这些任务）。
    """
    import db
    cn, ce = set(changed_nodes or []), set(changed_edges or [])
    if not cn and not ce:
        return []
    hit = set()
    prev = db.get_net_sim(state.get("production", {}).get("id", 1))
    if prev:
        try:
            for leg in json.loads(prev["legs_json"]):
                if ce & set(leg.get("edges", [])) or cn & set(leg.get("nodes", [])):
                    if leg.get("task_id"):
                        hit.add(leg["task_id"])
        except (TypeError, ValueError):
            pass
    sim = simulate(state, sched)
    for leg in sim["legs"]:
        edges = {s["id"] for s in leg["segments"] if s["kind"] == "edge"}
        nodes = {s["id"] for s in leg["segments"] if s["kind"] == "node"}
        nodes.update(n for n in (leg["fr"], leg["to"]) if n)
        if ce & edges or cn & nodes:
            if leg["task_id"]:
                hit.add(leg["task_id"])
    return sorted(hit)
