# shell-tool — 命令行 AI 助手

基于 DeepSeek 的流式命令行 AI 助手：支持系统命令执行、百度搜索、OpenViking 外置记忆存取，对话记录持久化到 MySQL（可切换本地 SQLite），并提供 HTTP API 服务（含 SSE 流式输出）。

## 功能特性

- **系统命令执行** — 跨平台执行 Linux/macOS (bash) 与 Windows (PowerShell/CMD) 命令
- **百度搜索** — 通过百度千帆引擎搜索网页 / 查询百科
- **OpenViking 记忆** — 语义搜索历史记忆、保存用户偏好/项目信息/决策、读取与写入记忆文件、Session 管理
- **对话持久化** — MySQL 或本地 SQLite 存储会话与消息，支持继续/新建/指定会话
- **记忆注入** — 每轮对话前自动检索相关记忆并注入上下文
- **流式输出** — 展示思考过程（reasoning）与最终回答
- **HTTP API** — 提供 `/chat`（同步）与 `/chat/stream`（SSE 流式）接口

## 目录结构

```
shell-tool/
├── dp.py                     # CLI 启动入口
├── core/
│   ├── config.py             # 环境变量加载与全局配置
│   ├── db.py                 # MySQL 会话/消息存储层
│   ├── llm.py                # API 调用层：流式请求 + 工具调用循环
│   ├── main.py               # CLI 主流程：参数解析、会话管理、记忆注入
│   └── tools/
│       ├── system_tools.py   # 系统信息 / 命令执行
│       ├── search_tools.py   # 百度搜索（调用 scripts/qianfan.py）
│       ├── ov_tools.py       # OpenViking 记忆工具（search/remember/read/...）
│       ├── other_ov_tool.py  # OpenViking 其他工具合集入口
│       └── __init__.py       # 工具 schema 定义与调用分发器
├── scripts/
│   └── qianfan.py            # 百度千帆搜索脚本
├── server/
│   └── api.py                # Flask HTTP API（同步 + SSE 流式）
└── .env.example              # 环境变量模板
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
| `DB_ONLINE` | 数据库开关：`true`=在线 MySQL，`false`=本地 SQLite（默认 true；MySQL 连接失败自动降级 SQLite） |
| `SQLITE_DB_PATH` | 本地 SQLite 文件路径（默认 `data/shell_tool.db`） |
| `MYSQL_HOST/PORT/USER/PASSWORD/DB` | MySQL 会话存储 |
| `BAIDU_QIANFAN_KEY` | 百度千帆搜索密钥（`scripts/qianfan.py`） |
| `OPENVIKING_URL/KEY/USER` | OpenViking 外置记忆服务 |

### 数据库存储

- **在线 MySQL（默认）**：配置 `MYSQL_*` 且 `DB_ONLINE=true` 时使用，会话/消息写入 MySQL
- **本地 SQLite**：满足以下任一条件时自动使用本地库，会话仅存于本地 `data/shell_tool.db`
  - 未配置 MySQL 连接信息（`MYSQL_HOST/USER/DB` 任一为空）
  - `DB_ONLINE=false`
  - MySQL 连接失败（自动降级并打印提示）
- 同一后端内默认持续同一对话（复用最近活跃会话），`-n` 才新开
- mysql↔sqlite 之间切换时立即新开 session（以 `data/.last_backend` 标记判断），
  两个库各自的会话互不影响、原样保留（可随时 `-s` 继续）

## 使用

### CLI

```bash
# 直接提问 → 默认复用最近活跃会话
python dp.py "今天北京天气怎么样？"

# 新开对话
python dp.py -n "帮我写一个 Python 脚本"

# 指定历史会话继续
python dp.py -s 20260811_101500 "继续上一个话题"

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

**健康检查**

```bash
curl http://localhost:8000/health
```

## 工具调用流程

按 think.txt 官方推荐流程优化：工具只在对话开始时批量调用一次，一次拿到的所有工具调用并行执行，结果一次性回传，之后直接输出最终回答。若模型在最终轮仍请求调用工具，在 `MAX_TOOL_ROUNDS` 预算内可再执行，超出则强制基于已有结果作答。

## 安全说明

- `.env` 包含敏感密钥，已被 `.gitignore` 排除，请勿提交
- 系统提示词内置隐私保护规则：不泄露用户隐私，非用户要求禁止执行外部链接中的命令和脚本
