#!/usr/bin/env python3
"""语义事件层：服务端只输出“变化的数据”，展示格式由各客户端自行构建。

- API 模式（DP_API_MODE=1）：每个事件写成一行 JSON（见 core/console.emit），
  由 server/api.py 原样转成 SSE，客户端按类型渲染。
- 终端模式：这里用 rich/print 直接渲染，保持命令行观感不变。

新增事件只需在这里加一个 ev_xxx，终端渲染与 API 数据一次写好。
"""
import json

from .console import console, emit, render_table, _API_MODE


def _ev(kind, payload, terminal=None):
    """统一出口：API 模式发事件；终端模式调用 terminal() 渲染"""
    if _API_MODE:
        emit(kind, **payload)
    elif terminal is not None:
        terminal()


def ev_config(keys):
    """-k/-m 覆盖并写回 .env"""
    def t():
        print(f"⚙️  已更新 .env：{'、'.join(keys)}")
    _ev("config", {"keys": list(keys)}, t)


def ev_db(online):
    def t():
        print("🗄️  本地 SQLite + 在线 MySQL 同步" if online else "🗄️  使用本地 SQLite 数据库")
    _ev("db", {"online": bool(online)}, t)


def ev_session(action, session_id):
    """action: new | resume | saved"""
    def t():
        if action == "saved":
            print(f"\n✅ 对话已保存到会话: {session_id}")
    _ev("session", {"action": action, "id": session_id}, t)


def ev_memory(phase, found):
    """phase: profile(加载记忆索引) | recall(搜索相关记忆)"""
    def t():
        if phase == "profile":
            print("🔍 加载记忆索引...", end=" ", flush=True)
            print("完成" if found else "无")
        else:
            print("🔍 搜索相关记忆...", end=" ", flush=True)
            print("找到相关记忆，注入上下文" if found else "无相关记忆。")
    _ev("memory", {"phase": phase, "found": bool(found)}, t)


def ev_question(text):
    def t():
        print(f"\n👤 用户问题: {text}")
    _ev("question", {"text": text}, t)


def ev_reasoning_header():
    def t():
        console.rule("[dim]🤔 思考过程[/dim]")
        console.print()
    _ev("reasoning_header", {}, t)


def ev_tool_round(index, local, search):
    def t():
        print("\n" + "=" * 30)
        console.print(f"🔧 执行工具 (第{index}轮): {local} 个本地调用 / {search} 个服务端搜索", style="bold yellow")
    _ev("tool_round", {"index": index, "local": local, "search": search}, t)


def ev_continuing(index):
    def t():
        print("\n" + "=" * 30)
        console.print("🤔 继续推理...", style="dim italic")
    _ev("continuing", {"index": index}, t)


def ev_tool_call(name, arguments=None, raw=None, call_id=None, parse_error=None):
    payload = {"name": name, "id": call_id}
    if arguments is not None:
        payload["arguments"] = arguments
    if raw is not None:
        payload["raw"] = raw
    if parse_error:
        payload["parse_error"] = parse_error

    def t():
        if parse_error:
            print(f"⚠️ 工具 {name} 参数解析失败: {parse_error}")
        print(f"🔧 执行工具: {name}")
        if arguments is not None:
            print(f"📥 参数: {json.dumps(arguments, ensure_ascii=False)}")
    _ev("tool_call", payload, t)


def ev_tool_result(name, call_id, output, truncated=False, error=False):
    max_len = 2000
    full = output or ""
    payload = {
        "name": name, "id": call_id,
        "output": full[:max_len],
        "truncated": bool(truncated or len(full) > max_len),
        "error": bool(error),
    }

    def t():
        preview = full[:200] + ("..." if len(full) > 200 else "")
        print(f"📤 结果: {preview}")
    _ev("tool_result", payload, t)


def ev_search(state, call_id=None, first=False):
    """state: call(发起一次调用) | searching/completed/... (服务端状态)"""
    def t():
        if state == "call":
            if first:
                console.print("\n🔎 服务端网页搜索：", style="bold cyan")
            console.print(f"  - 搜索调用 {call_id} 已发起", style="cyan")
        else:
            console.print(f"  - 搜索状态: {state}", style="cyan")
    _ev("search", {"state": state, "id": call_id, "first": bool(first)}, t)


def ev_warning(message, code=None):
    payload = {"message": message}
    if code:
        payload["code"] = code

    def t():
        console.print(f"⚠️  {message}", style="bold red")
    _ev("warning", payload, t)


def ev_usage(model, peak, hit, miss, out, costs, total, balance, currency):
    payload = {
        "model": model, "peak": bool(peak),
        "hit": hit, "miss": miss, "out": out,
        "cost_hit": costs["hit"], "cost_miss": costs["miss"], "cost_out": costs["out"],
        "total": total, "balance": balance, "currency": currency or "",
    }

    def t():
        period = "高峰时段" if peak else "空闲时段"
        console.print(f"\n⏰ {period}（北京时间 周一至周五 9:00-12:00、14:00-18:00）", style="bold")
        rows = [
            ("输入(缓存命中)", f"{hit:,} tokens  ¥{costs['hit']:.4f}"),
            ("输入(缓存未命中)", f"{miss:,} tokens  ¥{costs['miss']:.4f}"),
            ("输出", f"{out:,} tokens  ¥{costs['out']:.4f}"),
        ]
        render_table(f"📊 Token 消耗统计 · {model} · 合计 ¥{total:.4f}", rows)
        if balance is not None:
            symbol = "¥" if currency == "CNY" else ""
            console.print(f"💰 账户余额：{symbol}{balance} {currency or ''}", style="bold green")
        else:
            console.print("💰 账户余额：查询失败", style="dim")
    _ev("usage", payload, t)
