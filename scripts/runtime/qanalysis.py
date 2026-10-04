# -*- coding: utf-8 -*-
"""qanalysis.py — QQ 群消息分析引擎 (构建在 qqlib 数据层之上)

设计参考的开源实现:
  - Koishi0425 / ZiHuixi  QQgroup-annual-report-analyzer  词频 + 新词发现(PMI/熵) + 多维排行榜
  - BarryZM/chatLog             成员画像 + 周×时热力图 + 图片占比
  - REI-330/wx-qq-group-reporter 活跃趋势 + 夜猫子 + 人格标签
  - DomRR/chat_logs_analysis_for_qq  时段活跃 + 响应时间 + 亲密度
  - hellodigua/ChatLab          复读检测 + 互动排行 + 潜水指数
  - usamimeri/QQLog             时间序列 + 词频 + 图文表情占比

对外接口全部返回**可 JSON 序列化**的纯 Python 结构, 便于 CLI 与 Web 复用。

性能: 走 nt_msg 的 40027/40003 索引取有序 rowid(2万条 0.02s), 再按 seq 批量
从 FTS 取正文、按 rowid 批量取 blob 解码媒体 —— 单群 20 万条约数秒。
"""
import os
import re
import sys
import json
import time
import math
import sqlite3
import datetime
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 元素渲染与媒体标记统一由数据层提供 (qqlib), 导出/上下文/报告共用一套语义
from qqlib import (text_from_blob,        # blob 盲解析兜底取正文 (FTS 有索引延迟)
                   KIND_LABEL, TAG_ONLY,
                   media_label as _media_label,
                   fmt_size, fmt_dur,
                   has_rich,              # 富元素预筛 (已下沉到数据层, 见 qqlib)
                   is_gray_blob)          # 灰条系统消息的字节级识别 (零解码成本)

# ── 元素类型 → 中文标签 (由 qqlib 提供, 见 KIND_LABEL 导入) ─────────────────
# 计入「带图/带表情」统计的 kind
IMAGE_KINDS = ("pic",)
EMOJI_KINDS = ("face", "emoji", "marketface")

WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_WARNED = set()


def _warn_once(key, msg):
    """同一类问题在一次进程里只提示一次。

    qanalysis 是库, 不该往 stdout 打东西(会污染 `-o -` 这类管道输出), 所以
    统一走 stderr; 用 key 去重是为了 20 万条消息时不刷屏。
    """
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(f"⚠ {msg}", file=sys.stderr)

# ── 中文停用词 (紧凑但覆盖常见虚词/语气词/网络水词) ──────────────────────────
STOPWORDS = set("""
的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好
自己 这 那 他 她 它 们 个 中 大 为 上 来 用 们 到 时 与 或 及 等 被 把 让 给 对
但 但是 而 而且 并 并且 因为 所以 如果 虽然 然后 还是 就是 只是 这样 那样 什么
怎么 为什么 哪里 这个 那个 这些 那些 我们 你们 他们 她们 咱们 大家 各位 一个 一些
一下 一直 一定 一样 已经 可以 可能 应该 需要 觉得 感觉 现在 今天 明天 昨天 时候
问题 东西 事情 地方 情况 方式 方法 方面 内容 部分 时间 一直 还有 没有 不是 不会
不能 不要 不用 不过 真的 好像 似乎 其实 反正 应该 感觉 之前 之后 以后 以前 另外
以及 或者 而是 的话 之类 等等 一样 这么 那么 怎么 咋 啥 啊 吧 呢 吗 哦 噢 嗯 哈
呀 哇 唉 哎 唉 喂 嘿 嘻 呵 咕 嘛 咯 啦 嘞 诶 咦 唔 额 呃 喔 诶 诶 呀 哟 耶
一 二 三 四 五 六 七 八 九 十 百 千 万 亿 个 只 条 张 块 点 分 秒 年 月 日 天 号
这个 那个 哪个 每个 某个 整个 别的 其它 其他 任何 所有 全部 每个 各种 各自
哈哈 哈哈哈 哈哈哈哈 呵呵 嘿嘿 嘻嘻 emmm hmm 哈哈 哈哈 233 666 awsl 笑死 蚌埠
我能 你可以 我们是 这个是 不知道 为什么 怎么样 怎么办 有没有 是不是 好不好
""".split())

# 兜底分词时用于过滤的「功能字」
STOPCHARS = set("的了在是我你他她它们和与就都也很不这那有会要能个上下么呢吧啊呀哦嗯哈"
                "把被对给让到去过来说看想要可以应该因为所以但是如果然后还是只")


# ── 富元素「快速预筛」 ───────────────────────────────────────────────────────
# _varint / _EL_TYPE_TAG / has_rich 已下沉到数据层 qqlib —— 让 read_group
# 与这里的 collect 共用同一套预筛, 不再各留一份会漂移的实现。


def search_recent(store, gid, kw, limit=30, scan=3000):
    """在「最近 scan 条消息」的正文里搜索。

    用来补 FTS 的短板: QQ 的 group_msg_fts 有明显索引延迟, 刚收到/刚发出的
    消息往往还搜不到(实测新消息全为 0 命中)。这里走 collect() 的 blob 兜底
    取正文, 因此能覆盖最新消息。
    """
    low = (kw or "").lower()
    if not low:
        return []
    gname = store.group_names().get(str(gid), "")
    out = []
    for m in collect(store, gid, limit=scan, with_media=False):
        if low in (m["text"] or "").lower():
            out.append({"gid": str(gid), "gname": gname, "ts": m["ts"],
                        "uin": m["uin"], "text": (m["text"] or "")[:300]})
            if len(out) >= limit:
                break
    return out


# ── 采集辅助 ─────────────────────────────────────────────────────────────────

def _bulk(conn, tpl, ids, per=800):
    """容错批量 IN 查询: 整批失败则降级逐行(跳过坏页行)。"""
    out = []
    ids = list(ids)
    for i in range(0, len(ids), per):
        ch = ids[i:i + per]
        q = tpl % ",".join("?" * len(ch))
        try:
            out.extend(conn.execute(q, ch).fetchall())
        except sqlite3.DatabaseError:
            for k in ch:
                try:
                    r = conn.execute(tpl % "?", (k,)).fetchone()
                    if r:
                        out.append(r)
                except sqlite3.DatabaseError:
                    pass
    return out


def _fetch_basic(nt, order, per=900):
    """批量取 rowid -> (seq, ts, uin, uid)。

    只读 40020(发送者 NT UID): 40033(发送者QQ号) 列坏页极多, 一旦整批 IN 失败
    就要逐行重试, 7.9 万条会退化成十几万次单行查询(实测多花 30 秒)。改用
    40020 + 成员映射表(uid->QQ号) 换算, 更快且覆盖率更高。
    """
    Q_SAFE = ('SELECT rowid,"40001","40050","40020" '
              'FROM group_msg_table WHERE rowid IN (%s)')
    out = {}
    for i in range(0, len(order), per):
        ch = order[i:i + per]
        rows = None
        try:
            rows = nt.execute(Q_SAFE % ",".join("?" * len(ch)), ch).fetchall()
        except sqlite3.DatabaseError:
            rows = []
            for rid in ch:
                try:
                    r = nt.execute(Q_SAFE % "?", (rid,)).fetchone()
                    if r:
                        rows.append(r)
                except sqlite3.DatabaseError:
                    continue
        for r in rows:
            out[r[0]] = (r[1], r[2] or 0, "", str(r[3] or ""))
    return out


def collect(store, gid, days=None, limit=None, with_media=True,
            with_text=True, page=4000, after=None, before=None,
            media_detail=False):
    """快速采集群消息(按时间倒序)。

    返回 [{"rid","seq","ts","uin","text","mk","at","system"}] , mk=出现的媒体类型列表。
    system=True 表示灰条系统消息(撤回/戳一戳/入群等), 它的 uin 可能是**触发该
    操作的人而不是发言者**, 统计与渲染时都要单独对待。
    days: 只取最近 N 天; limit: 最多 N 条(从最新往回)。
    after/before: 时间戳上下界 [after, before); 用于 --date / --from/--to。
    media_detail: True 时额外带 m["media"] = extract_media 的完整元素列表
                  (卡片标题/文件名/表情名等), 供「对话原料」渲染使用。
    """
    gid = str(gid)
    try:
        key = int(gid)
    except (TypeError, ValueError):
        return []
    nt = store.nt()
    cutoff = int(time.time()) - int(days) * 86400 if days else 0
    cap = limit or 2_000_000

    # 1) 按 QQ 自带索引取有序 rowid (40003 单调≈时间)
    order = []
    off = 0
    while len(order) < cap:
        n = min(page, cap - len(order))
        try:
            rids = [r[0] for r in nt.execute(
                'SELECT rowid FROM group_msg_table WHERE "40027"=? '
                'ORDER BY "40003" DESC LIMIT ? OFFSET ?', (key, n, off))]
        except sqlite3.DatabaseError:
            break
        if not rids:
            break
        order.extend(rids)
        off += n
        if len(rids) < n:
            break

    if not order:
        return []

    # 2) 批量取 seq / ts / 发送者(优先 40033 真实QQ号, 坏页时降级 40020 UID)
    basic = _fetch_basic(nt, order)

    msgs = []
    for rid in order:                     # 保持时间倒序
        r = basic.get(rid)
        if not r:
            continue
        seq, ts, uin, uid = r
        if cutoff and ts and ts < cutoff:
            break                         # 已越过时间下界
        if after and ts and ts < after:
            break                         # 倒序, 后续只会更早
        if before and ts and ts >= before:
            continue                      # 时间上界: 跳过更新的, 不中断
        msgs.append({"rid": rid, "seq": seq, "ts": ts,
                     "uin": uin or uid, "uid": uid,
                     "text": "", "mk": [], "at": 0})

    if not msgs:
        return []

    # 3) 批量取 blob (媒体解析 + 正文兜底都要用)
    blobs = {}
    if with_media or with_text:
        rids = [m["rid"] for m in msgs]
        for rid, blob in _bulk(nt, 'SELECT rowid,"40800" FROM group_msg_table '
                                   'WHERE rowid IN (%s)', rids):
            if blob:
                blobs[rid] = blob

    # 4) 正文: 先按 seq 从 FTS 批量取明文
    if with_text:
        seqs = [m["seq"] for m in msgs if m["seq"]]
        ft = store.fts()
        if ft is not None and seqs:
            tx = {}
            for a, b in _bulk(ft, "SELECT [40001],[41701] FROM group_msg_fts "
                                  "WHERE [40001] IN (%s)", seqs):
                if a is not None:
                    tx[a] = b or ""
            for m in msgs:
                m["text"] = tx.get(m["seq"], "") or ""

    # 4b) FTS 没索引到的, 从 blob 盲解析兜底。
    #     QQ 的 FTS 索引有**延迟**: 最新收到的消息往往还没进 FTS, 只走 FTS 会
    #     让「最近的消息全部无正文」, 热词/复读分析随之失真。
    if with_text:
        for m in msgs:
            if m["text"]:
                continue
            blob = blobs.get(m["rid"])
            if not blob or has_rich(blob):
                continue                  # 富元素交给下面的媒体分支
            try:
                m["text"] = (text_from_blob(blob) or "").strip()
            except Exception:
                pass

    # 5) 媒体 (blob 解码, 只对含富元素的消息, 单条约 0.1ms)
    if with_media and hasattr(store, "extract_media"):
        rich_n = ok_n = 0
        for m in msgs:
            blob = blobs.get(m["rid"])
            if not blob or not has_rich(blob):
                continue                  # 纯文本: 跳过昂贵的解码
            rich_n += 1
            try:
                media = store.extract_media(blob)
            except Exception:
                continue
            ok_n += 1
            kinds = []
            for md in media:
                k = md.get("kind")
                if k and k not in kinds:
                    kinds.append(k)
            m["mk"] = kinds
            if media_detail:
                m["media"] = media
        # 富元素一条都没解出来, 基本是可选依赖(blackboxprotobuf)没装。这种失败
        # 原来被 `except Exception: continue` 静默吃掉 —— 表现为「原料里一张图、
        # 一条引用都没有」, 但命令照常退出 0, 极难察觉。宁可吵一次。
        if rich_n and not ok_n:
            _warn_once("media_decode",
                       f"{rich_n} 条带富元素的消息全部解码失败, 图片/引用/表情"
                       f"将缺失 —— 检查可选依赖 blackboxprotobuf 是否安装")

    # 5b) 灰条系统消息标记 (撤回/戳一戳/入群/邀请)。
    #     实测这个群的 122 条灰条里: 戳一戳/拍一拍 **完全没有发送者**(40033 为空/0),
    #     撤回**有**发送者且就是撤回者本人 (59/61 的 uin 昵称出现在文案里)。
    #     标记出来是为了解决两件事, 不是为了纠正「归错人」:
    #       1. 渲染歧义 —— display_name 用**本群群名片**, 而灰条文案里用的是
    #          **全局昵称**, 同一个人显示成两个名字。`casual: 随意~喵 撤回了一条
    #          消息` 读起来像 casual 在转述别人的事, 实际是同一人。
    #       2. 统计口径 —— 灰条是事件通知, 不是一次发言; 不排除会虚增发言量
    #          (本群占 3.3%), 并把没有发送者的那批聚成一个幽灵成员「未知」
    #          (实测 142 条全落在一个空 uin 桶里)。
    for m in msgs:
        m["system"] = is_gray_blob(blobs.get(m["rid"]))

    # 5) 归一发送者: uid -> QQ号 (与 40033 主路径统一, 避免同一人算两个)
    try:
        uid2uin = store.member_maps()["uid2uin"]
    except Exception:
        uid2uin = {}
    if uid2uin:
        for m in msgs:
            if m["uin"].startswith("u_"):
                m["uin"] = uid2uin.get(m["uin"], m["uin"])

    # 6) @提及 (正文里的 @ , 不含 @全体成员)
    for m in msgs:
        t = m["text"] or ""
        if "@" in t:
            m["at"] = len(re.findall(r"@(?!全体成员)", t))
    return msgs


# ── 通用工具 ─────────────────────────────────────────────────────────────────

def _nick_map(store):
    mm = store.member_maps()
    return mm["uin2nick"], mm["uid2uin"]


# ── 昵称清洗 ─────────────────────────────────────────────────────────────────
# 实测群里 1148 条成员的昵称里混着大量垃圾: 零宽字符 / 双向控制符(「随意ᅠ⁧~喵⁧‭」
# 里的 ᅠ⁧‭ 是 U+1160 / U+2067 / U+202D)、解码失败的替换符(U+FFFD)、以及
# 用户把昵称留空或设成 `null` / `undefined` / `1` / `。` 的情况。
#
# 这些东西的害处不只是难看:
#   · 零宽字符在终端/HTML 里占位但不可见, 对齐会错位;
#   · Agent 抄写昵称时很容易把不可见字符抄丢 → 回填 QQ号 失败、头像配不上;
#   · `null` / `undefined` 这类名字完全没有辨识度, 出现在「谁在说话」的位置
#     等于把发言者信息丢了。
# 所以统一在这里清一遍; 清完没剩下可辨识内容的, 降级成「尾号XXXX」。
_INVISIBLE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff"
    "\u115f\u1160\u3164\uffa0\u00ad]")
_JUNK_NICK = {"", "null", "none", "nan", "undefined", "nil", "void", "0", "-",
              "--", ".", "。", "、", "?", "？", "暂无", "待定", "未知"}
# 纯标点/纯空白/纯符号的昵称同样没有辨识度
_MEANINGLESS = re.compile(r"^[\s\W_]*$")
# 纯数字昵称: 只有 5 位以上才可能是真的 QQ号式昵称, 1~4 位(如 "1" "23")
# 在「谁在说话」的位置上等于没有名字
_SHORT_DIGITS = re.compile(r"^\d{1,4}$")


def clean_nick(name, uin=None):
    """昵称清洗: 去不可见字符 → 判可辨识度 → 不行就降级成「尾号XXXX」。

    降级而非留空是有意的 —— 报告里写「尾号0156 说」比写 `null 说` 有用得多,
    读者至少能对上号。`uin` 只用于生成降级名, 不会被写回数据。
    """
    s = _INVISIBLE.sub("", str(name or ""))
    s = re.sub(r"[\s\u3000]+", " ", s).strip()
    if len(s) > 24:                       # 昵称被塞成小作文时截断 (留 "…" 提示)
        s = s[:24].rstrip() + "…"
    if (s.lower() in _JUNK_NICK or _MEANINGLESS.match(s)
            or _SHORT_DIGITS.match(s)):
        u = re.sub(r"\D", "", str(uin or ""))
        return f"尾号{u[-4:]}" if len(u) >= 4 else "匿名"
    return s


def group_member_names(store, gid):
    """{uin: 群名片/昵称} —— 只取该群成员。

    群名片是**分群**的(同一个人在不同群可能叫不同名字), 全局映射表会用
    「先遇到的」那张名片覆盖, 所以单群分析要优先用本群名片。
    """
    out = {}
    try:
        for uin, nick, card in store.gi().execute(
                'SELECT "1002","20002","64003" FROM group_member3 '
                'WHERE "60001"=?', (str(gid),)):
            if uin:
                u = str(uin)
                out[u] = clean_nick(card or nick or u, u)
    except Exception:
        pass
    return out


def display_name(store, uin, gmap=None):
    """把消息里的 uid/QQ号 转成可读昵称 (优先该群名片)。"""
    uin2nick, uid2uin = _nick_map(store)
    key = str(uin or "")
    if key.startswith("u_"):
        key = uid2uin.get(key, key)
    if not key:
        return "未知"
    if gmap:
        nm = gmap.get(key)
        if nm:
            return nm
    return clean_nick(uin2nick.get(key, ""), key) or key


def _norm(text):
    """归一化文本: 去空白/表情标签/链接, 转小写。用于复读与分词。"""
    t = text or ""
    t = re.sub(r"\[[^\[\]]{1,10}\]", " ", t)       # [图片] [表情] 等标签
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()


# ── 1. 活跃热力图 (周 × 时) ──────────────────────────────────────────────────

def activity_heatmap(msgs):
    """返回 7×24 矩阵 (周一起) 与峰值信息。"""
    m = [[0] * 24 for _ in range(7)]
    total = 0
    for x in msgs:
        ts = x["ts"]
        if not ts:
            continue
        d = datetime.datetime.fromtimestamp(ts)
        m[d.weekday()][d.hour] += 1
        total += 1
    peak = (0, 0, 0)
    for wd in range(7):
        for h in range(24):
            if m[wd][h] > peak[2]:
                peak = (wd, h, m[wd][h])
    return {"matrix": m, "max": max((max(r) for r in m), default=0),
            "total": total, "peak": {"weekday": peak[0], "hour": peak[1],
                                     "n": peak[2],
                                     "label": "%s %02d:00"
                                              % (WEEKDAY_CN[peak[0]], peak[1])}}


def render_heatmap(hm, block="▁▂▃▄▅▆▇█", show_zero="·"):
    """把热力图渲染成等宽字符画 (终端友好)。"""
    mx = hm["max"] or 1
    head = [""] * 24                      # 每个格子 2 列
    for h in range(0, 24, 3):
        head[h] = "%2d" % h
    lines = ["     " + "".join(h if h else "  " for h in head)]
    for wd in range(7):
        cells = []
        for h in range(24):
            n = hm["matrix"][wd][h]
            if n == 0:
                cells.append(" " + show_zero)
            else:
                lv = min(len(block) - 1, int(n / mx * (len(block) - 1) + 0.5))
                cells.append(" " + block[lv])
        lines.append("%s %s" % (WEEKDAY_CN[wd], "".join(cells)))
    return "\n".join(lines)


def hourly(msgs):
    h = [0] * 24
    for x in msgs:
        if x["ts"]:
            h[datetime.datetime.fromtimestamp(x["ts"]).hour] += 1
    return h


def daily(msgs, days=30):
    c = collections.Counter()
    for x in msgs:
        if x["ts"]:
            c[datetime.datetime.fromtimestamp(x["ts"]).strftime("%Y-%m-%d")] += 1
    return [{"d": d, "n": c[d]} for d in sorted(c)][-days:]


# ── 2. 热词 ──────────────────────────────────────────────────────────────────

_JIEBA = None


def _jieba():
    global _JIEBA
    if _JIEBA is None:
        try:
            import jieba
            jieba.setLogLevel(60)
            _JIEBA = jieba
        except Exception:
            _JIEBA = False
    return _JIEBA or None


_CJK = re.compile(r"[\u4e00-\u9fff]+")
_LAT = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]{1,}")


def segment(text):
    """分词: 优先 jieba, 缺失时降级为 拉丁词 + 中文 bigram。"""
    jb = _jieba()
    if jb:
        return [w for w in jb.lcut(text) if w.strip()]
    toks = [m.group(0).lower() for m in _LAT.finditer(text)]
    for m in _CJK.finditer(text):
        s = m.group(0)
        if 2 <= len(s) <= 4:
            toks.append(s)
        for i in range(len(s) - 1):
            toks.append(s[i:i + 2])
    return toks


def _ok_word(w):
    if not w or len(w) < 2:
        return False
    if w in STOPWORDS:
        return False
    if w.isdigit():
        return False
    if re.fullmatch(r"[a-z]{1,2}", w):          # 过短英文
        return False
    if re.fullmatch(r"[0-9a-f]{8,}", w):
        return False
    if re.fullmatch(r"[\W_]+", w):
        return False
    # 仅在兜底 bigram 分词时启用「功能字」过滤 (jieba 已产出真词, 不该再切)
    if not _jieba() and _CJK.fullmatch(w) and len(w) == 2 \
            and (w[0] in STOPCHARS or w[1] in STOPCHARS):
        return False
    return True


def top_words(msgs, n=50, min_count=2, extra_stop=None, samples=2):
    """热词榜。返回 [{"w","n","users","u","samples":[...]}]"""
    stop = set(extra_stop or ()) | STOPWORDS
    freq = collections.Counter()
    wusers = collections.defaultdict(collections.Counter)
    wsample = collections.defaultdict(list)
    for m in msgs:
        t = _norm(m["text"])
        if not t:
            continue
        seen = set()
        for w in segment(t):
            w = w.strip()
            if not _ok_word(w) or w in stop:
                continue
            freq[w] += 1
            wusers[w][m["uin"]] += 1
            if w not in seen:
                seen.add(w)
                if len(wsample[w]) < samples:
                    s = (m["text"] or "").strip()
                    if s:
                        wsample[w].append(s[:80])
    out = []
    for w, c in freq.most_common():
        if c < min_count:
            break
        users = [u for u, _ in wusers[w].most_common(5)]
        out.append({"w": w, "n": c, "users": users, "u": len(wusers[w]),
                    "samples": wsample[w]})
        if len(out) >= n:
            break
    return out


# ── 3. 成员画像 ──────────────────────────────────────────────────────────────

def user_profiles(msgs, store, gmap=None):
    """逐成员画像。返回 {"by_uin": {...}, "rank": [...]}"""
    prof = {}

    def get(uin):
        if uin not in prof:
            prof[uin] = {"uin": uin, "n": 0, "chars": 0, "pic": 0, "emoji": 0,
                         "voice": 0, "video": 0, "file": 0, "at": 0, "quote": 0,
                         "night": 0, "link": 0, "first_ts": 0, "last_ts": 0,
                         "max_chars": 0}
        return prof[uin]

    for m in speakers(msgs):
        u = m["uin"]
        p = get(u)
        p["n"] += 1
        t = m["text"] or ""
        c = len(t)
        p["chars"] += c
        p["max_chars"] = max(p["max_chars"], c)
        mk = m["mk"]
        if "pic" in mk:
            p["pic"] += 1
        if any(k in mk for k in EMOJI_KINDS):
            p["emoji"] += 1
        if "ptt" in mk:
            p["voice"] += 1
        if "video" in mk:
            p["video"] += 1
        if "file" in mk:
            p["file"] += 1
        if "quote" in mk:
            p["quote"] += 1
        p["at"] += m["at"]
        if re.search(r"https?://", t):
            p["link"] += 1
        if m["ts"]:
            h = datetime.datetime.fromtimestamp(m["ts"]).hour
            if h < 6:
                p["night"] += 1
            p["first_ts"] = min(p["first_ts"] or m["ts"], m["ts"])
            p["last_ts"] = max(p["last_ts"], m["ts"])

    for p in prof.values():
        p["avg_chars"] = round(p["chars"] / p["n"], 1) if p["n"] else 0
        p["pic_rate"] = round(p["pic"] / p["n"] * 100, 1) if p["n"] else 0
        p["emoji_rate"] = round(p["emoji"] / p["n"] * 100, 1) if p["n"] else 0
        p["night_rate"] = round(p["night"] / p["n"] * 100, 1) if p["n"] else 0
        p["name"] = display_name(store, p["uin"], gmap)
        p["tags"] = persona_tags(p)
    rank = sorted(prof.values(), key=lambda x: -x["n"])
    return {"by_uin": prof, "rank": rank}


def persona_tags(p):
    """根据行为自动打标签 (参考 wx-qq-group-reporter)。"""
    tags = []
    n = p["n"] or 1
    if p["n"] >= 20 and p["night_rate"] >= 25:
        tags.append("夜猫子")
    if p["n"] >= 20 and p["emoji_rate"] >= 30:
        tags.append("表情包达人")
    if p["n"] >= 20 and p["pic_rate"] >= 20:
        tags.append("图王")
    if p["avg_chars"] >= 60:
        tags.append("长篇输出")
    elif p["avg_chars"] <= 4 and p["n"] >= 20:
        tags.append("惜字如金")
    if p["n"] >= 20 and p["quote"] / n >= 0.25:
        tags.append("回复狂")
    if p["n"] >= 20 and p["at"] / n >= 0.15:
        tags.append("@达人")
    if p["max_chars"] >= 800:
        tags.append("小作文")
    if 0 < p["n"] <= 3:
        tags.append("潜水")
    return tags


def night_owls(msgs, store, start=0, end=6, top=15, gmap=None):
    """夜猫子榜 (start<=hour<end 的发言数)。"""
    c = collections.Counter()
    for m in speakers(msgs):
        if m["ts"]:
            h = datetime.datetime.fromtimestamp(m["ts"]).hour
            if start <= h < end:
                c[m["uin"]] += 1
    return [{"uin": u, "name": display_name(store, u, gmap), "n": n}
            for u, n in c.most_common(top)]


def emoji_rank(msgs, store, top=15, gmap=None):
    """表情/表情包榜。"""
    c = collections.Counter()
    for m in speakers(msgs):
        if any(k in m["mk"] for k in EMOJI_KINDS):
            c[m["uin"]] += 1
    return [{"uin": u, "name": display_name(store, u, gmap), "n": n}
            for u, n in c.most_common(top)]


def image_rank(msgs, store, top=15, gmap=None):
    c = collections.Counter()
    for m in speakers(msgs):
        if "pic" in m["mk"]:
            c[m["uin"]] += 1
    return [{"uin": u, "name": display_name(store, u, gmap), "n": n}
            for u, n in c.most_common(top)]


# ── 4. 复读 / 队形 ───────────────────────────────────────────────────────────

def repeat_chains(msgs, min_len=3, top=20):
    """复读检测: 时间连续、文本相同的消息成串。返回最长的链条。"""
    seq = list(reversed(msgs))                # 时间正序
    chains = []
    i = 0
    while i < len(seq):
        t = _norm(seq[i]["text"])
        if not t or len(t) < 1:
            i += 1
            continue
        j = i + 1
        while j < len(seq) and _norm(seq[j]["text"]) == t:
            j += 1
        run = seq[i:j]
        if len(run) >= min_len:
            users = collections.Counter(x["uin"] for x in run)
            chains.append({
                "text": (seq[i]["text"] or "").strip()[:60],
                "n": len(run), "users": len(users),
                "who": [u for u, _ in users.most_common(5)],
                "start_ts": run[0]["ts"], "end_ts": run[-1]["ts"],
            })
        i = j
    chains.sort(key=lambda x: -x["n"])
    return chains[:top]


def top_exact(msgs, top=20, min_count=2):
    """最常被复述的原文 (口头禅/队形素材)。"""
    c = collections.Counter()
    for m in msgs:
        t = (m["text"] or "").strip()
        if 2 <= len(t) <= 40 and not t.startswith("["):
            c[t] += 1
    return [{"text": t, "n": n} for t, n in c.most_common(top)
            if n >= min_count]


# ── 5. @ 互动网络 ────────────────────────────────────────────────────────────

def at_network(msgs, store, top=20):
    """被 @ 次数排行 (从正文 @昵称 粗略统计)。"""
    c = collections.Counter()
    for m in msgs:
        for hit in re.findall(r"@([^\s@,，。:：]{1,16})", m["text"] or ""):
            if hit and hit != "全体成员":
                c[hit] += 1
    return [{"name": k, "n": v} for k, v in c.most_common(top)]


def quote_network(msgs, store, top=15, gmap=None):
    """回复关系: 谁回复得多。"""
    c = collections.Counter()
    for m in msgs:
        if "quote" in m["mk"]:
            c[m["uin"]] += 1
    return [{"uin": u, "name": display_name(store, u, gmap), "n": n}
            for u, n in c.most_common(top)]


def speakers(msgs):
    """只产出「真实发言」的消息 (排除灰条系统消息与无法归属的行)。

    所有「按人统计」的函数都该用它。灰条是事件通知不是发言, 混进来会虚增
    发言量 (本群占 3.3%); 更明显的是**没有发送者**的那批 (戳一戳/拍一拍,
    实测 87/87 无 uin) 会一起落进空 uin 桶, 变成一个 142 条的幽灵成员「未知」。
    """
    for m in msgs:
        if m.get("system"):
            continue
        u = str(m.get("uin") or "")
        if not u or u in ("0", "-1") or u.startswith("u_"):
            continue
        yield m


# ── 6. 潜水榜 ────────────────────────────────────────────────────────────────

def lurkers(store, gid, msgs, top=20):
    """潜水榜: 群成员里发言最少的人 (需要 group_member3 的成员名单)。"""
    try:
        rows = store.gi().execute(
            'SELECT "1002","20002","64003" FROM group_member3 WHERE "60001"=?',
            (str(gid),)).fetchall()
    except Exception:
        return []
    spoken = collections.Counter(m["uin"] for m in speakers(msgs))
    members = []
    for uin, nick, card in rows:
        uin = str(uin or "")
        if not uin or uin == "0":
            continue
        members.append({"uin": uin, "name": clean_nick(card or nick or uin, uin),
                        "n": spoken.get(uin, 0)})
    members.sort(key=lambda x: (x["n"], x["name"]))
    total = len(members)
    silent = sum(1 for x in members if x["n"] == 0)
    return {"total": total, "silent": silent,
            "silent_rate": round(silent / total * 100, 1) if total else 0,
            "list": members[:top]}


# ── 7. 综合报告 ──────────────────────────────────────────────────────────────

def report(store, gid, days=None, limit=None, top_words_n=30):
    """一次性产出完整分析报告 (JSON 友好)。"""
    t0 = time.time()
    msgs = collect(store, gid, days=days, limit=limit)
    names = store.group_names()
    gname = names.get(str(gid), str(gid))
    rep = {
        "gid": str(gid), "name": gname,
        "days": days, "generated_at": int(time.time()),
        "count": len(msgs),
        "elapsed": None,
    }
    if not msgs:
        rep["elapsed"] = round(time.time() - t0, 2)
        return rep
    ts = [m["ts"] for m in msgs if m["ts"]]
    rep["range"] = {
        "first": min(ts), "last": max(ts),
        "first_str": datetime.datetime.fromtimestamp(min(ts)).strftime(
            "%Y-%m-%d %H:%M"),
        "last_str": datetime.datetime.fromtimestamp(max(ts)).strftime(
            "%Y-%m-%d %H:%M"),
    }
    gmap = group_member_names(store, gid)
    rep["heatmap"] = activity_heatmap(msgs)
    rep["hourly"] = hourly(msgs)
    rep["daily"] = daily(msgs)
    rep["words"] = top_words(msgs, n=top_words_n)

    up = user_profiles(msgs, store, gmap=gmap)
    rep["speakers"] = len(up["rank"])
    rep["top_speakers"] = [
        {"name": p["name"], "uin": p["uin"], "n": p["n"], "chars": p["chars"],
         "avg_chars": p["avg_chars"], "pic": p["pic"], "emoji": p["emoji"],
         "night": p["night"], "tags": p["tags"]}
        for p in up["rank"][:20]]
    rep["night_owls"] = night_owls(msgs, store, gmap=gmap)
    rep["emoji_rank"] = emoji_rank(msgs, store, gmap=gmap)
    rep["image_rank"] = image_rank(msgs, store, gmap=gmap)
    rep["repeat"] = repeat_chains(msgs)
    rep["top_exact"] = top_exact(msgs)
    rep["at_network"] = at_network(msgs, store)
    rep["quote_rank"] = quote_network(msgs, store, gmap=gmap)
    rep["lurkers"] = lurkers(store, gid, msgs)
    # 媒体构成
    km = collections.Counter()
    for m in msgs:
        for k in m["mk"]:
            km[k] += 1
    rep["media_kinds"] = [
        {"kind": k, "label": KIND_LABEL.get(k, k), "n": v}
        for k, v in km.most_common()]
    rep["elapsed"] = round(time.time() - t0, 2)
    return rep


def report_cached(store, gid, days=None, limit=None, top_words_n=30,
                  no_cache=False):
    """report() 的带缓存版本 (源库签名变化时自动失效)。"""
    if no_cache:
        return report(store, gid, days=days, limit=limit,
                      top_words_n=top_words_n)
    key = f"report_v1_{gid}_{days}_{limit}_{top_words_n}"
    return store.cached(key, lambda: report(store, gid, days=days, limit=limit,
                                            top_words_n=top_words_n))


# ── 渲染 (终端) ──────────────────────────────────────────────────────────────

def bar(n, mx, width=32):
    if mx <= 0:
        return ""
    return "█" * max(1, int(n / mx * width))


def disp_w(s):
    """显示宽度: 全角算 2 列 (终端表格对齐用)。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in str(s))


def pad(s, w):
    s = str(s)
    return s + " " * max(0, w - disp_w(s))


def render_report(rep, store=None):
    """把 report() 渲染成终端文本 (Markdown 风格)。"""
    L = []
    if not rep.get("count"):
        return "该群在指定范围内没有消息。"
    L.append("# %s 群聊分析" % rep["name"])
    L.append("群号 %s · 消息 %d 条 · 参与者 %d 人" % (
        rep["gid"], rep["count"], rep.get("speakers", 0)))
    r = rep.get("range") or {}
    L.append("时间范围: %s ~ %s" % (r.get("first_str"), r.get("last_str")))
    L.append("")

    L.append("## 活跃热力图 (周 × 时)")
    L.append(render_heatmap(rep["heatmap"]))
    L.append("峰值: %s (%d 条)" % (rep["heatmap"]["peak"]["label"],
                                  rep["heatmap"]["peak"]["n"]))
    L.append("")

    L.append("## 发言排行 Top 15")
    mx = max((p["n"] for p in rep["top_speakers"]), default=1)
    for i, p in enumerate(rep["top_speakers"][:15], 1):
        tag = (" " + "/".join(p["tags"])) if p["tags"] else ""
        L.append("%2d. %s %5d  %s%s" % (
            i, pad(p["name"][:18], 18), p["n"], bar(p["n"], mx), tag))
    L.append("")

    if rep.get("words"):
        L.append("## 热词 Top 20")
        L.append("  ".join("%s(%d)" % (w["w"], w["n"]) for w in rep["words"][:20]))
        L.append("")

    if rep.get("night_owls"):
        L.append("## 夜猫子 (0-6 点)")
        L.append(" · ".join("%s %d" % (x["name"], x["n"]) for x in rep["night_owls"][:10]))
        L.append("")

    if rep.get("emoji_rank"):
        L.append("## 表情达人")
        L.append(" · ".join("%s %d" % (x["name"], x["n"]) for x in rep["emoji_rank"][:10]))
        L.append("")

    if rep.get("repeat"):
        L.append("## 复读 / 队形 Top 8")
        for c in rep["repeat"][:8]:
            L.append("  x%-3d %s" % (c["n"], c["text"]))
        L.append("")

    if rep.get("top_exact"):
        L.append("## 高频原句")
        for c in rep["top_exact"][:10]:
            L.append("  %-6d %s" % (c["n"], c["text"]))
        L.append("")

    if rep.get("media_kinds"):
        L.append("## 内容构成")
        L.append(" · ".join("%s %d" % (x["label"], x["n"]) for x in rep["media_kinds"]))
        L.append("")

    lk = rep.get("lurkers") or {}
    if lk.get("total"):
        L.append("## 潜水情况")
        L.append("群成员 %d 人, 期间零发言 %d 人 (%.1f%%)" % (
            lk["total"], lk["silent"], lk["silent_rate"]))
        L.append("")
    return "\n".join(L)


# ── 对话原料 (Agent 读的「内容」, 而不是统计数字) ───────────────────────────
#
# 思路参考 chatlog / wechat-daily-report: 工具只负责**把原始对话整理成
# 适合 LLM 阅读的形状** —— 时间正序、按话题间隙切段、媒体元素转成人类可读
# 的一行标记 —— 剩下的「这段在聊什么/谁解决了什么问题/有什么结论」交给
# Agent 自己读。统计类函数(热词/画像)只是辅助素材, 不该当作分析本身。

_ONLY_TAGS = TAG_ONLY     # 与导出层共用同一条判据 (qqlib.TAG_ONLY)


def media_label(md):
    """媒体元素 -> 可读标记 (实现见 qqlib.media_label, 导出与上下文共用一套)"""
    return _media_label(md)


def msg_lines(m, store, gmap=None, tfmt="%H:%M"):
    """把一条消息渲染成 1-2 行文本 (主行 + 引用行)。"""
    ts = m.get("ts") or 0
    tm = datetime.datetime.fromtimestamp(ts).strftime(tfmt) if ts else "--:--"
    if m.get("system"):
        # 灰条(撤回/戳一戳/入群)一律署「系统」。撤回虽然能拿到撤回者, 但文案里
        # 用的是**全局昵称**、而这里显示的是**本群群名片**, 同一个人两个名字会让
        # `casual: 随意~喵 撤回了一条消息` 读成「casual 在转述别人」; 戳一戳则
        # 根本没有发送者。统一署「系统」比猜一个名字更准确。
        who = "系统"
    else:
        who = display_name(store, m.get("uin"), gmap)
        if who in ("未知", "0", ""):
            who = "系统"              # 兜底: 确实解析不出发送者
    text = (m.get("text") or "").strip()
    media = m.get("media") or []
    quotes = [x for x in media if x.get("kind") == "quote"]
    tags = [media_label(x) for x in media if x.get("kind") != "quote"]
    if tags:
        # 同名标签合并计数: [图片] [图片] [图片] -> [图片×3]
        n, order = {}, []
        for t in tags:
            if t not in n:
                n[t] = 0
                order.append(t)
            n[t] += 1
        tags = [(t[:-1] + f"×{n[t]}]") if n[t] > 1 else t for t in order]
    if tags and _ONLY_TAGS.match(text):
        text = ""                     # 已由标签表达, 不再重复 "[图片]" 占位
    body = " ".join([text] + tags).strip()
    lines = [f"{tm} {who}: {body}"]
    for q in quotes:
        qtx = re.sub(r"\s+", " ", (q.get("name") or "")).strip()[:110]
        if not qtx:
            continue
        # 引用带「回复谁 + 原消息时间」: 群聊里"这条在接谁的话"是理解上下文
        # 的关键线索, 只给正文会丢掉这一层。
        #
        # 被引用人的名字**必须与正文署名同源**(本群名片), 不能直接用 47421:
        # 47421 存的是**全局 QQ 昵称**, 一旦群名片改过名, 同一个人就会出现两个
        # 名字 —— 实测本群 34 条引用里 6 条错: `风轮` 被写成 `倾峰`(×4)、
        # `艾斯爱慕` 被写成 `A?`(×2), 读起来像两个人在对话。所以先用 47403
        # (原发送者 QQ号) 回本群名片, 拿不到才退回 47421。
        q_uin = str(q.get("uin") or "").strip()
        who_q = (q.get("who") or "").strip()
        head = display_name(store, q_uin, gmap) if q_uin else ""
        if not head or head in ("未知", q_uin):
            head = who_q or head
        qts = q.get("ts") or 0
        if qts:
            try:
                head = (datetime.datetime.fromtimestamp(qts)
                        .strftime("%m-%d %H:%M") + " " + head).strip()
            except (OverflowError, OSError, ValueError):
                pass
        lines.append(f"{' ' * (len(tm) + 1)}└ 回复 {head}: 「{qtx}」"
                     if head else f"{' ' * (len(tm) + 1)}└ 引用「{qtx}」")
    return lines


def build_segments(msgs, gap_min=30):
    """把消息切成「话题段」: 相邻消息间隔 > gap_min 分钟即视为换话题。

    返回 [{"start","end","items"}] (时间正序)。gap 是启发式 —— 它不判断话题
    是否真的变了, 只是给 LLM 一个「呼吸点」, 便于按段阅读和引用。
    """
    ms = sorted((m for m in msgs if m.get("ts")), key=lambda m: m["ts"])
    segs, cur = [], None
    gap = max(int(gap_min or 30), 1) * 60
    for m in ms:
        ts = m["ts"]
        if cur is None or (ts - cur["end"]) > gap:
            cur = {"start": ts, "end": ts, "items": []}
            segs.append(cur)
        cur["end"] = ts
        cur["items"].append(m)
    return segs


def segment_time(segs):
    """(最早, 最晚) 时间戳; 空则 (None, None)。"""
    if not segs:
        return None, None
    return segs[0]["start"], segs[-1]["end"]


def nick_collisions(gmap):
    """{昵称: [QQ号, ...]} —— 该群内**昵称重名**的成员。

    这不是边角情况: 实测某个 1148 人的群里「星辰」对应两个 QQ号,「Echo」三个,
    「null」两个, 还有一批昵称是纯空白/纯标点的。一旦重名, 只写昵称的分析 JSON
    就没法唯一确定是谁 —— 头像会配错人, 议题参与者会张冠李戴。
    所以这里把重名关系提前算出来, 在原料里显式告诉 Agent 必须写 QQ号。
    """
    rev = collections.defaultdict(list)
    for u, n in (gmap or {}).items():
        rev[n].append(u)
    return {n: sorted(us) for n, us in rev.items() if len(us) > 1}


def collect_members(segs, gmap=None, limit=80):
    """原料里出现过的成员 → [(QQ号, 昵称, 发言数)], 按发言量降序。

    这张表是给 **Agent** 用的: 报告要求用 QQ 号标注人物, 才能自动配上头像。
    只列真正出现在这批原料里的人, 避免把几百人的群名片全倒出来。收两类人:

      · 发言者 —— 计数即发言条数;
      · **只被引用、本时段没发言的人** (计数 0)。不收他们的话, 引用原文说话
        时就只能写个名字、配不上头像 —— 实测某群 32 条引用里 1 人只出现在
        别人的引用里, 本人在窗口内一条消息都没发。

    灰条系统消息不计入 —— 它是事件通知, 且戳一戳/拍一拍根本没有发送者。
    """
    cnt, name = {}, {}

    def note(u, nm):
        if u not in cnt:
            cnt[u] = 0
        if u not in name:
            name[u] = nm

    for sg in segs or []:
        for m in (sg.get("items") or []):
            for q in (m.get("media") or []):        # 被引用的人(先登记, 见下)
                if q.get("kind") != "quote":
                    continue
                qu = str(q.get("uin") or "").strip()
                if not qu or qu in ("0", "-1") or qu.startswith("u_"):
                    continue
                note(qu, (gmap or {}).get(qu) or clean_nick(q.get("who") or "", qu))
            if m.get("system"):
                continue
            u = str(m.get("uin") or "").strip()
            if not u or u in ("0", "-1") or u.startswith("u_"):
                continue                      # 系统消息 / 未解析出 QQ号
            note(u, (gmap or {}).get(u) or clean_nick("", u))
            cnt[u] += 1
    rows = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(u, name[u], n) for u, n in rows[:limit]]


def render_context(segs, store, gmap=None, max_chars=None, kicker=""):
    """把话题段渲染成 LLM 可读的纯文本。超长时从最早的段开始丢弃。"""
    blocks = []
    for i, sg in enumerate(segs, 1):
        d1 = datetime.datetime.fromtimestamp(sg["start"])
        d2 = datetime.datetime.fromtimestamp(sg["end"])
        same_day = d1.date() == d2.date()
        span = (f"{d1:%m-%d %H:%M}–{d2:%H:%M}" if same_day
                else f"{d1:%m-%d %H:%M}–{d2:%m-%d %H:%M}")
        mins = max(1, int((sg["end"] - sg["start"]) / 60) + 1)
        head = f"## 段{i} · {span} · {len(sg['items'])} 条 · 约 {mins} 分钟"
        if sg["start"] == sg["end"]:
            head = f"## 段{i} · {d1:%m-%d %H:%M} · {len(sg['items'])} 条"
        lines = [head]
        # 段内跨天时给每条消息带上日期, 否则 23:58 后面跟着 00:12 会读错顺序
        tfmt = "%H:%M" if same_day else "%m-%d %H:%M"
        for m in sg["items"]:
            lines.extend(msg_lines(m, store, gmap, tfmt=tfmt))
        blocks.append("\n".join(lines))

    txt = "\n\n".join(blocks)
    if max_chars and len(txt) > max_chars and len(blocks) > 1:
        keep, used = [], 0
        for blk in reversed(blocks):          # 从最新的段往回保留
            if keep and used + len(blk) > max_chars:
                break
            keep.append(blk)
            used += len(blk)
        dropped = len(blocks) - len(keep)
        txt = (f"（前 {dropped} 段因长度限制已省略, 仅保留最近 "
               f"{len(keep)} 段）\n\n" + "\n\n".join(reversed(keep)))
    if kicker:
        txt = kicker + "\n\n" + txt

    # 成员对照表: 让 Agent 能把「昵称」映射回 QQ号填进分析 JSON,
    # 报告据此自动配头像 (nickname→QQ 反查会漏, 表里有就是准的)。
    mem = collect_members(segs, gmap)
    if mem:
        txt = member_table(mem, nick_collisions(gmap)) + "\n\n" + txt
    return txt


def member_table(mem, coll=None, per_line=4):
    """渲染「发言成员对照表」。

    之前把 30 多个人挤在一行 `·` 分隔, 长到换行、读起来也难找人。改成多列
    对齐; 重名成员后面挂 `⚠` 并把候选 QQ号全列出来 —— Agent 看到 ⚠ 就知道
    这个昵称不能只写名字。
    """
    if not mem:
        return ""
    head = ["# 发言成员对照表 (昵称=QQ号)",
            "# 写分析 JSON 时, 人物/问答/金句里请带上对应的 QQ号, "
            "报告就能自动配上真实头像。"]
    if coll:
        # 重名必须把**全部候选**列出来, 不能只标一个 ⚠ —— 否则 Agent 仍然不知道
        # 该填哪个, 校验器也只能看到一个候选就「放心」地自动回填, 反而配错头像。
        items = " | ".join("%s=%s" % (n, "/".join(us))
                          for n, us in sorted(coll.items()))
        head.append(f"# ⚠ 以下昵称重名, 必须用 QQ号区分: {items}")
    cells = []
    for u, nm, _n in mem:
        mark = "⚠" if coll and nm in coll else ""
        cells.append(f"{nm}{mark}={u}")
    w = max((disp_w(c) for c in cells), default=0) + 2
    lines = []
    for i in range(0, len(cells), per_line):
        row = cells[i:i + per_line]
        # 每行都以 `#` 开头: (1) 整块在视觉上是一个注释区; (2) 下游
        # (qvalidate.load_context) 能明确认出「这是元信息, 不是聊天记录」,
        # 否则对齐用的多空格会让它被当成消息行。
        lines.append("# " + "  ".join(pad(c, w) for c in row).rstrip())
    return "\n".join(head + lines)


def est_tokens(text):
    """粗估 token 数 (字符数 * 0.7)。

    中英混排下 BPE 大致是 1 汉字≈0.6~1 token、1 英文单词≈1.3 token, 0.7 是
    偏保守的折中值。只用于判断「原料是不是快超窗口了」, 不追求精确。
    """
    return int(len(text or "") * 0.7)


def context_json(segs, store, gmap=None):
    """结构化版本, 便于脚本/Agent 精确引用 (带时间戳与发送者 QQ 号)。"""
    out = []
    for i, sg in enumerate(segs, 1):
        lines = []
        for m in sg["items"]:
            lines.append({
                "ts": m["ts"],
                "time": datetime.datetime.fromtimestamp(m["ts"]).strftime("%H:%M"),
                "who": display_name(store, m.get("uin"), gmap),
                "uin": m.get("uin"),
                "text": (m.get("text") or "").strip(),
                "media": [media_label(x) for x in (m.get("media") or [])],
            })
        out.append({
            "index": i,
            "start_ts": sg["start"], "end_ts": sg["end"],
            "start": datetime.datetime.fromtimestamp(sg["start"]).strftime("%Y-%m-%d %H:%M"),
            "end": datetime.datetime.fromtimestamp(sg["end"]).strftime("%H:%M"),
            "count": len(sg["items"]),
            "lines": lines,
        })
    return out


# ── CLI 直跑 (调试) ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import qqlib
    gid = sys.argv[1] if len(sys.argv) > 1 else None
    days = int(sys.argv[2]) if len(sys.argv) > 2 else None
    s = qqlib.QQStore()
    if not gid:
        gs = s.groups_overview()
        gid = gs[0]["id"]
        print("(未指定群号, 用最活跃的 %s)" % gid, file=sys.stderr)
    rep = report(s, gid, days=days)
    print(render_report(rep, s))
