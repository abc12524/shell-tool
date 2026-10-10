# skill: script
# description: 脚本/代码增删改查（CRUD）+ 结构骨架（精确字符串替换，对齐 edit 工具）
# status: enable
# usage:
#   arguments.action = read | outline | add | edit | delete
#   数据来源二选一：file_path（本地文件，可写；改后写回并生成 .bak）或 url（http/https，只读）
#   read   : symbol（单个/数组）或 pattern（正则）批量取符号；或 start_line~end_line 行范围；
#            都不给则返回结构骨架。输出带行号，超 limit 截断并给续读提示
#   outline: 返回结构骨架（每个符号带起止行号）
#   add    : 在 old_code 之后插入 new_code（省略 old_code 则追加末尾）
#   edit   : 用 new_code 精确替换 old_code（唯一匹配才执行，多处需 replace_all=true）
#   delete : 删除 old_code
#   写入动作默认返回 unified diff；dry_run=true 只回显 diff 不落盘
#   示例: {"action": "read", "file_path": "core/main.py", "symbol": "parse_args"}
from ._script_impl import script_editor


def run(arguments):
    a = arguments or {}
    return script_editor(
        action=a.get("action", ""),
        file_path=a.get("file_path"),
        url=a.get("url"),
        language=a.get("language"),
        symbol=a.get("symbol"),
        pattern=a.get("pattern"),
        start_line=a.get("start_line"),
        end_line=a.get("end_line"),
        limit=a.get("limit", 400),
        old_code=a.get("old_code"),
        new_code=a.get("new_code"),
        replace_all=a.get("replace_all", False),
        dry_run=a.get("dry_run", False),
    )
