# -*- coding: utf-8 -*-
"""qvalidate.py — 分析 JSON 的校验与自动修复

为什么需要它
============
`cli.py html` 是**宽容**的: 字段名拼错、类型不对、百分比超 100, 它都不报错,
只是把那块**静默不渲染**。结果就是「报告少了一整节, 但没人发现」。

astrbot 那个项目的做法值得借鉴 —— 它的分析器拿到 LLM 输出后不是直接用了,
而是先过一遍 `validate_parsed_data()`, 不过就带着报错信息重试。我们没有 LLM
调用环节, 但**有一件它没有的东西: 原始聊天记录**。所以这里的校验比它更强:

  · 结构校验      字段名/类型/必填, 把「拼错导致静默丢块」变成明确报错
  · 昵称→QQ号回填 用成员对照表补 `uin`, 唯一命中才自动补, 重名就要求手填
  · 原文核对      金句/附录的 `text` 必须能在原料里找到 (抓转述与幻觉)
  · 署名反查      找到原文后, 核对它**是不是这个人说的** (抓张冠李戴)
  · 系统消息检测  引用了撤回/戳一戳这类灰条时告警 (那不是任何人的发言)

用法
====
    python cli.py validate analysis.json --context context.txt   # 最严格
    python cli.py validate analysis.json --group <群ID>       # 只查结构+成员
    python cli.py validate analysis.json --context ctx.txt --fix -o fixed.json
"""
from __future__ import annotations

import json
import os
import re
import sys
import qprivacy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── 顶层字段表 ────────────────────────────────────────────────────────────────
# 只有这里列出的键会被 qreport 渲染; 表外的一律当拼写错误报出来。
SCHEMA = {
    "group": str, "gid": (str, int), "kicker": str, "period": str,
    "generated": str, "headline": str, "title_quality": str,
    "stats": list, "activity": list, "summary": (list, str),
    "quality": dict, "topics": list, "qa": list, "todos": list,
    "people": list, "quotes": list, "insights": list, "risks": list,
    "timeline": list, "appendix": list,
}

# 需要「人名 + 可选 QQ号」的字段位置
# (板块, 列表内取值方式, 名字字段, QQ号字段)
PERSON_SLOTS = [
    ("people", None, "name", "uin"),
    ("quotes", None, "who", "uin"),
    ("appendix", None, "who", "uin"),
    ("topics", "quotes", "who", "uin"),
    ("qa", None, "asker", "asker_uin"),
    ("qa", None, "answerer", "answerer_uin"),
    ("todos", None, "owner", None),
]

# 需要做「原文核对」的字段
VERBATIM_SLOTS = [
    ("quotes", None, "text"),
    ("appendix", None, "text"),
    ("topics", "quotes", "text"),
    ("people", None, "quote"),
]

_RISK_LEVELS = ("info", "warn", "bad")
# 媒体占位标签在核对时要忽略 ("[图片 802×958]" 这类是程序加的, 不是原文)
_TAG = re.compile(r"\[[^\[\]]{1,16}\]")
_WS = re.compile(r"\s+")
_INVIS = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff"
    "\u115f\u1160\u3164\uffa0\u00ad]")


def _squash(s):
    """归一化用于比对的文本: 去不可见字符/空白/媒体标签, 统一大小写。"""
    s = _INVIS.sub("", str(s or ""))
    s = _TAG.sub("", s)
    return _WS.sub("", s).lower()


class Report:
    """校验结果收集器。"""

    def __init__(self):
        self.errors = []          # 会导致渲染失败/丢块
        self.warnings = []        # 会影响质量 (署名错、疑似转述)
        self.infos = []           # 提示
        self.fixes = []           # 已自动修的问题
        self.data = None          # 修复后的数据 (--fix 时用)

    def add(self, level, path, msg):
        getattr(self, {"error": "errors", "warn": "warnings",
                       "info": "infos"}[level]).append(
            {"path": path, "msg": msg})

    def fix(self, path, msg):
        self.fixes.append({"path": path, "msg": msg})

    @property
    def ok(self):
        return not self.errors

    def to_dict(self):
        return {"ok": self.ok, "errors": self.errors,
                "warnings": self.warnings, "infos": self.infos,
                "fixes": self.fixes}

    def text(self):
        L = []
        tag = {"errors": ("✗ 错误", "error"), "warnings": ("⚠ 警告", "warn"),
               "infos": ("· 提示", "info"), "fixes": ("✔ 已修", "fix")}
        for k in ("errors", "warnings", "fixes", "infos"):
            items = getattr(self, k)
            if not items:
                continue
            L.append("%s (%d)" % (tag[k][0], len(items)))
            for it in items:
                L.append("    %-28s %s" % (it["path"], it["msg"]))
            L.append("")
        if not L:
            L = ["✔ 未发现问题。"]
        return qprivacy.diagnostic("\n".join(L).rstrip())


# ── 原料加载 ──────────────────────────────────────────────────────────────────

_MEMBER_CELL = re.compile(r"^(.+?)⚠?=(\d{5,})$")
# 重名提示行: `# ⚠ 以下昵称重名, 必须用 QQ号区分: 示例成员=<成员甲_ID>/<成员乙_ID> | Echo=a/b/c`
_DUP_ROW = re.compile(r"([^\s=|]+)=((?:\d{5,})(?:/\d{5,})+)")
# 消息行: "17:40 星辰: 内容" 或 "10-03 00:20 系统: 内容"
_MSG_LINE = re.compile(r"^(?:\d{2}-\d{2} )?\d{2}:\d{2} (.+?): ?(.*)$")


def load_context(path):
    """从 `cli context` 的原料文件里解析出: 成员表 / 重名关系 / 原文索引 / 系统消息。

    解析原料而不是重查数据库, 有两个好处:
      1. 校验的基准与 Agent **读到的东西**严格一致 (不会因窗口不同而误报);
      2. 不需要再连一次解密库。
    """
    with open(path, encoding="utf-8-sig") as f:
        raw = f.read()
    nick2uin, dups = {}, {}
    index, system_texts, lines = {}, set(), []
    for ln in raw.splitlines():
        if ln.startswith("#"):
            if "⚠" in ln and "重名" in ln:
                for nm, us in _DUP_ROW.findall(ln.split(":", 1)[-1]):
                    dups[nm] = sorted(set(dups.get(nm, []) + us.split("/")))
                continue
            # 成员表是一整块 `#` 注释; 网格行用 2+ 空格对齐, 不能按单空格切
            for c in re.split(r"\s{2,}", ln.lstrip("#").strip()):
                m = _MEMBER_CELL.match(c.strip())
                if m:
                    nick2uin.setdefault(m.group(1).strip(), m.group(2))
            continue
        if ln.startswith("##"):
            continue
        m = _MSG_LINE.match(ln)
        if not m:
            continue
        who, text = m.group(1).strip(), m.group(2).strip()
        lines.append((who, text))
        if who == "系统":
            system_texts.add(_squash(text))
            continue
        u = nick2uin.get(who)
        sq = _squash(text)
        if sq:
            index.setdefault(sq, set())
            if u:
                index[sq].add(u)
    # 兜底: 同一昵称在表里出现两次也算重名 (与 ⚠ 行合并, 不覆盖)
    seen = {}
    for nm, u in nick2uin.items():
        seen.setdefault(nm, set()).add(u)
    for nm, us in seen.items():
        if len(us) > 1:
            dups[nm] = sorted(set(dups.get(nm, [])) | us)
    return {"nick2uin": nick2uin, "dups": dups, "index": index,
            "system_texts": system_texts, "squashed": _squash(raw),
            "n_lines": len(lines)}


def load_from_store(store, gid, limit=30000, days=None):
    """没有原料文件时, 退而求其次从库里现取 (结构+成员可用, 原文核对较弱)。"""
    import qanalysis
    msgs = qanalysis.collect(store, gid, limit=limit, days=days,
                             media_detail=False)
    gmap = qanalysis.group_member_names(store, gid)
    nick2uin = {}
    index, system_texts = {}, set()
    seen = {}
    for u, nm in gmap.items():
        seen.setdefault(nm, set()).add(str(u))
        nick2uin.setdefault(nm, u)
    for m in msgs:
        sq = _squash(m.get("text"))
        if not sq:
            continue
        if m.get("system"):
            system_texts.add(sq)
            continue
        index.setdefault(sq, set())
        if m.get("uin"):
            index[sq].add(str(m["uin"]))
    return {"nick2uin": nick2uin,
            "dups": {n: sorted(v) for n, v in seen.items() if len(v) > 1},
            "index": index, "system_texts": system_texts,
            "squashed": _squash("".join(_squash(m.get("text")) for m in msgs)),
            "n_lines": len(msgs)}


# ── 校验主流程 ────────────────────────────────────────────────────────────────

def validate(data, ctx=None, strict_verbatim=True):
    """校验并（就地，在副本上）修复分析 JSON。返回 Report。"""
    r = Report()
    if not isinstance(data, dict):
        r.add("error", "(root)", "分析 JSON 顶层必须是对象 {…}, 实际是 %s"
              % type(data).__name__)
        r.data = data
        return r
    d = json.loads(json.dumps(data, ensure_ascii=False))     # 深拷贝
    nick2uin = dict((ctx or {}).get("nick2uin") or {})
    dups = dict((ctx or {}).get("dups") or {})
    index = dict((ctx or {}).get("index") or {})
    system_texts = set((ctx or {}).get("system_texts") or {})
    squashed = (ctx or {}).get("squashed") or ""

    _check_root(d, r)
    _check_quality(d, r, nick2uin)
    _backfill_uins(d, r, nick2uin, dups)
    if strict_verbatim and (index or squashed):
        _check_verbatim(d, r, index, system_texts, squashed)
    _check_counts(d, r)
    _check_risks(d, r)
    r.data = d
    return r


def _check_root(d, r):
    for k in d:
        if k not in SCHEMA:
            r.add("warn", k, "报告不认识这个字段 (拼写错误? 它不会被渲染)")
            continue
        want = SCHEMA[k]
        if not isinstance(d[k], want):
            names = (want.__name__ if isinstance(want, type)
                     else "/".join(t.__name__ for t in want))
            r.add("error", k, "类型应为 %s, 实际 %s"
                  % (names, type(d[k]).__name__))
    if not any(k in d for k in
               ("topics", "quality", "people", "quotes", "summary", "insights")):
        r.add("error", "(root)", "没有任何可渲染板块: 至少要有 "
              "summary / quality / topics / people / quotes / insights 之一")
    if d.get("gid") and not re.fullmatch(r"\d{5,}", str(d["gid"])):
        r.add("warn", "gid", "群号看起来不像数字: %r" % d["gid"])


def _num(v):
    """容忍 "42%" / "42 左右" 这类写法。"""
    try:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        m = re.search(r"-?\d+(?:\.\d+)?", str(v))
        return float(m.group()) if m else None
    except (TypeError, ValueError):
        return None


def _check_quality(d, r, nick2uin):
    q = d.get("quality")
    if q is None:
        return
    if not isinstance(q, dict):
        r.add("error", "quality", "应为对象 {…}")
        return
    dims = q.get("dimensions")
    if not isinstance(dims, list) or not dims:
        r.add("error", "quality.dimensions", "应为非空数组")
        return
    if len(dims) > 8:
        r.add("info", "quality.dimensions", "维度 %d 个, 建议 3-6 个" % len(dims))
    names = set(nick2uin)
    total, clean = 0.0, []
    for i, x in enumerate(dims):
        p = "quality.dimensions[%d]" % i
        if not isinstance(x, dict):
            r.add("error", p, "应为对象")
            continue
        nm = str(x.get("name") or "").strip()
        if not nm:
            r.add("error", p + ".name", "缺失或为空")
        else:
            if len(nm) > 12:
                r.add("warn", p + ".name",
                      "维度名 %d 字, 要求 2-6 字的高层抽象 (具体内容放 comment)"
                      % len(nm))
            if nm in names:
                r.add("warn", p + ".name",
                      "「%s」是群成员昵称, 维度名必须抽象, 不能出现具体人名" % nm)
            x["name"] = nm
        v = _num(x.get("percentage", x.get("pct")))
        if v is None:
            r.add("warn", p + ".percentage", "不是数字 (%r), 已按 0 处理"
                  % x.get("percentage"))
            v = 0.0
        elif v != x.get("percentage"):
            x["percentage"] = v
            r.fix(p + ".percentage", "转成数字 %g" % v)
        v = max(0.0, min(100.0, v))
        x["percentage"] = v
        total += v
        if not str(x.get("comment") or "").strip():
            r.add("warn", p + ".comment", "缺少点评 (comment)")
        clean.append(x)
    if total > 100.0001 and clean:
        f = 100.0 / total
        for x in clean:
            x["percentage"] = round(x["percentage"] * f, 1)
        r.fix("quality.dimensions[*].percentage",
              "总和 %.1f%% 超 100%%, 按 %.3f 等比缩放" % (total, f))
        total = 100.0
    if abs(total - 100) > 12:
        r.add("info", "quality.dimensions",
              "占比合计 %.1f%% (判读上一般应接近 100%%)" % total)
    if not str(q.get("summary") or "").strip():
        r.add("info", "quality.summary", "缺少全群表现的一句话总结")


def _iter_people(d, slot, sub):
    """产出 (path, 条目dict) —— sub 为 None 时是板块本身, 否则是它的子列表。"""
    sec = d.get(slot)
    if not isinstance(sec, list):
        return
    if sub is None:
        for i, it in enumerate(sec):
            if isinstance(it, dict):
                yield "%s[%d]" % (slot, i), it
        return
    for i, it in enumerate(sec):
        if not isinstance(it, dict):
            continue
        lst = it.get(sub)
        if not isinstance(lst, list):
            continue
        for j, e in enumerate(lst):
            if isinstance(e, dict):
                yield "%s[%d].%s[%d]" % (slot, i, sub, j), e


def _resolve(name, nick2uin, dups):
    """昵称 → (uin, 状态)。状态: ok / dup / miss"""
    if not name:
        return None, "miss"
    if name in dups:
        return None, "dup"
    u = nick2uin.get(name)
    return (str(u), "ok") if u else (None, "miss")


def _backfill_uins(d, r, nick2uin, dups):
    if not nick2uin:
        return
    for slot, sub, nf, uf in PERSON_SLOTS:
        for path, it in _iter_people(d, slot, sub):
            nm = str(it.get(nf) or "").strip()
            if uf is None:                      # 只需名字的槽位 (todos.owner)
                if nm and _resolve(nm, nick2uin, dups)[1] == "miss":
                    r.add("info", path + "." + nf, "「%s」不在成员表里" % nm)
                continue
            cur = it.get(uf)
            if cur not in (None, ""):
                if re.fullmatch(r"\d{5,}", str(cur)):
                    continue                    # 已经是合规 QQ 号
                # 写成了昵称/带括号/带空格
                got, st = _resolve(str(cur).strip().strip("[]"), nick2uin, dups)
                if st == "ok":
                    it[uf] = got
                    r.fix(path + "." + uf, "%r → %s" % (cur, got))
                    continue
                r.add("warn", path + "." + uf,
                      "不是有效 QQ 号 (%r), 也无法从昵称还原" % cur)
                continue
            if not nm:
                r.add("error", path + "." + nf, "条目缺少人名")
                continue
            u, st = _resolve(nm, nick2uin, dups)
            if st == "ok":
                it[uf] = u
                r.fix(path + "." + uf, "按昵称「%s」补上 %s" % (nm, u))
            elif st == "dup":
                r.add("warn", path + "." + uf,
                      "昵称「%s」在本群重名 (%s), 必须手填 QQ号, 否则头像会配错人"
                      % (nm, " / ".join(dups[nm])))
            else:
                r.add("info", path + "." + uf,
                      "「%s」不在成员表里 → 只能用首字头像" % nm)

    # contributors 既可能是 QQ号, 也可能是 {name, uin} 或昵称
    sec = d.get("topics")
    if isinstance(sec, list):
        for i, t in enumerate(sec):
            if not isinstance(t, dict):
                continue
            cs = t.get("contributors")
            if not isinstance(cs, list):
                continue
            out = []
            for j, c in enumerate(cs):
                p = "topics[%d].contributors[%d]" % (i, j)
                if isinstance(c, dict):
                    nm = str(c.get("name") or "").strip()
                    if not c.get("uin"):
                        u, st = _resolve(nm, nick2uin, dups)
                        if st == "ok":
                            c["uin"] = u
                            r.fix(p + ".uin", "补上 %s" % u)
                    out.append(c)
                    continue
                s = str(c).strip()
                if re.fullmatch(r"\d{5,}", s):
                    out.append(s)
                    continue
                u, st = _resolve(s.strip("[]"), nick2uin, dups)
                if st == "ok":
                    out.append(u)
                    r.fix(p, "%r → %s" % (c, u))
                elif st == "dup":
                    r.add("warn", p, "「%s」重名 (%s), 必须手填 QQ号"
                          % (s, " / ".join(dups[s])))
                    out.append(s)
                else:
                    out.append(s)
            t["contributors"] = out


def _check_verbatim(d, r, index, system_texts, squashed):
    """金句/附录必须来自原料, 且署名要对得上。"""
    for slot, sub, tf in VERBATIM_SLOTS:
        for path, it in _iter_people(d, slot, sub):
            tx = str(it.get(tf) or "").strip()
            sq = _squash(tx)
            if len(sq) < 6 and sq not in index and sq not in system_texts:
                if tx:
                    r.add("info", path + "." + tf, "太短, 跳过原文核对")
                continue
            if sq in system_texts:
                r.add("warn", path + "." + tf,
                      "这是系统灰条(撤回/戳一戳/入群), 是事件通知而不是谁说的话, "
                      "不该当引用")
                continue
            if sq in index:
                claimed = str(it.get("uin") or "")
                senders = index[sq]
                if claimed and senders and claimed not in senders:
                    r.add("warn", path + "." + tf,
                          "署名与原文不符: 这句话的发送者是 %s, 但标注的是 %s"
                          % (" / ".join(sorted(senders)), claimed))
                continue
            if squashed and sq in squashed:
                r.add("warn", path + "." + tf,
                      "仅匹配到跨行或元信息片段，无法核对署名；请引用单条原消息")
                continue
            r.add("warn", path + "." + tf,
                  "未在原料中找到这段原文 → 疑似转述或凭记忆写的: %s"
                  % (tx[:36] + ("…" if len(tx) > 36 else "")))


def _check_counts(d, r):
    lim = {"topics": (1, 8, "3-6"), "people": (3, 8, "3-8"),
           "insights": (3, 6, "3-6"), "quotes": (4, 6, "4-6"),
           "timeline": (6, 10, "6-10"), "appendix": (3, 6, "3-6"),
           "qa": (1, 15, "只收真正答上的")}
    for k, (lo, hi, hint) in lim.items():
        v = d.get(k)
        if not isinstance(v, list) or not v:
            continue
        if len(v) > hi:
            r.add("info", k, "%d 条, 建议 %s 条" % (len(v), hint))
        elif len(v) < lo:
            r.add("info", k, "%d 条, 建议 %s 条" % (len(v), hint))


def _check_risks(d, r):
    v = d.get("risks")
    if not isinstance(v, list):
        return
    for i, it in enumerate(v):
        if not isinstance(it, dict):
            r.add("error", "risks[%d]" % i, "应为对象")
            continue
        lv = str(it.get("level") or "")
        if lv not in _RISK_LEVELS:
            r.add("warn", "risks[%d].level" % i,
                  "应为 info/warn/bad, 实际 %r" % lv)
        if not str(it.get("text") or "").strip():
            r.add("error", "risks[%d].text" % i, "缺失")


# ── 从报告角度反查: 哪些板块会因为缺字段而渲染不出来 ────────────────────────

def render_coverage(d):
    """粗略报告「哪些板块有内容」。让 Agent 一眼看出自己漏写了什么。"""
    out = []
    for k in ("headline", "stats", "activity", "summary", "quality", "topics",
              "qa", "todos", "people", "quotes", "insights", "risks",
              "timeline", "appendix"):
        v = d.get(k)
        n = len(v) if isinstance(v, list) else (1 if v else 0)
        out.append((k, n))
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description="校验分析 JSON (可选自动修复)")
    ap.add_argument("src", help="分析 JSON 文件 (- 表示 stdin)")
    ap.add_argument("--context", help="cli context 输出的原料文件 (推荐, 可查原文)")
    ap.add_argument("--group", help="群号/群名 (没有原料文件时退而求其次)")
    ap.add_argument("--days", type=int, help="配合 --group: 只取最近 N 天")
    ap.add_argument("--fix", action="store_true", help="输出修复后的 JSON")
    ap.add_argument("-o", "--out", help="修复后 JSON 的输出路径 (默认 stdout)")
    ap.add_argument("--json", action="store_true", help="校验结果以 JSON 输出")
    a = ap.parse_args()

    data = (json.load(sys.stdin) if a.src == "-"
            else json.load(open(a.src, encoding="utf-8-sig")))
    ctx = None
    if a.context:
        ctx = load_context(a.context)
    elif a.group:
        import qqlib
        ctx = load_from_store(qqlib.QQStore(), a.group, days=a.days)

    r = validate(data, ctx)
    if a.json:
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=1))
    else:
        if ctx:
            print("基准: %d 条消息, %d 个可辨识昵称, 重名 %d 个\n"
                  % (ctx.get("n_lines", 0), len(ctx.get("nick2uin") or {}),
                     len(ctx.get("dups") or {})))
        print(r.text())
        cov = [(k, n) for k, n in render_coverage(r.data) if n == 0]
        if cov:
            print("\n(以下板块为空, 报告里不会出现: %s)"
                  % ", ".join(k for k, _ in cov))
    if a.fix:
        txt = json.dumps(r.data, ensure_ascii=False, indent=1)
        if a.out and a.out != "-":
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(txt)
            print("\n已写出修复版: %s" % a.out)
        elif not a.json:
            print("\n" + txt)
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
