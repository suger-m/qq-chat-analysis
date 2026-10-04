# -*- coding: utf-8 -*-
"""demo_read.py — 读取解密后的 NTQQ 数据库，解码最近群消息
用法: python demo_read.py [条数]  (默认 10)
"""
import sqlite3, sys, datetime, re

sys.stdout.reconfigure(encoding="utf-8")
import os
import qqlib
account = qqlib.detect_account()
if not account:
    raise SystemExit("未找到解密快照，请先运行 fresh")
OUT = os.path.join(qqlib.OUTPUT_DIR, account)

import blackboxprotobuf

NOISE = re.compile(r'^\d{5,}$|^[A-Za-z0-9+/=]{20,}$|^[0-9a-fA-F-]{16,}$')

def collect_text(obj, acc):
    """递归收集 protobuf 树中的可读字符串叶节点"""
    if isinstance(obj, dict):
        for v in obj.values():
            collect_text(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            collect_text(v, acc)
    elif isinstance(obj, bytes):
        try:
            s = obj.decode("utf-8")
            if s.strip() and not NOISE.match(s.strip()):
                acc.append(s.strip())
        except UnicodeDecodeError:
            pass
    elif isinstance(obj, str):
        if obj.strip() and not NOISE.match(obj.strip()):
            acc.append(obj.strip())

def main(n=10):
    g = sqlite3.connect(fr"{OUT}\group_info.db")
    gc = g.cursor()
    # group_list 列名是字段号: 通过采样找到 群号(纯数字) 和 群名(含中文/字母) 两列
    cols = [r[1] for r in gc.execute("PRAGMA table_info(group_list)")]
    sample = gc.execute(f"SELECT * FROM group_list LIMIT 200").fetchall()
    code_col = name_col = None
    for ci in range(len(cols)):
        vals = [row[ci] for row in sample if row[ci] is not None]
        digits = sum(1 for v in vals if isinstance(v, (int, str)) and str(v).isdigit() and 5 <= len(str(v)) <= 12)
        texts = sum(1 for v in vals if isinstance(v, str) and v and not str(v).isdigit())
        if vals and digits >= len(vals) * 0.8 and digits >= 3:
            code_col = ci
        if vals and texts >= len(vals) * 0.5 and texts >= 3 and len(max(vals, key=lambda x: len(str(x)))) > 1:
            name_col = ci
    name_map = {}
    if code_col is not None and name_col is not None:
        for row in sample:
            try:
                name_map[str(row[code_col])] = str(row[name_col])
            except Exception:
                pass
        print(f"(群映射: 列 {cols[code_col]} -> {cols[name_col]}, 共 {len(name_map)} 群)")
    else:
        print("group_list 列:", cols[:20], "code_col:", code_col, "name_col:", name_col)
    g.close()

    con = sqlite3.connect(fr"{OUT}\nt_msg.db")
    cur = con.cursor()
    print(f"\n== 最近 {n} 条群消息 (rowid 降序, 跳过坏行) ==\n")
    # 防御式读取: 只查 rowid, 不碰 BLOB 列, 避免全表扫描 malformed
    rowids = [r[0] for r in cur.execute(
        f"SELECT rowid FROM group_msg_table ORDER BY rowid DESC LIMIT ?", (n * 3,))]
    shown = 0
    for rid in rowids:
        if shown >= n:
            break
        try:
            row = cur.execute(
                'SELECT "40021", "40033", "40090", "40050", "40800" '
                'FROM group_msg_table WHERE rowid = ?', (rid,)).fetchone()
        except sqlite3.DatabaseError:
            continue  # 坏行, 跳过
        if row is None:
            continue
        gid, uin, nick, ts, blob = row
        t = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        text = ""
        if blob:
            try:
                msg, _ = blackboxprotobuf.decode_message(blob)
                acc = []
                collect_text(msg, acc)
                text = " ".join(acc)[:120]
            except Exception:
                text = f"<{len(blob)}B 未解析>"
        gname = name_map.get(str(gid), gid)
        print(f"[{t}] {gname} | {nick or uin}: {text}")
        shown += 1
    con.close()

if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10)
