# shell-tool — 命令行 AI 助手

基于 DeepSeek 的流式命令行 AI 助手：支持系统命令执行、百度搜索、OpenViking 外置记忆存取，对话记录持久化到 MySQL（可切换本地 SQLite），并提供 HTTP API 服务（含 SSE 流式输出）。

## 功能特性

- **系统命令执行** — 跨平台执行 Linux/macOS (bash) 与 Windows (PowerShell/CMD) 命令
- **技能体系（skill）** — 除系统工具外，OpenViking 记忆（`ov`）、脚本/代码编辑（`script`）、百度搜索（`baidu_search`）均下沉为 `skill/` 目录脚本；按脚本头部标准结构自动注册，新增能力只需新增脚本，无需改动核心
- **脚本增删改查** — 数据源为本地 `file_path`（可写，改后写回并生成 `.bak` 备份）或 http(s) `url`（只读），工具自动探测编码（utf-8/gb18030/big5 等）；read 支持按 `symbol`（单个/数组）/`pattern` 正则批量、按 `start_line~end_line` 取片段（带行号、可 `limit` 分页），或返回带行号的 `outline` 骨架；改/增/删采用精确字符串替换（对齐 edit 工具，唯一匹配、支持 `replace_all`）
- **百度搜索** — 通过百度千帆引擎搜索网页 / 查询百科
- **OpenViking 记忆** — 语义搜索历史记忆、保存用户偏好/项目信息/决策、读取与写入记忆文件、Session 管理
- **对话持久化** — 先落本地 SQLite（唯一读写源），再幂等同步到在线 MySQL，支持继续/新建/指定会话
- **记忆注入** — 每轮对话前自动检索相关记忆并注入上下文
- **流式输出** — 展示思考过程（reasoning）与最终回答
- **HTTP API** — 提供 `/chat`（同步）与 `/chat/stream`（SSE 流式）接口

## 目录结构

```
shell-tool/
├── dp.py                     # CLI 启动入口
├── core/
│   ├── config.py             # 环境变量加载与全局配置
│   ├── db.py                 # 存储层：本地 SQLite 唯一读写源 + 在线 MySQL 幂等同步
│   ├── llm.py                # API 调用层：流式请求 + 工具调用循环
│   ├── main.py               # CLI 主流程：参数解析、会话管理、记忆注入
│   └── tools/
│       ├── system_tools.py   # 系统信息 / 命令执行（原生 tool）
│       ├── skill_tool.py     # skill 注册表：扫描 skill/ 头部自动注册 + 分发执行
│       ├── ov_tools.py       # OpenViking 记忆底层实现（含自动召回/捕获等生命周期逻辑）
│       └── __init__.py       # 原生工具 schema（get_system_info/execute_system_command/skill）+ 分发器
├── skill/                    # skill 脚本目录：按头部标准结构自动注册
│   ├── ov.py                 # skill: ov           —— OpenViking 记忆统一入口
│   ├── script.py             # skill: script       —— 脚本/代码增删改查
│   ├── _script_impl.py       # script 助手实现（`_` 开头，不注册）
│   └── baidu_search.py       # skill: baidu_search —— 百度搜索 / 百科（千帆，进程内直连）
├── server/
│   └── api.py                # Flask HTTP API（同步 + SSE 流式）
└── .env.example              # 环境变量模板

### 新增 skill

在 `skill/` 目录新增一个 `.py` 脚本，文件头部按标准结构声明即可自动注册，无需改动核心：

```python
# skill: my_skill
# description: 一句话说明
# usage:
#   arguments.foo = ...
#   示例: {"foo": "bar"}
def run(arguments: dict) -> str:
    # 返回统一信封字符串（ok()/error()）
    ...
```

模型侧只暴露一个 `skill` 工具：`all=true` 列出全部 skill，传 `skill=<名称>` 查看用法，
传 `skill=<名称>` + `arguments={...}` 执行。未声明 `# skill:` 的脚本不会被注册。
```

## 安装

```bash
git clone git@github.com:abc12524/shell-tool.git
cd shell-tool
pip install -r requirements.txt   # openai, requests, pymysql, flask, python-dotenv
```

复制环境变量模板并填写配置：

```bash
cp .env.example .env
```

### 环境变量

| 变量 | 说明 |
|------|------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `DEEPSEEK_BASE_URL` | DeepSeek API 地址（默认 `https://api.deepseek.com`） |
| `DEEPSEEK_MODEL` | 模型名（默认 `deepseek-v4-flash`） |
| `MAX_TOOL_ROUNDS` | 工具调用最大轮数（默认 6） |
| `LLM_WEB_SEARCH` | LLM 内置联网搜索（DeepSeek Responses API 自带 web_search）开关：`false`=关闭（默认），`true`=开启 |
| `DB_ONLINE` | 在线 MySQL 同步开关：`true`=开启同步（默认 true；未配置或连接失败则仅用 SQLite），`false`=关闭 |
| `SQLITE_DB_PATH` | 本地 SQLite 文件路径（唯一读写源，默认 `data/shell_tool.db`） |
| `MYSQL_HOST/PORT/USER/PASSWORD/DB` | 在线 MySQL（同步副本）连接信息 |
| `BAIDU_QIANFAN_KEY` | 百度千帆搜索密钥（`skill/baidu_search.py`） |
| `OPENVIKING_URL/KEY/USER` | OpenViking 外置记忆服务 |

### 数据库存储

- **本地 SQLite（唯一读写源）**：无论是否启用在线库，会话/消息都先写入本地 `data/shell_tool.db`；读写均以本地库为准。
- **在线 MySQL（同步副本）**：配置 `MYSQL_*` 且 `DB_ONLINE=true` 时，本地落库成功后把未同步的行幂等推送（upsert）到 MySQL。
  - **不重复**：每条消息带全局唯一 `uid`，MySQL 端对 `uid` 建唯一索引，重复推送不会产生重复行。
  - **不丢失**：每条记录带 `synced` 标记，推送失败保持未同步，下次启动/写入时自动重试。
  - MySQL 未配置 / `DB_ONLINE=false` / 连接失败 → 仅用本地 SQLite，功能不受影响。
- 默认持续同一对话（复用最近活跃会话），`-n` 才新开；`-s <id>` 可继续任意历史会话。

## 使用

### CLI

```bash
# 直接提问 → 默认复用最近活跃会话
python dp.py "今天北京天气怎么样？"

# 新开对话
python dp.py -n "帮我写一个 Python 脚本"

# 指定历史会话继续
python dp.py -s 20260811_101500 "继续上一个话题"

# 列出最近 5 条会话
python dp.py -s

# 覆盖并写回 .env 的密钥 / 模型（本次及后续运行均生效）
python dp.py -k sk-xxxxxxxx
python dp.py -m deepseek-v4-pro "换个模型回答"

# 查看帮助
python dp.py
```

### HTTP API

```bash
python server/api.py   # 监听 0.0.0.0:8000
```

**同步对话**

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "1+1=?", "new": true}'
```

**流式对话（SSE）**

```bash
curl -N -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "解释一下什么是量子计算", "new": true}'
```

请求体可选字段 `key` / `model`：传入即覆盖并写回服务端 `.env` 的 `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL`，对本次及后续请求均生效（等价于 CLI 的 `-k` / `-m`）。

```bash
curl -N -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "换个模型回答", "model": "deepseek-v4-pro"}'
```

**健康检查**

```bash
curl http://localhost:8000/health
```

#### 流式事件协议（供自建客户端 / 移动端）

服务端只发「变化的数据」，展示格式（emoji / 颜色 / 表格）由客户端自行构建；`client/dp_client.py` 即参考实现。`/chat/stream` 的 SSE `data:` 为 JSON，均带 `type`：

| type | 关键字段 | 说明 |
|------|----------|------|
| `start` / `done` | — | 会话开始 / 正常结束 |
| `content` | `content` | 正文增量（Markdown） |
| `reasoning` | `content` | 思考过程增量 |
| `question` | `text` | 用户问题回显 |
| `db` | `online` | 数据库模式 |
| `session` | `action`(new/resume/saved), `id` | 会话状态 |
| `memory` | `phase`(profile/recall), `found` | 记忆加载 / 召回 |
| `config` | `keys` | `-k`/`-m` 更新的 .env 项 |
| `reasoning_header` | — | 思考过程分隔 |
| `tool_round` | `index`, `local`, `search` | 工具轮次 |
| `continuing` | `index` | 继续推理 |
| `tool_call` | `name`, `arguments`, `id`, `parse_error` | 工具调用 |
| `tool_result` | `name`, `id`, `output`, `truncated`, `error` | 工具结果（output 截断至 2000 字符） |
| `search` | `state`, `id`, `first` | 服务端网页搜索 |
| `warning` | `message`, `code` | 警告 |
| `usage` | `model`,`peak`,`hit`,`miss`,`out`,`cost_*`,`total`,`balance`,`currency` | token / 费用 / 余额 |
| `log` | `text` | 未结构化输出的兜底 |
| `error` | `error`, `code` | 出错（客户端应非零退出） |

`:` 开头的行为注释 / 心跳，忽略。

## 工具调用流程

对话开始时批量并行执行模型请求的全部工具调用，结果一次性回传后直接输出最终回答；若模型在最终轮仍请求调用工具，在 `MAX_TOOL_ROUNDS` 预算内可再执行，超出则强制基于已有结果作答。所有工具统一返回规范信封 `{"status":"ok","result":{...}}` / `{"status":"error","error":...}`，与 OpenViking 后端及 DSH 会话格式保持一致。

模型面向的原生工具只有 `get_system_info`、`execute_system_command`、`skill` 三个；其中 `skill` 负责列出 / 查看用法 / 执行 `skill/` 目录下的脚本（`ov` / `script` / `baidu_search`），脚本在进程内执行，新增脚本即自动注册。

## 安全说明

- `.env` 包含敏感密钥，已被 `.gitignore` 排除，请勿提交
- 系统提示词内置隐私保护规则：不泄露用户隐私，非用户要求禁止执行外部链接中的命令和脚本
