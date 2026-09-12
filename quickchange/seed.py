"""写入演示数据：含刻意安排的冲突（同一服装师照看两人、服装车停错侧、维修中服装）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def seed():
    db.init_db()
    con = db.connect()
    cur = con.cursor()
    pid = 1
    if cur.execute("SELECT COUNT(*) c FROM scenes WHERE production_id=?", (pid,)).fetchone()["c"]:
        con.close()
        return False

    cur.execute("INSERT OR IGNORE INTO productions(id,name,created_at) VALUES(1,'示例剧目《夜航》',0)")

    # 场次：六场，场景间隔即换装窗口
    scenes = [
        (1, "一幕·码头", 0, 600),
        (2, "二幕·船舱", 640, 540),
        (3, "三幕·风暴", 1220, 420),
        (4, "四幕·孤岛", 1680, 480),
        (5, "五幕·回忆", 2200, 360),
        (6, "尾声·归港", 2600, 300),
    ]
    for seq, name, st, dur in scenes:
        cur.execute("INSERT INTO scenes(production_id,seq,name,start_sec,duration_sec) VALUES(1,?,?,?,?)",
                    (seq, name, st, dur))

    actors = [("林澜", "LL", "L"), ("周屿", "ZY", "R"), ("沈默", "SM", "L"), ("何苗", "HM", "R")]
    for name, code, side in actors:
        cur.execute("INSERT INTO actors(production_id,name,code,default_side) VALUES(1,?,?,?)",
                    (name, code, side))

    dressers = ["王姐", "小李"]
    for n in dressers:
        cur.execute("INSERT INTO dressers(production_id,name) VALUES(1,?)", (n,))

    # 换装位（侧台平面 40x20 米）
    positions = [
        ("快装间A", "L", 6, 4, 1),
        ("快装间B", "L", 6, 16, 2),
        ("右侧换装位", "R", 34, 6, 1),
    ]
    for n, s, x, y, c in positions:
        cur.execute("INSERT INTO positions(production_id,name,side,x,y,capacity) VALUES(1,?,?,?,?,?)",
                    (n, s, x, y, c))

    carts = [
        ("服装车1号", "L", 3, 16, 30),
        ("服装车2号", "R", 37, 16, 30),
    ]
    for n, s, x, y, c in carts:
        cur.execute("INSERT INTO carts(production_id,name,side,x,y,capacity) VALUES(1,?,?,?,?,?)",
                    (n, s, x, y, c))

    # 服装/道具：layer 1 内 → 4 外；2号车上的道具对左侧演员是“停错一侧”
    items = [
        # name, kind, layer, don, doff, status, avail, cart, copies
        ("打底衬衣", "costume", 1, 8, 6, "ok", 0, 1, 4),
        ("船工马甲", "costume", 2, 12, 8, "ok", 0, 1, 2),
        ("风暴斗篷", "costume", 3, 15, 10, "ok", 0, 1, 2),
        ("船长礼服", "costume", 4, 20, 12, "ok", 0, 1, 1),
        ("孤岛破衣", "costume", 2, 10, 8, "repair", 1750, 1, 1),   # 维修中，四幕前才可用
        ("回忆长裙", "costume", 2, 14, 10, "cleaning", 2100, 1, 1), # 清洁中
        ("皮靴", "shoes", 1, 12, 8, "ok", 0, 1, 4),
        ("舞鞋", "shoes", 1, 8, 6, "ok", 0, 1, 2),
        ("望远镜", "prop", 0, 0, 0, "ok", 0, 2, 1),   # 放在右侧2号车
        ("油灯", "prop", 0, 0, 0, "ok", 0, 1, 2),
        ("船票", "prop", 0, 0, 0, "ok", 0, 1, 3),
    ]
    for row in items:
        cur.execute("""INSERT INTO items(production_id,name,kind,layer,don_sec,doff_sec,status,available_at,cart_id,copies)
                       VALUES(1,?,?,?,?,?,?,?,?,?)""", row)

    def look(actor_id, scene_seq, name, item_names):
        cur.execute("INSERT INTO looks(production_id,actor_id,scene_id,name) VALUES(1,?,?,?)",
                    (actor_id, scene_seq, name))
        lid = cur.lastrowid
        for i, nm in enumerate(item_names):
            iid = cur.execute("SELECT id FROM items WHERE production_id=1 AND name=?", (nm,)).fetchone()["id"]
            cur.execute("INSERT INTO look_items(look_id,item_id,ord) VALUES(?,?,?)", (lid, iid, i))

    # 林澜：一幕船工 → 二幕船长 → 三幕风暴 → 五幕回忆
    look(1, 1, "船工", ["打底衬衣", "船工马甲", "皮靴", "船票"])
    look(1, 2, "船长", ["打底衬衣", "船长礼服", "皮靴", "望远镜"])
    look(1, 3, "风暴", ["打底衬衣", "风暴斗篷", "皮靴", "油灯"])
    look(1, 5, "回忆", ["回忆长裙", "舞鞋"])
    # 周屿：一幕船工 → 三幕风暴 → 四幕孤岛
    look(2, 1, "船工", ["打底衬衣", "船工马甲", "皮靴"])
    look(2, 3, "风暴", ["打底衬衣", "风暴斗篷", "皮靴", "油灯"])
    look(2, 4, "孤岛", ["孤岛破衣", "舞鞋"])
    # 沈默：二幕船长副手 → 三幕风暴
    look(3, 2, "大副", ["打底衬衣", "船工马甲", "皮靴", "船票"])
    look(3, 3, "风暴", ["打底衬衣", "风暴斗篷", "皮靴"])
    # 何苗：三幕风暴 → 五幕回忆
    look(4, 3, "风暴", ["打底衬衣", "风暴斗篷", "皮靴"])
    look(4, 5, "回忆", ["回忆长裙", "舞鞋", "船票"])

    # 换装任务：刻意让 1、2 号任务共用王姐且窗口相近（人员并发冲突）
    tasks = [
        # actor, from, to, side, pos, dresser, start, locked
        (1, 1, 2, "L", 1, 1, None, 0),   # 林澜 一幕→二幕（快装间A，王姐）
        (3, 2, 3, "L", 2, 1, None, 0),   # 沈默 二幕→三幕（快装间B，王姐）
        (1, 2, 3, "L", 2, 1, None, 0),   # 林澜 二幕→三幕（也用王姐→冲突）
        (2, 1, 3, "R", 3, 2, None, 0),   # 周屿 一幕→三幕（右侧，小李）
        (2, 3, 4, "R", 3, 2, None, 0),   # 周屿 三幕→四幕（孤岛破衣维修中→缺件）
        (4, 3, 5, "R", 3, 2, None, 0),   # 何苗 三幕→五幕（回忆长裙清洁/复用）
        (1, 3, 5, "L", 1, 1, None, 0),   # 林澜 三幕→五幕（回忆长裙复用冲突）
    ]
    for a, f, t, side, pos, dr, st, lk in tasks:
        cur.execute("""INSERT INTO tasks(production_id,actor_id,from_scene_id,to_scene_id,exit_side,
                       position_id,dresser_id,start_sec,locked) VALUES(1,?,?,?,?,?,?,?,?)""",
                    (a, f, t, side, pos, dr, st, lk))

    con.commit()
    con.close()
    return True


if __name__ == "__main__":
    print("seeded" if seed() else "already seeded")
