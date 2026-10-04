#!/usr/bin/env bash
# QQ 群聊分析 — 端到端回归
#
# 覆盖: 全部 CLI 子命令 / 5 种导出格式 / html 渲染 / validate 校验链
# 用法:  bash tests/regress.sh [群号或群名片段]
#        默认取消息最多的群 (由 cli.py groups 决定)。
#
# 为什么留成常驻脚本: 这个项目没有单元测试框架, 改完数据层/渲染层只能靠真库跑
# 一遍才有信心。之前每次都是临时写、跑完删, 下次要复跑又得重写。
set -u
PACKAGE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PACKAGE/scripts/runtime" || exit 1

PY="$(bash "$PACKAGE/scripts/qq.sh" --python-path)"
NF="--no-fresh"          # 回归不触发重新解密, 用当前快照
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT" 2>/dev/null' EXIT

PASS=0; FAIL=0; FAILED=()

# run <名字> <cli.py 子命令及参数...>
# 全局开关必须写在 `cli.py` 之后、子命令之前 (它属于顶层 parser)。
run() {
  local name="$1"; shift
  if "$PY" cli.py $NF "$@" >"$OUT/$name.out" 2>"$OUT/$name.err"; then
    PASS=$((PASS+1)); printf '  ok   %s\n' "$name"
  else
    FAIL=$((FAIL+1)); FAILED+=("$name")
    printf '  FAIL %s\n' "$name"
    sed -n '1,6p' "$OUT/$name.err" | sed 's/^/       /'
  fi
}

G="${1:-}"
if [ -z "$G" ]; then
  G="$("$PY" cli.py $NF groups -n 1 --json 2>/dev/null \
      | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print((d[0] if isinstance(d,list) else d["groups"][0])["id"])' 2>/dev/null)"
fi
[ -n "$G" ] || { echo "拿不到群号, 请显式传入"; exit 2; }
echo "回归目标群: 已选定（编号隐藏）"
echo

echo "== 基础 =="
run groups        groups -n 3
run groups_json   groups -n 3 --json
run read          read "$G" -n 40
run read_json     read "$G" -n 40 --json
run search        search 图片 -g "$G" -n 5
run stats         stats -g "$G"

echo "== 分析 (统计类) =="
# analyze / heatmap 没有 -n, 其余支持
for c in analyze heatmap; do
  run "$c" "$c" "$G" --days 2 --limit 800
done
for c in words people repeat lurkers night; do
  run "$c" "$c" "$G" --days 2 --limit 800 -n 5
done

echo "== 导出 (5 种格式) =="
for fmt in json jsonl txt csv md; do
  run "export_$fmt" export "$G" -o "$OUT/e.$fmt" --fmt "$fmt" --days 2
done
run report_md   report "$G" -o "$OUT/r.md" --days 2 --limit 800
run report_json report "$G" -o "$OUT/r.json" --days 2 --limit 800

echo "== 取料 =="
run context      context "$G" --limit 300
run context_json context "$G" --limit 300 --json

"$PY" cli.py $NF context "$G" --limit 500 --max-chars 300000 \
      >"$OUT/ctx.txt" 2>/dev/null
"$PY" cli.py $NF context "$G" --limit 300 --json \
      >"$OUT/ctx.json" 2>/dev/null

echo "== 引用署名 (必须与正文同源) =="
# 被引用人的名字必须来自本群名片, 不能是全局 QQ 昵称 —— 否则改过群名片的人
# 会在引用行里变成另一个名字(示例：群名片与全局昵称不同),
# 读起来像两个人在对话。判据: 每个 `└ 回复 ... <名>: 「` 的 <名> 都得在成员表里。
if "$PY" - "$OUT/ctx.txt" <<'PYEOF'
import re, sys
txt = open(sys.argv[1], encoding="utf-8").read()
names = set()
for ln in txt.splitlines():
    if not ln.startswith("#"):
        continue
    body = ln.lstrip("#").strip()
    if "必须用 QQ号区分" in body:                 # 重名行: name=uin/uin | name=uin
        seg = body.split(":", 1)[-1]
        for cell in seg.split("|"):
            mm = re.match(r"\s*(.+?)=(\d{5,})", cell)
            if mm:
                names.add(mm.group(1).strip().replace("⚠", ""))
        continue
    # 数据行是多列对齐 (2+ 空格分列); 昵称本身可以含空格(call茅子-Hock Tan),
    # 所以按「最后一个 = 数字」从右往左切, 不能用 [^\s]+ 当昵称。
    for cell in re.split(r"\s{2,}", body):
        mm = re.match(r"(.+?)=(\d{5,})\s*$", cell.strip())
        if mm:
            names.add(mm.group(1).strip().replace("⚠", ""))
bad = []
for seg in re.findall(r"└ 回复 [^\n]*", txt):
    mm = re.match(r"└ 回复 (?:[\d-]+ [\d:]{5} )?(.+?): 「", seg)
    if mm and mm.group(1).strip() not in names:
        bad.append(mm.group(1).strip())
assert not bad, "引用署名不在成员表(疑似全局昵称): %s" % sorted(set(bad))[:5]
PYEOF
then
  PASS=$((PASS+1)); printf '  ok   quote_author (署名同源)\n'
else
  FAIL=$((FAIL+1)); FAILED+=("quote_author")
  printf '  FAIL quote_author\n'
fi

echo "== 昵称反查 (重名不许猜) =="
# 报告只写昵称时靠 nick_index 配头像。它原来是全局表 + 重名「保留先出现的」,
# 等于掷骰子: 全库 4,958 个有意义的重名里 4,847 个被强行解析, 会配上陌生人的脸。
# 现在按群建表, 且只保留唯一命中的名字。
if "$PY" - "$G" <<'PYEOF' >"$OUT/nick.out" 2>&1
import sys
sys.path.insert(0, ".")
import qqlib, qanalysis
gid = sys.argv[1]
s = qqlib.QQStore()
gi = s.nick_index(gid)
gm = qanalysis.group_member_names(s, gid)
rev = {}
for u, n in gm.items():
    rev.setdefault(n, []).append(u)
dup = {n: us for n, us in rev.items() if len(us) > 1}
bad = [n for n in dup if gi.get(n)]
assert not bad, "群内重名昵称被强行解析: %s" % sorted(bad)[:5]
wrong = [(n, gi[n], us[0]) for n, us in rev.items()
         if n not in dup and n in gi and gi[n] != us[0]]
assert not wrong, "唯一名解析错: %s" % wrong[:5]
PYEOF
then
  PASS=$((PASS+1)); printf '  ok   nick_index (按群 + 重名不猜)\n'
else
  FAIL=$((FAIL+1)); FAILED+=("nick_index")
  printf '  FAIL nick_index\n'; sed -n '1,6p' "$OUT/nick.out" | sed 's/^/       /'
fi

echo "== validate (校验链) =="
# 从 context 的 --json 造一份最小合法分析 (good) 与一份埋了问题的 (bad)
"$PY" - "$OUT/ctx.json" "$OUT/good.json" "$OUT/bad.json" <<'PYEOF'
import json, sys
ctx, good_path, bad_path = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    c = json.load(open(ctx, encoding="utf-8"))
except Exception:
    c = {}
first = None
for sg in (c.get("segments") or []):
    for ln in sg.get("lines") or []:
        if ln.get("who") not in ("系统", "未知") and len(ln.get("text") or "") >= 6:
            first = ln
            break
    if first:
        break
good = {
    "group": c.get("name"), "gid": c.get("gid"),
    "headline": "回归样本", "summary": ["回归样本"],
    "quality": {"title": "构成", "dimensions": [
        {"name": "技术讨论", "percentage": 60, "comment": "点评"},
        {"name": "闲聊水群", "percentage": 40, "comment": "点评"}],
        "summary": "总结"},
    "people": [{"name": (first or {}).get("who", "甲"), "role": "角色",
                "desc": "描述"}],
    "insights": ["观察"],
}
if first:
    good["quotes"] = [{"who": first["who"], "time": "00:00", "text": first["text"]}]
json.dump(good, open(good_path, "w", encoding="utf-8"), ensure_ascii=False)
bad = json.loads(json.dumps(good, ensure_ascii=False))
bad["topic"] = [{"title": "拼错的字段名"}]              # 未知字段 → warn
bad["quality"]["dimensions"][0]["percentage"] = 200     # 超 100% → 自动归一化
bad["risks"] = [{"level": "notice", "text": "非法 level"}]   # 非法取值 → warn
bad["stats"] = "这里应该是数组"                          # 类型错 → error (退出码 1)
json.dump(bad, open(bad_path, "w", encoding="utf-8"), ensure_ascii=False)
PYEOF

run validate_clean validate "$OUT/good.json" --context "$OUT/ctx.txt"
run validate_json  validate "$OUT/good.json" --context "$OUT/ctx.txt" --json
run validate_regrp validate "$OUT/good.json" --group "$G"

# --fix 面对「有 error 的样本」仍然要落盘修复版, 但退出码保持非 0 (有错误要告诉调用方)。
# 所以这里不能用 run (它要求退出码 0), 单独断言「文件写出来了 + 占比被归一化」。
"$PY" cli.py $NF validate "$OUT/bad.json" --context "$OUT/ctx.txt" \
      --fix -o "$OUT/fixed.json" >"$OUT/vfix.out" 2>&1
if [ -s "$OUT/fixed.json" ] && "$PY" - "$OUT/fixed.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
tot = sum(x["percentage"] for x in d["quality"]["dimensions"])
assert 99.5 <= tot <= 100.5, "占比未归一化: %s" % tot
assert all(isinstance(x["percentage"], (int, float))
           for x in d["quality"]["dimensions"]), "占比未转成数字"
PYEOF
  then
  PASS=$((PASS+1)); printf '  ok   validate_fix (落盘 + 占比归一化)\n'
else
  FAIL=$((FAIL+1)); FAILED+=("validate_fix")
  printf '  FAIL validate_fix\n'; sed -n '1,6p' "$OUT/vfix.out" | sed 's/^/       /'
fi

# 带病样本必须报错 (退出码非 0) 且抓到那个拼错的字段名
if "$PY" cli.py $NF validate "$OUT/bad.json" --context "$OUT/ctx.txt" \
     >"$OUT/vbad.out" 2>&1; then
  FAIL=$((FAIL+1)); FAILED+=("validate_bad(应报错却通过)")
  printf '  FAIL validate_bad (应报错却通过)\n'
elif grep -q "topic" "$OUT/vbad.out"; then
  PASS=$((PASS+1)); printf '  ok   validate_bad (退出码非0 且抓到拼错字段)\n'
else
  FAIL=$((FAIL+1)); FAILED+=("validate_bad(未抓到字段)")
  printf '  FAIL validate_bad (未抓到拼错字段)\n'
fi

echo "== HTML 渲染 =="
run html      html "$OUT/good.json"   -o "$OUT/g.html"  --no-avatar
run html_dark html "$OUT/good.json"   -o "$OUT/gd.html" --theme dark --no-avatar
run html_fixed html "$OUT/fixed.json" -o "$OUT/gf.html" --no-avatar

echo
echo "结果: $PASS 通过, $FAIL 失败"
if [ "$FAIL" -gt 0 ]; then
  printf '失败项: %s\n' "${FAILED[*]}"
  exit 1
fi
