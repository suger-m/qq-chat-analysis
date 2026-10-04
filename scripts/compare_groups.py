# -*- coding: utf-8 -*-
"""compare_groups.py — 多群横向对比 (CLI 没有的能力)

对若干群跑同一套统计, 输出对比表 (Markdown 或 JSON), 用于回答
「这几个群哪个更活跃」「A 群和 B 群在聊什么不一样」这类问题。

用法:
  python compare_groups.py --days 30 "前端摸鱼" "算法日常"
  python compare_groups.py --days 30 --json "前端摸鱼" "算法日常"
  python compare_groups.py --days 30 --limit 200000 --md "群A" "群B" > cmp.md

群名可以用片段(同 cli.py 的解析规则)。需要设置 QQ_PY / QQ_DATA_DIR 时
参考 references/install.md；可通过统一入口 scripts/qq.py compare 运行。
"""
import os
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))


sys.path.insert(0, os.path.join(HERE, 'runtime'))

import qqlib                      # noqa: E402
import qanalysis                  # noqa: E402
import cli                        # noqa: E402


def resolve(store, token):
    """与 cli.py 一致的群名解析 (片段匹配, 取消息最多者)。"""
    token = str(token or "").strip()
    names = store.group_names()
    gs = {g["id"]: g for g in store.groups_overview()}
    if token in gs:
        return token, names.get(token, token)
    low = token.lower()
    hits = [(gid, nm) for gid, nm in names.items() if low in (nm or "").lower()]
    if not hits:
        raise SystemExit(f"找不到群: {token!r}")
    hits.sort(key=lambda x: -(gs.get(x[0], {}).get("count") or 0))
    return hits[0]


def summarize(store, gid, name, days, limit):
    rep = qanalysis.report_cached(store, gid, days=days, limit=limit)
    hm = rep.get("heatmap") or {}
    daily = rep.get("daily") or []
    lk = rep.get("lurkers") or {}
    return {
        "gid": gid,
        "name": name,
        "count": rep.get("count", 0),
        "speakers": rep.get("speakers", 0),
        "days": rep.get("days"),
        "first": (rep.get("range") or {}).get("first_str"),
        "last": (rep.get("range") or {}).get("last_str"),
        "peak": (hm.get("peak") or {}).get("label"),
        "peak_n": (hm.get("peak") or {}).get("n"),
        "avg_per_day": round(
            sum(d["n"] for d in daily) / len(daily), 1) if daily else 0,
        "top1": (rep.get("top_speakers") or [{}])[0].get("name") if
                rep.get("top_speakers") else None,
        "top1_n": (rep.get("top_speakers") or [{}])[0].get("n") if
                  rep.get("top_speakers") else 0,
        "members": lk.get("total"),
        "silent_rate": lk.get("silent_rate"),
        "media": {m["kind"]: m["n"] for m in (rep.get("media_kinds") or [])},
        "words": [w["w"] for w in (rep.get("words") or [])[:30]],
        "_rep": rep,
    }


def render_md(rows):
    L = ["# 群聊横向对比", ""]
    hdr = ["群名", "消息数", "发言人数", "日均", "峰值时段", "最活跃", "成员", "潜水率"]
    L.append("| " + " | ".join(hdr) + " |")
    L.append("|" + "---|" * len(hdr))
    for r in rows:
        L.append("| %s | %d | %d | %s | %s | %s (%d) | %s | %s%% |" % (
            r["name"], r["count"], r["speakers"], r["avg_per_day"],
            r["peak"] or "-", r["top1"] or "-", r["top1_n"],
            r["members"] if r["members"] is not None else "-",
            r["silent_rate"] if r["silent_rate"] is not None else "-"))
    L.append("")
    L.append("## 内容构成")
    L.append("")
    kinds = []
    for r in rows:
        for k in r["media"]:
            if k not in kinds:
                kinds.append(k)
    if kinds:
        L.append("| 群名 | " + " | ".join(
            qanalysis.KIND_LABEL.get(k, k) for k in kinds) + " |")
        L.append("|" + "---|" * (len(kinds) + 1))
        for r in rows:
            L.append("| %s | " % r["name"] + " | ".join(
                str(r["media"].get(k, 0)) for k in kinds) + " |")
    L.append("")
    L.append("## 热词对照 (各自 Top 10)")
    L.append("")
    for r in rows:
        L.append("- **%s**: %s" % (r["name"], " / ".join(r["words"][:10])))
    # 热词重叠
    if len(rows) >= 2:
        common = set(rows[0]["words"])
        for r in rows[1:]:
            common &= set(r["words"])
        L.append("")
        L.append("**共同热词** (各群 Top30 交集): "
                 + ("、".join(sorted(common)) if common else "无"))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="多群聊记录横向对比")
    ap.add_argument("groups", nargs="+", help="群号或群名片段 (可多个)")
    ap.add_argument("--days", type=int, help="只看最近 N 天")
    ap.add_argument("--limit", type=int, help="每群最多分析 N 条")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--md", action="store_true", help="输出 Markdown 表格")
    ap.add_argument("--account", help="指定QQ号")
    ap.add_argument('--no-fresh', action='store_true', help='使用当前快照')
    args = ap.parse_args()

    store = cli.get_store(args)
    rows = []
    for tok in args.groups:
        gid, name = resolve(store, tok)
        print(f"分析 [{name}] ({gid}) …", file=sys.stderr)
        rows.append(summarize(store, gid, name, args.days, args.limit))

    if args.json:
        for r in rows:
            r.pop("_rep", None)
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
        return
    if args.md:
        print(render_md(rows))
        return
    # 默认也给 Markdown
    print(render_md(rows))


if __name__ == "__main__":
    main()
