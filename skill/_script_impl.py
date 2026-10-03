#!/usr/bin/env python3
"""脚本类工具：script_editor —— 面向文件的脚本 增 / 删 / 改 / 查（CRUD）+ 结构骨架

数据来源只支持 file_path（本地文件）或 url（http/https，只读），由工具自行读取与编码探测，
输出统一 UTF-8；本地文件修改会写回原路径，并先生成 .bak 备份。

- 编码自动探测（utf-8 / BOM / gb18030 / big5 / latin-1 兜底），换行统一为 '\\n'，写回还原原换行；
- read：支持 symbol（单个或多个）/ pattern（正则）批量、start_line~end_line 行范围，输出带行号；
- outline：返回结构骨架，每个符号带起止行号，便于随后按范围精确读；
- 大文件 / 大范围输出按 limit 限长，并给出续读提示，避免被整体截断；
- 编辑采用「精确字符串替换」，对齐自带 edit 工具：唯一匹配才执行，多处匹配需 replace_all=true。

语言：python / java / kotlin / c / cpp / csharp / javascript / typescript / shell
"""
import ast
import difflib
import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

import requests

from core.tools.envelope import ok, error


# ============================ 结构解析（供 outline） ============================

@dataclass
class SymbolRange:
    """一个可定位符号的范围（1-based，含装饰器/注解）"""
    name: str
    kind: str          # function | class | method
    start: int
    end: int
    signature: str     # 单行签名，用于骨架输出


@dataclass
class ParseResult:
    language: str
    lines: int
    imports: List[str]
    globals: List[str]
    symbols: List[SymbolRange]
    calls: Dict[str, Set[str]]


class PythonParser:
    """Python 解析器：使用 ast，得到最精确的符号范围与调用关系"""

    def __init__(self, code: str):
        self.code = code
        self.lines = code.splitlines()
        self.tree = ast.parse(code)

    def parse(self) -> ParseResult:
        imports: List[str] = []
        globals_: List[str] = []
        symbols: List[SymbolRange] = []
        calls: Dict[str, Set[str]] = {}

        for node in self.tree.body:
            if isinstance(node, ast.Import):
                imports += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                imports.append(f"{mod}({','.join(a.name for a in node.names)})")
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        globals_.append(t.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(self._func_sym(node))
                calls[node.name] = self._calls_of(node)
            elif isinstance(node, ast.ClassDef):
                symbols.append(self._class_sym(node))
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(self._method_sym(node.name, child))
                        calls[f"{node.name}.{child.name}"] = self._calls_of(child)

        return ParseResult("python", len(self.lines), imports, globals_, symbols, calls)

    @staticmethod
    def _start_line(node: ast.AST) -> int:
        decos = getattr(node, "decorator_list", None)
        if decos:
            return min(d.lineno for d in decos)
        return node.lineno

    def _func_sym(self, node: ast.AST) -> SymbolRange:
        args = ", ".join(a.arg for a in node.args.args)
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        return SymbolRange(node.name, "function", self._start_line(node), node.end_lineno,
                           f"def {node.name}({args}){ret}")

    @staticmethod
    def _class_sym(node: ast.AST) -> SymbolRange:
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        return SymbolRange(node.name, "class", node.lineno, node.end_lineno,
                           f"class {node.name}({bases})")

    def _method_sym(self, cls: str, node: ast.AST) -> SymbolRange:
        args = ", ".join(a.arg for a in node.args.args)
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        return SymbolRange(f"{cls}.{node.name}", "method", self._start_line(node), node.end_lineno,
                           f"def {node.name}({args}){ret}")

    @staticmethod
    def _calls_of(node: ast.AST) -> Set[str]:
        out: Set[str] = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name):
                    out.add(n.func.id)
                elif isinstance(n.func, ast.Attribute):
                    out.add(n.func.attr)
        out.discard(getattr(node, "name", None))
        return out


class BraceParser:
    """大括号语言通用解析器基类：正则匹配签名 + {} 配对求结束行

    子类只需覆盖 LANG / FUNC_PATTERNS / CLASS_PATTERNS / IMPORT_PATTERNS。
    """

    LANG = "text"
    FUNC_PATTERNS: List[str] = []
    CLASS_PATTERNS: List[str] = []
    IMPORT_PATTERNS: List[str] = []
    CALL_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "new"}

    def __init__(self, code: str):
        self.code = code
        self.lines = code.splitlines()
        # 屏蔽注释与字符串，避免 "// class Foo" 之类误匹配；保持行号不变
        self.clean = self._mask_comments_and_strings(code)
        self.clean_lines = self.clean.splitlines()

    @staticmethod
    def _mask_comments_and_strings(code: str) -> str:
        out: List[str] = []
        i, n = 0, len(code)
        in_line = in_block = False
        quote: Optional[str] = None
        while i < n:
            c = code[i]
            nxt = code[i + 1] if i + 1 < n else ""
            if in_line:
                if c == "\n":
                    in_line = False
                    out.append(c)
                else:
                    out.append(" ")
                i += 1
                continue
            if in_block:
                if c == "*" and nxt == "/":
                    in_block = False
                    out.append("  ")
                    i += 2
                    continue
                out.append("\n" if c == "\n" else " ")
                i += 1
                continue
            if quote:
                if c == "\\":
                    out.append("  ")
                    i += 2
                    continue
                if c == quote:
                    quote = None
                out.append("\n" if c == "\n" else " ")
                i += 1
                continue
            if c == "/" and nxt == "/":
                in_line = True
                out.append("  ")
                i += 2
                continue
            if c == "/" and nxt == "*":
                in_block = True
                out.append("  ")
                i += 2
                continue
            if c in ('"', "'", "`"):
                quote = c
                out.append(" ")
                i += 1
                continue
            out.append(c)
            i += 1
        return "".join(out)

    def parse(self) -> ParseResult:
        imports: List[str] = []
        symbols: List[SymbolRange] = []
        calls: Dict[str, Set[str]] = {}

        i = 0
        total = len(self.clean_lines)
        while i < total:
            stripped = self.clean_lines[i].strip()

            for pat in self.IMPORT_PATTERNS:
                m = re.match(pat, stripped)
                if m:
                    imports.append(m.group(1))
                    break

            is_class = False
            for pat in self.CLASS_PATTERNS:
                m = re.match(pat, stripped)
                if m:
                    is_class = True
                    end = self._find_block_end(i)
                    if end is not None:
                        name = m.group("name")
                        symbols.append(SymbolRange(name, "class", i + 1, end + 1,
                                                   self._clean_sig(stripped)))
                        self._parse_members(i + 1, end, name, symbols)
                        i = end
                    break

            if not is_class:
                for pat in self.FUNC_PATTERNS:
                    m = re.match(pat, stripped)
                    if m:
                        end = self._find_block_end(i)
                        if end is not None:
                            name = m.group("name")
                            symbols.append(SymbolRange(name, "function", i + 1, end + 1,
                                                       self._clean_sig(stripped)))
                            callees = self._collect_calls(i, end)
                            callees.discard(name)
                            calls[name] = callees
                            i = end
                        break

            i += 1

        return ParseResult(self.LANG, len(self.lines), imports, [], symbols, calls)

    def _find_block_end(self, start_line: int) -> Optional[int]:
        """从 start_line 找第一个 { 并配对到匹配的 }，返回其行号（0-based）"""
        depth = 0
        started = False
        for i in range(start_line, len(self.clean_lines)):
            for c in self.clean_lines[i]:
                if c == "{":
                    depth += 1
                    started = True
                elif c == "}":
                    depth -= 1
                    if started and depth == 0:
                        return i
        return None

    def _parse_members(self, start: int, end: int, cls_name: str, symbols: List[SymbolRange]) -> None:
        """在类体内扫一遍，把方法登记为 ClassName.method"""
        for i in range(start, end):
            stripped = self.clean_lines[i].strip()
            for pat in self.FUNC_PATTERNS:
                m = re.match(pat, stripped)
                if m:
                    block_end = self._find_block_end(i)
                    if block_end is not None and block_end <= end:
                        symbols.append(SymbolRange(
                            f"{cls_name}.{m.group('name')}", "method",
                            i + 1, block_end + 1, self._clean_sig(stripped)))
                    break

    def _collect_calls(self, start: int, end: int) -> Set[str]:
        out: Set[str] = set()
        text = "\n".join(self.clean_lines[start:end + 1])
        for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", text):
            name = m.group(1)
            if name not in self.CALL_KEYWORDS:
                out.add(name)
        return out

    @staticmethod
    def _clean_sig(line: str) -> str:
        return re.sub(r"\s*\{?\s*$", "", line)[:120]


class JavaParser(BraceParser):
    LANG = "java"
    IMPORT_PATTERNS = [r"^import\s+([\w\.\*]+)\s*;"]
    CLASS_PATTERNS = [
        r"^(?:public|private|protected|abstract|final|static|\s)*"
        r"(?:class|interface|enum|record)\s+(?P<name>\w+)",
    ]
    FUNC_PATTERNS = [
        r"^(?:public|private|protected|static|final|synchronized|abstract|native|\s)*"
        r"[\w<>\[\],\s\.]+\s+(?P<name>\w+)\s*\([^)]*\)\s*(?:throws [\w,\s]+)?\s*\{",
    ]


class KotlinParser(BraceParser):
    LANG = "kotlin"
    IMPORT_PATTERNS = [r"^import\s+([\w\.\*]+)"]
    CLASS_PATTERNS = [
        r"^(?:public|private|internal|open|abstract|sealed|data|enum|\s)*"
        r"(?:class|object|interface)\s+(?P<name>\w+)",
    ]
    FUNC_PATTERNS = [
        r"^(?:public|private|internal|open|override|suspend|inline|operator|\s)*"
        r"fun\s+(?:<[^>]+>\s*)?(?P<name>\w+)\s*\(",
        r"^(?:public|private|internal|\s)*"
        r"val\s+(?P<name>\w+)\s*=",
    ]


class CppParser(BraceParser):
    LANG = "cpp"
    IMPORT_PATTERNS = [r"^#include\s*[<\"]([^>\"]+)[>\"]"]
    CLASS_PATTERNS = [
        r"^(?:class|struct)\s+(?P<name>\w+)",
    ]
    FUNC_PATTERNS = [
        r"^(?:[\w:<>,\*&\s]+?)\s+(?P<name>[\w:~]+)\s*\([^;]*\)\s*(?:const)?\s*\{",
    ]


class CSharpParser(BraceParser):
    LANG = "csharp"
    IMPORT_PATTERNS = [r"^using\s+([\w\.]+)\s*;"]
    CLASS_PATTERNS = [
        r"^(?:public|private|protected|internal|abstract|sealed|static|partial|\s)*"
        r"(?:class|interface|struct|enum|record)\s+(?P<name>\w+)",
    ]
    FUNC_PATTERNS = [
        r"^(?:public|private|protected|internal|static|virtual|override|abstract|async|sealed|partial|extern|\s)*"
        r"[\w<>\[\],\?\.\s]+\s+(?P<name>\w+)\s*\([^;]*\)\s*(?:where [^{]+)?\s*\{",
    ]


class JavaScriptParser(BraceParser):
    LANG = "javascript"
    IMPORT_PATTERNS = [
        r"^import\s+.*?from\s+['\"]([^'\"]+)['\"]",
        r"^const\s+\w+\s*=\s*require\(['\"]([^'\"]+)['\"]\)",
    ]
    CLASS_PATTERNS = [
        r"^(?:export\s+)?(?:default\s+)?class\s+(?P<name>\w+)",
    ]
    FUNC_PATTERNS = [
        r"^(?:export\s+)?(?:async\s+)?function\s+(?P<name>\w+)\s*\(",
        r"^(?:export\s+)?const\s+(?P<name>\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>",
        r"^(?:export\s+)?const\s+(?P<name>\w+)\s*=\s*(?:async\s*)?function",
    ]


class ShellParser(BraceParser):
    LANG = "shell"
    IMPORT_PATTERNS = [
        r"^source\s+(\S+)",
        r"^\.\s+(\S+)",
    ]
    CLASS_PATTERNS: List[str] = []
    FUNC_PATTERNS = [
        r"^(?:function\s+)?(?P<name>[A-Za-z_]\w*)\s*\(\s*\)\s*\{",
        r"^function\s+(?P<name>[A-Za-z_]\w*)\s*\{",
    ]
    _SHELL_KEYWORDS = {"if", "then", "elif", "else", "fi", "for", "while",
                       "do", "done", "case", "esac", "function", "return",
                       "echo", "export", "source"}

    def _collect_calls(self, start: int, end: int) -> Set[str]:
        out: Set[str] = set()
        for i in range(start, min(end + 1, len(self.lines))):
            line = self.lines[i].strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^(?:[\w\.\-/]+\s+)?([A-Za-z_]\w*)\b", line)
            if m:
                out.add(m.group(1))
        out -= self._SHELL_KEYWORDS
        return out


PARSERS = {
    "python": PythonParser, "py": PythonParser,
    "java": JavaParser,
    "kotlin": KotlinParser, "kt": KotlinParser, "kts": KotlinParser,
    "c": CppParser, "cpp": CppParser, "cc": CppParser, "cxx": CppParser,
    "h": CppParser, "hpp": CppParser,
    "csharp": CSharpParser, "cs": CSharpParser,
    "javascript": JavaScriptParser, "js": JavaScriptParser, "jsx": JavaScriptParser,
    "mjs": JavaScriptParser, "cjs": JavaScriptParser,
    "typescript": JavaScriptParser, "ts": JavaScriptParser, "tsx": JavaScriptParser,
    "shell": ShellParser, "sh": ShellParser, "bash": ShellParser, "zsh": ShellParser,
}


def _normalize_language(language: str) -> str:
    return (language or "").strip().lower().lstrip(".")


def get_parser(code: str, language: str):
    key = _normalize_language(language)
    cls = PARSERS.get(key)
    if not cls:
        supported = ", ".join(sorted(set(PARSERS)))
        raise ValueError(f"不支持的语言 {language}（可用: {supported}）")
    return cls(code)


def render_skeleton(parsed: ParseResult) -> str:
    """把解析结果渲染成紧凑骨架文本（省 token，LLM 友好）"""
    out = [f"# {parsed.language} | {parsed.lines} lines"]
    if parsed.imports:
        out.append(f"imports: {', '.join(parsed.imports)}")
    if parsed.globals:
        out.append(f"globals: {', '.join(parsed.globals)}")
    out.append("")
    for s in parsed.symbols:
        prefix = "  " if s.kind == "method" else ""
        out.append(f"{prefix}[{s.kind}] {s.signature}  (L{s.start}-{s.end})")
    if parsed.calls:
        out.append("\ncalls:")
        for caller, callees in parsed.calls.items():
            if callees:
                out.append(f"  {caller} -> {', '.join(sorted(callees))}")
    return "\n".join(out)


def build_skeleton(code: str, language: str) -> str:
    """解析 code 并返回骨架文本（解析失败抛异常，由调用方转错误信封）"""
    return render_skeleton(get_parser(code, language).parse())


# ============================ 文件 / URL 读取 ============================

_EXT_LANG = {
    ".py": "python", ".pyw": "python",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
}


def infer_language(file_path: str) -> Optional[str]:
    """按扩展名推断语言（用于 file_path 模式自动补齐 language）"""
    return _EXT_LANG.get(os.path.splitext(file_path or "")[1].lower())


def _decode_bytes(raw: bytes):
    """自动探测编码并解码，返回 (text, encoding)。latin-1 兜底不会失败。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1"), "latin-1"


def _detect_newline(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def read_file(file_path: str):
    """读取本地文件，返回 (text, encoding, newline, err)。

    text 统一为 '\\n' 换行，newline 记录原文件换行风格（写入时还原）。
    """
    try:
        with open(file_path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None, None, None, f"文件不存在: {file_path}"
    except IsADirectoryError:
        return None, None, None, f"路径是目录: {file_path}"
    except OSError as e:
        return None, None, None, f"读取失败 - {e}"
    text, enc = _decode_bytes(raw)
    newline = _detect_newline(text)
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc, newline, None


def write_file(file_path: str, text: str, encoding: str = "utf-8", newline: str = "\n"):
    """写回文件，沿用读取时的编码与换行；返回错误信息或 None。"""
    try:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        data = normalized if newline in (None, "", "\n") else normalized.replace("\n", newline)
        with open(file_path, "w", encoding=encoding or "utf-8", newline="") as f:
            f.write(data)
        return None
    except (OSError, UnicodeEncodeError) as e:
        return f"写入失败 - {e}"


def fetch_url(url: str, timeout: int = 30):
    """下载 http(s) 文本，返回 (text, encoding, err)；自动探测编码，统一 '\\n'。"""
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        return None, None, f"下载失败 - {e}"
    text, enc = _decode_bytes(resp.content)
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc, None


def backup_file(file_path: str):
    """写回前生成备份，返回 (backup_path, err)。

    首次备份写 <file>.bak（永久保留原始版本），之后每次改用时间戳
    <file>.bak.YYYYmmddHHMMSS（同名时追加序号），避免历史被覆盖。
    """
    try:
        bak = file_path + ".bak"
        if os.path.exists(bak):
            stamp = time.strftime("%Y%m%d%H%M%S")
            bak = f"{file_path}.bak.{stamp}"
            n = 1
            while os.path.exists(bak):
                bak = f"{file_path}.bak.{stamp}.{n}"
                n += 1
        shutil.copy2(file_path, bak)
        return bak, None
    except OSError as e:
        return None, f"备份失败 - {e}"


# ============================ 读取（按符号 / 正则 / 行范围 / 骨架） ============================

DEFAULT_LIMIT = 400


def _is_plain(language) -> bool:
    """语言为空或不在解析器表中时按纯文本处理（md/txt 等）"""
    key = _normalize_language(language)
    return not key or key not in PARSERS


def _select_symbols(parsed: ParseResult, symbol, pattern):
    """按符号名（可为单个或多个）与正则筛选符号，返回 (selected, err)"""
    wanted: List[str] = []
    if symbol:
        wanted = [symbol] if isinstance(symbol, str) else [str(x) for x in symbol]
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return None, f"正则表达式非法: {e}"
    selected = [s for s in parsed.symbols if s.name in wanted or (rx and rx.search(s.name))]
    return selected, None


def _emit_symbols(lines, selected, limit, source):
    """输出符号体（带绝对行号），超限截断并给出续读提示"""
    width = len(str(max(s.end for s in selected)))
    out: List[str] = []
    shown = 0
    truncated = False
    next_hint = None
    names: List[str] = []
    for s in selected:
        names.append(s.name)
        out.append(f"# {s.name} ({s.kind}) L{s.start}-{s.end}")
        for i in range(s.start - 1, s.end):
            if shown >= limit:
                truncated = True
                next_hint = f"{s.name} L{i + 1}"
                break
            out.append(f"{i + 1:>{width}}: {lines[i]}")
            shown += 1
        if truncated:
            break
    payload = {"action": "read", "target": "symbols", "symbols": names,
               "result": "\n".join(out), "truncated": truncated}
    if truncated:
        payload["next_hint"] = next_hint
    payload.update(source)
    return ok(payload)


def _emit_matches(code, symbol, pattern, limit, source):
    """纯文本按 symbol（子串）/ pattern（正则）逐行匹配，带绝对行号输出。"""
    lines = code.splitlines()
    try:
        rx = re.compile(pattern) if pattern else None
    except re.error as e:
        return error(f"正则表达式非法: {e}", code="bad_pattern")
    wanted: List[str] = []
    if symbol:
        wanted = [symbol] if isinstance(symbol, str) else [str(x) for x in symbol]
    idxs = [i for i, line in enumerate(lines)
            if (rx and rx.search(line)) or any(w in line for w in wanted)]
    if not idxs:
        return error("未匹配到内容", code="not_found")
    width = len(str(len(lines)))
    shown = idxs[:limit]
    truncated = len(idxs) > limit
    body = [f"{i + 1:>{width}}: {lines[i]}" for i in shown]
    payload = {"action": "read", "target": "text", "matches": len(idxs),
               "result": "\n".join(body), "truncated": truncated}
    if truncated:
        payload["next_hint"] = f"共 {len(idxs)} 处匹配，已显示前 {limit} 处"
    payload.update(source)
    return ok(payload)


def _emit_range(code, start, end, limit, source):
    """输出行范围（带绝对行号），超限截断并给出 next_start_line"""
    lines = code.splitlines()
    total = len(lines)
    if start < 1 or end < 1:
        return error(f"行号从 1 开始: {start}-{end}", code="bad_range")
    if start > total:
        return error(f"起始行 {start} 超出总行数 {total}", code="bad_range")
    if start > end:
        return error(f"起始行 {start} 大于结束行 {end}", code="bad_range")
    end = min(end, total)
    truncated = False
    if end - start + 1 > limit:
        end = start + limit - 1
        truncated = True
    width = len(str(total))
    body = [f"{i + 1:>{width}}: {lines[i]}" for i in range(start - 1, end)]
    payload = {"action": "read", "target": "lines", "start_line": start,
               "end_line": end, "total_lines": total,
               "result": "\n".join(body), "truncated": truncated}
    if truncated:
        payload["next_start_line"] = end + 1
    payload.update(source)
    return ok(payload)


def _read(code, language, symbol, pattern, start_line, end_line, limit, source):
    """read 动作：优先按 symbol/pattern 批量，其次按行号范围，最后回退结构骨架/纯文本"""
    if not code.strip():
        return error("文件内容为空", code="empty_code")

    plain = _is_plain(language)

    if symbol or pattern:
        if plain:
            return _emit_matches(code, symbol, pattern, limit, source)
        try:
            parsed = get_parser(code, language).parse()
        except SyntaxError as e:
            return error(f"解析失败 - {e}", code="parse_failed")
        except ValueError as e:
            return error(str(e), code="unsupported_language")
        except Exception as e:
            return error(f"解析失败 - {e}", code="parse_failed")
        selected, err = _select_symbols(parsed, symbol, pattern)
        if err:
            return error(err, code="bad_pattern")
        if not selected:
            available = ", ".join(s.name for s in parsed.symbols) or "(无)"
            return error(f"未匹配到符号（可用: {available}）", code="symbol_not_found")
        return _emit_symbols(code.splitlines(), selected, limit, source)

    if start_line is not None or end_line is not None:
        total = len(code.splitlines())
        try:
            start = int(start_line) if start_line is not None else 1
            end = int(end_line) if end_line is not None else total
        except (TypeError, ValueError):
            return error("start_line / end_line 必须为整数", code="bad_range")
        return _emit_range(code, start, end, limit, source)

    if plain:
        total = len(code.splitlines())
        return _emit_range(code, 1, total, limit, source)
    try:
        payload = {"action": "read", "target": "outline",
                   "language": _normalize_language(language),
                   "result": build_skeleton(code, language)}
        payload.update(source)
        return ok(payload)
    except SyntaxError as e:
        return error(f"解析失败 - {e}", code="parse_failed")
    except ValueError as e:
        return error(str(e), code="unsupported_language")
    except Exception as e:
        return error(f"解析失败 - {e}", code="parse_failed")


def _replace(code: str, old_code: str, new_code: str, replace_all: bool):
    """精确替换。返回 (new_src, count, err)。

    对齐 edit 工具：
    - old_code 找不到 -> 报错；
    - 匹配多处且 replace_all=false -> 报错并提示使用 replace_all；
    - 否则替换（replace_all=true 替换全部，否则替换唯一命中）。
    """
    count = code.count(old_code)
    if count == 0:
        return None, 0, "old_code 未找到（需与源码逐字符完全一致，包含缩进与换行）"
    if count > 1 and not replace_all:
        lines_hit: List[int] = []
        start = 0
        for _ in range(count):
            idx = code.find(old_code, start)
            lines_hit.append(code.count("\n", 0, idx) + 1)
            start = idx + len(old_code)
        return None, count, (
            f"old_code 匹配到 {count} 处（行 {', '.join(map(str, lines_hit))}），无法确定目标；"
            f"请提供更精确的片段，或设 replace_all=true 全部匹配"
        )
    if replace_all:
        return code.replace(old_code, new_code), count, None
    return code.replace(old_code, new_code, 1), 1, None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _split_lines(code: str):
    """拆成行列表并记录是否以换行结尾（写回时还原）。"""
    trailing = code.endswith("\n")
    lines = code.split("\n")
    if trailing and lines and lines[-1] == "":
        lines.pop()
    return lines, trailing


def _join_lines(lines: List[str], trailing: bool) -> str:
    text = "\n".join(lines)
    if trailing and text:
        text += "\n"
    return text


def _new_lines(new_code: str) -> List[str]:
    if not new_code:
        return []
    lines = new_code.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _replace_lines(code: str, start, end, new_code: str):
    """按行号替换 [start, end]（1-based，含端点）为 new_code，返回 (new_src, err)。"""
    lines, trailing = _split_lines(code)
    total = len(lines)
    if start is None or start < 1 or start > total:
        return None, f"起始行需在 1-{total}: {start}"
    if end is None:
        end = start
    if end < start or end > total:
        return None, f"结束行需在 {start}-{total}: {end}"
    lines[start - 1:end] = _new_lines(new_code)
    return _join_lines(lines, trailing), None


def _insert_after_line(code: str, at, new_code: str):
    """在 start_line 行之后插入 new_code（0 表示文件开头），返回 (new_src, err)。"""
    lines, trailing = _split_lines(code)
    total = len(lines)
    at = total if at is None else at
    if at < 0 or at > total:
        return None, f"插入行需在 0-{total}: {at}"
    lines[at:at] = _new_lines(new_code)
    return _join_lines(lines, trailing), None


def _add_after(code: str, anchor: str, new_code: str, replace_all: bool):
    """在 anchor 之后按「行边界」插入 new_code，返回 (new_src, count, err)。

    统一换行语义，避免 anchor 与 new_code 粘连成 `WORKDIR /appRUN ...`：
    - 若 anchor 不以换行结尾，则补一个换行再放 new_code；
    - 若 new_code 不以换行结尾，则补一个换行（其后已有换行时不重复补）。
    """
    count = code.count(anchor)
    if count == 0:
        return None, 0, "old_code 未找到（需与源码逐字符完全一致，包含缩进与换行）"
    if count > 1 and not replace_all:
        lines_hit: List[int] = []
        start = 0
        for _ in range(count):
            idx = code.find(anchor, start)
            lines_hit.append(code.count("\n", 0, idx) + 1)
            start = idx + len(anchor)
        return None, count, (
            f"old_code 匹配到 {count} 处（行 {', '.join(map(str, lines_hit))}），无法确定目标；"
            f"请提供更精确的片段，或设 replace_all=true 全部匹配"
        )
    out: List[str] = []
    pos = 0
    inserted = 0
    while True:
        idx = code.find(anchor, pos)
        if idx == -1:
            out.append(code[pos:])
            break
        end = idx + len(anchor)
        out.append(code[pos:end])
        if replace_all or inserted == 0:
            seg = ""
            if not anchor.endswith("\n") and not new_code.startswith("\n"):
                seg += "\n"
            seg += new_code
            if not new_code.endswith("\n") and code[end:end + 1] != "\n":
                seg += "\n"
            out.append(seg)
            inserted += 1
            if not replace_all:
                out.append(code[end:])
                break
        pos = end
    return "".join(out), count, None


DIFF_LIMIT = 200


def _make_diff(before: str, after: str):
    """生成 unified diff（3 行上下文），返回 (diff_text, truncated)。"""
    diff = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile="before", tofile="after", lineterm="", n=3))
    truncated = len(diff) > DIFF_LIMIT
    if truncated:
        diff = diff[:DIFF_LIMIT]
    text = "\n".join(diff)
    if truncated:
        text += f"\n... (diff 截断，仅显示前 {DIFF_LIMIT} 行)"
    return text, truncated


def _finish(file_path, encoding, newline, action, count, count_key, new_src,
            before=None, dry_run=False):
    """写回前备份，再落盘；返回摘要 + 变更 diff（不含全量源码，省 token）。

    before 为修改前内容时附带 unified diff；dry_run=True 时只回显 diff 不落盘。
    """
    payload = {"action": action, "file_path": file_path, count_key: count}
    if before is not None:
        text, truncated = _make_diff(before, new_src)
        payload["diff"] = text
        if truncated:
            payload["diff_truncated"] = True
    if dry_run:
        payload["written"] = False
        payload["dry_run"] = True
        return ok(payload)
    bak, berr = backup_file(file_path)
    if berr:
        return error(berr, code="io_error")
    werr = write_file(file_path, new_src, encoding, newline)
    if werr:
        return error(werr, code="io_error")
    payload["written"] = True
    payload["backup"] = bak
    return ok(payload)


def script_editor(action: str = None, file_path: str = None, url: str = None,
                  language: str = None, symbol=None, pattern: str = None,
                  start_line: int = None, end_line: int = None, limit: int = DEFAULT_LIMIT,
                  old_code: str = None, new_code: str = None, replace_all: bool = False,
                  dry_run: bool = False) -> str:
    """脚本编辑 + 结构化读取工具（对齐 OpenViking / DSH 规范信封）。

    数据来源（二选一）：file_path（本地文件，可写）或 url（http/https，只读）。
    工具自行读取并探测编码，输出统一 UTF-8；本地修改写回原文件并生成 .bak 备份。

    动作：
    - read    : symbol（单个/数组）或 pattern（正则）批量取符号；或 start_line~end_line
                行范围；都不给则返回结构骨架。输出带行号，超 limit 截断并给续读提示
    - outline : 返回结构骨架（含每个符号的起止行号）
    - add     : 在 old_code 之后插入 new_code；old_code 省略则追加到末尾
    - edit    : 用 new_code 精确替换 old_code
    - delete  : 删除 old_code
    写入动作（add/edit/delete）默认回显 unified diff；dry_run=true 时只回显 diff 不落盘。
    """
    act = (action or "").strip().lower()
    new_code = new_code if new_code is not None else ""
    try:
        limit = int(limit) if limit else DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    if limit < 1:
        limit = DEFAULT_LIMIT

    source = {}
    if file_path:
        source["file_path"] = file_path
    elif url:
        source["url"] = url
    else:
        return error("需要提供 file_path 或 url", code="missing_source")

    if act not in ("read", "outline", "add", "edit", "delete"):
        return error(f"未知操作 '{action}'（应为 read/outline/add/edit/delete）", code="unknown_action")

    encoding, newline = "utf-8", "\n"
    writable = False
    if file_path:
        code, encoding, newline, io_err = read_file(file_path)
        if io_err:
            return error(io_err, code="io_error")
        writable = True
        if not language:
            language = infer_language(file_path)
    else:
        code, encoding, io_err = fetch_url(url)
        if io_err:
            return error(io_err, code="io_error")
        if not language:
            language = infer_language(urlparse(url).path)
    if code is None:
        code = ""

    if act == "read":
        return _read(code, language, symbol, pattern, start_line, end_line, limit, source)

    if act == "outline":
        if not code.strip():
            return error("文件内容为空", code="empty_code")
        if _is_plain(language):
            payload = {"action": "outline", "language": "text",
                       "result": render_skeleton(
                           ParseResult("text", len(code.splitlines()), [], [], [], {}))}
            payload.update(source)
            return ok(payload)
        try:
            payload = {"action": "outline", "language": _normalize_language(language),
                       "result": build_skeleton(code, language)}
            payload.update(source)
            return ok(payload)
        except SyntaxError as e:
            return error(f"解析失败 - {e}", code="parse_failed")
        except ValueError as e:
            return error(str(e), code="unsupported_language")
        except Exception as e:
            return error(f"解析失败 - {e}", code="parse_failed")

    # add / edit / delete 需要可写来源
    if not writable:
        return error("url 为只读，不能修改", code="read_only")

    line_mode = start_line is not None

    if act == "delete":
        if line_mode:
            new_src, err = _replace_lines(code, _as_int(start_line), _as_int(end_line), "")
            if err:
                return error(err, code="bad_range")
            return _finish(file_path, encoding, newline, "delete", 1, "replaced", new_src,
                           before=code, dry_run=dry_run)
        if not old_code:
            return error("delete 需要提供 old_code 或 start_line", code="missing_argument")
        new_src, count, err = _replace(code, old_code, "", replace_all)
        if err:
            return error(err, code="not_found")
        return _finish(file_path, encoding, newline, "delete", count, "replaced", new_src,
                       before=code, dry_run=dry_run)

    if act == "edit":
        if line_mode:
            new_src, err = _replace_lines(code, _as_int(start_line), _as_int(end_line), new_code)
            if err:
                return error(err, code="bad_range")
            return _finish(file_path, encoding, newline, "edit", 1, "replaced", new_src,
                           before=code, dry_run=dry_run)
        if not old_code:
            return error("edit 需要提供 old_code 或 start_line", code="missing_argument")
        if new_code == old_code:
            return error("new_code 与 old_code 相同，无需修改", code="no_change")
        new_src, count, err = _replace(code, old_code, new_code, replace_all)
        if err:
            return error(err, code="not_found")
        return _finish(file_path, encoding, newline, "edit", count, "replaced", new_src,
                       before=code, dry_run=dry_run)

    # add
    if not new_code:
        return error("add 需要提供 new_code", code="missing_argument")
    if line_mode:
        new_src, err = _insert_after_line(code, _as_int(start_line), new_code)
        if err:
            return error(err, code="bad_range")
        if not new_src.endswith("\n"):
            new_src += "\n"
        return _finish(file_path, encoding, newline, "add", 1, "inserted", new_src,
                       before=code, dry_run=dry_run)
    if not old_code:
        sep = "" if code.endswith("\n") or new_code.startswith("\n") else "\n"
        new_src, count = code + sep + new_code, 1
        if not new_src.endswith("\n"):
            new_src += "\n"
    else:
        new_src, count, err = _add_after(code, old_code, new_code, replace_all)
        if err:
            return error(err, code="not_found")
    return _finish(file_path, encoding, newline, "add", count, "inserted", new_src,
                   before=code, dry_run=dry_run)
