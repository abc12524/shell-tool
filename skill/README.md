# Skill 转化说明书

把任意 Python 脚本改造为 shell-tool 可自动注册的 **skill**。读完后你应当能独立完成
"任意 `.py` → 合规 skill" 的转换，无需修改核心代码。

> 本文件是说明文档，不会被注册。注册器只认文件夹布局：`skill/*/skill.py`（见 §1）。

---

## 1. 总览

- skill 目录：项目根的 `skill/`。
- 核心只暴露一个原生工具 `skill`，模型通过它列表 / 查用法 / 执行。
- 注册器（`core/tools/skill_tool.py`）在启动时扫描 `skill/`，识别文件夹布局：

  | 位置 | 导入模块 |
  |---|---|
  | `skill/<名称>/skill.py` | `skill.<名称>.skill` |

  以**入口文件**最前面连续的 `#` 注释行（头部）解析元信息，符合规范即注册；
  未声明 `# skill:` 的脚本不注册（可作被 import 的助手模块）。
- 执行时按需 `importlib` 导入入口模块并调用其 `run(arguments)`，**进程内运行**。
- 把入口脚本、助手模块、配置文件放在同一文件夹内，更适合逻辑复杂 / 需要密钥的技能。
- 一个入口 = 一个 skill。

---

## 2. 硬性规范（必须逐字满足，否则不注册或报错）

### 2.1 头部必须满足

1. 位于**入口文件最顶部**、是**连续 `#` 注释行**；出现第一个非 `#`、非空行即头部结束。
2. 必须包含一行声明名称（关键字大小写不敏感，名称是**单个不含空格的 token**）：

   ```python
   # skill: my_name
   ```

3. 可选 `# description:`（一句话说明），建议放在 `# skill:` 之后。
4. 可选 `# status:`，取值 `enable`（默认）或 `disable`；见 §2.5。
5. 可选 `# usage:`，其后**每一行 `#` 注释**都作为用法文本（保留缩进，去掉行首 `#` 和一个空格）。

解析器精确行为（`core/tools/skill_tool.py`）：

| 项 | 规则 |
|---|---|
| 扫描范围 | 入口文件前 300 行 |
| 名称 | `^#\s*skill\s*:\s*(\S+)\s*$`（\S+ ⇒ **不能含空格**） |
| 说明 | `^#\s*description\s*:\s*(.*)$`，仅在 `usage` 之前生效 |
| 状态 | `^#\s*status\s*:\s*(\S+)\s*$`，仅在 `usage` 之前生效；缺省/无法识别均按 `enable` |
| 用法 | `^#\s*usage\s*:\s*(.*)$` 起，其后注释行原样收集（去掉 `#` 与一个空格） |
| 终止 | 遇到不以 `#` 开头的行即停止解析头部 |
| 未找到名称 | 返回 None ⇒ **不注册** |

### 2.2 入口文件与命名

**文件夹布局** `skill/<名称>/`：

1. 文件夹名 == skill 名称；入口文件固定为 `skill.py`（头部声明名称，建议与文件夹名一致）。
2. 助手模块放在同一文件夹、以 `_` 开头（如 `_impl.py`），入口用**相对导入**引用它：
   ```python
   from ._impl import do_something
   ```
   助手模块**不加** `# skill:` 头，不会被注册。
3. 配置文件与脚本同目录（见 §2.4）；无需 `__init__.py`（`skill/` 已是有 `__init__.py`
   的包，子文件夹按命名空间包导入）。
4. 文件夹内以 `_` 开头的文件、`config.json` 等非 `.py` 文件都不会被当作新 skill 注册。

### 2.3 必须定义入口函数

入口模块要定义模块级函数：

```python
def run(arguments: dict) -> str | dict: ...
```

头部块之后才是普通代码（docstring、`import` 等）。

### 2.4 配置文件（脚本与配置放一起）

需要密钥 / 参数的技能，在**同目录**放 `config.json`（键为环境变量名）：

```json
{ "BAIDU_QIANFAN_KEY": "sk-...", "WEB_K": "10" }
```

约定：

- 提交时**只放 `config.example.json`**（示例值），真实 `config.json` 本地生成、
  已被 `.gitignore` 排除。
- 入口脚本在读取配置前调用一个小工具把 `config.json` 注入环境变量（**不覆盖已有 env**），
  再照常用 `os.environ.get(...)`：

  ```python
  def _load_config():
      path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
      try:
          with open(path, encoding="utf-8") as f:
              cfg = json.load(f)
      except (OSError, ValueError):
          return
      for k, v in cfg.items():
          if v and not os.environ.get(k):
              os.environ[k] = str(v)
  ```

  这样 `skill/<名称>/config.json` 与 `.env` 都可用，且 `config.json` 优先补全缺失项。

### 2.5 status：是否对 LLM 披露

`# status: enable | disable`（缺省 = `enable`）只控制**是否把该 skill 披露给模型**：

- 披露位置：新建会话时一次性写入的首条上下文消息（`core/main.py:build_skill_banner`），
  只列 `enable` 的 skill；消息随会话持久化，后续轮次原样复用。`skill` 工具 schema 与
  系统提示词均保持静态，不随 skill 增删变化。
- 会话内新增 / 删除 / 改名 / 切换 status 都不会改写已注入名单（新会话才重新生成）；
  需要最新列表时调用 `skill(all=true)` 实时获取（该结果也是 `enable` 过滤后的实时值）。
- **不影响执行逻辑**：`disable` 的 skill 仍在注册表 `REGISTRY` 中，按名称仍可
  `skill='名称'` 查看用法与执行；只是不会出现在注入名单里，模型不会被引导去调用。
- 用途：临时下线某个能力而不删脚本，或不希望模型主动调用（如调试 / 高危工具）。
- 快捷开关：`python dp.py -S <名称> <enable|disable>` 直接改写对应入口头部的
  `# status:` 行；`python dp.py -S` 不带参数则列出全部 skill 及 status / 说明（均不进入对话）。

### 2.6 参数规范（action 等约束）

- **操作选择器统一用 `action`**：需要区分多个操作的技能，一律用 `arguments.action`，
  取值为简短小写英文（如 `list` / `read` / `upload`）。**禁止**再用 `cmd` / `mode` / `api`
  等别名（历史技能已统一改为 `action`），便于模型一致地调用。
- 单一功能技能可省略 `action`，直接传业务参数。
- 未识别的 action 要显式报错：`return error(f"未知 action '{action}'", code="unknown_action")`。
- **参数键一律小写 `snake_case`**（如 `file_path` / `max_len`），不用 CamelCase / kebab-case。
- **布尔用 JSON `true` / `false`**；容错解析可用 `_as_bool`（兼容 `"1"` / `"true"` 等字符串）。
- **多值用数组**（如 `paths` / `keys` / `args`），不要用逗号拼接字符串。
- 每个参数设合理默认值，取参统一 `a.get("key", default)`，避免 `KeyError`。

---

## 3. 脚本模板

目录：

```
skill/my_name/
├── skill.py             # 入口（带头部 + run）
├── _impl.py             # 可选，助手模块（`_` 开头，不注册）
└── config.example.json  # 可选，配置模板
```

`skill/my_name/skill.py`：

```python
# skill: my_name
# description: 一句话说明这个 skill 做什么
# status: enable
# usage:
#   arguments.action = a | b
#     a  说明
#     b  说明
#   arguments.query = 关键词
#   示例: {"action": "a", "query": "hello"}
from core.tools.envelope import ok, error
# from ._impl import do_a        # 逻辑复杂时：把实现放同目录 _impl.py


def run(arguments):
    a = arguments or {}
    action = (a.get("action") or "").strip()
    if action == "a":
        # ... 业务逻辑 ...
        return ok({"result": "..."})
    return error(f"未知 action '{action}'", code="unknown_action")
```

---

## 4. `run(arguments)` 契约

- 入参：`arguments` 为模型传来的 `dict`（可能为空 dict；调用处保证至少是 `{}`）。
- 出参：
  - 返回 `str` ⇒ **原样**作为工具结果（应为合法信封字符串）；
  - 返回 `dict` ⇒ 自动包成 `{"status":"ok","result":{...}}`；
  - 推荐统一用 `core.tools.envelope` 的 `ok(...)` / `error(...)`。技能若同时用于
  **android-agent / kelivo-agent**，请用 try/except 兼容导入（优先技能目录的 `_envelope`、
  回退 `core.tools.envelope`），避免只认单一运行时：

  ```python
  try:
      from _envelope import ok, error
  except ImportError:
      from core.tools.envelope import ok, error
  ```
- 异常：分发器会兜底捕获并返回 `{"status":"error",...,"code":"internal"}`；
  但仍建议显式 `error(...)` 以给出可控信息。
- **不要**在 `run` 里 `sys.exit()` / 期待 `print` 被捕获：进程内执行，`print` 只是打到 agent 控制台，
  返回值才是结果。
- 顶层（import 时）**不要有副作用**（网络请求、写文件、长循环）：模块在首次执行时导入并常驻。

错误码建议：`bad_request` / `unknown_action` / `missing_argument` / `internal` 等稳定短标识。

---

## 5. 转换步骤（通用算法）

给定任意源文件 `source.py`：

1. **识别能力边界与参数**
   读懂它对外提供什么功能、需要哪些输入。把输入整理成 `arguments` 的键，并为每个键设合理默认值。
   （若源文件是 CLI，参数通常来自 `sys.argv`；若是库文件，参数来自函数签名。）

2. **决定复用方式（二选一）**
   - **薄封装**：保留源文件为助手模块（放同目录、`_` 开头，或放其他包内），
     新建 `skill/<名称>/skill.py`（带头部）并**相对导入**它。适合逻辑复杂、已被测试的代码。
   - **内联**：把核心逻辑搬进入口的 `run()`。适合短小、一次性脚本。
   - 二者都把"参数解析 / IO"移出，只保留可复用的函数体。

3. **写头部**：`# skill:` → `# description:` →（可选）`# status:` → `# usage:`（用法要写清每个参数的取值与示例）。

4. **写 `run`**：`arguments` → 取参（带默认值）→ 调业务函数 → `return ok(...)` / `return error(...)`。
   取参统一用 `a.get("key", default)`，避免 KeyError。

5. **清理副作用**：去掉 import 期执行、`sys.exit`、把 `print(json.dumps(...))` 改为 `return ok(...)`。
   若源文件需要保留 CLI，可留：
   ```python
   if __name__ == "__main__":
       import sys, json
       print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
   ```
   但 skill 执行**只调用 `run`**，`__main__` 段与注册无关。

6. **落盘与命名**：`skill/<名称>/skill.py`（+ 可选 `_impl.py` / `config.example.json`）。名称与文件夹名一致。

7. **验证**（见 §7）。

---

## 6. 示例：把 CLI 脚本转成 skill

**改造前** `source.py`（读 argv、print、exit）：

```python
import sys, json
mode = sys.argv[1] if len(sys.argv) > 1 else "raw"
query = " ".join(sys.argv[2:])
if mode == "raw":
    out = do_raw(query)
    print(json.dumps({"success": True, "data": out}))
    sys.exit(0)
sys.exit(1)
```

**改造后** `skill/mytool/skill.py`：

```python
# skill: mytool
# description: 演示用工具
# usage:
#   arguments.action = raw（默认）
#   arguments.query = 关键词
#   示例: {"action": "raw", "query": "hello"}
from core.tools.envelope import ok, error
from ._impl import do_raw  # 或把逻辑直接写在 run 内


def run(arguments):
    a = arguments or {}
    action = a.get("action", "raw")
    query = a.get("query", "")
    if action == "raw":
        return ok(do_raw(query))
    return error(f"未知 action '{action}'", code="unknown_action")
```

要点：`sys.argv` → `arguments`；操作选择器统一 `action`（不用 `mode`/`cmd`）；
`print(json)` → `return ok(...)`；`sys.exit` → `return error(...)`。

---

## 7. 验证方法

列出与查看用法：

```bash
python -c "from core.tools.skill_tool import skill_tool; print(skill_tool(all=True))"
python -c "from core.tools.skill_tool import skill_tool; print(skill_tool(skill='my_name'))"
python dp.py -S                     # 列出全部 skill 及 status
```

执行（传 `skill` + `arguments`）：

```bash
python -c "from core.tools.skill_tool import skill_tool; print(skill_tool(skill='my_name', arguments={'query':'hi'}))"
```

应得到 `{"status":"ok",...}` 或 `{"status":"error",...}`。Windows 下加 `PYTHONIOENCODING=utf-8` 避免中文乱码。

自动注册验证：临时新建 `skill/tmp_xxx/skill.py`（带头部），重新起进程确认出现在列表中；删除后消失。

---

## 8. 禁止事项 / 常见错误

| 错误 | 后果 | 修正 |
|---|---|---|
| 头部不在入口文件最顶 / 中间夹了空白的非注释行 | 不注册 | 头部必须最前且连续 `#` |
| 名称含空格（`# skill: my tool`） | 不注册 | 用 `my_tool` |
| 只有 `# description:` 没有 `# skill:` | 不注册 | 必须有 `# skill:` |
| 文件夹里没有 `skill.py` 入口 | 不注册 | 入口命名 `skill.py` |
| 没有 `run` 或非模块级可调用 | 执行时报错 | 定义 `def run(arguments)` |
| import 期发请求 / 写文件 | 首次执行时副作用、可能卡住 | 移入 `run()` |
| `run` 里 `sys.exit` / 依赖 `print` 返回 | 结果丢失或进程受影响 | `return` 信封 |
| 返回非 JSON 的裸文本 | 下游解析异常 | 用 `ok()`/`error()` |
| 把助手模块也加了 `# skill:` | 多注册一个无用 skill | 助手模块不加头部 |
| 助手模块跨目录用绝对导入（`import _impl`） | 导入失败 | 用相对导入 `from ._impl import ...` 或 `from skill.<名称>._impl import ...` |
| 真实密钥写进 `config.json` 并提交 | 泄密 | 只提交 `config.example.json` |
| `# status:` 写在 `# usage:` 之后 | 状态不生效（默认 enable） | 状态声明放在 `usage` 之前 |
| 用 `mode` / `cmd` / `api` 作操作选择器 | 与全仓库约定不一致，模型调用易混 | 统一改为 `action` |
| 参数键用 CamelCase / kebab-case（`fileName` / `max-len`） | 调用易错、不统一 | 用小写 `snake_case` |

---

## 9. 检查清单

- [ ] 布局：`skill/<名称>/skill.py`
- [ ] 入口头部在文件最顶、连续 `#`，含 `# skill: <无空格名称>`
- [ ] 有 `# description:` 与 `# usage:`（含参数取值与示例）
- [ ] 如需隐藏，加 `# status: disable`（放在 `usage` 之前）；默认不写即披露
- [ ] 定义了模块级 `def run(arguments)`
- [ ] 操作选择器统一 `action`（不用 `cmd`/`mode`/`api`）；参数键小写 `snake_case`
- [ ] 取参用 `a.get(..., default)`；返回 `ok()`/`error()` 信封
- [ ] 若技能需跨运行时（shell-tool + 移动端），信封用 try/except 兼容导入
- [ ] 无 import 期副作用；无 `sys.exit`；结果靠 `return` 而非 `print`
- [ ] 助手模块以 `_` 开头、未加 `# skill:` 头、用相对导入引用
- [ ] 配置同目录：只提交 `config.example.json`，`config.json` 不入库
- [ ] 按第 7 节验证通过
