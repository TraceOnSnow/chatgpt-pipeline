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

也可以直接准备官方导出的 ZIP。适配器读取 `conversations.json` 或 `conversations-N.json`，记录 ZIP、成员、账号、会话和消息指纹，不解压媒体。`account-a` 是示例标识，同一个真实账号应持续使用同一个自定标识：

```sh
.venv/bin/python scripts/memory-import.py prepare \
  --zip /absolute/path/to/ChatGPT-export.zip \
  --out runtime/memory-example \
  --profile profiles/example.json --account-key account-a
```

[示例 profile](profiles/example.json) 是通用合成配置，只含筛选和复核提示；可以复制修改后用于新批次。prepare 离线冻结实际提示与 SHA，不会改写旧批次。可用 `--index runtime/index.sqlite3` 对照已有索引：只跳过同账号、同会话、同正文指纹的材料，不猜测不同账号的关联。附件和未知结构记录为缺口，有文字不表示理解了图片或音频。

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
