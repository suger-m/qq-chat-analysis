"""Collect one member's group messages and bounded conversation context."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import heapq
import json
from pathlib import Path
import re
import sqlite3

import qanalysis
import qqlib


def positive(value):
    number = int(value)
    if number < 1:
        raise ValueError("必须为正整数")
    return number


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise ValueError("必须为非负整数")
    return number


def order_number(value):
    try:
        return int(value or 0)
    except (ValueError, TypeError, OverflowError):
        return 0


def time_window(args):
    """Date-only end points include that date; datetime ends are exclusive."""
    def parse(value, end=False):
        date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
        result = datetime.fromisoformat(value)
        if date_only and end:
            result += timedelta(days=1)
        return int(result.timestamp())

    try:
        if args.date:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
                raise ValueError("--date 使用 YYYY-MM-DD")
            if args.from_date or args.to_date:
                raise ValueError("--date 与 --from/--to 不能同时使用")
            after, before = parse(args.date), parse(args.date, end=True)
        elif args.days:
            if args.from_date or args.to_date:
                raise ValueError("--days 与 --from/--to 不能同时使用")
            now = datetime.now()
            after, before = int((now - timedelta(days=args.days)).timestamp()), int(now.timestamp())
        else:
            after = parse(args.from_date) if args.from_date else None
            before = parse(args.to_date, end=True) if args.to_date else None
        if after is not None and before is not None and after >= before:
            raise ValueError("起始时间必须早于结束时间")
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("时间范围无效；支持 YYYY-MM-DD 或 ISO 日期时间：" + str(exc)) from None
    return after, before


def resolve_person(store, token, gid=None):
    """Resolve exactly; aliases never merge different account IDs."""
    token = str(token).strip()
    if not token:
        raise ValueError("请指定成员 QQ 号、NT UID 或完整昵称")
    maps = store.member_maps()
    uid2uin = {str(k): str(v) for k, v in maps.get("uid2uin", {}).items() if v}
    if re.fullmatch(r"\d{5,15}", token):
        uin = token
    elif token.startswith("u_") and re.fullmatch(r"u_[A-Za-z0-9_-]+", token):
        uin = uid2uin.get(token, token)
    else:
        candidates = set()
        sql = 'SELECT "1000","1002","20002","64003" FROM group_member3'
        parameters = ()
        if gid:
            sql += ' WHERE "60001"=?'
            parameters = (str(gid),)
        try:
            rows = store.gi().execute(sql, parameters)
            for uid, number, nick, card in rows:
                canonical = str(number or uid2uin.get(str(uid)) or uid or "")
                if canonical and any(str(n or "").strip().casefold() == token.casefold()
                                     for n in (nick, card)):
                    candidates.add(canonical)
        except sqlite3.DatabaseError:
            raise ValueError("成员名册不可完整读取；请直接指定 QQ 号或 NT UID") from None
        if not gid:
            candidates.update(str(number) for number, nick in maps.get("uin2nick", {}).items()
                              if str(nick).strip().casefold() == token.casefold())
        if len(candidates) > 1:
            raise ValueError("该昵称对应多位成员；请改用 QQ 号或 NT UID，避免跨群重名混淆")
        if not candidates:
            raise ValueError("未找到该完整昵称；请使用 QQ 号、NT UID 或该群完整群名片")
        uin = next(iter(candidates))
    aliases = {uin, *(uid for uid, number in uid2uin.items() if number == uin)}
    if token.startswith("u_"):
        aliases.add(token)
    name = qanalysis.clean_nick(maps.get("uin2nick", {}).get(uin, token), uin)
    return {"uin": uin, "name": name, "aliases": aliases, "uid2uin": uid2uin}


class Reader:
    """Read indexed row IDs first; decode bodies only for selected messages."""
    def __init__(self, store, identity):
        self.store, self.identity = store, identity
        self.issues = Counter()
        self.failed_groups = set()

    def groups(self):
        result = set()
        try:
            # Covering QQ's (40027,40003,40002) index avoids whole-row aggregation.
            for (gid,) in self.store.nt().execute(
                    'SELECT DISTINCT "40027" FROM group_msg_table ORDER BY "40027"'):
                if str(gid or "").isdigit() and int(gid) > 0:
                    result.add(str(gid))
        except sqlite3.DatabaseError:
            self.issues["group_discovery_errors"] += 1
            result.update(str(gid) for gid in self.store.group_names() if str(gid).isdigit())
        return sorted(result, key=int)

    def metadata(self, gid, indexed):
        ids = [rid for rid, _, _ in indexed]
        basic = qanalysis._fetch_basic(self.store.nt(), ids)
        self.issues["unreadable_messages"] += len(set(ids) - set(basic))
        result = []
        unresolved = []
        for rid, order, fragment in indexed:
            if rid not in basic:
                continue
            msg_id, ts, _, uid = basic[rid]
            uid = str(uid or "")
            uin = self.identity["uid2uin"].get(uid, uid if uid.isdigit() else "")
            try:
                ts = int(ts or 0)
                if ts:
                    datetime.fromtimestamp(ts)
            except (ValueError, TypeError, OverflowError, OSError):
                self.issues["invalid_timestamps"] += 1
                ts = 0
            if order is None or fragment is None:
                self.issues["missing_message_order"] += 1
            item = {"rid": rid, "order": order, "fragment": fragment,
                    "seq": str(msg_id or ""), "ts": ts, "uid": uid,
                    "uin": uin, "gid": str(gid)}
            if uid in self.identity["aliases"]:
                item["uin"] = self.identity["uin"]
            elif not uin:
                unresolved.append(item)
            result.append(item)
        if unresolved:
            # 40033 is unreliable on damaged snapshots: only read it for identities
            # absent from the UID map, using the existing per-row fallback reader.
            numbers = dict(qanalysis._bulk(
                self.store.nt(), 'SELECT rowid,"40033" FROM group_msg_table WHERE rowid IN (%s)',
                [item["rid"] for item in unresolved]))
            for item in unresolved:
                number = str(numbers.get(item["rid"]) or "")
                item["uin"] = number if number.isdigit() and number != "0" else item["uid"]
                if not number.isdigit() or number == "0":
                    self.issues["unresolved_senders"] += 1
        return result

    def scan(self, gid, page_size=1000):
        cursor = None
        while True:
            sql = 'SELECT rowid,"40003","40002" FROM group_msg_table WHERE "40027"=?'
            params = [int(gid)]
            if cursor is not None:
                sql += ' AND ("40003","40002") < (?,?)'
                params.extend(cursor)
            sql += ' ORDER BY "40003" DESC,"40002" DESC LIMIT ?'
            params.append(page_size)
            try:
                indexed = self.store.nt().execute(sql, params).fetchall()
            except sqlite3.DatabaseError:
                self.failed_groups.add(str(gid))
                return
            if not indexed:
                return
            yield from self.metadata(gid, indexed)
            next_cursor = indexed[-1][1:]
            if len(indexed) < page_size:
                return
            if any(part is None for part in next_cursor) or next_cursor == cursor:
                self.failed_groups.add(str(gid))
                return
            cursor = next_cursor

    def body(self, item):
        blob = self.store._blob_of("group_msg_table", item["rid"])
        if blob is None:
            self.issues["missing_message_bodies"] += 1
        row = (item["rid"], item["seq"], item["uin"], "", item["gid"], item["ts"])
        try:
            message = self.store._rows_to_msgs(item["gid"], [row])[0]
        except (sqlite3.DatabaseError, ValueError, TypeError, IndexError, OverflowError):
            self.issues["unreadable_bodies"] += 1
            message = {"gid": item["gid"], "ts": item["ts"], "text": "[消息正文读取失败]",
                       "media": [], "system": qqlib.is_gray_blob(blob)}
        message.update({"rid": str(item["rid"]), "rowid": str(item["rid"]),
                        "uin": item["uin"], "uid": item["uid"],
                        "message_id": item["seq"], "order": str(item["order"]),
                        "fragment": str(item["fragment"])})
        return message

    def nearby(self, item, count, forward):
        if not count or item["order"] is None or item["fragment"] is None:
            return []
        op, direction = (">", "ASC") if forward else ("<", "DESC")
        sql = ('SELECT rowid,"40003","40002" FROM group_msg_table WHERE "40027"=? '
               f'AND ("40003","40002") {op} (?,?) '
               f'ORDER BY "40003" {direction},"40002" {direction} LIMIT ?')
        try:
            indexed = self.store.nt().execute(sql, (
                int(item["gid"]), item["order"], item["fragment"], count)).fetchall()
        except (sqlite3.DatabaseError, OverflowError):
            self.issues["context_read_errors"] += 1
            return []
        return self.metadata(item["gid"], indexed)

    def quoted(self, item, quote):
        seq = str(quote.get("seq") or "")
        if not seq.isdigit():
            return []
        try:
            indexed = self.store.nt().execute(
                'SELECT rowid,"40003","40002" FROM group_msg_table '
                'WHERE "40027"=? AND "40003"=? ORDER BY "40002" LIMIT 20',
                (int(item["gid"]), int(seq))).fetchall()
        except (sqlite3.DatabaseError, OverflowError):
            self.issues["context_read_errors"] += 1
            return []
        result = self.metadata(item["gid"], indexed)
        if quote.get("uin"):
            result = [row for row in result if row["uin"] == str(quote["uin"])]
        if quote.get("ts"):
            result = [row for row in result if row["ts"] == int(quote["ts"])]
        return result


def in_window(item, after, before):
    ts = item["ts"]
    if not ts:
        return after is None and before is None
    return (after is None or ts >= after) and (before is None or ts < before)


def collect(store, identity, gids=None, after=None, before=None, limit=300,
            context_before=3, context_after=3, context_minutes=15, progress=None):
    all_groups = gids is None
    reader = Reader(store, identity)
    gids = gids if gids is not None else reader.groups()
    matches, matched_count = [], 0
    for index, gid in enumerate(gids, 1):
        if progress:
            progress(f"成员消息筛选中：群 {index}/{len(gids)}")
        for item in reader.scan(gid):
            if item["uin"] != identity["uin"] or not in_window(item, after, before):
                continue
            blob = store._blob_of("group_msg_table", item["rid"])
            if qqlib.is_gray_blob(blob):
                continue
            matched_count += 1
            key = (item["ts"], order_number(item["seq"]), int(item["gid"]), item["rid"])
            if not limit:
                matches.append((key, item))
            elif len(matches) < limit:
                heapq.heappush(matches, (key, item))
            elif key > matches[0][0]:
                heapq.heapreplace(matches, (key, item))
    targets = [item for _, item in sorted(matches, key=lambda pair: pair[0])]
    target_keys = {(item["gid"], item["rid"]) for item in targets}
    messages = {}
    for item in targets:
        key = (item["gid"], item["rid"])
        messages[key] = reader.body(item)
        messages[key].update(role="target", reasons=["target"], context_for=[])

    for item in targets:
        anchor = messages[(item["gid"], item["rid"])]
        candidates = [(row, "before") for row in reader.nearby(item, context_before, False)]
        candidates.extend((row, "after") for row in reader.nearby(item, context_after, True))
        for quote in anchor.get("media") or []:
            if quote.get("kind") == "quote":
                candidates.extend((row, "quoted") for row in reader.quoted(item, quote))
        for row, reason in candidates:
            key = (row["gid"], row["rid"])
            if key in target_keys or not in_window(row, after, before):
                continue
            if reason != "quoted" and (not row["ts"] or not item["ts"]
                    or abs(row["ts"] - item["ts"]) > context_minutes * 60):
                continue
            if key not in messages:
                messages[key] = reader.body(row)
                messages[key].update(role="context", reasons=[], context_for=[])
            message = messages[key]
            if reason not in message["reasons"]:
                message["reasons"].append(reason)
            if item["seq"] not in message["context_for"]:
                message["context_for"].append(item["seq"])

    group_names = store.group_names()
    grouped = defaultdict(list)
    for message in messages.values():
        grouped[message["gid"]].append(message)
    groups = []
    for gid, items in sorted(grouped.items(), key=lambda pair: int(pair[0])):
        gmap = qanalysis.group_member_names(store, gid)
        for message in items:
            message["who"] = ("系统" if message.get("system") else
                              qanalysis.display_name(store, message["uin"], gmap))
            message["time"] = datetime.fromtimestamp(message["ts"]).isoformat(sep=" ") if message["ts"] else "未知"
        items.sort(key=lambda message: (message["ts"], order_number(message["order"]),
                                       order_number(message["fragment"]), int(message["rid"])))
        groups.append({"gid": gid, "name": group_names.get(gid, gid),
                       "target_count": sum(message["role"] == "target" for message in items),
                       "context_count": sum(message["role"] == "context" for message in items),
                       "messages": items})
    target_messages = [message for group in groups for message in group["messages"]
                       if message["role"] == "target"]
    stats = {"groups_scanned": len(gids), "groups_with_matches": len(groups),
             "groups_failed": sorted(reader.failed_groups),
             "matched_before_limit": matched_count, "target_count": len(target_messages),
             "context_count": len(messages) - len(target_messages),
             "omitted_by_limit": matched_count - len(target_messages),
             "issues": dict(reader.issues)}
    stats["complete"] = not stats["groups_failed"] and not any(reader.issues.values()) and not stats["omitted_by_limit"]
    hourly = [0] * 24
    for message in target_messages:
        if message["ts"]:
            hourly[datetime.fromtimestamp(message["ts"]).hour] += 1
    return {"kind": "person_context", "target": {"uin": identity["uin"], "name": identity["name"]},
            "scope": {"all_groups": all_groups,
                      "account": str(store.account), "after": after, "before_exclusive": before,
                      "limit": limit, "context_before": context_before, "context_after": context_after,
                      "context_minutes": context_minutes},
            "stats": stats, "hourly": hourly, "groups": groups}


def render_context(result, store):
    """Keep normal message lines compatible with validate --context."""
    names = defaultdict(set)
    per_group = {}
    for group in result["groups"]:
        quote_names = qanalysis.group_member_names(store, group["gid"])
        gmap = {message["uin"]: message["who"] for message in group["messages"]
                if message["uin"] and not message.get("system")}
        for message in group["messages"]:
            for quote in message.get("media") or []:
                if quote.get("kind") == "quote" and quote.get("uin"):
                    uin = str(quote["uin"])
                    gmap.setdefault(uin, qanalysis.display_name(store, uin,
                                                               quote_names))
        per_group[group["gid"]] = gmap
        for uin, name in gmap.items():
            names[name].add(uin)
    members, aliases = {}, {}
    for gmap in per_group.values():
        for uin, name in list(gmap.items()):
            original = name
            if (name, uin) in aliases:
                name = aliases[(name, uin)]
            else:
                index = sorted(names[name]).index(uin) + 1
                if len(names[name]) > 1:
                    name = original[:12] + "·成员" + str(index)
                while ((name in members and members[name] != uin)
                       or (name != original and name in names and names[name] != {uin})):
                    index += 1
                    name = original[:12] + "·成员" + str(index)
                aliases[(original, uin)] = name
            gmap[uin] = name
            members[name] = uin
    stats, scope = result["stats"], result["scope"]
    lines = ["# 发言成员对照表 (昵称=QQ号)"]
    lines.extend(f"# {name}={uin}" for name, uin in sorted(members.items()) if uin.isdigit())
    lines.extend([
        "", "# 成员定向总结原料", f"# 目标：{result['target']['name']} ({result['target']['uin']})",
        f"# 时间下界：{scope['after']}；时间上界（不含）：{scope['before_exclusive']}；本机时区",
        f"# 目标发言 {stats['target_count']} 条；辅助上下文 {stats['context_count']} 条；"
        f"有命中群 {stats['groups_with_matches']} 个；扫描群 {stats['groups_scanned']} 个",
        f"# 限制前匹配 {stats['matched_before_limit']} 条；因 --limit 未输出 {stats['omitted_by_limit']} 条",
        "# 角色为 target 的消息才计入目标发言；context 保留原发送者，只用于解释对话。",
        "# 群按本地账号的可读记录确定；所有消息和相邻上下文均限制在指定时间范围。",
    ])
    if not stats["complete"]:
        lines.append("# 覆盖不完整：" + json.dumps({"groups_failed": stats["groups_failed"],
                     "issues": stats["issues"], "omitted_by_limit": stats["omitted_by_limit"]}, ensure_ascii=False))
    for group in result["groups"]:
        lines.extend(["", f"## 群：{group['name']} ({group['gid']})",
                      f"# 本群目标 {group['target_count']} 条；上下文 {group['context_count']} 条"])
        gmap = per_group[group["gid"]]
        last_date = None
        for message in group["messages"]:
            date = message["time"].split(" ")[0]
            if date != last_date:
                lines.append(f"# 日期：{date}")
                last_date = date
            lines.append(f"# role={message['role']}；message_id={message['message_id']}；"
                         f"reason={','.join(message['reasons'])}；context_for={','.join(message['context_for'])}")
            lines.extend(qanalysis.msg_lines(message, store, gmap, tfmt="%m-%d %H:%M"))
    return "\n".join(lines) + "\n"


def run(args, get_store, info):
    after, before = time_window(args)
    if args.context_before > 100 or args.context_after > 100:
        raise ValueError("前后上下文各最多 100 条；可按时间或群分批提取")
    limit = args.limit if args.limit is not None else (0 if after is not None or before is not None else 300)
    store = get_store(args)
    gid = None
    if args.group is not None:
        token = args.group.strip()
        if not token:
            raise ValueError("指定单群时群名不能为空")
        if re.fullmatch(r"\d{5,15}", token):
            gid = token
        else:
            names = store.group_names()
            exact = [key for key, name in names.items() if str(name).casefold() == token.casefold()]
            hits = exact or [key for key, name in names.items() if token.casefold() in str(name).casefold()]
            if len(hits) != 1:
                raise ValueError("群名未匹配或匹配到多个群；请提供唯一完整群名或群号")
            gid = str(hits[0])
    identity = resolve_person(store, args.person, gid)
    result = collect(store, identity, [gid] if gid else None, after, before, limit,
                     args.context_before, args.context_after, args.context_minutes, progress=info)
    result["scope"]["all_groups"] = bool(args.all_groups)
    result["scope"]["group"] = gid
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n" if args.json else render_context(result, store)
    if not args.json and args.max_chars and len(text) > args.max_chars:
        raise ValueError(f"原料共 {len(text)} 字符，超过 --max-chars；请缩小日期/群范围、"
                         "设置 --limit，或使用 --max-chars 0 输出完整原料。未截断消息。")
    if args.out and args.out != "-":
        output = Path(args.out).expanduser().resolve()
        package = Path(__file__).resolve().parents[2]
        if output == package or package in output.parents:
            raise ValueError("原料包含本地身份信息，请将 -o 输出到技能包外")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        info("成员定向原料已写入指定文件")
    else:
        print(text, end="")
    info(f"完成：目标发言 {result['stats']['target_count']} 条，上下文 {result['stats']['context_count']} 条")
    if not result["stats"]["complete"]:
        info("覆盖不完整，请查看原料中的 stats 或覆盖说明后再总结")
