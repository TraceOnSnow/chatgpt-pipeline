# ChatGPT 材料管道

把历史聊天整理为可继续写作的 Markdown：验证来源、筛选候选、生成初稿、独立复核，再由用户明确导入内容库。生成不等于入库，入库不等于发表。

筛选摘要只负责定位。明确要求记录、存档或补充的材料应回读原始问答和现稿后处理；普通闲聊不强行成稿。新任务冻结原文优先规则，保存的AI回答注明归属，旧任务不自动升级或重发。流程与有界上下文见 [内容契约](docs/content-flow.md)。

运行时会在本地工作区记录来源、请求、原始回复、用量与恢复信息；这些私人数据不随公开仓库提供。模型只写正文，程序维护身份和来源关系；这些校验不代表内容经过外部事实核查。

## 安装

需要 Python 3.11 或更新版本，以及已经安装的 `uv`。将两个仓库放在同一父目录，保留以下目录名：

```sh
git clone https://github.com/TraceOnSnow/obsidian-workflow.git
git clone https://github.com/TraceOnSnow/chatgpt-pipeline.git
cd chatgpt-pipeline
python3 scripts/workbench.py setup
.venv/bin/python -m chatgpt_pipeline --help
.venv/bin/python scripts/memory-import.py --help
```

setup 只创建项目内 `.venv` 并安装锁定依赖，不创建材料工作区或笔记库，不调用模型。离线索引本身只用 Python 标准库；上述完整安装还会安装同级 [obsidian-workflow](https://github.com/TraceOnSnow/obsidian-workflow) 提供的 `markdown-library`，供后续入库使用。

## 准备自己的来源

来源、账号标识、工作区、笔记库和密钥均不随仓库提供。以下路径需要替换为自己的明确路径，输出目录必须位于本仓库 `runtime/` 下且尚不存在。原始来源只读，不根据文件名或聊天标题推断账号关系。

已有上传器产生的 `chatgpt-upload-commit` 数据时，可以先验证再索引。普通导出 JSON 不能冒充上传 commit：

```sh
.venv/bin/python -m chatgpt_pipeline verify \
  /absolute/path/to/exports/account-a \
  /absolute/path/to/exports/account-a/commits/HASH.commit.json
.venv/bin/python -m chatgpt_pipeline ingest \
  --exports /absolute/path/to/exports \
  --database runtime/index.sqlite3
.venv/bin/python -m chatgpt_pipeline status --database runtime/index.sqlite3
.venv/bin/python -m chatgpt_pipeline bulk-prepare \
  --database runtime/index.sqlite3 --out runtime/selection-example
```

已有处理批次后，可用只读增量计划核对新会话和续聊，再决定是否授权新一轮筛选。它校验指定或最新的已发布 commit，与基线逐条比较消息（ID、角色、正文 SHA 都相同才算已处理），统计本人消息条数、字数和语音标记，列出工作区回执与 Library 来源登记，并按现有分块和预占公式估算调用数与费用上限。基线默认取 `--workspace` 中 imports 已登记的密封批次实际排队的消息（各 job `input.json` 里 `primary=true` 的条目）；按日期截断前的消息、只作上下文的消息和批次跳过的会话都不算已处理。显式传 `--baseline-catalog`（须与某个已发布 commit 逐字节一致）时，整份 catalog 视为已处理，输出会标注 `treated_as_processed=whole_catalog`。传 `--vault` 时，Library 证据里消息 ID、角色、正文 SHA 完全一致的本人消息从待处理中扣除并计为 `covered_by_library`；只登记了会话的旧来源仍只作提示。工作区数据库以 `mode=ro` 只读事务读取，存在 `state.sqlite3-journal` 时拒绝运行。不调用模型、不写工作区或笔记库，输出文件必须尚不存在：

```sh
.venv/bin/python -m chatgpt_pipeline incremental-plan \
  /absolute/path/to/exports/account-a \
  --workspace runtime/workbench --vault /absolute/path/to/vault \
  --since 2026-01-01T00:00:00+00:00 \
  --out runtime/catchup-example/plan.json --summary runtime/catchup-example/plan.md
```

成稿目标数要等筛选、复核和编辑合并之后才知道，计划只给出每目标两次调用的预占上限和验证器允许的条目上限，不是报价；实际费用以运行账本为准。

要回原始聊天按本人原话整理时，用只读的 `archive-read` 在已校验 commit 里找会话，不必自己 grep 档案或拼消息。按本人消息正文关键词（大小写不敏感，也匹配标题；多个关键词默认全部命中，`--any` 任一命中）、本人消息日期范围、会话 ID（完整或 8 位以上唯一前缀）和是否含语音筛选；纯日期按 `--tz` 解释，`--until` 含当天。候选列表给出 ID、首末本人消息时间、本人消息数、语音数、标题和截断的命中片段。加 `--conversation` 输出该会话的规范化本人消息，每条带消息 ID、时间和 `text_sha256`；`--context` 另附前一条助手回复的截断上下文（不是本人原话）。默认 JSON，`--format markdown` 输出可读稿：

```sh
.venv/bin/python -m chatgpt_pipeline archive-read /absolute/path/to/exports/account-a \
  --since 2026-01-09 --until 2026-01-10 --tz America/Chicago --voice --keyword 记录
.venv/bin/python -m chatgpt_pipeline archive-read /absolute/path/to/exports/account-a \
  --conversation CONVERSATION_ID --context --format markdown --out runtime/read-example/chat.md
```

现稿已经覆盖某段聊天时，`covered-evidence` 生成可直接交给 `markdown-library cover-source --source` 的 `chatgpt-source-first-covered-v1` JSON。默认取该会话全部有文字的本人消息，`--message` 可显式指定（可含助手消息，但至少一条本人消息，最多 200 条）；`text_sha256` 与管线内部一致，是规范化消息正文 UTF-8 字节的 sha256。`input_source` 记录 commit、catalog、对象路径与哈希及选取方式。正文被 catalog 标为过期或规范化有缺口时拒绝生成（过期可显式 `--allow-stale`）：

```sh
.venv/bin/python -m chatgpt_pipeline covered-evidence /absolute/path/to/exports/account-a \
  --conversation CONVERSATION_ID --decision "现稿已覆盖这段的要点" --out runtime/cover-example/covered.json
```

两个命令都只校验并读取档案：不调用模型、不读写工作区或笔记库；`--out` 只写新文件（0600），已存在即拒绝，写文件时终端只打印计数。每次运行都会完整校验 commit，档案按小时更新时可用 `--commit` 固定同一版本。

也可以直接准备官方导出的 ZIP。适配器读取 `conversations.json` 或 `conversations-N.json`，记录 ZIP、成员、账号、会话和消息指纹，不解压媒体。`account-a` 是示例标识，同一个真实账号应持续使用同一个自定标识：

```sh
.venv/bin/python scripts/memory-import.py prepare \
  --zip /absolute/path/to/ChatGPT-export.zip \
  --out runtime/memory-example \
  --profile profiles/example.json --account-key account-a
```

[示例 profile](profiles/example.json) 是通用合成配置，只含筛选和复核提示；可以复制修改后用于新批次。prepare 离线冻结实际提示与 SHA，不会改写旧批次。可用 `--index runtime/index.sqlite3` 对照已有索引：只跳过同账号、同会话、同正文指纹的材料，不猜测不同账号的关联。附件和未知结构记录为缺口，有文字不表示理解了图片或音频。

### Claude Code 会话中的本人消息（只读）

`claude-code-messages` 只读取一个项目会话目录顶层的 `*.jsonl`（子 Agent 记录在子目录，不读），抽出本人输入：`origin.kind=human` 的 user 记录，以及 `humanTurn` 的中途插话（`queued_command`）。工具结果、任务通知、peer 转述、压缩摘要、技能/宿主注入、中断提示、本地命令输出和 Agent 启动会话的说明都排除，并按原因计数。开头/结尾的 `<system-reminder>` 被剥掉，`<pasted_content>` 区段标出位置、不算本人字数（`own_chars`）。以记录 `uuid` 去重，会话归属取记录里的 `sessionId`；与上一轮收尾按钮标签完全一致的短回复标为 `is_button_reply`。

```sh
.venv/bin/python -m chatgpt_pipeline claude-code-messages \
  --dir /absolute/path/to/.claude/projects/<project> \
  --out runtime/example/claude-code-messages.jsonl \
  --segments runtime/example/claude-code-segments.json \
  --since 2026-01-01 --until 2026-01-31
```

`--since`/`--until` 的日期按 Asia/Shanghai 自然日（`--until` 含当天），也接受 ISO 时间。输出文件必须尚不存在，可选 `--format json`、`--no-context`（不带上一条助手回复末尾 200 字）。不调用模型，不写 Vault 或 workbench，不复制原始会话。

查重辅助（都是启发式，字段名带 `heuristic`）：

- `--library-writes`：每条本人消息加 `library_writes_heuristic`，即从这条消息到下一条本人消息之前，助手的库写调用。先沿 parentUuid 链归属；链在压缩处断开时，按文件顺序归属。识别的调用有四类：Bash 里的 `markdown-library` create/replace/edit/import/organize/move/merge/publish-article/cover-source（含变量、shell 函数和 Python 列表包装，以及之后再运行的这类辅助脚本）；库 MCP 的 create/replace/edit/merge/move/organize；对 Vault 路径的 Edit/Write。默认只预览的子命令，没带 `--apply` 时记为 `applied: false`。子 Agent 记录和直接改文件的脚本看不到，所以“没有写操作”不能证明内容没进库。
- `--dictation`：加确定性的 `dictation_candidate`。去掉粘贴和链接后，本人原话不少于 60 字（不计空白）；不是按钮回复，也不是斜杠命令；含经历/感想特征词或保存意图词。规则常量在 `claude_code.py` 的 `DICTATION_*`。
- `--uncovered-dictation FILE`：另写一个 JSON，列出没有 applied 库写的口述候选，按（会话，上海日期）分组。每条只有 uuid、日期、字数和前 80 字，放私密目录。

## 有限筛选与初稿

准备后先检查 `runtime/memory-example/scope.json`、`gaps.json` 和 `progress.json`。以下命令会发送付费请求；只有在确认所选来源和累计预算后才执行。示例预算为该筛选批次累计 0.10 USD，并非单次调用额度或处理完成保证：

```sh
.venv/bin/python scripts/memory-import.py run \
  --out runtime/memory-example --workers 1 --budget-usd 0.10 --call \
  --key-file /absolute/private/openrouter.env
.venv/bin/python scripts/memory-import.py export --out runtime/memory-example
.venv/bin/python scripts/memory-import.py notes-init \
  --out runtime/memory-example --workspace runtime/workbench-example
```

密钥文件使用 `OPENROUTER_API_KEY=...` 格式，不写入 Git；直接 CLI 模型入口也支持 `OPENROUTER_API_KEY` 环境变量。模型和供应商策略由现有源码定义，实际可用性及价格以供应商为准。

notes-init 只准备候选，不生成或导入笔记。打开只读模式网页，先查看材料：

```sh
.venv/bin/python -m chatgpt_pipeline workbench-serve \
  --workspace runtime/workbench-example --port 8766 --no-resume
```

浏览器访问 `http://127.0.0.1:8766`。需要生成时，结束当前实例，使用同一工作区显式增加 `--call --key-file /absolute/private/openrouter.env` 后启动。不要同时运行两个实例。生成阶段与独立复核最多两次调用；新笔记工作区使用源码中独立的累计预算上限，筛选与笔记账本分别保留。

新任务使用 [markdown-content-v1](docs/content-flow.md)。复核异常保留完整可读初稿并显示待复核状态；空、截断和供应商错误不会当作完成。模型调用先记账，未知结果保留费用预留；重启不清零，不自动重发没有回复的请求。已有回复优先离线恢复，不能通过换目录或任务身份绕开账本。

## 明确入库

先用 `obsidian-workflow` 初始化自己的空笔记库，再在网页启动命令中增加 `--vault /absolute/path/to/your-vault`。库工具的安装、初始化与编辑方式见其 [README](https://github.com/TraceOnSnow/obsidian-workflow)。点击导入才会写入；待复核稿还需明确确认。来源身份去重让重复入库复用已有笔记，保留之后的用户编辑；不会把来源档案一起搬入笔记库。

网站可选接入 [homepage](https://github.com/TraceOnSnow/homepage)。本项目不要求该仓库，也不负责网站部署或自动发表私人正文。私人稿和发表稿分别维护。

## 验证与部署边界

```sh
PYTHONPATH=src:../obsidian-workflow/src \
  .venv/bin/python -m unittest discover -s tests -v
node --check src/chatgpt_pipeline/web/app.js
```

测试使用临时来源、工作区和模拟模型回复；部分 HTTP 测试需要允许绑定本机回环端口。

仓库包含内容处理程序及可选的部署辅助脚本。CLI 中的作者默认路径、生产启动器中的固定服务名及路径检查，是原部署示例；使用前显式指定自己的来源、输出、Vault 和密钥路径。`scripts/workbench.py start/stop` 会检查同名用户服务，首次使用优先采用上面的模块入口，不照搬生产服务配置。工作区和备份包含私人正文、模型输入及回复，应自行保护，不能提交到公开仓库。

## 本地配置与协作

代码直接在此仓库开发，通过标准 `git add`、`git commit`、`git push` 协作。真实数据、运行目录、凭据及本地部署说明由 `.gitignore` 排除；共享Skill可按需读取同目录的私有 `LOCAL.md`。个人资料不写入共享示例或默认配置。
