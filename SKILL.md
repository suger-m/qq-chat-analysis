---
name: qq-chat-analysis
description: 当用户需要读取、搜索、导出本地 QQ（NTQQ）群聊或私聊，或按成员汇总单群/跨群消息、总结最新消息、生成群聊日报和 HTML 分析报告时使用。
metadata:
  compatibility: Requires Python 3.10+ or uv; live NTQQ key extraction requires Windows and a logged-in local QQ process.
---

# QQ 聊天记录分析

完整阅读聊天内容，归纳有起因、推进和结论的议题。词频和发言排行只能作为辅助证据。
用户要快速总结时直接在聊天中答复；要求 HTML 报告时按下方流程完成校验与模板渲染。

## 安装与运行

本技能使用标准 `SKILL.md`、`scripts/`、`references/` 结构。同一份目录可以安装到支持
Agent Skills 的客户端；复制或链接整个 `qq-chat-analysis/` 目录，安装位置由客户端决定。
平台安装方法和依赖准备见 [安装说明](references/install.md)。

运行时从**本次加载的 SKILL.md 所在目录**定位脚本，不猜用户目录、客户端名称或旧会话路径。
以下命令中的 `<技能目录>` 替换成该实际位置；输出保持相对于当前工作目录。

Windows PowerShell：

```powershell
$skillDir = '<技能目录>'
$qq = Join-Path $skillDir 'scripts/qq.ps1'
& $qq context '目标群名' --limit 300 --max-chars 200000
```

Bash：

```bash
SK="<技能目录>"
bash "$SK/scripts/qq.sh" context "目标群名" --limit 300 --max-chars 200000
```

也可直接使用跨客户端 Python 入口：

```bash
uv run --locked --script "<技能目录>/scripts/qq.py" context "目标群名" --limit 300 --max-chars 200000
```

入口优先尊重显式 `QQ_PY`；未指定时优先用 `uv` 的隔离环境与包内锁文件，首次使用需下载
依赖，缓存就绪后可用 uv 的 `--offline` 选项。没有 uv 时尝试 PATH 中的 Python，Windows
额外支持 `py -3`。不搜索任何客户端的私有 Python 目录。依赖清单位于
`scripts/runtime/requirements.txt`，用自备环境时先安装它。

实时密钥提取和源库同步仅支持 Windows，并要求本机 QQ 已登录。在其他系统上使用已解密
快照时设置 `QQ_DATA_DIR` 并明确使用 `--no-fresh`；不要声称读取到了实时消息。
`schema`、`example`、`validate`、`html` 不需要运行 QQ。所有模板和资源均随包携带。

## 代码与私密数据

- 代码和固定模板放在技能包内；聊天快照、密钥、成员映射、缓存和本机配置放在包外。
- `QQ_DATA_DIR` 指定数据目录，默认系统临时目录下的 `qq_decrypt`。临时目录可能被清理，
  重建快照需要 QQ 运行并登录。
- `QQ_SOURCE_DIR` 指向 QQ 账号目录的父目录，可覆盖数据目录中 `settings.json` 的
  `source_base`。找不到源库时检查该设置；不要把实际值写入技能文档。
- `QQ_NT_DATA_DIR` 可覆盖媒体缓存目录；默认按当前账号解析，避免多账号串库。
- 打包技能只包含说明、代码、依赖清单和虚构测试，不包含数据库、密钥、本机配置、
  成员表或真实聊天样例。

## 获取完整且尽可能新的原料

1. 用 `groups` 确认群名与群号；同名群需确认命令提示的候选，避免选错。
2. 用 `context` 获取原料。要求最新 N 条时明确传 `--limit N`，不加日期窗口。
   日期需求用 `--date`、`--days` 或 `--from/--to`。
3. 命令默认比较源数据库及 WAL 与解密快照时间，陈旧时尝试同步。QQ 未运行或同步失败时
   必须说明用的是旧快照，不能声称已获取最新。`fresh --check` 仅检查，`--fresh` 强制同步；
   只有明确接受已有快照或做回归检查时才用 `--no-fresh`。
4. 完整阅读所有原料。若提示早期段落因 `--max-chars` 被省略，调大上限或分批取出后补读。
   时间段只是阅读辅助，不能机械当作议题。

文本原料开头的成员映射供本地分析使用。JSON 原料的 `hourly` 是该窗口的 24 小时消息量，
写报告时直接使用，勿漏掉跨日凌晨时段。消息总数包含系统事件；发言人数按非系统消息的
不同发送者计数，不能把发言条数当人数。引用行必须结合发言者和时间阅读。

## 按成员总结（单群 / 所有群）

用户要回顾某个人的发言时，先读 [成员总结指南](references/person-analysis.md)，使用
`person` 指定成员及 `--group` / `--all-groups`，可加 `--date`、`--days` 或 `--from/--to`。

```powershell
& $qq person '<成员ID>' --group '目标群名' --days 7 -o person-context.txt
& $qq person '<成员ID>' --all-groups --from '2026-10-01' --to '2026-10-21' -o person-context.txt
```

命令保留目标发言、同群相邻消息和引用关系。完整阅读原料，按角色区分目标观点与旁人上下文，
跨群结论注明来源群和时间。总结前核对覆盖说明与数量限制；需要 HTML 时沿用报告流程，
以 `person-context.txt` 作为校验原文。详细参数、范围口径与分批办法见成员总结指南。

## HTML 报告流程

先读 [分析指南](references/analysis-guide.md)，再创建结构化 JSON。字段不确定时运行
`schema` 或 `example`；示例 ID 是占位符，仅在本地工作文件中填入必要的真实映射。

```powershell
& $qq context '目标群名' --limit 300 --max-chars 200000 > context.txt
# Agent 完整阅读后编写 analysis.json
& $qq validate analysis.json --context context.txt --fix -o analysis.validated.json
& $qq html analysis.validated.json --theme light -o report.html
```

中文重定向需使用 UTF-8；Windows PowerShell 5.1 建议显式指定 UTF-8 编码或用原生 Python
捕获 stdout 写文件，避免默认 UTF-16。读取 JSON 时允许 UTF-8 BOM。

`validate` 检查结构、占比、成员归属与原文。退出码 0 只代表没有结构错误；仍要查看警告，
修正错误引用、署名不符及重名歧义后再渲染。`--fix` 能补字段，不能替代语义判断。
不适用的板块可以留空，不为了凑数量编造问答、待办或人物。生成后提供 HTML 链接并打开预览。

### 人物头像

人物、引用、附录使用 `uin`；问答分别使用 `asker_uin`、`answerer_uin`；议题参与者用
成员 ID 或 `{name, uin}`。从本地成员映射填入，昵称重名时以实际发送者的 ID 区分，不能猜。
唯一昵称可以由校验器回填；仅被引用的人也需按映射核对。

默认启用头像，渲染时尝试官方接口并内联图片，接口失败时降级为首字头像。不能保证接口
永远可用。仅在用户明确要求关闭头像或联网时使用 `--no-avatar`，不要擅自添加该参数。

### 隐私与脱敏

- 用户可见的说明、帮助示例和聊天答复用 `<成员ID>`、`<群ID>`、`<用户目录>` 等占位符，
  不复制真实 QQ 号、本机用户名、绝对私密路径、密钥、登录 token 或完整成员对照表。
- 原料和分析 JSON 保留必要的身份映射以准确配头像，只存本地工作区，不作为可转发交付物。
- HTML 默认隐藏结构化 QQ 号和群号，头像 CSS 用无身份含义的编号；图片内联，分享后无需
  再请求含账号号的远程头像 URL。渲染文本会遮蔽已知身份编号、本地路径、邮箱、常见手机号
  和明显的密钥格式。生成前仍需人工检查自由文本与图片中的私人信息。
- 昵称和真实头像仍能识别人；它们按用户需求保留。不能把这样的报告称为匿名报告。
- 对地域攻击、私密细节等用中性概述。风险提醒须基于已看到的内容，不添加假设性警告。

## 其他命令

| 需求 | 命令 |
|---|---|
| 最新 N 条原料 | `context <群名> --limit N` |
| 成员单群 / 跨群总结原料 | `person <成员ID> --group <群名>` / `person <成员ID> --all-groups` |
| 逐条阅读 / 私聊 | `read <群名> -n N` / `read --c2c <成员ID> -n N` |
| 搜索（最新消息兜底） | `search <词> -g <群名> --scan` |
| 导出 | `export <群名> -o <文件> --fmt txt/jsonl/json/csv/md` |
| 客观数字 | `heatmap`、`people`、`words`、`repeat`、`lurkers`、`night` |
| 多群对比 | `compare <群A> <群B>`，默认也检查数据新鲜度 |

媒体和系统事件的字段细节见 [数据结构说明](references/schema.md)。富元素解码全部失败时
检查 `blackboxprotobuf`，不能把缺依赖造成的空结果当成群里没发媒体。

## 维护与验证

修改包内代码后先运行离线测试，再用已解密快照做端到端回归。测试临时产物放在工作或临时
目录，不把聊天内容留在包内：

```powershell
$py = & $qq -PythonPath
& $py -m unittest discover -s (Join-Path $skillDir 'tests') -p 'test_*.py'
```

```bash
bash "$SK/tests/regress.sh" "目标群名"
```

不要直接用 SQL 绕过库的容错读取：FTS 有延迟，rowid 与消息时间不等序，数据库有零星坏页。
修改解密流程时保留“先写临时快照、成功后替换、失败保留原快照”的机制。
