#!/usr/bin/env python3
"""命令输出分流：判定哪些内容「必须保真」，据此选择 rtk 的裁剪模式。

背景（线上实测，非推测）：mode=auto 会先识别内容类型再换成另一种表示——
diff 被当成 grep 结果只剩文件头时间戳、JSON 被 elided 掏空、源码每 3 行删 3 行、
find 输出按目录重排成「300 files in 15 dirs」、日志折成「411 matches in 1F」。
压缩率最高（约 42%），代价是结论本身被删掉。mode=chain 只做 ansi / progress /
dedup / base64-elide / cap 这类无损裁剪，真实命令输出上只能省约 5%。

所以按内容分流：白名单内的类型走 chain 保真，其余交给 auto 争取压缩率。
判定以输出内容为主 —— 大量命令是 `ssh host 'a; b; c'` 这种拼接形式，
按命令前缀根本判不出来，只有看输出才知道是 diff / JSON / 表格还是源码。

OUTPUT_TRIM_ROUTE:
  chain_whitelist（默认）= 命中白名单走 chain，其余 auto
  off                    = 不分流，全部用 OUTPUT_TRIM_MODE
"""
import json
import re

from .. import config

# 白名单 = 必须保真、禁止 auto 重写的输出
_FIDELITY_CLASSES = ('cmd', 'diff', 'json', 'source', 'table', 'listing', 'paths', 'log', 'config')

# 命令层快速命中（内容层判不出来时才靠它们）；只放「输出必然是结构化文本」的命令
_FIDELITY_CMD_HINTS = (
    'diff', 'git show', 'git log -p', 'jq', 'json.tool',
    'psql', 'mysql', 'sqlite3', 'docker inspect',
    'lscpu', 'lsblk', 'lspci', 'lsusb', 'systemctl cat', 'systemctl show',
)

_RE_DIFF = re.compile(r'^(?:diff --git |@@ -\d+(?:,\d+)? \+\d+|--- \S[^\n]*\n\+\+\+ )', re.M)
_RE_JSON_KEY = re.compile(r'^\s*"[^"\n]{1,80}"\s*:', re.M)
_RE_CODE = re.compile(
    r'(?:^|\n)[ \t]*(?:def |class |async def |function |import |from \S+ import|#include'
    r'|public |private |protected |static |const |let |var |func |package |return'
    r'|if \(|for \(|while \(|switch \(|else \{|@@|</?\w+>|\};?\s*$)', re.M)
# 表格分隔线：psql `----+----`、mysql `+-----+`、markdown `|---|`。
# 只在「整行只由 -+|:= 空格组成且含 '-'」时才算，避免把 `echo =====` 当表格
_RE_TABLE_RULE = re.compile(r'^[ \t]*[-+|:=][-+|:= \t]*$', re.M)
_RE_PERM = re.compile(r'^[-dlbcps][rwxsStT-]{9}[.+]?\s+\d', re.M)
_RE_ABS_PATH = re.compile(r'^(?:/|\\\\|[A-Za-z]:\\)\S*$', re.M)
# 日志行：允许 grep -n 的「行号-」/「行号:」前缀，否则 grep 出来的日志判不出来
_RE_LOG_TS = re.compile(
    r'^\s*(?:\d+[-:])?(?:\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}|\d{2}:\d{2}:\d{2}|\w{3} \d{1,2} \d{2}:\d{2})',
    re.M)
# 配置文件（YAML/INI/systemd/env/nginx）：`key: value` 或 `key = value` 成片出现。
# auto 对这类内容同样是 elided 掏空，而配置的每一行都可能是关键项
_RE_CONF_KV = re.compile(r'^\s*[A-Za-z_][\w.-]{0,40}\s*[:=]', re.M)


def fidelity_class(command, output):
    """判定输出属于哪类「保真内容」，不命中返回空串。

    以内容特征为主：线上大量命令是 `ssh host 'a; b; c'` 这种拼接形式，按命令
    前缀根本判不出来，只有看输出才知道是 diff / JSON / 表格还是源码。
    """
    cmd = (command or '').lower()
    if any(hint in cmd for hint in _FIDELITY_CMD_HINTS):
        return 'cmd'

    output = output or ''
    # 只看头尾：超长输出的中段对判定没有增量信息，头部 12KB 足够
    probe = output if len(output) <= 12288 else output[:12288] + '\n' + output[-2048:]
    if not probe.strip():
        return ''

    if _RE_DIFF.search(probe):
        return 'diff'

    stripped = probe.lstrip()
    if stripped[:1] in ('{', '['):
        try:
            json.loads(output)
            return 'json'
        except (ValueError, TypeError):
            # 超长 / 被截断的 JSON 走结构特征兜底
            if len(_RE_JSON_KEY.findall(probe)) >= 3:
                return 'json'

    rule = _RE_TABLE_RULE.search(probe)
    if rule and ('|' in rule.group(0) or '+' in rule.group(0)) and '|' in probe:
        return 'table'

    lines = [ln for ln in probe.splitlines() if ln.strip()]
    if lines:
        perm = len(_RE_PERM.findall(probe))
        if perm >= 3 or (perm >= 1 and lines[0].startswith('total ')):
            return 'listing'

        # 纯路径列表（find / tree -f），auto 会按目录数重排成「300 files in 15 dirs」
        paths = len(_RE_ABS_PATH.findall(probe))
        if len(lines) >= 8 and paths >= len(lines) * 0.8:
            return 'paths'

        indented = sum(1 for ln in lines if ln[:1] in (' ', '\t'))
        if indented >= len(lines) * 0.45 and len(_RE_CODE.findall(probe)) >= 2:
            return 'source'

        if len(_RE_LOG_TS.findall(probe)) >= 5:
            return 'log'

        # 配置成片出现时也算保真内容。放在 source 之后：配置行大多是 key: value，
        # 既不缩进也没有代码关键字，只有成片出现才能确认是配置文件而不是普通文本
        if len(_RE_CONF_KV.findall(probe)) >= max(5, len(lines) * 0.5):
            return 'config'

    return ''


def route_mode(command, output):
    """返回这次裁剪该用的 mode：白名单内容 chain，其余 auto。

    分流关闭（OUTPUT_TRIM_ROUTE=off）或用户把 OUTPUT_TRIM_MODE 显式设成非 auto
    时，直接沿用配置值，不做内容判定。
    """
    configured = config.OUTPUT_TRIM_MODE
    route = str(getattr(config, 'OUTPUT_TRIM_ROUTE', '') or '').strip().lower()
    if configured != 'auto' or route != 'chain_whitelist':
        return configured
    return 'chain' if fidelity_class(command, output) in _FIDELITY_CLASSES else 'auto'
