# Skill 转化说明书

把任意 Python 脚本改造为 shell-tool 可自动注册的 **skill**。读完后你应当能独立完成
"任意 `.py` → 合规 skill" 的转换，无需修改核心代码。

> 本文件是说明文档，不会被注册（注册器只扫描 `skill/*.py`）。

---

## 1. 总览

- skill 目录：项目根的 `skill/`。
- 核心只暴露一个原生工具 `skill`，模型通过它列表 / 查用法 / 执行。
- 注册器（`core/tools/skill_tool.py`）在启动时扫描 `skill/*.py`：
  - 读文件**最前面连续的 `#` 注释行**（头部），符合规范即注册；
  - 未声明 `# skill:` 的脚本不注册（可作被 import 的助手模块）。
- 执行时按需 `importlib` 导入 `skill.<文件名>` 并调用其 `run(arguments)`，**进程内运行**。
- 一个脚本 = 一个 skill（`ov` / `script` / `search` 就是这样各占一项）。

---

## 2. 硬性规范（必须逐字满足，否则不注册或报错）

### 2.1 头部必须满足

1. 位于**文件最顶部**、是**连续 `#` 注释行**；出现第一个非 `#`、非空行即头部结束。
2. 必须包含一行声明名称（关键字大小写不敏感，名称是**单个不含空格的 token**）：

   ```python
   # skill: my_name
   ```

3. 可选 `# description:`（一句话说明），建议放在 `# skill:` 之后。
4. 可选 `# usage:`，其后**每一行 `#` 注释**都作为用法文本（保留缩进，去掉行首 `#` 和一个空格）。

解析器精确行为（`core/tools/skill_tool.py`）：

| 项 | 规则 |
|---|---|
| 扫描范围 | 文件前 300 行 |
| 名称 | `^#\s*skill\s*:\s*(\S+)\s*$`（\S+ ⇒ **不能含空格**） |
| 说明 | `^#\s*description\s*:\s*(.*)$`，仅在 `usage` 之前生效 |
| 用法 | `^#\s*usage\s*:\s*(.*)$` 起，其后注释行原样收集（去掉 `#` 与一个空格） |
| 终止 | 遇到不以 `#` 开头的行即停止解析头部 |
| 未找到名称 | 返回 None ⇒ **不注册** |

### 2.2 脚本必须满足

1. 文件名 `*.py`，且**不以 `_` 开头**（`__init__.py` / `_helper.py` 会被跳过）。
2. 推荐**文件名 stem == skill 名称**（注册键取头部名称，模块按文件名导入；一致可避免混淆）。
3. 定义模块级函数：

   ```python
   def run(arguments: dict) -> str | dict: ...
   ```

4. 头部块之后才是普通代码（docstring、`import` 等）。

---

## 3. 脚本模板

```python
# skill: my_name
# description: 一句话说明这个 skill 做什么
# usage:
#   arguments.action = a | b
#     a  说明
#     b  说明
#   arguments.query = 关键词
#   示例: {"action": "a", "query": "hello"}
from core.tools.envelope import ok, error


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
  - 推荐统一用 `core.tools.envelope` 的 `ok(...)` / `error(...)`。
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
   - **薄封装**：保留源文件为助手模块（**加在 `skill/` 里但不加头部**，或放在其他包内），
     新建 `skill/<name>.py`（带头部）并 `import` 它。适合逻辑复杂、已被测试的代码。
   - **内联**：把核心逻辑搬进 `skill/<name>.py` 的 `run()`。适合短小、一次性脚本。
   - 二者都把"参数解析 / IO"移出，只保留可复用的函数体。

3. **写头部**：`# skill:` → `# description:` → `# usage:`（用法要写清每个参数的取值与示例）。

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

6. **命名与落盘**：`skill/<name>.py`，名称与文件名 stem 一致。

7. **验证**（见第 8 节）。

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

**改造后** `skill/mytool.py`：

```python
# skill: mytool
# description: 演示用工具
# usage:
#   arguments.mode = raw（默认）
#   arguments.query = 关键词
#   示例: {"mode": "raw", "query": "hello"}
from core.tools.envelope import ok, error
from mytool_impl import do_raw  # 或把逻辑直接写在 run 内


def run(arguments):
    a = arguments or {}
    mode = a.get("mode", "raw")
    query = a.get("query", "")
    if mode == "raw":
        return ok(do_raw(query))
    return error(f"未知 mode '{mode}'", code="unknown_action")
```

要点：`sys.argv` → `arguments`；`print(json)` → `return ok(...)`；`sys.exit` → `return error(...)`。

---

## 7. 禁止事项 / 常见错误

| 错误 | 后果 | 修正 |
|---|---|---|
| 头部不在文件最顶 / 中间夹了空白的非注释行 | 不注册 | 头部必须最前且连续 `#` |
| 名称含空格（`# skill: my tool`） | 不注册 | 用 `my_tool` |
| 只有 `# description:` 没有 `# skill:` | 不注册 | 必须有 `# skill:` |
| 文件名以 `_` 开头 | 被跳过 | 改名 |
| 没有 `run` 或非模块级可调用 | 执行时报错 | 定义 `def run(arguments)` |
| import 期发请求 / 写文件 | 首次执行时副作用、可能卡住 | 移入 `run()` |
| `run` 里 `sys.exit` / 依赖 `print` 返回 | 结果丢失或进程受影响 | `return` 信封 |
| 返回非 JSON 的裸文本 | 下游解析异常 | 用 `ok()`/`error()` |
| 把助手模块也加了 `# skill:` | 多注册一个无用 skill | 助手模块不加头部 |

---

## 8. 验证方法

列出与查看用法：

```bash
python -c "from core.tools.skill_tool import skill_tool; print(skill_tool(all=True))"
python -c "from core.tools.skill_tool import skill_tool; print(skill_tool(skill='my_name'))"
```

执行（传 `skill` + `arguments`）：

```bash
python -c "from core.tools.skill_tool import skill_tool; print(skill_tool(skill='my_name', arguments={'query':'hi'}))"
```

应得到 `{"status":"ok",...}` 或 `{"status":"error",...}`。Windows 下加 `PYTHONIOENCODING=utf-8` 避免中文乱码。

自动注册验证：临时新建 `skill/tmp_xxx.py`（带头部），重新起进程确认出现在列表中；删除后消失。

---

## 9. 检查清单

- [ ] 文件在 `skill/`，`*.py`，不以 `_` 开头
- [ ] 头部在文件最顶、连续 `#`，含 `# skill: <无空格名称>`
- [ ] 有 `# description:` 与 `# usage:`（含参数取值与示例）
- [ ] 定义了模块级 `def run(arguments)`
- [ ] 取参用 `a.get(..., default)`；返回 `ok()`/`error()` 信封
- [ ] 无 import 期副作用；无 `sys.exit`；结果靠 `return` 而非 `print`
- [ ] 助手模块未加 `# skill:` 头
- [ ] 按第 8 节验证通过
