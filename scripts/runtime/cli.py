# -*- coding: utf-8 -*-
"""cli.py — QQ 聊天记录命令行工具 (数据 + 分析)

数据层基于 qqlib (NTQQ 解密库), 分析层基于 qanalysis。

用法示例:
  python cli.py groups                          # 群列表 (按最近活跃排序)
  python cli.py groups --json                   # 机器可读
  python cli.py read <群号|群名> [-n 20] [--days 3]
  python cli.py read --c2c <对方QQ号> [-n 20]   # 读私聊
  python cli.py search <关键词> [-g 群号] [-n 30]
  python cli.py stats [-g 群号] [--days 30]

  # ── 群消息分析 ──
  python cli.py analyze <群号|群名> [--days N]   # 综合报告 (热力图/排行/热词/复读/潜水)
  python cli.py heatmap <群号> [--days N]        # 周×时 活跃热力图
  python cli.py words   <群号> [-n 40] [--days N]# 热词榜
  python cli.py people  <群号> [--days N]        # 成员画像 + 自动标签
  python cli.py repeat  <群号> [--days N]        # 复读/队形检测
  python cli.py lurkers <群号> [--days N]        # 潜水榜
  python cli.py night   <群号> [--days N]        # 夜猫子榜

  python cli.py export  <群号> -o out.json [--fmt json|csv|md] [--days N]
  python cli.py web [--port 8765]               # 启动可视化 Web
  python cli.py refresh                         # 重新提取密钥+解密 (需QQ在线)

  # ── Agent 内容分析工作流 (推荐) ──
  python cli.py fresh                              # 0. 同步最新消息 (按需重新解密)
  python cli.py context <群号> --date 2026-10-02   # 1. 取对话原料(分段)
  python cli.py validate analysis.json --context context.txt --fix -o validated.json
  python cli.py html validated.json -o report.html  # 4. 渲染 HTML 报告
  # (2. Agent 阅读原料, 产出 analysis.json —— 见 skills/qq-chat-analysis)

  python cli.py fresh [--check]                    # 检查数据是否陈旧 / 同步最新
  python cli.py refresh                            # 无条件重新解密 (需QQ在线)

数据新鲜度:
  分析类命令默认会先比较「QQ 源加密库」与「output/ 解密产物」的时间戳,
  产物落后时自动重新解密 —— 保证分析基于最新消息而不是旧 checkpoint。
  用 --no-fresh 跳过该检查 (直接读当前快照, 更快), --fresh 强制重新解密。

通用选项:
  --account QQ   指定账号 (默认自动选最新解密账号)
  --json         输出 JSON (便于脚本/Agent 消费)
  --no-cache     忽略分析缓存, 强制重算
  --no-fresh     跳过数据新鲜度检查
  --fresh        强制重新解密最新消息
"""
import os
import sys
import re
import json
import time
import argparse
import datetime

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qqlib
import qanalysis
import qprivacy


# ── 输出 ─────────────────────────────────────────────────────────────────────

def out_json(obj):
    """统一的 JSON 输出。"""
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")


def info(*a):
    """诊断信息一律走 stderr, 不污染 stdout 的机器可读输出。"""
    print(*(qprivacy.diagnostic(x) for x in a), file=sys.stderr)


def fmt_ts(ts, fmt="%m-%d %H:%M:%S"):
    return datetime.datetime.fromtimestamp(ts).strftime(fmt)


def fmt_full(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def parse_date(s, end=False):
    d = datetime.datetime.strptime(s, "%Y-%m-%d")
    if end:
        d += datetime.timedelta(days=1)
    return int(d.timestamp())


def _disp_w(s):
    """显示宽度: 中日韩全角字符算 2 列。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in str(s))


def pad(s, w, align="<"):
    """按**显示宽度**补齐 (中文表格对齐必需, str.ljust 会把中文算 1 列)。"""
    s = str(s)
    d = w - _disp_w(s)
    if d <= 0:
        return s
    return (s + " " * d) if align == "<" else (" " * d + s)


def human_size(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


# ── 群解析 ───────────────────────────────────────────────────────────────────

def resolve_group(store, token):
    """把「群号」或「群名片段」解析成群号。

    返回 (gid, name) ; 找不到返回 (None, None)。
    名称匹配歧义时取消息量最大的那个, 并提示其余候选。
    """
    token = str(token or "").strip()
    names = store.group_names()
    # A numeric group ID or a unique name does not need the full-table activity
    # aggregate. That aggregate can fail on an unrelated damaged SQLite page,
    # even though the requested group's rows remain readable.
    if token.isdigit():
        return token, names.get(token, token)
    low = token.lower()
    hits = [(gid, nm) for gid, nm in names.items()
            if low in (nm or "").lower()]
    if not hits:
        raise SystemExit(f"找不到群: {token!r}  (可用 `cli.py groups` 查看)")
    if len(hits) == 1:
        return hits[0]
    gs = store.groups_overview()
    by_id = {g["id"]: g for g in gs}
    hits.sort(key=lambda x: -(by_id.get(x[0], {}).get("count") or 0))
    gid, nm = hits[0]
    if len(hits) > 1:
        others = ", ".join(f"{g}({n})" for g, n in hits[1:4])
        info(f"⚠ {token!r} 匹配到 {len(hits)} 个群, 选用消息最多的 [{nm}]; 其它: {others}")
    return gid, nm


def get_store(args=None):
    acct = getattr(args, "account", None) if args else None
    if not qqlib.detect_account() and not getattr(args, "no_fresh", False):
        initial = acct or qqlib.qpaths.detect_source_account()
        if initial:
            ok, msg = qqlib.sync_latest(initial)
            if not ok:
                raise SystemExit(qprivacy.diagnostic(msg))
            acct = initial
    store = qqlib.QQStore(acct)
    info(f"账号: {store.account}")
    if args is not None:
        ensure_fresh(args, store)
    return store


def ensure_fresh(args, store):
    """分析前确保数据基于「最新消息」而非旧 checkpoint。

    比较 QQ 源加密库与 output/ 解密产物的 mtime, 源库更新则重新解密。
    默认开启; --no-fresh 跳过, --fresh 强制刷新。
    QQ 未运行时只警告不阻塞 (密钥提取需要 QQ 进程内存)。
    """
    if getattr(args, "no_fresh", False):
        return
    force = getattr(args, "fresh", False)
    fr = qqlib.freshness(store.account)
    if not fr["stale"] and not force:
        return
    if fr["qq_running"] is False:
        mins = max(1, fr["lag_sec"] // 60)
        info(f"⚠ 数据为旧快照 (落后约 {mins} 分钟), 但 QQ 未运行无法同步; "
             f"可用 --no-fresh 跳过此检查")
        return
    why = "按要求强制同步" if force else (
        f"源库比产物新 {fr['lag_sec'] // 60} 分 {fr['lag_sec'] % 60} 秒")
    info(f"↻ 检测到数据陈旧 ({why}), 正在解密最新消息 (约 40s)…")
    t0 = time.time()
    ok, msg = qqlib.sync_latest(store.account)
    info(("✓ " if ok else "✗ ") + f"{msg} (耗时 {time.time() - t0:.0f}s)")


def collect_msgs(store, args):
    """按通用参数(days/limit/no-cache)取消息。"""
    gid, gname = resolve_group(store, args.group)
    info(f"群 [{gname}] ({gid}) 采集消息中…")
    t0 = time.time()
    msgs = qanalysis.collect(store, gid, days=args.days, limit=args.limit)
    info(f"共 {len(msgs)} 条, 耗时 {time.time()-t0:.1f}s")
    return gid, gname, msgs


# ── 数据类命令 ───────────────────────────────────────────────────────────────

def cmd_groups(args):
    store = get_store(args)
    gs = store.groups_overview()
    names = store.group_names()
    for g in gs:
        g["name"] = g["name"] or names.get(g["id"], "")
        g["last"] = fmt_ts(g["last_ts"]) if g["last_ts"] else ""
    if args.json:
        return out_json(gs[:args.n])
    print(f"共 {len(gs)} 个有消息记录的群:\n")
    print(f"{pad('群号', 12)} {pad('消息数', 9, '>')}  "
          f"{pad('最近活跃', 18)} 群名")
    for g in gs[:args.n]:
        print(f"{pad(g['id'], 12)} {pad(g['count'], 9, '>')}  "
              f"{pad(g['last'], 18)} {g['name']}")


def cmd_read(args):
    store = get_store(args)
    after = before = None
    if args.date:
        after, before = parse_date(args.date), parse_date(args.date, end=True)
    else:
        if args.from_date:
            after = parse_date(args.from_date)
        if args.to_date:
            before = parse_date(args.to_date, end=True)
    if not after and args.days:
        after = int(time.time()) - args.days * 86400
    if args.c2c:
        msgs = store.read_c2c(args.c2c, limit=args.n, before_ts=before)
        me = store.account
        if args.json:
            return out_json(msgs)
        print(f"\n== 与 {args.c2c} 的私聊, 最新 {len(msgs)} 条 ==\n")
        for m in reversed(msgs):
            who = "我" if str(m["uin"]) == me else str(m["uin"])
            print(f"[{fmt_ts(m['ts'])}] {who}: {m['text']}")
        return
    gid, gname = resolve_group(store, args.group)
    msgs = store.read_group(gid, limit=args.n, before_ts=before,
                            after_ts=after, offset=args.offset)
    if args.json:
        for m in msgs:
            m["time"] = fmt_full(m["ts"])
        return out_json(msgs)
    print(f"\n== [{gname}] {len(msgs)} 条 ==\n")
    only_tags = qqlib.TAG_ONLY          # 与导出/上下文共用同一条判据
    for m in reversed(msgs):
        nick = store.nick_of(m["uin"], m["nick"])
        ks = []
        for md in m.get("media") or []:
            k = md.get("kind")
            if k not in ks:
                ks.append(k)
        extra = ("  " + " ".join("<%s>" % qanalysis.KIND_LABEL.get(k, k)
                                 for k in ks)) if ks else ""
        text = m["text"] or ""
        if ks and only_tags.match(text.strip()):
            text = ""            # 媒体已渲染, 不再重复 "[图片]" 这类占位
        print(f"[{fmt_ts(m['ts'])}] {nick}({m['uin']}): {text}{extra}")
        for md in m.get("media") or []:
            if md.get("kind") != "quote":
                continue
            qtx = re.sub(r"\s+", " ", md.get("name") or "").strip()
            if not qtx:
                continue
            qts = md.get("ts") or 0
            head = md.get("who") or ""
            if qts:
                head = (fmt_ts(qts, "%m-%d %H:%M") + " " + head).strip()
            print(f"{' ' * 12}└ 回复 {head}: 「{qtx[:110]}」")


def cmd_search(args):
    t0 = time.time()
    store = get_store(args)
    gid = None
    gname = ""
    if args.group:
        gid, gname = resolve_group(store, args.group)
    results = store.search(args.keyword, gid=gid, limit=args.n)
    # FTS 索引有延迟: 最新消息常常搜不到, 再扫一遍最近消息的正文兜底
    if gid and len(results) < args.n and args.scan:
        extra = qanalysis.search_recent(store, gid, args.keyword,
                                        limit=args.n, scan=args.scan)
        seen = {(r["ts"], (r["text"] or "")[:60]) for r in results}
        for r in extra:
            k = (r["ts"], (r["text"] or "")[:60])
            if k not in seen:
                seen.add(k)
                results.append(r)
        results.sort(key=lambda r: -(r["ts"] or 0))
        results = results[:args.n]
    if args.json:
        return out_json(results)
    print(f"\n找到 {len(results)} 条 (耗时 {time.time()-t0:.1f}s):\n")
    for r in results:
        nm = r.get("gname") or store.group_names().get(r["gid"], r["gid"])
        print(f"[{fmt_ts(r['ts'])}] {nm}: {r['text'][:120]}")


def cmd_stats(args):
    store = get_store(args)
    gid = None
    if args.group:
        gid, _ = resolve_group(store, args.group)
    daily = store.stats_daily(days=args.days, gid=gid)
    hours = store.stats_hours(gid=gid)
    top = store.stats_top(gid, 20) if gid else []
    if args.json:
        return out_json({"daily": daily, "hours": hours, "top": top})
    scope = f"群 [{store.group_names().get(gid, gid)}]" if gid else "全部群"
    print(f"{scope} 近 {args.days} 天统计\n")
    print(f"消息总量: {sum(d['n'] for d in daily)}")
    mx = max((d["n"] for d in daily), default=0) or 1
    for d in daily:
        print(f"  {d['d']}  {d['n']:>7}  {qanalysis.bar(d['n'], mx, 40)}")
    print("\n时段分布 (0-23时):")
    hmax = max(hours) or 1
    for i, n in enumerate(hours):
        print(f"  {i:02d}时  {n:>7}  {qanalysis.bar(n, hmax, 40)}")
    if top:
        print("\n发言 Top 20:")
        for i, s in enumerate(top, 1):
            print(f"  {i:>2}. {s['nick']}({s['uin']}): {s['n']}")


# ── 分析类命令 ───────────────────────────────────────────────────────────────

CMD_KEYS = {
    "analyze": ("heatmap", "top_speakers", "words", "night_owls", "emoji_rank",
                "repeat", "top_exact", "media_kinds", "lurkers", "daily"),
}


def _report(store, args, top_words_n=30):
    gid, gname = resolve_group(store, args.group)
    info(f"群 [{gname}] ({gid}) 分析中…")
    t0 = time.time()
    rep = qanalysis.report_cached(
        store, gid, days=args.days, limit=args.limit,
        top_words_n=top_words_n, no_cache=args.no_cache)
    info(f"完成, 耗时 {time.time()-t0:.1f}s ({rep.get('count', 0)} 条消息)")
    return rep


def cmd_analyze(args):
    store = get_store(args)
    rep = _report(store, args)
    if args.json:
        return out_json(rep)
    print(qanalysis.render_report(rep))


def cmd_heatmap(args):
    store = get_store(args)
    rep = _report(store, args)
    hm = rep.get("heatmap") or {}
    if args.json:
        return out_json({"gid": rep["gid"], "name": rep["name"],
                         "days": rep.get("days"), "heatmap": hm,
                         "hourly": rep.get("hourly"), "daily": rep.get("daily")})
    print(f"# {rep['name']} 活跃热力图 (周 × 时) · {rep['count']} 条消息")
    print(qanalysis.render_heatmap(hm))
    print("峰值: %s (%d 条)" % (hm["peak"]["label"], hm["peak"]["n"]))
    print("\n每小时总量:")
    hmax = max(rep["hourly"]) or 1
    for i, n in enumerate(rep["hourly"]):
        print(f"  {i:02d}时 {n:>7} {qanalysis.bar(n, hmax, 36)}")


def cmd_words(args):
    store = get_store(args)
    rep = _report(store, args, top_words_n=max(args.n, 30))
    ws = rep.get("words", [])[:args.n]
    if args.json:
        return out_json({"gid": rep["gid"], "name": rep["name"],
                         "days": rep.get("days"), "count": rep["count"],
                         "words": ws})
    print(f"# {rep['name']} 热词 Top {len(ws)} · {rep['count']} 条消息")
    mx = ws[0]["n"] if ws else 1
    for i, w in enumerate(ws, 1):
        print(f"{i:>3}. {w['w']:<16} {w['n']:>6}  {qanalysis.bar(w['n'], mx, 26)}"
              f"  ({w['u']}人)")
    if ws and ws[0].get("samples"):
        print("\n例句:")
        for w in ws[:5]:
            if w.get("samples"):
                print(f"  [{w['w']}] {w['samples'][0][:60]}")


def cmd_people(args):
    store = get_store(args)
    rep = _report(store, args)
    people = rep.get("top_speakers", [])[:args.n]
    if args.json:
        return out_json({"gid": rep["gid"], "name": rep["name"],
                         "days": rep.get("days"), "count": rep["count"],
                         "speakers": rep.get("speakers"),
                         "people": people})
    print(f"# {rep['name']} 成员画像 · {rep['count']} 条消息 / "
          f"{rep.get('speakers')} 人发言")
    print(f"{'#':>3} {pad('昵称', 20)} {'发言':>6} {'字数':>8} {'均长':>5} "
          f"{'图':>5} {'表情':>5} {'深夜':>5}  标签")
    for i, p in enumerate(people, 1):
        print(f"{i:>3} {pad(p['name'][:20], 20)} {p['n']:>6} {p['chars']:>8} "
              f"{p['avg_chars']:>5} {p['pic']:>5} {p['emoji']:>5} "
              f"{p['night']:>5}  {'/'.join(p.get('tags') or [])}")


def cmd_repeat(args):
    store = get_store(args)
    rep = _report(store, args)
    if args.json:
        return out_json({"gid": rep["gid"], "name": rep["name"],
                         "repeat": rep.get("repeat"),
                         "top_exact": rep.get("top_exact")})
    print(f"# {rep['name']} 复读 / 队形 (连续 ≥3 条同文)")
    for c in (rep.get("repeat") or [])[:args.n]:
        t = datetime.datetime.fromtimestamp(c["start_ts"]).strftime("%m-%d %H:%M")
        print(f"  x{c['n']:<3} {t}  {c['text']}  ({c['users']}人)")
    print(f"\n# 高频原句")
    for c in (rep.get("top_exact") or [])[:args.n]:
        print(f"  {c['n']:<6} {c['text']}")


def cmd_lurkers(args):
    store = get_store(args)
    rep = _report(store, args)
    lk = rep.get("lurkers") or {}
    if args.json:
        return out_json({"gid": rep["gid"], "name": rep["name"],
                         "days": rep.get("days"), **lk})
    if not lk.get("total"):
        print("拿不到群成员名单 (该群无 group_member3 记录)。")
        return
    print(f"# {rep['name']} 潜水情况")
    print(f"群成员 {lk['total']} 人, 期间零发言 {lk['silent']} 人 "
          f"({lk['silent_rate']}%)")
    print(f"\n发言最少 Top {args.n}:")
    for i, m in enumerate(lk["list"][:args.n], 1):
        print(f"  {i:>3}. {pad((m['name'] or '').strip()[:22], 22)} {m['n']} 条")


def cmd_night(args):
    store = get_store(args)
    rep = _report(store, args)
    if args.json:
        return out_json({"gid": rep["gid"], "name": rep["name"],
                         "night_owls": rep.get("night_owls"),
                         "hours": list(range(args.start, args.end))})
    print(f"# {rep['name']} 夜猫子榜 ({args.start}-{args.end} 点)")
    mx = (rep["night_owls"][0]["n"] if rep.get("night_owls") else 1)
    for i, x in enumerate((rep.get("night_owls") or [])[:args.n], 1):
        print(f"  {i:>3}. {pad(x['name'][:20], 20)} {x['n']:>6}  "
              f"{qanalysis.bar(x['n'], mx, 30)}")


# ── 导出 / 服务 ──────────────────────────────────────────────────────────────

def cmd_export(args):
    store = get_store(args)
    gid, gname = resolve_group(store, args.group)
    after = parse_date(args.from_date) if args.from_date else None
    before = parse_date(args.to_date, end=True) if args.to_date else None
    if not after and args.days:
        after = int(time.time()) - args.days * 86400
    fmt = args.fmt or (os.path.splitext(args.out)[1][1:] or "json")
    if fmt not in ("json", "jsonl", "txt", "csv", "md"):
        fmt = "json"
    n = store.export(gid, args.out, fmt=fmt, after_ts=after, before_ts=before)
    info(f"已导出 {n} 条消息 -> {args.out}")
    if args.json:
        return out_json({"gid": gid, "name": gname, "out": args.out,
                         "fmt": fmt, "count": n})


def cmd_export_report(args):
    """导出分析报告 (json / md)。"""
    store = get_store(args)
    rep = _report(store, args)
    path = args.out
    if path.lower().endswith(".md"):
        with open(path, "w", encoding="utf-8") as f:
            f.write(qanalysis.render_report(rep))
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
    info(f"报告已写入 {path}")
    if args.json:
        return out_json({"out": path, "gid": rep["gid"], "count": rep["count"]})


# ── Agent 工作流: 取「内容」→ 渲染 HTML ──────────────────────────────────────

def cmd_context(args):
    """导出适合 LLM 阅读的对话原料 (按话题间隙分段)。

    这是「内容分析」工作流的**第一步**: 本命令不产出任何结论, 只把原始对话
    整理成时间正序、分好段、媒体转成可读标记的样子, 交给 Agent 去读懂。
    """
    store = get_store(args)
    gid, gname = resolve_group(store, args.group)

    after = before = None
    if args.date:
        after, before = parse_date(args.date), parse_date(args.date, end=True)
    else:
        if args.from_date:
            after = parse_date(args.from_date)
        if args.to_date:
            before = parse_date(args.to_date, end=True)
    if not after and args.days:
        after = int(time.time()) - args.days * 86400

    limit = args.limit
    if limit is None and not after and not before:
        limit = 1500                    # 没给时间窗: 兜底只取最近 1500 条

    info(f"群 [{gname}] ({gid}) 取对话原料中…")
    t0 = time.time()
    msgs = qanalysis.collect(store, gid, limit=limit, after=after, before=before,
                             media_detail=True)
    info(f"共 {len(msgs)} 条, 耗时 {time.time()-t0:.1f}s")

    if not msgs:
        msg = "该时间窗内没有消息 (检查群名/日期, 或先用 groups 看看群列表)。"
        if args.json:
            return out_json({"gid": gid, "name": gname, "count": 0,
                             "segments": [], "text": ""})
        print(msg)
        return

    gmap = qanalysis.group_member_names(store, gid)
    segs = qanalysis.build_segments(msgs, gap_min=args.gap)
    a, b = qanalysis.segment_time(segs)
    speakers = len({str(m["uin"]) for m in qanalysis.speakers(msgs)})

    # 同一时间窗的 24 小时分布 (与报告窗口严格一致, 供 analysis.json 的 activity 用)
    hourly = [0] * 24
    for m in msgs:
        if m.get("ts"):
            hourly[datetime.datetime.fromtimestamp(m["ts"]).hour] += 1
    busy = " · ".join(f"{h:02d}时 {n}" for h, n in enumerate(hourly) if n)

    if args.json:
        return out_json({
            "gid": gid, "name": gname, "count": len(msgs),
            "speakers": speakers, "segments_n": len(segs),
            "first": fmt_full(a), "last": fmt_full(b),
            "hourly": hourly,
            "segments": qanalysis.context_json(segs, store, gmap),
        })

    rng = (f"{fmt_full(a)} → {fmt_full(b)}" if a else "")
    kicker = (f"# {gname} ({gid})\n"
              f"{rng} · {len(msgs)} 条消息 · {speakers} 人发言 · "
              f"{len(segs)} 个话题段\n"
              f"每小时消息量: {busy}\n"
              f"（原始聊天记录, 已按话题间隙 {args.gap} 分钟分段, 时间正序）")
    text = qanalysis.render_context(segs, store, gmap=gmap,
                                    max_chars=args.max_chars, kicker=kicker)
    # 原料体积直接决定「一次能不能读完」, 顺手报出来, 免得 Agent 拿到才发现截断
    info(f"原料 {len(text):,} 字符 · 约 {qanalysis.est_tokens(text):,} token")
    if args.max_chars and len(text) >= args.max_chars * 0.98:
        info(f"⚠ 已触及 --max-chars {args.max_chars:,} 上限, "
             f"最早的段被丢弃 —— 需要完整内容时请缩小时间窗分批取")
    print(text)


def cmd_html(args):
    """把分析 JSON 渲染成单文件 HTML 报告。"""
    import qreport
    if args.input == "-":
        data = json.load(sys.stdin)
    else:
        with open(args.input, encoding="utf-8-sig") as f:
            data = json.load(f)
    doc = qreport.render(data, theme=args.theme,
                         avatar=not getattr(args, "no_avatar", False))
    out = args.out or (os.path.splitext(args.input)[0] + ".html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    n_av = len(qreport.AVATARS)
    info(f"已生成 HTML 报告 -> {out} ({len(doc)/1024:.1f} KB, 头像 {n_av} 张)")
    if args.json:
        return out_json({"out": out, "bytes": len(doc), "avatars": n_av})


def cmd_schema(args):
    import qreport
    print(qreport.SCHEMA)


def cmd_example(args):
    import qreport
    out_json(qreport.EXAMPLE)


def cmd_validate(args):
    """校验分析 JSON: 结构 / 昵称→QQ号回填 / 金句原文核对 / 署名反查。

    `cli.py html` 是宽容的 —— 字段名写错它只是静默不渲染那一块, 报告「莫名其妙
    少了一节」却没人知道。这一步把这类问题在渲染前挑出来, 顺便用原料做几项
    html 做不到的检查: 金句是不是原文、是不是这个人说的。
    """
    import qvalidate
    if args.input == "-":
        data = json.load(sys.stdin)
    else:
        with open(args.input, encoding="utf-8-sig") as f:
            data = json.load(f)

    ctx = None
    if args.context:
        ctx = qvalidate.load_context(args.context)
    elif args.group:
        store = get_store(args)
        gid, gname = resolve_group(store, args.group)
        info(f"群 [{gname}] ({gid}) 建校验基准中…")
        ctx = qvalidate.load_from_store(store, gid, days=args.days)

    rep = qvalidate.validate(data, ctx)
    if ctx:
        info("基准: %d 条消息 · %d 个昵称 · %d 个重名昵称"
             % (ctx.get("n_lines", 0), len(ctx.get("nick2uin") or {}),
                len(ctx.get("dups") or {})))
    elif not args.group and not args.context:
        info("未提供 --context/--group: 只能做结构校验 (无法核对原文与署名)")

    if args.json:
        out_json(rep.to_dict())
    else:
        print(rep.text())
        empty = [k for k, n in qvalidate.render_coverage(rep.data) if n == 0]
        if empty:
            print("\n(空板块, 报告里不会出现: %s)" % ", ".join(empty))

    if args.fix:
        text = json.dumps(rep.data, ensure_ascii=False, indent=1)
        dest = args.out
        if dest is None:
            dest = ("-" if args.input == "-"
                    else os.path.splitext(args.input)[0] + ".fixed.json")
        if dest == "-":
            if not args.json:
                print("\n" + text)
        else:
            with open(dest, "w", encoding="utf-8") as f:
                f.write(text)
            info("已写出修复版 -> %s" % dest)
    args.exit_code = 0 if rep.ok else 1
    return args.exit_code


def cmd_web(args):
    from web import run
    run(port=args.port, account=args.account)


def cmd_refresh(args):
    """无条件重新提取密钥 + 解密 (底层命令, 不做新鲜度判断)。"""
    acct = args.account or qqlib.detect_account() or qqlib.qpaths.detect_source_account()
    info(f"重新提取密钥并解密账号 {acct} (需QQ在线)...")
    ok, msg = qqlib.sync_latest(acct, quiet=False)
    info(("✓ " if ok else "✗ ") + msg)
    sys.exit(0 if ok else 1)


def cmd_fresh(args):
    """检查 / 同步最新消息 (比较源库与解密产物)。"""
    acct = args.account or qqlib.detect_account() or qqlib.qpaths.detect_source_account()
    fr = qqlib.freshness(acct)
    if args.check:
        if args.json:
            return out_json(fr)
        info(f"账号: {acct}")
        info(f"源库最新: {fr['src_mtime_str'] or '未找到'}  {fr['src_dir'] or ''}")
        info(f"解密产物: {fr['out_mtime_str'] or '未找到'}")
        if fr["stale"]:
            info(f"→ 需要同步: 落后约 {max(1, fr['lag_sec'] // 60)} 分钟")
        else:
            info("→ 已是最新, 无需同步")
        return
    if not fr["src_mtime"] and not fr["out_mtime"]:
        raise SystemExit("未找到源库或快照；请配置 QQ_SOURCE_DIR 或指定 --account")
    if not fr["stale"] and not args.force:
        info(f"数据已是最新 (产物 {fr['out_mtime_str']})")
        if args.json:
            return out_json({**fr, "synced": False})
        return
    if fr["qq_running"] is False:
        info("✗ QQ 未运行, 无法提取密钥; 请先启动 QQ 再试")
        sys.exit(2)
    info(f"正在解密最新消息 (账号 {acct})…")
    t0 = time.time()
    ok, msg = qqlib.sync_latest(acct, quiet=False)
    if args.json:
        return out_json({**fr, "synced": ok, "msg": msg})
    info(("✓ " if ok else "✗ ") + f"{msg} (耗时 {time.time() - t0:.0f}s)")
    sys.exit(0 if ok else 1)


# ── 参数 ─────────────────────────────────────────────────────────────────────

def build_parser():
    # 通用开关: 主解析器 + 每个子命令都能识别(--json 放前面或后面都行)。
    # 子解析器用 SUPPRESS 默认值, 避免「默认值覆盖主解析器已解析的值」。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS, help="输出 JSON (机器可读)")
    common.add_argument("--no-cache", action="store_true",
                        default=argparse.SUPPRESS, help="忽略分析缓存")
    common.add_argument("--fresh", action="store_true",
                        default=argparse.SUPPRESS,
                        help="强制重新解密, 确保基于最新消息")
    common.add_argument("--no-fresh", action="store_true",
                        default=argparse.SUPPRESS,
                        help="跳过数据新鲜度检查 (直接用当前快照)")
    common.add_argument("--account", default=argparse.SUPPRESS,
                        help="指定QQ号")

    ap = argparse.ArgumentParser(
        description="QQ 聊天记录 CLI (NTQQ 解密库) — 数据查询 + 群消息分析",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--account", help="指定QQ号 (默认自动选最新解密账号)")
    ap.add_argument("--json", action="store_true", help="输出 JSON (机器可读)")
    ap.add_argument("--no-cache", action="store_true", help="忽略分析缓存")
    ap.add_argument("--fresh", action="store_true",
                    help="强制重新解密, 确保基于最新消息")
    ap.add_argument("--no-fresh", action="store_true",
                    help="跳过数据新鲜度检查")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, help_, fn, **kw):
        p = sub.add_parser(name, help=help_, parents=[common], **kw)
        p.set_defaults(fn=fn)
        return p

    p = add("groups", "群列表", cmd_groups)
    p.add_argument("-n", type=int, default=50)

    p = add("read", "读消息", cmd_read)
    p.add_argument("group", nargs="?", help="群号或群名片段")
    p.add_argument("--c2c", help="对方QQ号 (读私聊)")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--date", help="某天 2026-10-01")
    p.add_argument("--from", dest="from_date", help="起始日期")
    p.add_argument("--to", dest="to_date", help="结束日期")
    p.add_argument("--days", type=int, help="最近 N 天")
    p.add_argument("--offset", type=int, default=0, help="翻页偏移")
    p.add_argument("--limit", type=int)

    p = add("search", "全文搜索", cmd_search)
    p.add_argument("keyword")
    p.add_argument("-g", "--group", help="限定群号/群名")
    p.add_argument("-n", type=int, default=30)
    p.add_argument("--scan", type=int, default=3000,
                   help="FTS 结果不足时, 额外扫最近 N 条正文兜底 (0=关闭)")

    p = add("stats", "消息统计 (日/时段/Top)", cmd_stats)
    p.add_argument("-g", "--group", help="群号/群名 (默认全部)")
    p.add_argument("--days", type=int, default=30)

    # 分析类: 统一带 group / days / limit
    for name, help_, fn in (
            ("analyze", "综合报告 (热力图+排行+热词+复读+潜水)", cmd_analyze),
            ("heatmap", "活跃热力图 (周×时)", cmd_heatmap),
            ("words", "热词榜", cmd_words),
            ("people", "成员画像 + 自动标签", cmd_people),
            ("repeat", "复读/队形检测", cmd_repeat),
            ("lurkers", "潜水榜", cmd_lurkers),
            ("night", "夜猫子榜", cmd_night)):
        p = add(name, help_, fn)
        p.add_argument("group", help="群号或群名片段")
        p.add_argument("--days", type=int, help="只看最近 N 天")
        p.add_argument("--limit", type=int, help="最多分析 N 条 (从最新往回)")
        if name in ("words", "people", "repeat", "lurkers", "night"):
            p.add_argument("-n", type=int, default=20, help="显示条数")
        if name == "night":
            p.add_argument("--start", type=int, default=0)
            p.add_argument("--end", type=int, default=6)

    p = add("export", "导出聊天记录 (json/jsonl/txt/csv/md)", cmd_export)
    p.add_argument("group")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--fmt", choices=["json", "jsonl", "txt", "csv", "md"])
    p.add_argument("--from", dest="from_date")
    p.add_argument("--to", dest="to_date")
    p.add_argument("--days", type=int)

    p = add("report", "导出分析报告文件 (.json/.md)", cmd_export_report)
    p.add_argument("group")
    p.add_argument("-o", "--out", required=True, help="输出路径 (.json 或 .md)")
    p.add_argument("--days", type=int)
    p.add_argument("--limit", type=int, help="最多分析 N 条")

    # ── Agent 工作流: 取内容 → 渲染 HTML ──
    p = add("context", "取对话原料 (分段后的原始记录, 供 Agent 阅读)", cmd_context)
    p.add_argument("group", help="群号或群名片段")
    p.add_argument("--date", help="某天 2026-10-02")
    p.add_argument("--from", dest="from_date", help="起始日期")
    p.add_argument("--to", dest="to_date", help="结束日期")
    p.add_argument("--days", type=int, help="最近 N 天")
    p.add_argument("--limit", type=int, help="最多 N 条 (无时间窗时默认 1500)")
    p.add_argument("--gap", type=int, default=30,
                   help="话题分段间隔(分钟), 默认 30")
    p.add_argument("--max-chars", type=int, default=60000,
                   help="输出长度上限, 超出则丢弃最早的段")

    p = add("html", "把分析 JSON 渲染成单文件 HTML 报告", cmd_html)
    p.add_argument("input", help="分析 JSON 路径 (- 表示 stdin)")
    p.add_argument("-o", "--out", help="输出 HTML 路径 (默认同名 .html)")
    p.add_argument("--theme", choices=["dark", "light"], default="light")
    p.add_argument("--no-avatar", action="store_true",
                   help="不联网取 QQ 头像 (全部用首字渐变)")

    p = add("validate", "校验分析 JSON (结构/QQ号回填/原文核对)", cmd_validate)
    p.add_argument("input", help="分析 JSON 路径 (- 表示 stdin)")
    p.add_argument("--context", help="cli context 输出的原料文件 (最严格)")
    p.add_argument("--group", help="群号或群名 (没有原料文件时用)")
    p.add_argument("--days", type=int, help="配合 --group: 只取最近 N 天")
    p.add_argument("--fix", action="store_true", help="产出修复后的 JSON")
    p.add_argument("-o", "--out", help="修复版输出路径 (默认 <原名>.fixed.json)")

    p = add("web", "启动可视化 Web", cmd_web)
    p.add_argument("--port", type=int, default=8765)

    p = add("fresh", "同步最新消息 (检测源库更新并重新解密)", cmd_fresh)
    p.add_argument("--check", action="store_true",
                   help="只检查是否陈旧, 不刷新")
    p.add_argument("--force", action="store_true",
                   help="即使不陈旧也强制刷新")

    add("refresh", "无条件重新提取密钥+解密 (需QQ在线)", cmd_refresh)
    add("schema", "显示脱敏后的报告字段说明", cmd_schema)
    add("example", "显示使用身份占位符的分析示例", cmd_example)
    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()
    try:
        args.fn(args)
    except KeyboardInterrupt:
        info("\n已中断")
        sys.exit(130)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.exit(0)
    # 校验类命令用退出码表达结果 (有 error 就非 0), 便于脚本判断 / 接到工作流里
    rc = getattr(args, "exit_code", None)
    if rc is not None:
        sys.exit(rc)


if __name__ == "__main__":
    main()
