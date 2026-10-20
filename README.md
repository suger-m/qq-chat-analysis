# QQ Chat Analysis

让支持 Agent Skills 的 AI 客户端读取本地 NTQQ 聊天记录，整理话题、生成群聊日报，
并输出使用内置模板的单文件 HTML 报告。

技能使用标准 `SKILL.md` 结构，运行代码通过相对路径加载。可以在 Codex、Claude Code、
WorkBuddy 等支持该格式的客户端中安装，运行环境由 uv 或自备 Python 管理。

## 能做什么

- 读取群聊和私聊，按最新条数、日期或时间范围提取消息。
- 按指定成员汇总单群或所有本地群的发言，支持时间范围，保留相邻对话和引用上下文。
- 搜索关键词，导出 TXT、JSON、JSONL、CSV、Markdown。
- 统计活跃时段、发言情况、热词、复读和多个群的差异。
- 把消息原文交给 AI 归纳议题、问答和待办，再校验结构化结果并渲染 HTML。
- 检查快照新鲜度；同步采用进程锁和独立临时目录，失败时保留旧快照。

话题总结由 AI 阅读原文后生成。CLI 提供数据、统计、校验和模板渲染能力。

## 环境要求

| 功能 | 要求 |
|---|---|
| 实时提取密钥、解密并同步 NTQQ 源库 | Windows，本机 QQ 运行且已登录 |
| 读取已有解密快照 | 兼容的 NTQQ 快照；其他系统需设置数据目录并使用 `--no-fresh` |
| 校验分析 JSON、生成 HTML | Python 3.10+ 及依赖，无需运行 QQ |
| 推荐依赖管理方式 | [uv](https://docs.astral.sh/uv/getting-started/installation/) |
| AI 客户端 | 支持 Agent Skills，且能执行本机脚本 |

跨客户端安装支持与实时解密的系统支持是两个层面。macOS/Linux 的实时密钥提取尚未实现，
这些系统及其他客户端尚未逐一实测。云端客户端仅安装技能后无法直接访问本机 QQ 进程。

## 安装技能

### 使用 Skills CLI

如果已经有 Node.js/npm，可使用 [Skills CLI](https://github.com/vercel-labs/skills) 选择客户端：

```bash
npx skills add https://github.com/suger-m/qq-chat-analysis
```

这个命令安装技能文件。Python 运行依赖按下面的“快速开始”准备。

### 手动安装

克隆仓库，或下载并解压仓库 ZIP，把整个 `qq-chat-analysis` 目录放入客户端的 skills 目录。
本仓库根目录就是技能目录，`SKILL.md` 必须与 `scripts/`、`references/` 一起保留。

```bash
git clone https://github.com/suger-m/qq-chat-analysis.git
```

| 客户端 | 安装位置 |
|---|---|
| Codex | `$CODEX_HOME/skills/qq-chat-analysis`，未配置时通常为 `~/.codex/skills/qq-chat-analysis` |
| Claude Code | `~/.claude/skills/qq-chat-analysis` 或项目的 `.claude/skills/qq-chat-analysis` |
| WorkBuddy、Cursor 等 | 使用客户端的技能导入功能或其配置的 skills 目录 |

也可以从固定代码目录建立目录链接，让多个客户端使用同一份技能。安装后按客户端要求
重新加载技能或开启新会话。详细依赖配置见 [安装说明](references/install.md)。

## 快速开始

### 让 AI 生成报告

安装 uv，启动并登录本机 Windows QQ，然后向已加载技能的 AI 客户端发送：

> 使用 qq-chat-analysis，总结“目标群名”最新 300 条消息，用内置 HTML 模板生成报告。

AI 将确认目标群、获取原文、整理分析 JSON、校验并生成 HTML。同步失败时应明确说明
报告基于旧快照；无法确认新鲜度时不能把内容称为“最新消息”。

### 直接运行 CLI

在仓库目录运行：

```bash
uv run --locked --script scripts/qq.py groups
uv run --locked --script scripts/qq.py context "目标群名" --limit 300 --max-chars 200000
```

脚本用 PEP 723 声明依赖，`scripts/qq.py.lock` 固定解析后的版本。uv 在包外创建和缓存
运行环境；首次运行需要下载依赖。已准备缓存时可加 `--offline`。

也可通过包装脚本运行。Windows PowerShell：

```powershell
& ./scripts/qq.ps1 groups
& ./scripts/qq.ps1 context '目标群名' --limit 300 --max-chars 200000
```

Bash：

```bash
bash scripts/qq.sh groups
bash scripts/qq.sh context "目标群名" --limit 300 --max-chars 200000
```

从其他工作目录调用时使用技能脚本的完整路径；相对输出路径仍以当前工作目录为准。
包装入口优先使用显式 `QQ_PY`，否则优先使用 PATH 中的 uv，再尝试可用的 Python。

## HTML 报告流程

先获取完整原文，让 AI 阅读后按照 [分析指南](references/analysis-guide.md) 编写分析 JSON。
字段说明和虚构示例可以通过 `schema`、`example` 查看。

Windows PowerShell 示例：

```powershell
& ./scripts/qq.ps1 context '目标群名' --limit 300 --max-chars 200000 |
    Out-File -FilePath context.txt -Encoding utf8
# AI 阅读 context.txt 后编写 analysis.json
& ./scripts/qq.ps1 validate analysis.json --context context.txt --fix -o analysis.validated.json
& ./scripts/qq.ps1 html analysis.validated.json --theme light -o report.html
```

Windows PowerShell 5.1 的默认重定向编码可能不适合后续读取，示例显式使用 UTF-8。
校验通过代表结构检查通过，仍需检查警告、引用原文和署名。原料超出 `--max-chars` 时
需要扩大上限或分批阅读，避免漏掉较早的消息。

HTML 默认尝试获取并内联头像，接口失败时使用首字头像。明确要求不联网取头像时可加
`--no-avatar`。报告中保留的昵称与头像仍可能识别个人。

## 按成员总结

安装技能后可直接向 AI 提出：

> 总结某位成员最近 7 天在指定群的发言，保留必要上下文。

> 汇总某位成员在所有本地群中指定日期范围的消息，按主题总结并标注来源群和时间。

成员优先使用 QQ 号或 NT UID；完整昵称也可查询，重名时需要明确 ID。命令示例：

```powershell
# 单群，最近 7 天
& ./scripts/qq.ps1 person '<成员ID>' --group '目标群名' --days 7 -o person-context.txt

# 所有本地群，日期范围（结束日期包含整天）
& ./scripts/qq.ps1 person '<成员ID>' --all-groups --from '2026-10-01' --to '2026-10-21' -o person-context.txt
```

也可用 `uv run --locked --script scripts/qq.py person ...` 或 Bash 包装入口。
AI 完整阅读原料后可直接总结；需要 HTML 时再编写分析 JSON，校验时使用
`--context person-context.txt`，随后沿用 HTML 渲染命令。

- 单群与所有群共用 `person` 入口，`--group` / `--all-groups` 必选其一。
- 默认保留同群前后各 3 条、15 分钟内的上下文及引用信息；可用
  `--context-before`、`--context-after`、`--context-minutes` 调整。
- 有时间范围时默认提取全部目标发言；无时间范围时默认最近 300 条。`--limit` 是所有群
  合并后目标发言的总上限，`--limit 0` 提取全部，旁人上下文另计。
- `--json` 输出按群分组的结构化原料；`-o` 显式写入 UTF-8，路径须在技能包外。
- “所有群”限当前 QQ 账号本地可读的群记录。查看覆盖说明，区分目标发言、上下文与
  未输出/未能读取的内容。

完整参数和总结规范见 [成员总结指南](references/person-analysis.md)。

## 常用命令

下表中的参数均接在 `uv run --locked --script scripts/qq.py` 或包装入口之后。

| 需求 | 命令参数 |
|---|---|
| 查看群列表 | `groups` |
| 最新 300 条原料 | `context "目标群名" --limit 300 --max-chars 200000` |
| 成员单群发言原料 | `person "<成员ID>" --group "目标群名" --days 7` |
| 成员跨群发言原料 | `person "<成员ID>" --all-groups --days 7` |
| 读取 20 条消息 | `read "目标群名" -n 20` |
| 读取私聊 | `read --c2c <成员ID> -n 20` |
| 搜索关键词 | `search "关键词" -g "目标群名"` |
| 导出聊天记录 | `export "目标群名" --fmt jsonl -o messages.jsonl` |
| 按最近 7 天统计 | `stats -g "目标群名" --days 7` |
| 多群对比 | `compare "群A" "群B" --days 7` |
| 检查快照状态 | `fresh --check` |
| 强制同步 | `refresh` |
| 读取已有快照 | `groups --no-fresh` |
| 查看帮助 | `--help` 或 `<子命令> --help` |

`heatmap`、`people`、`words`、`repeat`、`lurkers`、`night` 等命令提供辅助统计。
消息总数可能包含系统事件，发言人数与发言条数应分别解释。

## 配置与隐私数据

| 环境变量 | 作用 |
|---|---|
| `QQ_DATA_DIR` | 解密快照、密钥、缓存和本机 settings.json 的包外目录 |
| `QQ_SOURCE_DIR` | QQ 账号目录的父目录，覆盖默认发现及 settings.json 配置 |
| `QQ_NT_DATA_DIR` | 媒体缓存目录 |
| `QQ_PY` | 自备 Python 解释器路径或 PATH 中的名称 |

`QQ_DATA_DIR` 默认是系统临时目录下的 `qq_decrypt`，临时目录可能被系统清理。
需要持久保存时请指定包外目录；技能禁止把该目录设置在技能包内。

```powershell
$env:QQ_DATA_DIR = '<包外数据目录>'
$env:QQ_SOURCE_DIR = '<QQ账号目录的父目录>'
```

在 macOS/Linux 处理已有兼容快照时，设置 `QQ_DATA_DIR` 并传 `--no-fresh`。
快照应保留 `QQ_DATA_DIR/output/<账号ID>/` 下的数据库目录结构。

聊天原文、分析 JSON、数据库、密钥和本机配置都属于本地工作数据。请放在仓库外，
不要提交到 Git。HTML 会遮蔽已知身份编号和部分敏感文本，但自由文本和图片仍需在分享前
人工检查。真实昵称和头像保留时，报告不能视为匿名报告。

## 常见问题

**找不到 Python 或依赖？**

安装 uv，或在包外 Python 3.10+ 虚拟环境中安装
`scripts/runtime/requirements.txt`，再用 `QQ_PY` 指定解释器。显式指定的环境不满足要求
时入口会报错，不会自动换用别的环境。具体命令见 [安装说明](references/install.md)。

**找不到源库或同步失败？**

确认 Windows QQ 已运行并登录，检查 `QQ_SOURCE_DIR` 是否指向账号目录的父目录。
同步需要读取进程内存；系统权限或进程访问限制可能影响提取。失败后会保留旧快照。
可用 `fresh --check` 查看状态，再根据明确的错误处理。

**换一个客户端后要重新解密吗？**

同一机器上的客户端可共用包外 `QQ_DATA_DIR`。移动技能代码目录不要求移动快照。
不同机器或云端需要单独准备数据来源，不能自动访问另一台机器的 QQ。

## 仓库结构

```text
qq-chat-analysis/
├── README.md
├── SKILL.md                   AI 使用说明
├── scripts/
│   ├── qq.py                  统一入口与 PEP 723 依赖声明
│   ├── qq.py.lock             uv 依赖锁
│   ├── qq.ps1 / qq.sh         PowerShell / Bash 包装入口
│   ├── compare_groups.py      多群对比
│   └── runtime/               数据解析、同步、分析、校验和 HTML 模板
├── references/
│   ├── install.md             安装与运行环境
│   ├── analysis-guide.md      内容分析和报告字段
│   └── schema.md              NTQQ 数据结构说明
└── tests/                     虚构数据离线测试及本地快照回归脚本
```

## 更新

Git 安装方式可在技能仓库中执行 `git pull --ff-only`。复制安装的客户端需分别替换代码，
包外数据与配置保留。维护依赖时同时更新 PEP 723 声明和 requirements，再生成新的锁文件。

本项目的分发结构参考 [superpowers](https://github.com/obra/superpowers)，技能格式遵循
[Agent Skills](https://agentskills.io/specification)。各客户端的发现方式按其自身安装机制处理。
