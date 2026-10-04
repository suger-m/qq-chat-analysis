# -*- coding: utf-8 -*-
"""qqlib.py — NTQQ 解密数据库数据层

数据来源: <数据目录>/output/<QQ号>/ 下的解密 SQLite 库
  - nt_msg.db          消息主库 (列名=protobuf字段号)
  - group_info.db      群信息 (group_list: 60001=群号 60007=群名)
  - group_msg_fts.db   群消息明文文本 (group_msg_fts 表: [41701]=正文, [40001]=消息seq)
  - buddy_msg_fts.db / msg_fts.db  好友消息明文

数据目录默认 = %TEMP%/qq_decrypt (可用 QQ_DATA_DIR 覆盖), 见 _data_dir()。

缓存: <数据目录>/cache/ 目录, 以源库 (size, mtime) 为失效信号
"""
import os
import re
import sys
import json
import zlib
import base64
import sqlite3
import datetime
import tempfile
import errno
import time
from contextlib import contextmanager
import qpaths

BASE = os.path.dirname(os.path.abspath(__file__))
# 进程内缓存: 键为 "账号:库签名:业务键", 避免反复 JSON 反序列化大对象
_MEMO = {}


def _data_dir():
    """数据目录: 解密快照(约 4GB) + 分析缓存 + 头像/媒体缓存。

    与代码目录**解耦** —— 代码可以整个搬进 skill, 只要这里指向有数据的地方。

    默认放**系统临时目录**(%TEMP%/qq_decrypt)。理由是 output/ 100% 可再生:
    `fresh` 重解密一次约 37 秒。被磁盘清理/Storage Sense 清掉的代价只是一次
    重解密, 不值得为它长期占着项目目录或用户目录。
    代价: 快照没了 + QQ 没运行 = 完全读不到数据(重建需要 QQ 在线提供密钥)。
    想固定位置就设 QQ_DATA_DIR。
    """
    return qpaths.data_dir()


DATA_DIR = _data_dir()
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
# QQNT 媒体缓存目录(明文: Pic/Video/Ptt/Emoji, 文件名=小写md5.ext)
NT_DATA_DIR = qpaths.nt_data_dir()  # compatibility default; stores use their own account

# ── 头像 ─────────────────────────────────────────────────────────────────────
# QQ 官方头像接口 (无需登录), 多 CDN 兜底。首个不带 /headimg_dl 的接口兼容性最好。
AVATAR_DIR = os.path.join(CACHE_DIR, "avatars")
AVATAR_CDNS = (
    "https://q1.qlogo.cn/g?b=qq&nk={uin}&s={size}",
    "https://q.qlogo.cn/headimg_dl?dst_uin={uin}&spec={size}&img_type=jpg",
    "https://q4.qlogo.cn/g?b=qq&nk={uin}&s={size}",
)
_AV_MEMO = {}          # (uin,size) -> bytes | None, 进程内缓存(含"失败"结果)

# ── QQ 经典小黄脸 ID → 名称 (静态兜底表) ──────────────────────────────────────
# 表来源: shuakami/qq-chat-exporter  docs 参考实现 get_face_name_by_id()。
# 本地 emoji.db.base_sys_emoji_table 优先(更新更全), 本表用于 emoji.db 缺失 /
# 记录不全时兜底 —— 表情是群聊情绪的重要信号, 不该只有一串数字 ID。
FACE_NAMES = {
    0: "微笑", 1: "撇嘴", 2: "色", 3: "发呆", 4: "得意", 5: "流泪", 6: "害羞", 7: "闭嘴",
    8: "睡", 9: "大哭", 10: "尴尬", 11: "发怒", 12: "调皮", 13: "呲牙", 14: "惊讶", 15: "难过",
    16: "酷", 17: "冷汗", 18: "抓狂", 19: "吐", 20: "偷笑", 21: "可爱", 22: "白眼", 23: "傲慢",
    24: "饥饿", 25: "困", 26: "惊恐", 27: "流汗", 28: "憨笑", 29: "大兵", 30: "奋斗", 31: "咒骂",
    32: "疑问", 33: "嘘", 34: "晕", 35: "折磨", 36: "衰", 37: "骷髅", 38: "敲打", 39: "再见",
    40: "擦汗", 41: "抠鼻", 42: "鼓掌", 43: "糗大了", 44: "坏笑", 45: "左哼哼", 46: "右哼哼", 47: "哈欠",
    48: "鄙视", 49: "委屈", 50: "快哭了", 51: "阴险", 52: "亲亲", 53: "吓", 54: "可怜", 55: "菜刀",
    56: "西瓜", 57: "啤酒", 58: "篮球", 59: "乒乓", 60: "咖啡", 61: "饭", 62: "猪头", 63: "玫瑰",
    64: "凋谢", 65: "示爱", 66: "爱心", 67: "心碎", 68: "蛋糕", 69: "闪电", 70: "炸弹", 71: "刀",
    72: "足球", 73: "瓢虫", 74: "便便", 75: "月亮", 76: "太阳", 77: "礼物", 78: "拥抱", 79: "强",
    80: "弱", 81: "握手", 82: "胜利", 83: "抱拳", 84: "勾引", 85: "拳头", 86: "差劲", 87: "爱你",
    88: "NO", 89: "OK", 96: "跳跳", 97: "发抖", 98: "怄火", 99: "转圈", 100: "磕头", 101: "回头",
    102: "跳绳", 103: "挥手", 104: "激动", 105: "街舞", 106: "献吻", 107: "左太极", 108: "右太极", 109: "闭眼",
    110: "流鼻涕", 111: "惊喜", 112: "骂人", 116: "爱情", 117: "飞吻", 118: "跳跳", 120: "颤抖", 121: "怄火",
    122: "转圈", 123: "磕头", 124: "回头", 125: "跳绳", 126: "投降", 127: "激动", 128: "乱舞", 129: "献吻",
    173: "嘿哈", 174: "捂脸", 175: "奸笑", 176: "机智", 177: "皱眉", 178: "耶", 179: "吃瓜", 180: "加油",
    181: "汗", 182: "天啊", 183: "Emm", 184: "社会社会", 185: "旺柴", 186: "好的", 187: "打脸", 188: "哇",
    189: "翻白眼", 190: "666", 191: "让我看看", 192: "叹气", 193: "苦涩", 194: "裂开", 195: "嘴唇", 196: "爱心",
    197: "惊喜", 201: "生气", 202: "吃惊", 203: "酸了", 204: "太难了", 205: "我想开了", 206: "右上看", 207: "嘿嘿嘿",
    208: "捂眼", 210: "敬礼", 211: "狗头", 212: "吐舌", 214: "哦", 215: "请", 216: "睁眼", 217: "敲开心",
    218: "震惊", 219: "让我康康", 220: "摸鱼", 221: "魔鬼笑", 222: "哦哟", 223: "傻眼", 224: "抽烟", 225: "笑哭",
    226: "汪汪", 227: "汗", 228: "打脸", 229: "无语", 230: "拥抱", 231: "摸头", 232: "加油", 233: "震惊哭",
    234: "托腮", 235: "我酸了", 236: "快哭了", 237: "吃糖", 238: "生气", 260: "拜托", 261: "求你了", 262: "好的",
    263: "我想开了", 264: "比心", 265: "啵啵", 266: "蹭蹭", 267: "拍手", 268: "佛系", 269: "喝奶茶", 270: "吃糖",
    271: "Doge", 277: "吃", 278: "呆", 279: "仔细分析", 280: "加油", 281: "崇拜", 282: "比心", 283: "庆祝",
    284: "生日快乐", 285: "舔屏", 286: "笑哭", 287: "doge", 288: "哈哈", 289: "酸了", 290: "汪汪", 291: "哦呼",
    292: "喵喵", 293: "求抱抱", 294: "期待", 295: "拜托了", 296: "元气满满", 297: "满分", 298: "坏笑", 299: "你真棒",
    300: "收到", 301: "拒绝", 302: "吃瓜", 303: "嗯哼", 304: "吃鲸", 305: "汗", 306: "无眼看", 307: "敬礼",
    308: "面无表情", 309: "摊手", 310: "灵魂出窍", 311: "脑阔疼", 312: "沧桑", 313: "捂脸哭", 314: "笑cry", 315: "无语凝噎",
    316: "@所有人", 317: "裂开", 318: "叹气", 319: "摸鱼", 320: "吃", 321: "呐", 322: "左看看", 323: "右看看",
    324: "叹气", 325: "我想开了", 326: "无语", 327: "问号", 328: "怂", 329: "犬", 330: "坏笑", 331: "喝奶茶",
    332: "吃瓜", 333: "鬼脸", 334: "震惊", 335: "嘿嘿", 336: "歪嘴", 337: "狂笑", 338: "嘻嘻", 339: "扶墙",
    340: "捂脸", 341: "奋斗", 342: "白眼",
}



def avatar_bytes(uin, size=140, timeout=6):
    """取 QQ 头像原始字节 (jpg)。命中 cache/avatars/ 直接返回, 失败返回 None。

    不抛异常 —— 头像只是装饰, 拿不到就由调用方降级成字母头像。
    """
    uin = str(uin or "").strip()
    if not uin.isdigit():
        return None
    key = (uin, size)
    if key in _AV_MEMO:
        return _AV_MEMO[key]
    try:
        os.makedirs(AVATAR_DIR, exist_ok=True)
    except OSError:
        pass
    path = os.path.join(AVATAR_DIR, f"{uin}_{size}.jpg")
    if os.path.isfile(path) and os.path.getsize(path) > 512:
        try:
            with open(path, "rb") as f:
                data = f.read()
            _AV_MEMO[key] = data
            return data
        except OSError:
            pass
    import urllib.request
    data = None
    for tpl in AVATAR_CDNS:
        try:
            req = urllib.request.Request(
                tpl.format(uin=uin, size=size),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://qzone.qq.com/"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
            # 接口总是返回 jpeg; 用魔数挡掉错误页/空响应
            if b and len(b) > 512 and b[:2] == b"\xff\xd8":
                data = b
                break
        except Exception:
            continue
    if data:
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError:
            pass
    _AV_MEMO[key] = data
    return data


def avatar_data_uri(uin, size=140):
    """头像 → data:URI (报告内联用, 保证单文件离线可看)。失败返回 None。"""
    b = avatar_bytes(uin, size)
    if not b:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(b).decode("ascii")


# ── 元素 → 可读标记 ──────────────────────────────────────────────────────────
# 导出(txt/md/csv/jsonl)与 LLM 上下文共用同一套渲染, 保证「同一条消息」
# 在任何出口都是一样的样子。参考 shuakami/qq-chat-exporter 的 text_exporter
# 与 modern_html_exporter: 它们把元素逐个渲染成 [图片消息]/[语音 12"]/
# 文件大小/尺寸, 而不是只写正文 —— 媒体本身也是对话内容。
KIND_LABEL = {
    "pic": "图片", "video": "视频", "ptt": "语音", "file": "文件",
    "face": "表情", "emoji": "表情", "marketface": "表情包",
    "card": "卡片", "md": "Markdown", "quote": "引用", "tip": "提示",
}
# 正文只由「媒体占位标签」构成(如 "[图片]"、" [图片×3]")时, 说明这条消息的
# 内容已经由 media_label 表达过了, 不该再重复一遍。仅在确实解析出媒体时使用。
TAG_ONLY = re.compile(r"^(?:\[[^\]]{0,60}\]\s*)+$")


def fmt_size(n):
    """字节 → 人类可读 (0/None 返回空串)"""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0
    return ""


def fmt_dur(sec):
    """秒 → 8" / 1'05" (0/None 返回空串)"""
    try:
        s = int(sec or 0)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    return "%d\"" % s if s < 60 else "%d'%02d\"" % (s // 60, s % 60)


def media_label(md):
    """把一条媒体元素压成一行可读标记, 供 LLM / 导出文件理解「这条消息是什么」。

    带体积与尺寸: 图片 [图片 802×958]、文件 [文件: x.pdf 1.2MB]、语音 [语音 12KB]。
    这些是「消息有多重」的直接信号, 只写 [图片] 会丢掉它。
    """
    k = md.get("kind")
    nm = (md.get("name") or "").strip()
    size, w, h = md.get("size") or 0, md.get("w") or 0, md.get("h") or 0
    dur = fmt_dur(md.get("dur"))
    if k == "pic":
        return "[图片 %d×%d]" % (w, h) if w and h else "[图片]"
    if k == "video":
        s = " ".join(x for x in (dur, fmt_size(size)) if x)
        return "[视频 %s]" % s if s else "[视频]"
    if k == "ptt":
        s = " ".join(x for x in (dur, fmt_size(size)) if x)
        return "[语音 %s]" % s if s else "[语音]"
    if k == "file":
        s = " ".join(x for x in (fmt_size(size),) if x)
        if nm:
            return "[文件: %s%s]" % (nm, (" " + s) if s else "")
        return "[文件 %s]" % s if s else "[文件]"
    if k in ("emoji", "face"):
        return "[表情: %s]" % nm if nm else "[表情]"
    if k == "marketface":
        return "[表情包: %s]" % nm if nm else "[表情包]"
    if k == "card":
        tag = (md.get("tag") or "卡片").strip()
        ti = (md.get("title") or "").strip()
        de = re.sub(r"\s+", " ", (md.get("desc") or "")).strip()
        s = "[%s: %s]" % (tag, ti) if ti else "[%s]" % tag
        if de and de != ti:
            s += " (%s)" % de[:90]
        return s
    if k == "md":
        body = (md.get("md") or "").strip()
        head = re.sub(r"\s+", " ", body.split("\n", 1)[0])[:110] if body else ""
        return "[图文卡片: %s]" % head if head else "[图文卡片]"
    if k == "btns":
        labs = [b.get("t") for row in (md.get("btns") or [])
                for b in row if isinstance(b, dict) and b.get("t")]
        return "[按钮: %s]" % " / ".join(labs[:4]) if labs else "[按钮组]"
    if k == "quote":
        who = (md.get("who") or "").strip()
        return "[引用: %s]" % who if who else "[引用]"
    if k == "tip":
        return "[%s]" % nm if nm else "[提示]"
    return "[%s]" % KIND_LABEL.get(k, k or "元素")


def avatar_group_data_uri(uins, size=140, maxn=8):
    """群成员头像 → 报告 Hero 用的 [data URI, ...] (去重、限量)。"""
    out, seen = [], set()
    for u in uins:
        u = str(u or "").strip()
        if not u.isdigit() or u in seen:
            continue
        seen.add(u)
        uri = avatar_data_uri(u, size)
        if uri:
            out.append(uri)
        if len(out) >= maxn:
            break
    return out

# ── 富元素「快速预筛」 ───────────────────────────────────────────────────────
# extract_media 要跑完整 blackboxprotobuf 解码 + 各类卡片解析(实测 ~0.1ms/条),
# 而绝大多数消息是纯文本(t=1 占 ~93%)。这里把「只有富元素才出现的字段号」编码成
# protobuf 的 tag 字节, 直接在 blob 上做子串查找 —— 命中才解码。
# 原先只有 qanalysis 用了这招, read_group 仍对**每条**消息全量解码, 白白把
# 时间花在纯文本上; 下沉到数据层后同一套预筛在两条路径上都生效。
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


# 元素类型字段: 每个 45002 元素都带此字段; 其后的 varint 即为类型值。
_EL_TYPE_TAG = _varint((45002 << 3) | 0)      # wire type 0 = varint
# 不算「富元素」的类型: 0=未识别, 1=纯文本, 8=灰条系统提示
_PLAIN_TYPES = (0, 1, 8)

# 兜底字段(某些元素可能不发 45002): 引用(474xx) / 媒体(45402) / 表情 / 卡片
_RICH_FALLBACK = tuple(_varint((f << 3) | w) for f, w in (
    (45402, 2), (47601, 0), (47602, 2), (80810, 0), (80900, 2),
    (47402, 0), (47403, 0), (47404, 0), (47416, 0), (47422, 0), (47423, 2),
    (47901, 2), (48602, 2), (48701, 2), (48751, 2),
))


def has_rich(blob):
    """blob 是否可能含富元素(媒体/卡片/表情/引用)。纯文本消息直接跳过解码。

    做法: 扫描元素类型字段 45002 的 protobuf tag, 读出紧随其后的 varint 类型值,
    只要出现非「文本/灰条」的类型就认为需要解码。比匹配固定字段名更严谨
    (如 引用 元素顶层并无 47413, 靠字段名单会漏)。
    """
    tag = _EL_TYPE_TAG
    n = len(blob)
    pos = blob.find(tag)
    while pos >= 0:
        j = pos + len(tag)
        v = 0
        shift = 0
        while j < n:
            b = blob[j]
            v |= (b & 0x7f) << shift
            j += 1
            if not (b & 0x80):
                break
            shift += 7
            if shift > 28:            # 异常 varint: 当作未识别, 直接解码
                return True
        if v not in _PLAIN_TYPES:
            return True
        pos = blob.find(tag, pos + 1)
    for t in _RICH_FALLBACK:
        if t in blob:
            return True
    return False


# ── 灰条系统消息的廉价识别 (不解码, 只查协议字段标记) ─────────────────────────
# 撤回 / 戳一戳 / 入群 / 邀请 等元素类型恒为 45002=8, 但承载文案的字段并不统一:
# 戳一戳走 48214(<gtip> token 流)或 48271(JSON items), 撤回走 477xx 且**两个
# 模板字段都没有**。所以除了模板字段, 还必须匹配「元素类型 = 8」这一协议标记,
# 否则撤回类会被整批漏掉(实测漏 ~40%)。
_GRAY_PROBE = (
    _EL_TYPE_TAG + _varint(8),          # 元素类型 45002=8 (一切灰条的共同标记)
    b"<gtip",                           # 无 45002 但有模板的变体
    _varint((48214 << 3) | 2),          # <gtip> token 流
    _varint((48271 << 3) | 2),          # JSON items 变体
)


def is_gray_blob(blob):
    """blob 是否为灰条系统消息(撤回/戳一戳/入群/邀请...)。

    只查协议字段标记, 不解码。比按文案匹配可靠: 用户自己发「撤回了什么瓜」
    这类文本不会被误判成系统消息, 而真正的灰条一个不漏。
    """
    return bool(blob) and any(p in blob for p in _GRAY_PROBE)


# ── protobuf 噪声过滤 ─────────────────────────────────────────────────────────
NOISE = re.compile(
    r"^(u_|u-)[A-Za-z0-9_-]{8,}$"        # nt uid
    r"|^\d{6,}$"                          # 纯长数字
    r"|^[0-9a-fA-F]{16,}$"                # hex
    r"|^[A-Za-z0-9+/=_-]{24,}$"           # base64 / base64url 长串(fileid 等)
    r"|^[0-9A-Fa-f]{16,}\.[A-Za-z0-9]{2,5}$"   # 媒体缓存文件名(大写hex.ext)
    r"|^/?download\?"                     # QQ 下载直链 ?appid=..&fileid=..
    r"|^[a-z_]+=[^&\s]*"                  # URL 查询串片段 spec=720 / appid=1407
    r"|^(?:[A-Za-z0-9-]+\.)+(?:com|cn|net|org)(?:\.[a-z]{2,3})?$"   # 域名
    r"|^https?://\S*$"                    # 裸链接
    r"|^(nt|nick_str|uin_str|type_str|action_str|suffix_str|jump_url|preview_str"
    r"|action_img_url|aio_url|uint|bytes|str)\d*$"  # protobuf 字段名噪声
)

def _walk_text(obj, acc):
    if isinstance(obj, dict):
        for v in obj.values():
            _walk_text(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _walk_text(v, acc)
    elif isinstance(obj, bytes):
        try:
            s = obj.decode("utf-8")
            _push(s, acc)
        except UnicodeDecodeError:
            pass
    elif isinstance(obj, str):
        _push(obj, acc)

def _push(s, acc):
    s = s.strip()
    if not s or len(s) > 3000:
        return
    if "<gtip" in s or "<qq " in s or "http://tianquan" in s:
        return
    if re.match(r"[A-Za-z]:[\\/]", s) or "\\nt_data\\" in s:
        return                                # 本地媒体缓存路径(不该进正文)
    if NOISE.match(s):
        return
    acc.append(s)

def _gz(v):
    """bytes/str -> str (灰条字段大多是 bytes)"""
    if isinstance(v, bytes):
        return v.decode("utf-8", "ignore")
    return v if isinstance(v, str) else ""


# ── 灰条系统消息 (45002=8) ───────────────────────────────────────────────────
# 参考实现: shuakami/qq-chat-exporter  qq-chat-export-server/src/parser/
#           simple_parser.rs :: parse_xml_gray_tip / parse_json_gray_tip
#
# 关键点: 灰条正文**不是一段纯文本**, 而是「token 流」——
#   <gtip align="center">
#     <qq uin="u_xxx" col="3" jp="<成员甲_ID>"/><nor txt="邀请"/>
#     <qq uin="u_yyy" col="3" jp="<成员乙_ID>"/> <nor txt="加入了群聊，并附带了30条聊天记录。"/>
#   </gtip>
# 逐 token 还原: <qq> → 人名(依次取 nm 属性 / uin→昵称表 / jp=QQ号),
# <nor>|<url> → txt 原文。另一变体是 JSON: 48271 = {"items":[{"type":"qq",...}]}。
# 直接"去标签"会把 <nor txt="邀请"> 的正文丢掉、把兄弟字段的昵称当成正文。
_GTIP_TAG = re.compile(r"<(qq|nor|url|img)\b([^>]*?)/?>", re.I)
_GTIP_ATTR = re.compile(r'([A-Za-z_][\w-]*)\s*=\s*"([^"]*)"')
_GTIP_ESC = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
             ("&apos;", "'"), ("&amp;", "&"))
# 灰条里内嵌的真元素 (47710/47711/... = 完整 element 子消息)
_NESTED_LABEL = {2: "[图片]", 3: "[文件]", 4: "[语音]", 5: "[视频]",
                 6: "[表情]", 10: "[卡片]", 11: "[表情包]", 16: "[卡片]"}
# 元素类型 -> 占位标签 (引用元素里内嵌的原消息没有文本时用)
_MEDIA_TAG = {2: "[图片]", 3: "[文件]", 4: "[语音]", 5: "[视频]",
              6: "[表情]", 7: "[引用]", 10: "[卡片]", 11: "[表情包]",
              16: "[卡片]", 17: "[按钮]"}
# 本地缓存路径 / 下载直链(不该出现在正文里)
_MPATH = re.compile(r"[A-Za-z]:\\|/download\?|fileid=")


def _unesc(s):
    for a, b in _GTIP_ESC:
        s = s.replace(a, b)
    return s


def _gray_names(el):
    """灰条元素内 uid -> 昵称: 48210 = [{1005:uid, 1006:昵称}]"""
    m = {}
    for p in (el.get("48210") or []):
        if isinstance(p, dict):
            u, n = _gz(p.get("1005")), _gz(p.get("1006")).strip()
            if u and n and not n.startswith("u_"):
                m.setdefault(u, n)
    return m


def _qq_token_name(attrs, names):
    """<qq> token -> 人名: nm 属性 > uin 反查昵称 > jp(QQ号)"""
    nm = (attrs.get("nm") or "").strip()
    if nm:
        return nm
    nm = names.get(attrs.get("uin") or "", "")
    if nm:
        return nm
    return (attrs.get("jp") or "").strip()


def _gtip_xml(xml, names):
    """<gtip> token 流 -> 可读文本 (token 之间不额外插分隔符, 与 QQ 一致)"""
    out = []
    for m in _GTIP_TAG.finditer(xml):
        tag = (m.group(1) or "").lower()
        attrs = {k: _unesc(v) for k, v in _GTIP_ATTR.findall(m.group(2) or "")}
        if tag == "qq":
            n = _qq_token_name(attrs, names)
            if n:
                out.append(n)
        elif tag in ("nor", "url"):
            t = (attrs.get("txt") or "").strip()
            if t:
                out.append(t)
    return re.sub(r"\s{2,}", " ", "".join(out)).strip()


def _gtip_json(js, names):
    """48271 JSON 变体 -> 可读文本"""
    try:
        obj = json.loads(js)
    except (ValueError, TypeError):
        return ""
    if not isinstance(obj, dict):
        return ""
    if not obj.get("items"):
        # 部分变体直接把文案放在 prompt/content
        for k in ("prompt", "content", "wording"):
            if isinstance(obj.get(k), str) and obj[k].strip():
                return obj[k].strip()
    out = []
    for it in (obj.get("items") or []):
        if not isinstance(it, dict):
            continue
        t = (it.get("type") or "").lower()
        if t == "qq":
            n = _qq_token_name({k: _gz(v).strip() for k, v in it.items()}, names)
            if n:
                out.append(n)
        elif t in ("nor", "url"):
            s = (it.get("txt") or "").strip()
            if s:
                out.append(s)
    return re.sub(r"\s{2,}", " ", "".join(out)).strip()


def _revoke_text(el):
    """撤回灰条 (45003=1) -> "X 撤回了一条消息"。

    字段实测(47713 的附言与 QCE 单测里的 wording 逐字一致, 可确证):
      47703/47704 = 操作者 uid   47705 = 操作者名
      47714 = 原消息发送者名     47713 = 附言(wording, 如"因为有错别字。")
      47710 = 被撤回消息的原始元素(图片等)
    """
    op = _gz(el.get("47705")).strip()
    orig = _gz(el.get("47714")).strip()
    if not op:
        op = orig or "成员"
    s = ("%s 撤回了 %s 的消息" % (op, orig)) if orig and orig != op \
        else ("%s 撤回了一条消息" % op)
    w = _gz(el.get("47713")).strip().rstrip("。！？!? \t")
    if w:
        s += "（%s）" % w
    return s


def _nested_label(el):
    """灰条里内嵌完整子元素(47710/47711/...) -> 类型标签"""
    for k in sorted(el, key=str):
        v = el[k]
        if not str(k).startswith("477") or not isinstance(v, dict):
            continue
        t = v.get("45002")
        if t in _NESTED_LABEL:
            return _NESTED_LABEL[t]
    return ""

def _gray_members(el):
    """无模板变体里能确证的信息: 涉事成员昵称 (48504/48505/48507/48508,
    以及 48541->48521->20002 嵌套的成员对象)"""
    names = []

    def add(v):
        s = _gz(v).strip()
        if s and not s.startswith("u_") and s not in names:
            names.append(s)

    for k in ("48504", "48505", "48507", "48508"):
        add(el.get(k))

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("20002", "1006"):
                    add(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(el.get("48541"))
    return names[:6]


def _has_gray_tip(body):
    elems = body if isinstance(body, list) else ([body] if isinstance(body, dict) else [])
    for el in elems:
        if not isinstance(el, dict):
            continue
        t = el.get("45002")
        if t == 8 or (t is None and (el.get("48214") or el.get("48271"))):
            return True
    return False


def _gray_tip_text(body):
    """灰条系统消息 -> 可读文本。

    优先按模板还原(XML token 流 / JSON items); 没有模板的变体**不猜语义**,
    只给出「这是系统消息 + 涉事成员」, 避免把裸昵称、本地文件路径、
    占位符(invitor/invitee/msg_num)当成正文吐给下游(那会污染 LLM 的理解)。
    """
    elems = body if isinstance(body, list) else ([body] if isinstance(body, dict) else [])
    out = []
    for el in elems:
        if not isinstance(el, dict):
            continue
        t = el.get("45002")
        if t is not None and t != 8:
            continue
        names = _gray_names(el)
        txt = ""
        if el.get("48214"):
            txt = _gtip_xml(_gz(el["48214"]), names)
        if not txt and el.get("48271"):
            txt = _gtip_json(_gz(el["48271"]), names)
        if not txt and el.get("45003") == 1 and (el.get("47705") or el.get("47714")):
            txt = _revoke_text(el)          # 撤回应先于内嵌元素判定
        if not txt:
            txt = _nested_label(el)
        if not txt:
            # 变体太多(45003=1/3/4/5/8...), 语义未确证: 只陈述「系统消息」+ 涉事成员
            who = _gray_members(el)
            txt = "[系统消息: {}]".format("、".join(who)) if who else "[系统消息]"
        if txt:
            out.append(txt)
    return " ".join(out)



def text_from_blob(blob):
    """protobuf 盲解析兜底提取正文"""
    if not blob:
        return ""
    import blackboxprotobuf
    try:
        msg, _ = blackboxprotobuf.decode_message(blob)
        body = msg.get("40800") if isinstance(msg, dict) else None
        if _has_gray_tip(body):
            # 灰条必须**优先**走模板解析: 盲目遍历会把兄弟字段里的昵称
            # (48210) 和占位符(48217 的 invitor/invitee/msg_num)当成正文
            # 捞出来, 得到 "w21 瑜笙 invitor invitee msg_num 30" 这种垃圾。
            tip = _gray_tip_text(body)
            if tip:
                return tip
        acc = []
        _walk_text(msg, acc)
        # 去重保序
        seen, out = set(), []
        for t in acc:
            if t not in seen:
                seen.add(t)
                out.append(t)
        text = " ".join(out)
        text = re.sub(r"\s*\b(?:nt|jp_str|nick_str|uin_str|type_str|action_str|"
                      r"suffix_str|preview_str)_?\d*\b", " ", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        # 清掉媒体/卡片残留的下载直链、查询串、CDN 域名与超长 fileid
        text = re.sub(r"\S*(?:/download\?|fileid=)\S*", " ", text)
        text = re.sub(r"\b(?:appid|fileid|spec|rkey|term|is_origin)=[^\s&]*", " ", text)
        text = re.sub(r"\b[A-Za-z0-9+/=_-]{40,}\b", " ", text)
        text = re.sub(r"\b(?:[A-Za-z0-9-]+\.)+(?:com|cn|net|org)(?:\.[a-z]{2,3})?\b",
                      " ", text)
        text = re.sub(r"[A-Za-z]:\\[^\s]*", " ", text)   # 本地路径残留
        text = re.sub(r"(?:^|\s)&(?=\s|$)", " ", text)   # 盲解析残留的孤立 &
        text = re.sub(r"\s{2,}", " ", text).strip()
        if not text:
            # 兜底: 仍有元素被解析成灰条时再试一次
            text = _gray_tip_text(body)
        return text
    except Exception:
        return ""

# ── 连接管理 ──────────────────────────────────────────────────────────────────

def detect_account():
    """自动选择 output/ 下 nt_msg.db 最新修改的账号"""
    best, best_mt = None, -1
    if not os.path.isdir(OUTPUT_DIR):
        return None
    for name in os.listdir(OUTPUT_DIR):
        if not name.isascii() or not name.isdigit():
            continue
        p = os.path.join(OUTPUT_DIR, name, "nt_msg.db")
        if os.path.isfile(p):
            mt = os.path.getmtime(p)
            if mt > best_mt:
                best, best_mt = name, mt
    return best


# ── 数据新鲜度: 源加密库 vs 解密产物 ─────────────────────────────────────────
# QQ 运行时持续往加密库写消息, 而 output/ 里的是「某次解密的快照」。
# 分析前必须比较两者, 否则会一直基于旧 checkpoint 做分析。
SOURCE_BASE = qpaths.source_base()
_KEY_DBS = ("nt_msg.db", "group_msg_fts.db", "group_info.db",
            "emoji.db", "buddy_msg_fts.db")


def source_dir(account):
    """QQ 源加密库目录 (账号目录优先, 回退 global)。"""
    for cand in (os.path.join(SOURCE_BASE, str(account), "nt_qq", "nt_db"),
                 os.path.join(SOURCE_BASE, "nt_qq", "global", "nt_db")):
        if os.path.isdir(cand):
            return cand
    return None


def _newest_mtime(d, names=_KEY_DBS):
    """库目录下关键 db 及其 -wal 的最新 mtime。

    QQ 默认开着 WAL: 最新消息很可能只落在 -wal 里、主库还没 checkpoint,
    所以必须把 -wal 一并计入, 否则会误判成「没有新消息」。
    """
    mx = 0.0
    if not d or not os.path.isdir(d):
        return mx
    for n in names:
        for suf in ("", "-wal"):
            try:
                mx = max(mx, os.path.getmtime(os.path.join(d, n + suf)))
            except OSError:
                pass
    return mx


def qq_running():
    """QQ.exe 是否在运行。返回 True / False / None(检测失败)。"""
    try:
        import subprocess
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/NH"],
            capture_output=True, text=True, timeout=10)
        return "QQ.exe" in (r.stdout or "")
    except Exception:
        return None


def freshness(account=None, tol=120):
    """数据新鲜度。

    stale=True 表示源库比解密产物新 (产物是旧 checkpoint), 需要重新解密。
    tol 为容差秒数: QQ 运行时一直在写源库, 容差太小会导致几乎每次命令都触发
    一次 ~68s 的全量解密。2 分钟足够新, 也足够稳。用 --fresh 可无视容差立即同步。
    """
    account = account or detect_account() or qpaths.detect_source_account()
    src = source_dir(account)
    out = os.path.join(OUTPUT_DIR, str(account or ""))
    src_mt = _newest_mtime(src)
    out_mt = _newest_mtime(out) if os.path.isdir(out) else 0.0
    stale = bool(src) and (not out_mt or src_mt > out_mt + tol)
    return {
        "account": account,
        "src_dir": src,
        "out_dir": out,
        "src_mtime": src_mt,
        "out_mtime": out_mt,
        "src_mtime_str": (datetime.datetime.fromtimestamp(src_mt)
                          .strftime("%Y-%m-%d %H:%M:%S") if src_mt else ""),
        "out_mtime_str": (datetime.datetime.fromtimestamp(out_mt)
                          .strftime("%Y-%m-%d %H:%M:%S") if out_mt else ""),
        "stale": stale,
        "lag_sec": max(0, int(src_mt - out_mt)) if stale else 0,
        "qq_running": qq_running(),
    }


@contextmanager
def _sync_lock(quiet=True, timeout=900):
    """Serialize publishers across processes, including the shared key_map.json.

    OS file locks are released on process exit; keep the lock file in place so
    another process cannot lock a different inode while a waiter still has it open.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, '.sync.lock'), 'a+b') as lock:
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b'\0')
            lock.flush()
        if os.name == 'nt':
            import msvcrt

            def acquire():
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + timeout
        waiting = False
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError('等待另一个同步任务超时') from exc
                if not waiting and not quiet:
                    print('  · 另一个同步任务正在运行，等待完成…',
                          file=sys.stderr, flush=True)
                waiting = True
                time.sleep(0.1)
        try:
            yield
        finally:
            release()


def sync_latest(account=None, quiet=True):
    """重新提取密钥 + 解密, 把 output/ 更新到最新。返回 (ok, msg)。

    采用「解密到临时目录 → 成功后原子替换」的方式。之前是直接覆盖
    output/<qq>/, 一旦解密中途失败 (磁盘/占用/中断), 会把好数据留在
    半成品状态 —— 实测曾把 3GB 的 nt_msg.db 写成 0 字节, 导致整个
    数据层不可用。现在失败只会丢弃临时目录, 原快照原样保留。
    """
    account = account or detect_account() or qpaths.detect_source_account()
    if not account:
        return False, "未找到账号；请指定 --account 或配置 QQ_SOURCE_DIR"
    try:
        account = qpaths.account_id(account)
    except ValueError as e:
        return False, str(e)
    dump = os.path.join(BASE, "dump_qq_key_auto.py")
    if not os.path.isfile(dump):
        return False, f"找不到解密脚本: {dump}"
    try:
        with _sync_lock(quiet=quiet):
            return _sync_latest_locked(account, dump, quiet)
    except TimeoutError:
        return False, "等待同步锁超时 (900s), 未发布新快照"
    except OSError as exc:
        return False, f"同步失败, 未发布新快照: {exc}"


def _sync_latest_locked(account, dump, quiet):
    import shutil
    import subprocess

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, str(account or ""))
    # Each invocation owns its staging directory; never remove another run's.
    tmp = tempfile.mkdtemp(prefix=f".tmp_{account}_", dir=OUTPUT_DIR)
    if not quiet:
        print(f"  · 同步最新消息中 (账号 {account}) …",
              file=sys.stderr, flush=True)
    # 密钥只写进数据目录, 不落代码目录 —— 它会随项目外泄, 且每次扫描都会重写
    os.makedirs(DATA_DIR, exist_ok=True)
    key_dump = os.path.join(DATA_DIR, "key_map.json")
    try:
        try:
            r = subprocess.run(
                [sys.executable, dump, "--qq", str(account),
                 "--out-dir", tmp, "-o", key_dump],
                cwd=BASE, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=900)
        except subprocess.TimeoutExpired:
            return False, "同步超时 (900s), 已保留原快照"
        if r.returncode != 0:
            tail = ((r.stdout or "") + (r.stderr or "")).strip()[-500:]
            return False, f"同步失败 (exit {r.returncode}), 已保留原快照。{tail}"

        # Serialized publication: keep the old snapshot available for rollback.
        old = out + ".old"
        if os.path.isdir(old):
            if not os.path.exists(out):
                os.replace(old, out)  # Recover an interrupted previous publication.
            else:
                shutil.rmtree(old, ignore_errors=True)
        moved = False
        try:
            if os.path.isdir(out):
                os.replace(out, old)
                moved = True
            os.replace(tmp, out)
        except OSError as exc:
            if moved:
                try:
                    os.replace(old, out)
                except OSError as recovery:
                    return False, (f"替换解密产物失败: {exc}; "
                                   f"原快照仍在备份目录, 自动恢复失败: {recovery}")
            return False, f"替换解密产物失败, 已保留原快照: {exc}"
        shutil.rmtree(old, ignore_errors=True)
        _MEMO.clear()          # 解密库换了, 进程内缓存必须作废
        return True, "已同步到最新"
    finally:
        # tmp is a unique direct child of OUTPUT_DIR allocated by this invocation.
        shutil.rmtree(tmp, ignore_errors=True)

class QQStore:
    def __init__(self, account=None):
        self.account = account or detect_account()
        if not self.account:
            raise RuntimeError(
                f"数据目录 {OUTPUT_DIR} 下没有已解密的账号数据。\n"
                "  解密需要从运行中的 QQ 进程内存取密钥, 所以:\n"
                "  1) 先启动 QQ 并保持登录, 再重跑本命令 (会自动重新解密);\n"
                f"  2) 若数据目录被系统清理过, 可设 QQ_DATA_DIR 换到不会被清的位置。")
        self.account = qpaths.account_id(self.account)
        self.dir = os.path.join(OUTPUT_DIR, self.account)
        self.media_dir = qpaths.nt_data_dir(self.account)
        import threading
        self._tls = threading.local()
        self._tls.nt = None
        self._tls.gi = None
        self._tls.fts = None
        self._tls.pi = None

    @property
    def nt_path(self):
        return os.path.join(self.dir, "nt_msg.db")

    def _connect(self, path):
        """带容错解码的连接。

        库里个别 TEXT 列(如群名片 64003)存有非 UTF-8 字节, 默认 text_factory
        会对整列抛 OperationalError 并**中断整个游标遍历** —— 表现为遍历到某行
        突然静默结束(实测群成员表在 42215/102079 行断掉, 之后全部成员丢失昵称)。
        改用 replace 解码即可继续遍历。
        """
        c = sqlite3.connect(path)
        c.text_factory = lambda b: b.decode("utf-8", "replace")
        return c

    def nt(self):
        if getattr(self._tls, "nt", None) is None:
            self._tls.nt = self._connect(self.nt_path)
        return self._tls.nt

    def gi(self):
        if getattr(self._tls, "gi", None) is None:
            self._tls.gi = self._connect(os.path.join(self.dir, "group_info.db"))
        return self._tls.gi

    def fts(self):
        if getattr(self._tls, "fts", None) is None:
            p = os.path.join(self.dir, "group_msg_fts.db")
            self._tls.fts = self._connect(p) if os.path.isfile(p) else None
        return self._tls.fts

    def db_sig(self):
        st = os.stat(self.nt_path)
        return f"{st.st_size}_{int(st.st_mtime)}"

    # ── 群名 ──────────────────────────────────────────────────────────────
    def group_names(self):
        """{群号: 群名}"""
        try:
            rows = self.gi().execute(
                'SELECT "60001", "60007" FROM group_list').fetchall()
            return {str(a): (b or "") for a, b in rows if a}
        except Exception:
            return {}

    # ── 通用缓存 ──────────────────────────────────────────────────────────
    def _cache_db(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        db = sqlite3.connect(os.path.join(CACHE_DIR, "cache.db"))
        db.execute(
            "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        return db

    def cached(self, key, builder):
        """带源库 mtime 签名的 JSON 缓存 (含进程内记忆化)。

        记忆化很重要: 成员映射是 9 万条的 dict, 若每次都从磁盘 JSON 反序列化,
        逐用户取名(如分析时给 500 个成员解析昵称)会白白重复解析数百次。
        """
        k = f"{self.account}:{self.db_sig()}:{key}"
        hit = _MEMO.get(k)
        if hit is not None:
            return hit
        db = self._cache_db()
        row = db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        if row:
            db.close()
            val = json.loads(row[0])
            _MEMO[k] = val
            return val
        val = builder()
        db.execute(
            "INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)",
            (k, json.dumps(val, ensure_ascii=False)))
        # 清理旧签名缓存
        db.execute("DELETE FROM kv WHERE k LIKE ? AND k != ?",
                   (f"{self.account}:%:{key}", k))
        db.commit()
        db.close()
        _MEMO[k] = val
        return val

    # ── 群列表 ────────────────────────────────────────────────────────────
    def groups_overview(self):
        def build():
            cur = self.nt().cursor()
            rows = cur.execute(
                'SELECT "40021", COUNT(*), MAX("40050") '
                'FROM group_msg_table GROUP BY "40021"').fetchall()
            names = self.group_names()
            out = []
            for gid, cnt, last_ts in rows:
                gid = str(gid)
                out.append({
                    "id": gid,
                    "name": names.get(gid, ""),
                    "count": cnt,
                    "last_ts": last_ts or 0,
                })
            out.sort(key=lambda x: -x["last_ts"])
            return out
        return self.cached("group_stats", build)

    # ── 坏页容错: rowid 降序扫描 ──────────────────────────────────────────
    # 解密库存在零星坏页: 走索引/全表排序的查询会报 malformed, 且个别垃圾
    # rowid(1e18级)。rowid 仅导航是安全的; 逐批读取, 坏批降级为逐行容错。
    def _scan_desc(self, table, cols_sql, pred, need,
                   after_ts=None, before_ts=None):
        nt = self.nt()
        out = []
        try:
            row = nt.execute(
                f"SELECT rowid FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
            last = row[0] if row else 0
        except sqlite3.DatabaseError:
            return out
        BATCH = 500
        while last > 0 and len(out) < need:
            batch = None
            try:
                batch = nt.execute(
                    f"SELECT rowid, {cols_sql} FROM {table} "
                    f"WHERE rowid<=? ORDER BY rowid DESC LIMIT {BATCH}",
                    (last,)).fetchall()
            except sqlite3.DatabaseError:
                # 坏批: 仅导航 rowid(不解析记录), 再逐行取, 坏行跳过
                try:
                    rids = [r[0] for r in nt.execute(
                        f"SELECT rowid FROM {table} WHERE rowid<=? "
                        f"ORDER BY rowid DESC LIMIT {BATCH}", (last,))]
                except sqlite3.DatabaseError:
                    break
                batch = []
                for rid in rids:
                    try:
                        batch.append(nt.execute(
                            f"SELECT rowid, {cols_sql} FROM {table} "
                            f"WHERE rowid=?", (rid,)).fetchone())
                    except sqlite3.DatabaseError:
                        pass
                batch = [b for b in batch if b]
            if not batch:
                break
            for r in batch:
                ts = r[-1]
                if before_ts and ts >= before_ts:
                    continue
                if after_ts and ts < after_ts:
                    continue
                if pred(r):
                    out.append(r)
            if after_ts and batch[-1][-1] < after_ts:
                break  # 之后只会更旧
            last = batch[-1][0] - 1
        out.sort(key=lambda r: -(r[-1] or 0))
        return out

    # ── 消息读取 ──────────────────────────────────────────────────────────
    def _fts_text(self, seq):
        f = self.fts()
        if f is None:
            return None
        try:
            row = f.execute(
                'SELECT [41701] FROM group_msg_fts WHERE [40001]=?',
                (seq,)).fetchone()
            t = row[0] if row and row[0] else None
            if t:
                t = re.sub(r"\s*\bnt_\d+\b", "", t).strip()
            return t
        except sqlite3.DatabaseError:
            return None

    def _fts_texts(self, seqs):
        """批量取正文: {seq: text}。

        逐条 _fts_text 是 N+1(整群 7.9 万条 = 7.9 万次查询, 实测 ~1.7s);
        改成按 400 个一批的 IN 查询。FTS 表若遇坏页整批失败, 回退逐条。
        """
        f = self.fts()
        if f is None or not seqs:
            return {}
        out, CH = {}, 400
        for i in range(0, len(seqs), CH):
            chunk = [str(s) for s in seqs[i:i + CH]]
            ph = ",".join("?" * len(chunk))
            try:
                rows = f.execute(
                    f'SELECT [40001],[41701] FROM group_msg_fts '
                    f'WHERE [40001] IN ({ph})', chunk).fetchall()
            except sqlite3.DatabaseError:
                for s in chunk:                 # 坏页回退逐条
                    t = self._fts_text(s)
                    if t:
                        out[s] = t
                continue
            for s, t in rows:
                if t:
                    out[str(s)] = re.sub(r"\s*\bnt_\d+\b", "", t).strip()
        return out

    def _page_by_seq(self, gid, limit, offset=0, before_ts=None, after_ts=None,
                     before_seq=None, cursor=False):
        """用 QQ 自带索引 group_msg_table_idx40027_40003_40002 按消息序号倒序分页。

        为什么必须这样做: 该库的 rowid 与时间**不同序** —— QQ 回填的历史消息
        rowid 反而更大(如 7.766e18), 而正常收发的消息 rowid 较小(7.692e18)。
        原先按 rowid 降序扫到 limit 条就停, 会把「当天最新的消息」整批漏掉、
        却返回一堆很旧的回填行(实测漏 58/60)。

        40027=本地会话ID(群号), 40003=消息序号(单调递增≈时间), 命中索引后
        0.01s 即可拿到正确的最近 N 条。

        **游标模式**(cursor=True): 按 (40003, 40002) 复合游标翻页, 每页只取
        limit 行并返回下一游标。整群导出必须用它 —— 递增 offset 分页时每翻一页
        都要从最新重扫一遍, 复杂度 O(n²)(实测 7.9 万条群仅分页就 ~90s)。
        游标必须是**复合**的: 索引三元组 (40027,40003,40002) 才唯一, 单用
        40003 会把同一序号的兄弟行整批漏掉(实测漏 3 条)。

        返回 (rows, next_cursor): next_cursor 为 None 表示已到最旧;
        非游标模式下恒为 None。索引不可用返回 None 由调用方回退。
        """
        try:
            key = int(gid)
        except (TypeError, ValueError):
            return None
        nt = self.nt()
        cursor_mode = bool(cursor)
        cols = 'rowid,"40003","40002"'
        if cursor_mode and before_seq is not None:
            s3, s2 = before_seq
            sql = (f'SELECT {cols} FROM group_msg_table WHERE "40027"=? '
                   'AND ("40003"<? OR ("40003"=? AND "40002"<?)) '
                   'ORDER BY "40003" DESC,"40002" DESC LIMIT ?')
            args = (key, s3, s3, s2, limit)
        else:
            cap = limit
            if not cursor_mode:
                cap = limit + offset
                if before_ts or after_ts:
                    cap = max(cap, 1500)  # 需按时间过滤, 多取一些
            sql = (f'SELECT {cols} FROM group_msg_table '
                   'WHERE "40027"=? ORDER BY "40003" DESC,"40002" DESC LIMIT ?')
            args = (key, cap)
        try:
            rids = nt.execute(sql, args).fetchall()
        except sqlite3.DatabaseError:
            return None
        if not rids:
            return [], None
        out, last_cur, stopped = [], None, False
        for rid, s03, s02 in rids:
            last_cur = (s03, s02)
            r = None
            try:
                r = nt.execute(
                    'SELECT rowid,"40001","40033","40090","40021","40050" '
                    'FROM group_msg_table WHERE rowid=?', (rid,)).fetchone()
            except sqlite3.DatabaseError:
                r = None
            if r is None:
                # 40033(发送者QQ) 坏页极多, 失败时改读 40020(发送者 NT UID),
                # 稍后由成员映射表换成 QQ 号
                try:
                    r = nt.execute(
                        'SELECT rowid,"40001","40020","40090","40021","40050" '
                        'FROM group_msg_table WHERE rowid=?', (rid,)).fetchone()
                except sqlite3.DatabaseError:
                    continue              # 整行不可读: 跳过
            if not r:
                continue
            ts = r[5] or 0
            if after_ts and ts and ts < after_ts:
                stopped = True            # 已越过时间下界(顺序≈时间)
                break
            if before_ts and ts and ts >= before_ts:
                continue
            out.append(r)
            if not cursor_mode and len(out) >= limit + offset:
                break
        if cursor_mode:
            # 索引查询取满 limit 才可能有更旧数据; 未取满/撞到时间下界/序号缺失即到底
            nxt = (None if (stopped or len(rids) < limit
                            or last_cur is None or last_cur[0] is None)
                   else last_cur)
            return out, nxt
        return out[offset:offset + limit], None

    def read_group(self, gid, limit=30, before_ts=None, after_ts=None,
                   offset=0, with_text=True):
        """按时间倒序读取群消息。

        优先走 40027/40003 索引(正确且极快), 索引不可用时回退到
        rowid 降序扫描(容错坏页, 但顺序可能不准)。
        """
        gid = str(gid)
        res = self._page_by_seq(gid, limit, offset, before_ts, after_ts)
        if res is None or (not res[0] and offset == 0):
            all_rows = self._scan_desc(
                "group_msg_table", '"40001", "40033", "40090", "40021", "40050"',
                lambda r: str(r[4]) == gid,
                need=limit + offset, after_ts=after_ts, before_ts=before_ts)
            page = all_rows[offset:offset + limit]
        else:
            page = res[0]
        return self._rows_to_msgs(gid, page, with_text)

    def _rows_to_msgs(self, gid, page, with_text=True):
        """原始行 -> 消息 dict (正文清洗 / 媒体标签 / 系统消息归类)。

        独立成方法是为了让 _read_all 的**游标分页**能复用同一套渲染逻辑 ——
        否则「整群遍历」与「翻页浏览」会各写一份清洗代码, 迟早各自漂移。
        """
        nt = self.nt()
        uid2uin = self.member_maps()["uid2uin"] if page else {}
        # 正文批量取(逐条查是 N+1, 整群 7.9 万条要 7.9 万次)
        fts = self._fts_texts([p[1] for p in page]) if with_text else {}
        msgs = []
        for rid, seq, uin, nick, _g, ts in page:
            # 索引路径拿到的是 40020(发送者 UID), 换成 QQ 号; 旧路径已是 QQ 号
            if isinstance(uin, str) and uin.startswith("u_"):
                uin = uid2uin.get(uin, "")
            blob = self._blob_of("group_msg_table", rid)
            text = (fts.get(str(seq)) or "") if with_text else ""
            from_fts = bool(text)
            system = is_gray_blob(blob)      # 撤回/戳一戳/入群等系统灰条
            media = []
            # 纯文本消息(t=1 占 ~93%)不必跑昂贵的 protobuf 解码 —— has_rich
            # 只做 tag 字节查找, 命中富元素才解码, 实测省掉 ~70% 的解码
            if blob and has_rich(blob):
                media = self.extract_media(blob)
            # 自带内容的元素(图/视频/语音/文件/卡片/Markdown/按钮) —— 表情/引用不算
            real = [m for m in media if m["kind"] in
                    ("pic", "video", "ptt", "file", "card", "md", "btns", "tip")]
            if with_text and not text:
                if real:
                    # 媒体消息: blob 盲解析会把 45802/45816 之类的下载直链与
                    # CDN 域名碎片全捞出来(用户看到的一长串), 直接用友好标签代替
                    tags = {"pic": "[图片]", "video": "[视频]", "ptt": "[语音]",
                            "card": "", "md": "", "btns": "", "tip": ""}
                    text = " ".join(p for p in (
                        tags.get(m["kind"], f"[文件] {m['name']}") for m in real) if p)
                else:
                    try:
                        text = text_from_blob(blob)
                    except sqlite3.DatabaseError:
                        text = "<读取失败>"
            text = re.sub(r"\s*\b(?:nt|jp_str|nick_str|uin_str)_?\d*\b", "", text or "")
            if "nudgeMall" in text or "zb.vip.qq.com" in text:
                # 戳一戳灰条(fts 路径): 去商城链接与占位残留 0
                text = re.sub(r"https?://\S+", "", text)
                text = re.sub(r"(?<![\d.])0(?![\d.])", "", text)
            if "/download?" in text or "fileid=" in text:
                # 媒体卡片泄漏的下载直链(FTS 也可能索引到)
                text = re.sub(r"\S*/download\?\S*", " ", text)
                text = re.sub(r"\b(?:appid|fileid|spec)=[^\s&]*", " ", text)
            if re.search(r'\{"(?:app|meta|prompt|appID|config)"\s*:', text) \
                    or "serviceID=" in text or "<msg " in text:
                # FTS/盲解析把卡片原始 JSON/XML 当正文索引了
                text = ""
            text = re.sub(r"\s{2,}", " ", text).strip()
            cards = [m for m in media if m["kind"] in ("card", "md")]
            if cards:
                # 卡片/Markdown 自带完整信息, 正文里的"[分享]xxx"之类提示语是重复的
                ctitle = (cards[0].get("title") or cards[0].get("name") or "").strip()
                t = (text or "").strip()
                if (not t or t.startswith("[") or t == "0"
                        or (ctitle and (t in ctitle or ctitle in t))):
                    text = ""
            if not real and not from_fts:
                # 纯表情/纯引用消息: 正文交给前端渲染, 不重复显示兜底文本
                if any(m["kind"] in ("emoji", "face", "marketface", "quote")
                       for m in media):
                    text = ""
            else:
                # 有真实文本(FTS)时, 去掉与表情重复的名字(如 "[汪汪]")
                for m in media:
                    if m["kind"] in ("emoji", "face", "marketface") and m.get("name"):
                        text = (text or "").replace(f"[{m['name']}]", " ").strip()
            if with_text and not text and not media:
                # 区分: blob 为 NULL 的是 QQNT 空壳行(内容在云端未同步到本地),
                # 有 blob 但无内容的是真·非文本(互动消息等)
                text = ("[内容未同步到本地]"
                        if blob is None else "[非文本消息]")
            msgs.append({
                # rowid/seq 是 1e18 级雪花数, 超出 JS Number 安全范围,
                # 必须以字符串返回, 否则前端 JSON.parse 精度丢失导致去重误判
                "rowid": str(rid), "seq": str(seq), "gid": gid,
                "uin": uin, "nick": nick or "", "ts": ts, "text": text or "",
                "media": media, "system": system,
            })
        return msgs

    def read_c2c(self, peer_uin, limit=30, before_ts=None, offset=0):
        """读取与某好友的私聊 (对方QQ号)"""
        row = self.nt().execute(
            'SELECT "48902" FROM nt_uid_mapping_table WHERE "1002"=?',
            (str(peer_uin),)).fetchone()
        if not row or not row[0]:
            return []
        uid = row[0]
        all_rows = self._scan_desc(
            "c2c_msg_table", '"40001", "40033", "40021", "40050"',
            lambda r: True,
            need=limit + offset, before_ts=before_ts)
        page = all_rows[offset:offset + limit]
        nt = self.nt()
        msgs = []
        for rid, seq, uin, ts in page:
            text = ""
            try:
                blob = nt.execute(
                    'SELECT "40800" FROM c2c_msg_table WHERE rowid=?',
                    (rid,)).fetchone()
                text = text_from_blob(blob[0] if blob else None)
            except sqlite3.DatabaseError:
                text = "<读取失败>"
            msgs.append({
                "rowid": str(rid), "seq": str(seq), "uin": uin,
                "peer": peer_uin, "ts": ts, "text": text or "",
            })
        return msgs

    # ── 成员映射 ──────────────────────────────────────────────────────────
    def pi(self):
        if getattr(self._tls, "pi", None) is None:
            p = os.path.join(self.dir, "profile_info.db")
            self._tls.pi = sqlite3.connect(p) if os.path.isfile(p) else None
        return self._tls.pi

    # ── 媒体(图片/视频/语音/文件) ────────────────────────────────────────
    def media_index(self):
        """nt_data 媒体文件索引 (进程内缓存一次)。

        返回 {"by_name": {小写文件名: 路径}, "by_stem": {小写词干: (路径,体积)}}。
        by_stem 供「消息里写 .jpg 但本地只剩 .png」这类跨扩展名兜底; 必须预先
        算好 —— 早期实现让 _resolve_media 每次遍历整个索引做 splitext, 解析
        800 条媒体就产生 **278 万次** splitext 调用(占总耗时 80%)。
        """
        cached = getattr(self._tls, "media_index", None)
        if cached is not None:
            return cached
        by_name, by_stem = {}, {}
        # 用 os.scandir 而不是 os.walk + os.path.getsize:
        # Windows 的 DirEntry 已缓存 size/类型(来自 FindFirstFile), e.stat()
        # **不再发独立 syscall**。实测 8664 个文件下 os.walk+getsize 要 1.76s
        # (其中 1.1s 是 8664 次 nt.stat), scandir 版降到 ~0.3s —— 每次冷启动
        # 的 `read` 都要付这笔钱。
        stack = [os.path.join(self.media_dir, s)
                 for s in ("Pic", "Video", "Ptt", "Emoji")]
        while stack:
            root = stack.pop()
            try:
                entries = list(os.scandir(root))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                low = e.name.lower()
                by_name.setdefault(low, e.path)
                stem = os.path.splitext(low)[0]
                try:
                    sz = e.stat(follow_symlinks=False).st_size
                except OSError:
                    sz = 0
                cur = by_stem.get(stem)
                if cur is None or sz > cur[1]:
                    by_stem[stem] = (e.path, sz)
        out = {"by_name": by_name, "by_stem": by_stem}
        self._tls.media_index = out
        return out

    def _resolve_media(self, name):
        """按 45402 文件名在媒体索引中定位。
        返回 (路径, 是否精确匹配); 未命中返回 (None, False)

        同名常有多个清晰度变体(_0 小图 / _198 / _720 高清), 原图(Ori)存在
        时用原图; 否则按 720 > 198 > 0 取最清晰的。
        """
        idx = self.media_index()
        by_name, by_stem = idx["by_name"], idx["by_stem"]
        n = name.lower()
        if n in by_name:
            return by_name[n], True
        stem, ext = os.path.splitext(n)
        for suf in ("", "_720", "_198", "_0"):
            cand = stem + suf + ext
            if cand in by_name:
                return by_name[cand], True
        # 扩展名不同(如原图已删只剩 Thumb): 用预建词干索引兜底, O(1)
        for suf in ("", "_720", "_198", "_0"):
            hit = by_stem.get(stem + suf)
            if hit:
                return hit[0], False
        return None, False

    def base_face_names(self):
        """经典小黄脸 ID→名字。

        本地 emoji.db.base_sys_emoji_table(约 306 个, 随 QQ 版本更新)优先,
        FACE_NAMES 静态表补缺 —— emoji.db 缺失/记录不全时仍能出名字,
        而不是退化成「[表情: 123]」这种对分析毫无信息量的占位。
        """
        cached = getattr(self, "_faces", None)
        if cached is not None:
            return cached
        m = dict(FACE_NAMES)
        p = os.path.join(self.dir, "emoji.db")
        if os.path.isfile(p):
            try:
                con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                for rid, nm in con.execute(
                        "SELECT [81211],[81212] FROM base_sys_emoji_table"):
                    if rid is None or nm is None:
                        continue
                    try:
                        m[int(rid)] = str(nm).lstrip("/")
                    except (TypeError, ValueError):
                        pass
                con.close()
            except sqlite3.DatabaseError:
                pass
        self._faces = m
        return m

    # ── 本地表情资源 (真图, 无需联网) ─────────────────────────────────────
    #  - 经典小黄脸: Emoji/BaseEmojiSyastems/EmojiSystermResource/<ID>/{png,apng}/<ID>.png
    #  - 商城表情:   Emoji/marketface/<80810>/<md5>(_aio).png
    #  - 收到表情:   Emoji/emoji-recv/<YYYY-MM>/Ori/<md5>.{png,jpg}
    def emoji_dirs(self):
        """(经典脸 ID->路径, 商城表情 ID->路径) 进程内缓存一次"""
        cached = getattr(self, "_emojidirs", None)
        if cached is not None:
            return cached
        faces, mfs = {}, {}
        base = os.path.join(self.media_dir, "Emoji")

        def rel(p):
            return os.path.relpath(p, self.media_dir).replace("\\", "/")

        sr = os.path.join(base, "BaseEmojiSyastems", "EmojiSystermResource")
        try:
            for d in os.listdir(sr):
                if not d.isdigit():
                    continue
                for sub in ("png", "apng"):
                    p = os.path.join(sr, d, sub, d + ".png")
                    if os.path.isfile(p):
                        faces[int(d)] = rel(p)
                        break
        except OSError:
            pass

        mfdir = os.path.join(base, "marketface")
        try:
            for d in os.listdir(mfdir):
                if not d.isdigit():
                    continue
                dp = os.path.join(mfdir, d)
                if not os.path.isdir(dp):
                    continue
                best = None
                try:
                    files = sorted(os.listdir(dp))
                except OSError:
                    continue
                for fn in files:
                    fp = os.path.join(dp, fn)
                    if not os.path.isfile(fp):
                        continue
                    low = fn.lower()
                    if low.endswith("_aio.png"):
                        best = fp
                        break
                    if best is None and low.endswith(
                            (".png", ".gif", ".jpg", ".jpeg", ".webp")):
                        best = fp
                if best:
                    mfs[int(d)] = rel(best)
        except OSError:
            pass
        self._emojidirs = (faces, mfs)
        return self._emojidirs

    @staticmethod
    def _txt(v):
        if isinstance(v, bytes):
            v = v.decode("utf-8", "ignore")
        return v.strip() if isinstance(v, str) else ""

    def _emoji_item(self, el, t):
        """表情元素 -> 渲染项。优先用本地真图, 否则名字交给前端映射"""
        faces, mfs = self.emoji_dirs()

        def nm_of(key):
            # QQ 里名字可能是 "[汪汪]" / "/汪汪" / "[/汪汪]" 几种写法
            s = self._txt(el.get(key)).strip()
            return s.strip("[]").lstrip("/").strip()

        if t == 11:
            # 商城/超级表情: 80810=表情ID, 80900=名字, 80903/80824=哈希
            mid = el.get("80810")
            nm = nm_of("80900")
            item = {"kind": "marketface", "id": mid, "name": nm,
                    "size": 0, "w": 0, "h": 0}
            web = mfs.get(mid) if isinstance(mid, int) else None
            if web:
                item["web"] = web
            else:
                # 本地未缓存该表情图片(目录为空): 用 80903(16字节md5) 走 CDN 兜底
                # URL: i.gtimg.cn/club/item/parcel/item/{md5[:2]}/{md5}/{s}x{s}.png
                h = el.get("80903")
                if isinstance(h, (bytes, bytearray)) and len(h) == 16:
                    item["md5"] = bytes(h).hex()
            return item
        # t == 6: 47601=ID, 47602=名字(如 "[汪汪]")
        fid = el.get("47601")
        nm = nm_of("47602")
        web = faces.get(fid) if isinstance(fid, int) else None
        if web:
            return {"kind": "face", "id": fid, "name": nm, "web": web,
                    "size": 0, "w": 0, "h": 0}
        if not nm:
            nm = self.base_face_names().get(fid) or ""
        return {"kind": "emoji", "id": fid, "name": nm,
                "size": 0, "w": 0, "h": 0}

    # ── 卡片消息 (45002=10 小程序/分享, 16 XML 卡片) ───────────────────────────
    # QQ 用 `app` 标识区分卡片来源, meta 里那层子对象的键名各不相同(见下表);
    # 字段名本身跨类型高度趋同(title/desc/preview/jumpUrl...), 故用"嗅探+特例":
    # 先按 app 定位子对象, 再按候选键名取值, 未知 app 也能兜底出卡片。
    _APP_META = {
        "com.tencent.miniapp_01": "detail_1",
        "com.tencent.miniapp.lua": "miniapp",
        "com.tencent.tuwen.lua": "news",
        "com.tencent.structmsg": "news",
        "com.tencent.music.lua": "music",
        "com.tencent.contact.lua": "contact",
        "com.tencent.troopsharecard": "contact",
        "com.tencent.map": "Location.Search",
        "com.tencent.announce.lua": "announce",
        "com.tencent.forum": "detail",
        "com.tencent.mannounce": "mannounce",
    }
    # meta 子对象键名 -> 卡片种类(决定图标与文案)
    _META_KIND = {
        "detail_1": "miniapp", "miniapp": "miniapp", "news": "news",
        "music": "music", "contact": "contact", "Location.Search": "location",
        "announce": "announce", "mannounce": "announce", "detail": "forum",
    }
    _APP_TAG = {
        "com.tencent.miniapp_01": "QQ小程序",
        "com.tencent.miniapp.lua": "微信小程序",
        "com.tencent.tuwen.lua": "分享",
        "com.tencent.structmsg": "分享",
        "com.tencent.music.lua": "音乐",
        "com.tencent.contact.lua": "群名片",
        "com.tencent.troopsharecard": "推荐群聊",
        "com.tencent.map": "位置",
        "com.tencent.announce.lua": "群公告",
        "com.tencent.mannounce": "群公告",
        "com.tencent.forum": "频道帖子",
    }
    _XML_TAG = {"35": "聊天记录", "": "卡片"}
    _T_KEYS = ("title", "nickname", "name", "channel_name", "text", "contact")
    _D_KEYS = ("desc", "summary", "address", "contact", "source", "text")
    _P_KEYS = ("preview", "cover", "thumb", "sourcelogo", "avatar")
    _I_KEYS = ("tagIcon", "icon", "source_icon", "sourcelogo", "avatar")
    _U_KEYS = ("jumpUrl", "jump_url", "qqdocurl", "url", "jumpURL", "pcJumpUrl")

    @staticmethod
    def _str(v):
        """bytes/str -> 干净字符串 (兼容 blackboxprotobuf 返回 bytes 的情况)"""
        if isinstance(v, (bytes, bytearray)):
            try:
                return bytes(v).decode("utf-8", "ignore").strip()
            except Exception:
                return ""
        return v.strip() if isinstance(v, str) else ""

    @classmethod
    def _pick(cls, d, keys, limit=200):
        """在 dict(含其一层子 dict) 中按候选键名取第一个非空值"""
        for src in [d] + [v for v in d.values() if isinstance(v, dict)]:
            for k in keys:
                s = cls._str(src.get(k))
                if s:
                    return s[:limit]
        return ""

    @staticmethod
    def _url(u):
        """规整卡片跳转地址: 补协议 / 过滤不可跳转的自定义 scheme"""
        u = (u or "").strip().replace("\\/", "/")
        if not u:
            return ""
        if u.startswith("//"):
            u = "https:" + u
        elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u):
            # QQ 里 "m.q.qq.com/a/s/xxx" 这类无协议地址
            if re.match(r"^[\w.-]+\.[a-z]{2,}(?:[:/]|$)", u):
                u = "https://" + u
            else:
                return ""          # mqqapi:// 之类只能在 QQ 客户端内打开
        return u if u.startswith(("http://", "https://")) else ""

    def _card(self, kind, title, desc, tag, thumb, icon, url):
        return {"kind": "card", "card": kind, "name": title or "",
                "title": title or "", "desc": desc or "", "tag": tag or "",
                "thumb": thumb or "", "icon": icon or "", "url": url or "",
                "size": 0, "w": 0, "h": 0}

    def _app_card(self, el):
        """45002=10: 47901 是 JSON 字符串。覆盖小程序/图文/音乐/名片/位置/群公告"""
        o = None
        raw = el.get("47901")
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", "ignore")
        if isinstance(raw, str) and raw.lstrip().startswith("{"):
            try:
                o = json.loads(raw)
            except Exception:
                o = None
        if not isinstance(o, dict):
            return None
        app = self._str(o.get("app"))
        meta = o.get("meta")
        sub, mk = None, ""
        if isinstance(meta, dict):
            mk = self._APP_META.get(app, "")
            if mk and isinstance(meta.get(mk), dict):
                sub = meta[mk]
            else:
                for k, v in meta.items():
                    if isinstance(v, dict):
                        sub, mk = v, k
                        break
        if not isinstance(sub, dict):
            return None
        if mk == "mannounce":
            # 群公告的 title/text 是 base64 编码的 UTF-8
            for k in ("title", "text"):
                v = sub.get(k)
                if isinstance(v, str) and v:
                    try:
                        pad = "=" * (-len(v) % 4)
                        sub[k] = base64.b64decode(v + pad).decode("utf-8", "ignore")
                    except Exception:
                        pass
        kind = self._META_KIND.get(mk, "news")
        # 频道帖子分支要用它做兜底标签, 必须先有初值(否则 UnboundLocalError)
        tag = self._APP_TAG.get(app, "卡片")
        title = self._pick(sub, self._T_KEYS)
        desc = self._pick(sub, self._D_KEYS)
        thumb = self._pick(sub, self._P_KEYS, 600)
        icon = self._pick(sub, self._I_KEYS, 600)
        url = self._url(self._pick(sub, self._U_KEYS, 800))
        if kind == "location":
            # 位置卡片: 生成高德网页地图跳转
            lat, lng = self._pick(sub, ("lat",)), self._pick(sub, ("lng",))
            if lat and lng and not url:
                from urllib.parse import quote
                url = ("https://uri.amap.com/marker?position=%s,%s&name=%s"
                       % (lng, lat, quote(title or "位置")))
            icon = icon or "https://webapi.amap.com/theme/v1.3/logo_small.png"
        if kind == "forum":
            # 频道帖子: 正文在 detail.feed 里(富文本 block 结构), 逐层捞出纯文本
            def _txts(node):
                got = []
                if isinstance(node, list):
                    for c in node:
                        got += _txts(c)
                elif isinstance(node, dict):
                    if "contents" in node:
                        got += _txts(node["contents"])
                    tc = node.get("text_content")
                    if isinstance(tc, dict):
                        s2 = self._str(tc.get("text"))
                        if s2:
                            got.append(s2)
                return got

            feed = sub.get("feed") if isinstance(sub.get("feed"), dict) else {}
            ft = " ".join(_txts(feed.get("title"))).strip()
            fb = re.sub(r"\s{2,}", " ",
                        " ".join(_txts(feed.get("contents")))).strip()
            if ft:
                title, desc = ft[:80], (fb[:240] or desc)
            elif fb:
                title, desc = fb[:60], fb[60:300]
            else:
                title = (re.sub(r"^\[[^\]]*\]\s*", "", self._str(o.get("prompt")))
                         or title)
            ci = sub.get("channel_info") if isinstance(sub.get("channel_info"), dict) else {}
            tag = " · ".join(x for x in (self._str(ci.get("guild_name")),
                                         self._str(ci.get("channel_name"))) if x) or tag
        if title.count("\n") and not desc:
            # 群公告把副标题塞在 title 里(换行分隔)
            head, _, tail = title.partition("\n")
            title, desc = head.strip(), tail.strip()
        if not title:
            title = self._str(o.get("prompt")) or ""
        if not title and not desc and not thumb:
            return None
        return self._card(kind, title, desc,
                          self._pick(sub, ("tag",)) or self._APP_TAG.get(app, "卡片"),
                          thumb, icon, url)

    def _xml_card(self, el):
        """45002=16: 48602 是 XML 字符串(可能是 gzip/zlib 压缩)"""
        raw = el.get("48602")
        s = self._str(raw)
        if not s.startswith("<"):
            data = raw if isinstance(raw, (bytes, bytearray)) else s.encode()
            for wbits in (15 + 32, -15):
                try:
                    s = zlib.decompress(data, wbits).decode("utf-8", "ignore")
                    if "<" in s:
                        break
                except Exception:
                    s = ""
        if "<" not in s:
            return None
        sid_m = re.search(r'serviceID="(\d+)"', s)
        sid = sid_m.group(1) if sid_m else ""
        brief = re.search(r'brief="([^"]*)"', s)
        titles = [self._unesc(t) for t in
                  re.findall(r"<title[^>]*>(.*?)</title>", s, re.S)]
        titles = [t for t in titles if t]
        summ = [self._unesc(t) for t in
                re.findall(r"<summary[^>]*>(.*?)</summary>", s, re.S)]
        src = re.search(r'<source[^>]*name="([^"]*)"', s)
        cnt = re.search(r'tSum="(\d+)"', s)
        title = (titles[0] if titles else
                 (self._unesc(brief.group(1)) if brief else "")).strip()
        desc = " ".join(summ).strip() or " ".join(titles[1:]).strip()
        if cnt and cnt.group(1) not in ("", "0", "1") and "条" not in desc:
            desc = ("共 %s 条消息 · " % cnt.group(1)) + desc
        tag = self._XML_TAG.get(sid, "卡片")
        if not title:
            title = tag
        return self._card("record" if sid == "35" else "xml", title, desc, tag, "", "", "")

    @staticmethod
    def _unesc(s):
        s = re.sub(r"<[^>]+>", " ", s or "")
        return (s.replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&apos;", "'")
                .replace("&amp;", "&").replace("&#39;", "'")).strip()

    def _md_item(self, el):
        """45002=14: 48701 是机器人 Markdown 正文(含 ![](图) 语法)"""
        s = self._str(el.get("48701"))
        if not s:
            return None
        s = re.sub(r"^\[\]\(%7B[^)]*\)\s*", "", s)   # 去掉 QQ 的 v2 版本前缀
        return {"kind": "md", "name": "", "md": s[:20000],
                "size": 0, "w": 0, "h": 0}

    def _btn_item(self, el):
        """45002=17: 48751 -> 行 -> 48753 按钮(48755 文案, 48761 跳转)"""
        rows = el.get("48751")
        if isinstance(rows, dict):
            rows = [rows]
        out = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            cells = r.get("48753")
            if isinstance(cells, dict):
                cells = [cells]
            row = []
            for c in cells if isinstance(cells, list) else []:
                if not isinstance(c, dict):
                    continue
                label = self._str(c.get("48755")) or self._str(c.get("48756"))
                if not label:
                    continue
                row.append({"t": label[:40],
                            "u": self._url(self._str(c.get("48761")))})
            if row:
                out.append(row)
        if not out:
            return None
        return {"kind": "btns", "name": "", "btns": out,
                "size": 0, "w": 0, "h": 0}

    def extract_media(self, blob):
        """从消息 blob 提取元素。

        45002 取值: 2 图片(45402 文件名) / 3 文件 / 4 语音 / 5 视频 /
        6 表情(47601 ID, 47602 名字) / 7 引用(47413) /
        10 卡片(47901 JSON: 小程序/分享/音乐/名片/位置/公告/频道) /
        11 商城表情(80810 ID, 80903 md5) / 14 机器人 Markdown(48701) /
        16 XML 卡片(48602) / 17 按钮组(48751) / 21 通话提示(48153)
        """
        if not blob:
            return []
        import blackboxprotobuf
        try:
            msg, _ = blackboxprotobuf.decode_message(blob)
        except Exception:
            return []
        body = msg.get("40800") if isinstance(msg, dict) else None
        elems = body if isinstance(body, list) else (
            [body] if isinstance(body, dict) else [])
        out = []
        for el in elems:
            if not isinstance(el, dict):
                continue
            t = el.get("45002")
            name = el.get("45402")
            if isinstance(name, bytes):
                name = name.decode("utf-8", "ignore")
            if not isinstance(name, str):
                # 45402 偶尔被解码成嵌套结构(非文件名), 视为无文件名
                name = None
            if not name:
                # 无文件名的富元素: 表情 / 引用 / 卡片 / 机器人 Markdown / 按钮 / 通话
                it = None
                if t in (6, 11):
                    it = self._emoji_item(el, t)
                elif t == 10:
                    it = self._app_card(el)
                elif t == 16:
                    it = self._xml_card(el)
                elif t == 14:
                    it = self._md_item(el)
                elif t == 17:
                    it = self._btn_item(el)
                elif t == 21:
                    # 语音/视频通话结束提示 (48153 / 48157 同一文案)
                    tip = self._str(el.get("48153")) or self._str(el.get("48157"))
                    if tip:
                        it = {"kind": "tip", "name": tip[:80],
                              "size": 0, "w": 0, "h": 0}
                if it is None and t in (2, 3, 4, 5):
                    # 45402 解码异常/缺失, 但**元素类型本身已经说明这是什么**。
                    # 参考 QCE: filename 只是可选字段, 缺了也要出 "图片/文件/
                    # 语音/视频" —— 否则整条消息退化成 "[非文本消息]"。
                    it = self._res_item(el, t, "")
                if it:
                    out.append(it)
                elif t == 7:
                    # 引用消息。字段(实测于真实库):
                    #   47401 = 原消息 seq (仅部分消息带)  47404 = 原消息时间(秒)
                    #   47403 = **原发送者 QQ号**(int)     47421 = 原发送者显示名(部分带)
                    #   47413 = 被引用正文(完整)           47423 = 被引用原始元素
                    # 有了 47403, 就能回群成员表还原"这条在接谁的话" ——
                    # 只给正文会丢掉群聊里最关键的对话线索。
                    q = self._txt(el.get("47413"))
                    inner = el.get("47423") if isinstance(el.get("47423"), dict) else None
                    # 47413 偶尔塞的是本地缓存路径或下载直链(引用的是图片时),
                    # 这不是正文 —— 换成内嵌元素的实际类型标签。
                    if not q or _MPATH.search(q):
                        q = ""
                        if inner:
                            q = (self._txt(inner.get("47413"))
                                 or self._txt(inner.get("45101")))
                            if not q or _MPATH.search(q):
                                q = _MEDIA_TAG.get(inner.get("45002"), "")
                    q = re.sub(r"[A-Za-z]:\\[^\s]*", " ", q)
                    q = re.sub(r"\S*(?:/download\?|fileid=)\S*", " ", q)
                    q = re.sub(r"\s{2,}", " ", q).strip()
                    if q:
                        who = self._txt(el.get("47421")).strip()
                        ruin = el.get("47403")
                        ruin = str(ruin) if isinstance(ruin, int) and ruin > 0 else (
                            ruin if isinstance(ruin, str) else "")
                        if ruin and ruin not in ("0", "None"):
                            if not who:
                                who = self.member_maps()["uin2nick"].get(ruin, "")
                            who = who or ruin
                        else:
                            ruin = ""
                        qts = el.get("47404")
                        out.append({"kind": "quote", "name": q[:300],
                                    "who": who[:40], "uin": ruin,
                                    "seq": str(el.get("47401") or ""),
                                    "ts": int(qts) if isinstance(qts, int)
                                    and qts > 1_000_000_000 else 0,
                                    "size": 0, "w": 0, "h": 0})
                continue
            out.append(self._res_item(el, t, name))
        return out

    def _res_item(self, el, t, name):
        """资源类元素(图/文件/语音/视频) -> 渲染项。

        name 允许为空: 只有类型码也是有效信息, 且尺寸/体积/本地缓存文件
        仍可读, 不该因为文件名解码失败就整条丢掉。
        """
        kind = {2: "pic", 3: "file", 4: "ptt",
                5: "video", 6: "emoji"}.get(t, "media")
        if kind == "media" and name:
            ext = os.path.splitext(name)[1].lower()
            if ext == ".mp4":
                kind = "video"
            elif ext in (".gif", ".webp"):
                kind = "emoji"
        item = {"kind": kind, "name": name or "",
                "size": el.get("45405") or 0,
                "w": el.get("45411") or 0, "h": el.get("45412") or 0}
        if name:
            p, exact = self._resolve_media(name)
            if p:
                rel = os.path.relpath(p, self.media_dir).replace("\\", "/")
                if not exact and kind == "video":
                    item["thumb"] = rel
                else:
                    item["web"] = rel
        return item

    def _blob_of(self, table, rid):
        try:
            r = self.nt().execute(
                f'SELECT "40800" FROM {table} WHERE rowid=?', (rid,)).fetchone()
            return r[0] if r else None
        except sqlite3.DatabaseError:
            return None

    def member_maps(self):
        """uid->uin / uid->昵称 / uin->昵称 (全部来自无坏页的小表)"""
        def build():
            uid2uin, uid2nick, uin2nick = {}, {}, {}
            try:
                for uid, uin in self.nt().execute(
                        'SELECT "48902", "1002" FROM nt_uid_mapping_table'):
                    if uid:
                        uid2uin[uid] = str(uin or "")
            except Exception:
                pass
            try:
                for uid, uin, nick, card in self.gi().execute(
                        'SELECT "1000", "1002", "20002", "64003" '
                        'FROM group_member3'):
                    if not uid:
                        continue
                    # 群成员表同时带 uid(1000) 与 QQ号(1002): 直接补全 uid->QQ号,
                    # 消息主表 40020 只有 uid, 靠这张表换算(nt_uid_mapping_table
                    # 只覆盖 6 千多条, 远不够)
                    if uin:
                        uid2uin.setdefault(uid, str(uin))
                    nm = card or nick
                    if card:
                        uid2nick[uid] = card
                    elif nick and uid not in uid2nick:
                        uid2nick[uid] = nick
                    # 群成员表自带 QQ号(1002): 直接建 uin->昵称 映射, 避免只靠
                    # nt_uid_mapping_table 换算导致大量成员只有号码没有昵称
                    if uin and nm:
                        uin2nick.setdefault(str(uin), nm)
            except Exception:
                pass
            try:
                for uin, nick in self.pi().execute(
                        'SELECT "1002", "20002" FROM profile_info_v2'):
                    if uin and nick:
                        uin2nick[str(uin)] = nick
            except Exception:
                pass
            for uid, nick in uid2nick.items():
                uin = uid2uin.get(uid)
                if uin and uin not in uin2nick:
                    uin2nick[uin] = nick
            return {"uid2uin": uid2uin, "uid2nick": uid2nick,
                    "uin2nick": uin2nick}
        # 键名带版本: 改过映射逻辑后强制重建, 避免命中旧结构缓存
        return self.cached("member_maps_v4", build)

    def nick_of(self, uin, nick):
        if nick and nick not in ("", "0"):
            return nick
        return self.member_maps()["uin2nick"].get(str(uin), str(uin))

    def nick_index(self, gid=None):
        """昵称(含群名片) → QQ号 反查; 重名一律**不猜**。

        报告里的「人物」是 Agent 按昵称写的, 靠这张反表自动配上真实头像。
        原来的实现有两个坑 (实测):

          1. 来源是**全局** `uin2nick` —— 那是 profile_info_v2 覆盖过的账号级昵称,
             群名片会被压掉 (实测 `群名片甲` 的群名片被全局名 `全局昵称甲` 顶掉)。
          2. 重名时「保留先出现的那个」等于**掷骰子**。全库 71,526 个有意义的昵称里
             4,958 个跨群重名, 其中 4,847 个被它强行解析 —— 本群 1148 人中有 127 人
             落在这个坑里 (`Alex` 有 13 个候选, 本群是 <成员甲_ID>, 却被解析成
             <成员乙_ID>)。结果是「没写 QQ号的重名」会配上陌生人的脸。

        所以改成: 按群建表 (gid 给了就只看这个群), 且**只保留唯一命中的名字**;
        重名的返回空 → 调用方降级成首字头像, 比配错人强。
        """
        def build():
            rev = {}
            if gid:
                rows = self.gi().execute(
                    'SELECT "1002","20002","64003" FROM group_member3 '
                    'WHERE "60001"=?', (str(gid),))
            else:
                rows = self.gi().execute(
                    'SELECT "1002","20002","64003" FROM group_member3')
            for uin, nick, card in rows:
                if not uin:
                    continue
                nm = (card or nick or "").strip()
                if nm and not nm.isdigit():
                    rev.setdefault(nm, set()).add(str(uin))
            return {n: next(iter(us)) for n, us in rev.items() if len(us) == 1}
        return self.cached("nick_index_v2_%s" % (gid or "all"), build)

    # ── 搜索 ──────────────────────────────────────────────────────────────
    def search(self, kw, gid=None, limit=30, max_scan=1_000_000):
        """全文搜索: fts 明文表 rowid 降序安全扫描(最近优先), Python 侧匹配"""
        f = self.fts()
        if f is None:
            return []
        pat = f"%{kw}%"
        gid = str(gid) if gid else None
        kw_l = kw.lower()
        out = []
        try:
            row = f.execute(
                "SELECT rowid FROM group_msg_fts ORDER BY rowid DESC LIMIT 1").fetchone()
            last = row[0] if row else 0
        except sqlite3.DatabaseError:
            return []
        B = 2000
        scanned = 0
        while last > 0 and len(out) < limit and scanned < max_scan:
            batch = None
            try:
                batch = f.execute(
                    f"SELECT rowid, [40021], [40050], [41701] FROM group_msg_fts "
                    f"WHERE rowid<=? ORDER BY rowid DESC LIMIT {B}",
                    (last,)).fetchall()
            except sqlite3.DatabaseError:
                try:
                    rids = [r[0] for r in f.execute(
                        "SELECT rowid FROM group_msg_fts WHERE rowid<=? "
                        f"ORDER BY rowid DESC LIMIT {B}", (last,))]
                except sqlite3.DatabaseError:
                    break
                batch = []
                for rid in rids:
                    try:
                        batch.append(f.execute(
                            "SELECT rowid, [40021], [40050], [41701] "
                            "FROM group_msg_fts WHERE rowid=?",
                            (rid,)).fetchone())
                    except sqlite3.DatabaseError:
                        pass
                batch = [b for b in batch if b]
            if not batch:
                break
            for r in batch:
                scanned += 1
                if gid and str(r[1] or "") != gid:
                    continue
                text = r[3] or ""
                if kw_l in text.lower():
                    out.append((r[1], r[2], text))
            last = batch[-1][0] - 1
        out.sort(key=lambda x: -(x[1] or 0))
        names = self.group_names()
        return [{"seq": None, "gid": str(g or ""), "gname": names.get(str(g or ""), ""),
                 "ts": ts or 0, "text": text[:300]} for g, ts, text in out[:limit]]

    def _scan_all(self, table, cols_sql, sink):
        """坏页容错全表扫描: rowid 导航分批, 坏批降级逐行, 坏行跳过"""
        nt = self.nt()
        try:
            row = nt.execute(
                f"SELECT rowid FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
            last = row[0] if row else 0
        except sqlite3.DatabaseError:
            return
        B = 2000
        while last > 0:
            batch = None
            try:
                batch = nt.execute(
                    f"SELECT rowid, {cols_sql} FROM {table} "
                    f"WHERE rowid<=? ORDER BY rowid DESC LIMIT {B}",
                    (last,)).fetchall()
            except sqlite3.DatabaseError:
                try:
                    rids = [r[0] for r in nt.execute(
                        f"SELECT rowid FROM {table} WHERE rowid<=? "
                        f"ORDER BY rowid DESC LIMIT {B}", (last,))]
                except sqlite3.DatabaseError:
                    break
                batch = []
                for rid in rids:
                    try:
                        batch.append(nt.execute(
                            f"SELECT rowid, {cols_sql} FROM {table} "
                            f"WHERE rowid=?", (rid,)).fetchone())
                    except sqlite3.DatabaseError:
                        pass
                batch = [b for b in batch if b]
            if not batch:
                break
            for r in batch:
                sink(r)
            last = batch[-1][0] - 1

    # ── 统计 ──────────────────────────────────────────────────────────────
    def stats_daily(self, days=30, gid=None):
        def build():
            where, args = "", []
            if gid:
                where = 'WHERE "40021"=?'
                args = [str(gid)]
            rows = self.nt().execute(
                f'SELECT date("40050",\'unixepoch\',\'localtime\') d, COUNT(*) '
                f'FROM group_msg_table {where} GROUP BY d ORDER BY d DESC '
                f'LIMIT {days}', args).fetchall()
            return [{"d": r[0], "n": r[1]} for r in reversed(rows) if r[0]]
        return self.cached(f"daily_{gid or 'all'}_{days}", build)

    def stats_top(self, gid, n=20):
        """发言 Top N — 40033 列坏页多发, 改扫 (40021,40020) 前置列自行聚合"""
        def build():
            g = str(gid)
            counts = {}

            def sink(r):
                if str(r[1]) == g:
                    u = r[2] or "?"
                    counts[u] = counts.get(u, 0) + 1

            self._scan_all("group_msg_table", '"40021", "40020"', sink)
            mm = self.member_maps()
            top = sorted(counts.items(), key=lambda x: -x[1])[:n]
            out = []
            for uid, c in top:
                uin = mm["uid2uin"].get(uid, "")
                nick = (mm["uid2nick"].get(uid)
                        or mm["uin2nick"].get(uin, "") or uin or uid)
                out.append({"uin": uin or uid, "nick": nick, "n": c})
            return out
        return self.cached(f"top_{gid}_{n}", build)

    def stats_hours(self, gid=None):
        def build():
            where, args = "", []
            if gid:
                where = 'WHERE "40021"=?'
                args = [str(gid)]
            rows = self.nt().execute(
                f'SELECT strftime(\'%H\',"40050",\'unixepoch\',\'localtime\') h, '
                f'COUNT(*) FROM group_msg_table {where} GROUP BY h', args).fetchall()
            d = {int(r[0]): r[1] for r in rows if r[0]}
            return [d.get(i, 0) for i in range(24)]
        return self.cached(f"hours_{gid or 'all'}", build)

    # ── 导出 ──────────────────────────────────────────────────────────────
    def export(self, gid, path, fmt="json", after_ts=None, before_ts=None):
        """导出群聊天记录。

        所有格式共用同一套元素渲染(qqlib.media_label + 引用/系统行): 图片、
        视频、语音、文件、表情、卡片、引用、系统灰条都会出现在结果里 —— 参考
        shuakami/qq-chat-exporter 的 text_exporter/json_exporter 做法, 只写
        `m["text"]` 会把媒体与"在回复谁"这两层信息整片丢掉。

        fmt: json / jsonl / txt / csv / md
        输出顺序统一为**时间正序**(旧 -> 新)。
        """
        msgs = self._read_all(gid, after_ts, before_ts)
        msgs.sort(key=lambda m: (m.get("ts") or 0, str(m.get("rowid") or "")))
        gname = self.group_names().get(str(gid), str(gid))
        rows = [self._export_row(m) for m in msgs]
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        span = ""
        if rows:
            fmt_t = lambda t: datetime.datetime.fromtimestamp(t).strftime(
                "%Y-%m-%d %H:%M:%S")
            span = "%s - %s" % (fmt_t(rows[0]["timestamp"]), fmt_t(rows[-1]["timestamp"]))

        if fmt == "json":
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"group": gname, "gid": str(gid), "count": len(rows),
                           "exported_at": now, "time_range": span,
                           "messages": rows}, f, ensure_ascii=False, indent=1)
        elif fmt == "jsonl":
            with open(path, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        elif fmt == "csv":
            import csv
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["时间", "QQ号", "昵称", "类型", "内容", "引用"])
                for r in rows:
                    w.writerow([r["time"], r["sender"]["uin"], r["sender"]["name"],
                                r["kind"], r["label"],
                                r["reply"] and r["reply"]["text"] or ""])
        elif fmt == "md":
            with open(path, "w", encoding="utf-8") as f:
                f.write("# %s 聊天记录\n\n" % gname)
                f.write("> 共 %d 条 · %s · 导出时间 %s\n" % (len(rows), span, now))
                cur_day = None
                for r in rows:
                    if r["day"] != cur_day:
                        cur_day = r["day"]
                        f.write("\n## %s\n\n" % cur_day)
                    f.write("- `%s` **%s**（%s）: %s\n"
                            % (r["time"][11:], r["sender"]["name"],
                               r["sender"]["uin"], r["label"]))
                    if r["reply"]:
                        f.write("  - └ 回复 %s: %s\n"
                                % (r["reply"]["who"] or "?", r["reply"]["text"]))
        else:   # txt
            with open(path, "w", encoding="utf-8") as f:
                bar = "=" * 46
                f.write("%s\n           QQ 聊天记录导出\n%s\n\n" % (bar, bar))
                f.write("聊天名称: %s\n聊天类型: 群聊\n消息总数: %d\n"
                        % (gname, len(rows)))
                if span:
                    f.write("时间范围: %s\n" % span)
                f.write("导出时间: %s\n\n" % now)
                for i, r in enumerate(rows, 1):
                    f.write("[%d] %s:\n时间: %s\n"
                            % (i, r["sender"]["name"], r["time"]))
                    f.write("QQ号: %s\n" % r["sender"]["uin"])
                    f.write("内容: %s\n" % (r["label"] or "[空消息]"))
                    if r["reply"]:
                        f.write("回复: %s - %s\n"
                                % (r["reply"]["who"] or "?", r["reply"]["text"]))
                    f.write("\n")
                f.write("%s\n              导出完成\n%s\n"
                        % (bar, bar))
                f.write("总计导出 %d 条消息\n导出时间: %s\n" % (len(rows), now))
        return len(msgs)

    def _read_all(self, gid, after_ts=None, before_ts=None, batch=500):
        """读完整个群 (时间正序由调用方排序)。

        用 40003 **游标分页**: 每页只取 batch 行, 复杂度 O(n)。早期用递增
        offset 分页, 每翻一页都要从最新重扫 (limit+offset) 行 —— 实测 7.9 万
        条的群光分页就 ~90s (offset=60000 时单次 _page_by_seq 已要 0.86s)。
        """
        gid = str(gid)
        msgs, seen, cursor = [], set(), None
        while True:
            res = self._page_by_seq(gid, batch, 0, before_ts, after_ts,
                                    before_seq=cursor, cursor=True)
            if res is None:
                # 索引不可用 → 回退 rowid 扫描(一次性全量, 容错坏页)
                rows = self._scan_desc(
                    "group_msg_table",
                    '"40001", "40033", "40090", "40021", "40050"',
                    lambda r: str(r[4]) == gid,
                    need=10 ** 9, after_ts=after_ts, before_ts=before_ts)
                return self._rows_to_msgs(gid, rows, True)
            rows, cursor = res
            for m in self._rows_to_msgs(gid, rows, True):
                if m["rowid"] not in seen:      # 游标边界/过滤去重
                    seen.add(m["rowid"])
                    msgs.append(m)
            if cursor is None:
                break
        return msgs

    def _export_row(self, m):
        """一条消息 -> 导出记录 (含渲染后的正文标签与引用行)"""
        ts = m.get("ts") or 0
        try:
            dt = datetime.datetime.fromtimestamp(ts) if ts else None
        except (OverflowError, OSError, ValueError):
            dt = None
        media = m.get("media") or []
        quotes = [x for x in media if x.get("kind") == "quote"]
        tags = [media_label(x) for x in media if x.get("kind") != "quote"]
        text = (m.get("text") or "").strip()
        if tags:
            n, order = {}, []
            for t in tags:                      # 同名标签合并计数
                if t not in n:
                    n[t] = 0
                    order.append(t)
                n[t] += 1
            tags = [(t[:-1] + "×%d]" % n[t]) if n[t] > 1 else t for t in order]
        if tags and TAG_ONLY.match(text):
            text = ""                           # 媒体已由标签表达, 去掉重复占位
        reply = None
        if quotes:
            q = quotes[0]
            qtx = re.sub(r"\s+", " ", (q.get("name") or "")).strip()
            if qtx:
                who = (q.get("who") or "").strip()
                qts = q.get("ts") or 0
                if qts:
                    try:
                        who = (datetime.datetime.fromtimestamp(qts)
                               .strftime("%m-%d %H:%M") + " " + who).strip()
                    except (OverflowError, OSError, ValueError):
                        pass
                reply = {"who": who, "text": qtx[:200], "seq": q.get("seq") or ""}
        # 系统灰条(撤回/戳一戳/入群/邀请)与用户发言区分: 默认会被归成 kind=text,
        # 混在正常发言里没法筛也没法单独统计。标成 system 让下游一眼能分开。
        kind = (",".join(dict.fromkeys(x.get("kind") or "text"
                                       for x in media)) or "text")
        if m.get("system"):
            kind = "system"
        return {
            "id": str(m.get("rowid") or ""),
            "seq": str(m.get("seq") or ""),
            "timestamp": ts,
            "time": dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "",
            "day": dt.strftime("%Y-%m-%d") if dt else "",
            "sender": {"uin": str(m.get("uin") or ""),
                       "name": self.nick_of(m.get("uin"), m.get("nick"))},
            "kind": kind,
            "text": text,
            "label": " ".join([text] + tags).strip(),
            "media": media,
            "reply": reply,
        }
