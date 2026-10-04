# 安装与依赖

## 分发方式

此包是单个 Agent Skill。借鉴 [superpowers](https://github.com/obra/superpowers) 的方式，
技能内容共用，客户端负责发现和安装；运行代码不引用客户端安装根目录。
本技能直接由聊天分析需求触发，无需 superpowers 的启动 hooks 或整套开发流程。

解压后把整个 `qq-chat-analysis/` 目录放入当前客户端的 skills 目录，也可以从固定代码目录
创建目录链接。确认客户端能读取其中的 `SKILL.md`，按客户端要求重新加载技能或开启新会话。
多个客户端可以引用同一份目录。复制安装的版本需要分别更新。

- Codex：放入 `$CODEX_HOME/skills/qq-chat-analysis`；未设置时通常是
  `~/.codex/skills/qq-chat-analysis`。
- Claude Code：放入 `~/.claude/skills/qq-chat-analysis`，或项目的
  `.claude/skills/qq-chat-analysis`。
- WorkBuddy、Cursor 及其他客户端：使用该客户端的技能导入功能或其配置的 skills 目录。
  不把这些位置写进运行脚本，也不假定所有客户端使用相同的目录。

不要只复制 `SKILL.md`。`scripts/qq.py`、`scripts/qq.py.lock`、`scripts/runtime/` 和
`references/` 应随技能一起安装。本地数据库、密钥、缓存和本机配置放在包外。
此 ZIP 可供文件导入和手动安装；它不是已经发布到任何客户端市场的插件。

## 运行环境

### 方式一：uv 隔离环境

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后运行：

```bash
uv run --locked --script "<技能目录>/scripts/qq.py" groups
```

脚本用 PEP 723 声明 Python 3.10+ 和依赖；`qq.py.lock` 固定解析后的版本。
环境与依赖缓存由 uv 管理，不存入技能目录。首次下载可能需要网络；断网使用前先准备缓存。
已有缓存时可直接运行 `uv run --offline --locked --script ...`。
PowerShell 和 Bash 包装入口在未设置 `QQ_PY` 时自动使用 PATH 中的 uv。

### 方式二：自备 Python 环境

在技能目录之外创建 Python 3.10+ 虚拟环境，安装依赖，并显式指定解释器。Windows 示例：

```powershell
$skillDir = '<技能目录>'
$venvDir = '<包外虚拟环境目录>'
py -3 -m venv $venvDir
$env:QQ_PY = Join-Path $venvDir 'Scripts/python.exe'
& $env:QQ_PY -m pip install -r (Join-Path $skillDir 'scripts/runtime/requirements.txt')
& (Join-Path $skillDir 'scripts/qq.ps1') groups
```

Bash 示例：

```bash
SK="<技能目录>"
VENV="<包外虚拟环境目录>"
python3 -m venv "$VENV"
export QQ_PY="$VENV/bin/python"
"$QQ_PY" -m pip install -r "$SK/scripts/runtime/requirements.txt"
bash "$SK/scripts/qq.sh" groups
```

`QQ_PY` 接受可执行文件路径或 PATH 中的解释器名称；不要附加命令参数。
显式指定的解释器不满足要求时入口报错，避免悄悄换到其他环境。
未指定且未安装 uv 时，入口尝试 PATH 中的 Python；Windows 也尝试 `py -3`。
pip 方式按 requirements 中的范围安装，完整版本锁定由 uv 方式提供。

## 数据配置与系统限制

| 设置 | 用途 |
|---|---|
| `QQ_DATA_DIR` | 包外解密快照、密钥、缓存和 settings.json 的目录 |
| `QQ_SOURCE_DIR` | QQ 账号目录的父目录；覆盖自动发现与 settings.json |
| `QQ_NT_DATA_DIR` | 媒体缓存目录 |
| `QQ_PY` | 显式指定自备 Python，优先于 uv |

默认仍使用系统临时目录下的 `qq_decrypt`，保留现有快照的兼容性。
搬迁代码不需要搬迁私密数据；换电脑时单独配置数据来源。需要持久保存快照时把
`QQ_DATA_DIR` 指向包外的持久目录。用同一快照目录的多个客户端共享同步锁。

实时同步从 Windows QQ 进程提取密钥，必须在拥有 QQ 数据的 Windows 机器本地运行。
远程/云端客户端不能仅通过安装技能访问本地 QQ。macOS/Linux 可处理兼容的已解密快照，
读取时使用 `--no-fresh`；这些系统的实时解密不在当前实现范围。

## 更新与维护

更新时替换技能代码目录，保留包外数据与配置。不要往技能包内放 `.venv`、快照或密钥。
修改依赖时同时修改 `scripts/qq.py` 的依赖声明与 `scripts/runtime/requirements.txt`，
再从技能目录运行 `uv lock --script scripts/qq.py --default-index https://pypi.org/simple`。
锁文件应只引用可公开访问的依赖来源，不包含私人镜像凭据或本机路径。

多群对比也使用同一个入口和环境：

```bash
uv run --locked --script "<技能目录>/scripts/qq.py" compare "群A" "群B" --days 7
```

`-PythonPath`（PowerShell）或 `--python-path`（Bash/Python）可输出实际解释器路径，
便于运行已有维护脚本。聊天读取、分析 JSON 校验和 HTML 渲染的业务流程见 SKILL.md。
