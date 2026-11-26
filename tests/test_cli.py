"""CLI 参数解析与输出模式的回归测试（标准库 unittest，无额外依赖）。

跑法：python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# 必须在 import core 之前隔离配置目录：config.py 在 import 时就会读 SHELL_TOOL_HOME
# 并创建 data/skill 目录，指向临时目录以免碰到真实的会话库与 skill。
_TMP_HOME = tempfile.mkdtemp(prefix="sht-test-")
os.environ["SHELL_TOOL_HOME"] = _TMP_HOME
os.environ.pop("DP_API_MODE", None)
# 假 key：让流程越过「缺 key 直接返回 1」的早退，同时保证测试绝不会真的调用模型
os.environ["DEEPSEEK_API_KEY"] = "test-key-not-used"

from core import console as C  # noqa: E402
from core.main import ExitCode, Options, run  # noqa: E402


def tearDownModule():
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


class TestOptions(unittest.TestCase):
    """Options.from_args：命令行契约（收的是不含程序名的参数列表）"""

    def test_question_words_joined(self):
        self.assertEqual(Options.from_args(["你好", "世界"]).question, "你好 世界")

    def test_new_flag(self):
        opts = Options.from_args(["-n", "hi"])
        self.assertTrue(opts.new)
        self.assertEqual(opts.question, "hi")

    def test_session_without_id_lists(self):
        self.assertTrue(Options.from_args(["-s"]).list_sessions)

    def test_session_with_id(self):
        opts = Options.from_args(["-s", "abc123"])
        self.assertEqual(opts.session, "abc123")
        self.assertFalse(opts.list_sessions)

    def test_skill_without_args_lists(self):
        self.assertTrue(Options.from_args(["-S"]).show_skills)

    def test_skill_with_status(self):
        opts = Options.from_args(["-S", "my_skill", "disable"])
        self.assertEqual(opts.skill_status, ("my_skill", "disable"))

    def test_hub_install_names(self):
        opts = Options.from_args(["-I", "a", "b"])
        self.assertEqual(opts.hub_action, ("install", ["a", "b"]))

    def test_hub_install_without_names_lists(self):
        self.assertEqual(Options.from_args(["-I"]).hub_action, ("list", None))

    def test_help_flag(self):
        self.assertTrue(Options.from_args(["-h"]).show_help)
        self.assertTrue(Options.from_args(["--help"]).show_help)

    def test_missing_value_raises_exit_code(self):
        for argv in (["-k"], ["-m"], ["-D"]):
            with self.subTest(argv=argv):
                with self.assertRaises(ExitCode) as ctx:
                    Options.from_args(argv)
                self.assertEqual(ctx.exception.code, 1)


class TestOutputMode(unittest.TestCase):
    """输出模式：单次调用生效、退出即恢复，且按线程隔离"""

    def test_emit_goes_to_given_stream_and_mode_restores(self):
        buf = io.StringIO()
        with C.output_mode(api_mode=True, stream=buf):
            C.emit("session", action="new", id="x")
            self.assertTrue(C.api_mode())
        self.assertEqual(json.loads(buf.getvalue())["t"], "session")
        self.assertFalse(C.api_mode(), "调用结束后应恢复默认（非 API 模式）")

    def test_redirect_stdout_captures_events(self):
        """不给 stream 时事件应跟随当前 sys.stdout，而不是 import 时的那个。"""
        buf = io.StringIO()
        with C.output_mode(api_mode=True), contextlib.redirect_stdout(buf):
            C.emit("content", content="hi")
        self.assertEqual(json.loads(buf.getvalue())["content"], "hi")

    def test_nested_modes(self):
        outer, inner = io.StringIO(), io.StringIO()
        with C.output_mode(api_mode=True, stream=outer):
            with C.output_mode(api_mode=True, stream=inner):
                C.emit("content", content="in")
            C.emit("content", content="out")
        self.assertIn('"in"', inner.getvalue())
        self.assertIn('"out"', outer.getvalue())
        self.assertNotIn('"out"', inner.getvalue())

    def test_threads_do_not_cross_talk(self):
        """两个并发调用各持自己的流：A 的事件不能出现在 B 的响应里。"""
        res, barrier = {}, threading.Barrier(2)

        def worker(name):
            buf = io.StringIO()
            with C.output_mode(api_mode=True, stream=buf):
                barrier.wait()
                for i in range(100):
                    C.emit("content", content=f"{name}{i}")
            res[name] = buf.getvalue().splitlines()

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(res["A"]), 100)
        self.assertEqual(len(res["B"]), 100)
        self.assertTrue(all(json.loads(l)["content"].startswith("A") for l in res["A"]))
        self.assertTrue(all(json.loads(l)["content"].startswith("B") for l in res["B"]))


class TestRunEntry(unittest.TestCase):
    """库入口：跨 asyncio.run() 后模式仍然生效，返回码而不是终止进程"""

    def _run(self, options):
        """跑一次库调用，返回（退出码, 结构化事件列表）；终端输出被丢弃。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):
            code = run(options, api_mode=True, stream=buf)
        events = [json.loads(line) for line in buf.getvalue().splitlines() if line]
        return code, events

    def test_events_reach_given_stream_across_asyncio_run(self):
        """模式设在 asyncio.run 外层，实际发事件在内层 task —— 上下文必须延续。"""
        code, events = self._run(Options(question="hi", session="no-such-session-id"))
        self.assertEqual(code, 1)
        self.assertTrue(events, "事件流为空：模式没有跨 asyncio.run 生效")
        self.assertIn("db", [e["t"] for e in events])
        self.assertFalse(C.api_mode(), "run() 返回后不能残留 API 模式")

    def test_show_skills_returns_zero(self):
        self.assertEqual(self._run(Options(show_skills=True))[0], 0)

    def test_help_returns_zero(self):
        self.assertEqual(self._run(Options(show_help=True))[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
