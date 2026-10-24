"""Run from codex/: python3 -m unittest discover -s tests -v"""
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hg = load("handoff_guard")
installer = load("install")
SID = "019a-test-session"


class World(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "codex"
        self.home.mkdir()
        self.state = root / "state"
        self.cwd = root / "project"
        self.cwd.mkdir()
        self.rollout = root / "rollout.jsonl"
        patcher = mock.patch.object(hg, "CODEX_HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.use(100_000)

    def use(self, tokens, window=533_000, session=SID, source="vscode", turns=0, legacy=False, meta=None, after=()):
        """A rollout as Codex 0.163 writes it; legacy=True counts turns the older way."""
        rows = [{"type": "session_meta", "payload": dict({"id": session, "source": source}, **(meta or {}))}]
        turn = ({"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}} if legacy else
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage"}}})
        rows += [turn] * turns
        if tokens is not None:
            rows += [
                {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"total_tokens": tokens}, "model_context_window": window}}}]
        rows += list(after)
        self.rollout.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def fire(self, name, **extra):
        event = dict(hook_event_name=name, session_id=SID, transcript_path=str(self.rollout), cwd=str(self.cwd))
        event.update(extra)
        return hg.handle(event, str(self.state))

    def notes(self, name="UserPromptSubmit", **extra):
        result = self.fire(name, **extra)
        if not result:
            return []
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], name)
        return result["hookSpecificOutput"]["additionalContext"].split("\n\n")

    def line(self, value):
        (self.home / "config.toml").write_text("model_auto_compact_token_limit = %d\n[tui]\n" % value, encoding="utf-8")


class Tiers(World):
    def test_quiet_below_the_first_tier(self):
        self.line(500_000)
        self.use(340_000)
        self.assertEqual(self.notes(), [])

    def test_each_tier_speaks_once_across_prompts_tools_and_processes(self):
        self.line(500_000)
        self.use(355_000)
        first = self.notes()
        self.assertEqual(len(first), 1)
        self.assertIn("只做 checkpoint", first[0])
        self.assertIn("50.0 万", first[0])
        self.assertIn("回复里不要提这条提醒", first[0])
        self.assertIn(".handoff/checkpoints/%s.md" % SID, first[0])
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.notes("PostToolUse"), [])

    def test_a_tool_call_carries_the_tier_and_says_it_is_not_tool_output(self):
        self.line(500_000)
        self.use(405_000)
        told = self.notes("PostToolUse")
        self.assertIn("是否需要交接", told[0])
        self.assertIn("不是工具输出", told[0])

    def test_a_jump_speaks_for_the_highest_tier_only(self):
        self.line(500_000)
        self.use(455_000)
        told = self.notes()
        self.assertEqual(len(told), 1)
        self.assertIn("最后一个保存点", told[0])
        self.assertIn("距离自动压缩约 4.5 万", told[0])

    def test_past_the_line_says_so(self):
        self.line(500_000)
        self.use(510_000)
        self.assertIn("已经超过自动压缩线", self.notes()[0])

    def test_a_conversation_carried_over_under_a_new_id_stays_quiet_for_what_it_reached(self):
        self.line(500_000)
        self.use(360_000, turns=6)
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.notes("PostToolUse"), [])
        self.use(405_000, turns=7)
        self.assertIn("是否需要交接", self.notes()[0])

    def test_turns_counted_the_older_way_also_mark_a_carried_conversation(self):
        self.line(500_000)
        self.use(360_000, turns=6, legacy=True)
        self.assertEqual(self.notes(), [])

    def test_a_fork_is_carried_over_even_when_its_first_check_has_no_usage_yet(self):
        self.line(500_000)
        fork = {"history_base": {"thread_id": "parent", "end_byte_offset": 1}}
        self.use(None, turns=1, meta=fork)
        self.assertEqual(self.notes(), [])
        self.use(360_000, turns=1, meta=fork)
        self.assertEqual(self.notes("PostToolUse"), [])
        self.use(405_000, turns=1, meta=fork)
        self.assertIn("是否需要交接", self.notes()[0])

    def test_a_new_conversation_whose_first_check_has_no_usage_hears_its_tier_later(self):
        self.line(500_000)
        self.use(None, turns=1)
        self.assertEqual(self.notes(), [])
        self.use(360_000, turns=1)
        self.assertIn("只做 checkpoint", self.notes("PostToolUse")[0])

    def test_after_a_tool_the_request_that_just_ended_counts(self):
        self.line(500_000)
        self.use(300_000, after=[{"type": "token_usage_record", "payload": {"usage": {"total_tokens": 405_000}}}])
        self.assertIn("是否需要交接", self.notes("PostToolUse")[0])

    def test_what_came_in_since_the_last_request_counts_but_image_data_does_not(self):
        self.line(500_000)
        image = {"type": "response_item", "payload": {"type": "function_call_output", "output": [
            {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 400_000}]}}
        self.use(330_000, after=[image])
        self.assertEqual(self.notes("PostToolUse"), [])
        text = {"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 120_000}}
        self.use(330_000, after=[image, text])
        told = self.notes("PostToolUse")[0]
        self.assertIn("只做 checkpoint", told)
        self.assertIn("36.0 万", told)

    def test_a_long_first_line_is_still_read(self):
        self.line(500_000)
        self.use(360_000, meta={"base_instructions": {"text": "x" * 100_000}})
        self.assertIn("只做 checkpoint", self.notes()[0])

    def test_a_new_conversation_that_starts_big_still_hears_its_tier(self):
        self.line(500_000)
        self.use(360_000, turns=1)
        self.assertIn("只做 checkpoint", self.notes()[0])

    def test_slash_commands_and_subagents_are_left_alone(self):
        self.line(500_000)
        self.use(455_000)
        self.assertIsNone(self.fire("UserPromptSubmit", prompt="/compact"))
        self.assertIsNone(self.fire("PostToolUse", agent_id="agent-1"))
        self.use(455_000, source={"subagent": "review"})
        self.assertIsNone(self.fire("PostToolUse"))

    def test_an_unknown_session_id_or_transcript_is_ignored(self):
        self.assertIsNone(hg.handle({"hook_event_name": "UserPromptSubmit", "session_id": "../x"}, str(self.state)))
        self.use(455_000, session="someone-else")
        self.assertEqual(self.notes(), [])


class Lines(World):
    def test_the_runtime_line_codex_logged_comes_first(self):
        self.line(800_000)
        with sqlite3.connect(str(self.home / "logs_2.sqlite")) as conn:
            conn.execute("CREATE TABLE logs (ts INTEGER, ts_nanos INTEGER, thread_id TEXT, feedback_log_body TEXT)")
            conn.execute("INSERT INTO logs VALUES (1, 0, ?, 'post sampling token usage auto_compact_scope_limit=Some(900000)')", (SID,))
        self.use(640_000, window=950_000)
        told = self.notes()[0]
        self.assertIn("自动压缩线约 90.0 万", told)
        self.assertIn("只做 checkpoint", told)

    def test_without_any_line_the_estimate_says_so(self):
        self.use(700_000, window=950_000)
        told = self.notes()[0]
        self.assertIn("85.5 万（估计）", told)
        self.assertIn("是否需要交接", told)

    def test_the_line_is_read_again_after_20_checks(self):
        self.line(900_000)
        for _ in range(20):
            self.notes()
        self.line(400_000)
        self.use(330_000)
        self.assertIn("40.0 万", self.notes()[0])


class Compactions(World):
    def test_a_compaction_is_counted_told_once_and_arms_the_tiers_again(self):
        self.line(500_000)
        self.use(455_000)
        self.notes()
        self.fire("PreCompact", trigger="auto")
        self.use(60_000)
        self.fire("PostCompact", trigger="auto")
        told = self.notes("PostToolUse")
        self.assertIn("第 1 次压缩（自动压缩，压缩前约 45.5 万 token）", told[0])
        self.assertIn("先回读已有 checkpoint", told[0])
        self.assertEqual(self.notes(), [])
        self.use(355_000)
        self.assertIn("只做 checkpoint", self.notes()[0])

    def test_from_the_second_compaction_the_notes_mention_stacked_summaries(self):
        self.line(500_000)
        self.fire("PostCompact", trigger="auto")
        self.assertNotIn("摘要叠在摘要上", self.notes()[0])
        self.fire("PostCompact", trigger="manual")
        second = self.notes()[0]
        self.assertIn("第 2 次压缩（手动 /compact）", second)
        self.assertIn("摘要叠在摘要上", second)
        self.use(355_000)
        self.assertIn("本会话已压缩 2 次", self.notes()[0])


class CheckpointWatch(World):
    def test_no_new_checkpoint_after_the_last_saving_point_asks_once_more(self):
        self.line(500_000)
        self.use(455_000)
        self.notes()
        nudge = self.notes()
        self.assertEqual(len(nudge), 1)
        self.assertIn("没有新写入的文件", nudge[0])
        self.assertIn("只提醒一次", nudge[0])
        self.assertEqual(self.notes(), [])

    def test_a_checkpoint_written_in_the_task_directory_leaves_nothing_owed(self):
        self.line(500_000)
        self.use(455_000)
        self.notes()
        folder = self.cwd / ".handoff" / "checkpoints"
        folder.mkdir(parents=True)
        (folder / ("%s.md" % SID)).write_text("# checkpoint", encoding="utf-8")
        self.assertEqual(self.notes(), [])

    def test_a_checkpoint_older_than_the_reminder_does_not_count(self):
        folder = self.cwd / ".handoff" / "checkpoints"
        folder.mkdir(parents=True)
        old = folder / "old.md"
        old.write_text("#", encoding="utf-8")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        self.line(500_000)
        self.use(455_000)
        self.notes()
        self.assertIn("没有新写入的文件", self.notes()[0])


class Main(unittest.TestCase):
    def test_the_output_is_ascii_json(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(hg, "handle", return_value={"hookSpecificOutput": {"additionalContext": "提醒"}}), \
                mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}"))), mock.patch.object(sys, "stdout", out):
            hg.main(["--state-dir", tmp])
        self.assertTrue(out.getvalue().isascii())
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"], "提醒")

    def test_a_failing_stdout_still_exits_zero(self):
        class Broken(io.StringIO):
            def write(self, text):
                raise BrokenPipeError()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(hg, "handle", return_value={"x": 1}), \
                mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}"))), \
                mock.patch.object(sys, "stdout", Broken()), mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(hg.main(["--state-dir", tmp]), 0)

    def test_old_sessions_are_pruned_and_one_lock_serves_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for number in range(hg.KEEP_SESSIONS + 3):
                path = folder / ("s%d.json" % number)
                path.write_text("{}", encoding="utf-8")
                Path(str(path) + ".lock").write_text("", encoding="utf-8")
                os.utime(path, (number, number))
            with hg.State(folder, "s-new"):
                pass
            self.assertEqual(len(list(folder.glob("*.json"))), hg.KEEP_SESSIONS)
            self.assertFalse((folder / "s0.json").exists())
            self.assertFalse((folder / "s0.json.lock").exists())
            self.assertTrue((folder / "s-new.json").exists())
            self.assertEqual(list(folder.glob("*.tmp")) + list(folder.glob(".*.tmp")), [])

    def test_bad_input_prints_nothing_and_exits_zero(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json"))), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(hg.main(["--state-dir", tempfile.gettempdir()]), 0)
        self.assertEqual(out.getvalue(), "")


class Installer(unittest.TestCase):
    def test_install_keeps_other_hooks_is_idempotent_and_uninstalls_only_its_own(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CODEX_HOME": tmp}), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            hooks = Path(tmp) / "hooks.json"
            other = {"type": "command", "command": "python3 other.py"}
            hooks.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [other]}]}}), encoding="utf-8")
            installer.main([])
            installer.main([])
            data = json.loads(hooks.read_text(encoding="utf-8"))["hooks"]
            self.assertEqual(sorted(data), ["PostCompact", "PostToolUse", "PreCompact", "UserPromptSubmit"])
            commands = [h["command"] for g in data["UserPromptSubmit"] for h in g["hooks"]]
            self.assertEqual(len(commands), 2)
            self.assertIn("python3 other.py", commands)
            self.assertTrue((Path(tmp) / "jiaobang" / "handoff_guard.py").is_file())
            self.assertTrue((Path(tmp) / "skills" / "jiaobang-handoff" / "SKILL.md").is_file())
            self.assertTrue(list(Path(tmp).glob("hooks.json.bak-jiaobang-*")))
            installer.main(["--uninstall"])
            data = json.loads(hooks.read_text(encoding="utf-8"))["hooks"]
            self.assertEqual(data, {"UserPromptSubmit": [{"hooks": [other]}]})
            self.assertFalse((Path(tmp) / "jiaobang").exists())

    def test_install_knows_its_own_entries_by_path_on_any_system_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CODEX_HOME": tmp}), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            hooks = Path(tmp) / "hooks.json"
            windows = {"type": "command", "command": '"C:\\Python\\python.exe" -X utf8 "C:\\Users\\a\\.codex\\jiaobang\\handoff_guard.py"'}
            lookalike = {"type": "command", "command": "python3 /opt/notjiaobang/handoff_guard.py"}
            hooks.write_text(json.dumps({"hooks": {"PostToolUse": [{"hooks": [windows, lookalike]}]}}), encoding="utf-8")
            installer.main([])
            installer.main([])
            commands = [h["command"] for g in json.loads(hooks.read_text(encoding="utf-8"))["hooks"]["PostToolUse"]
                        for h in g["hooks"]]
            self.assertEqual(len(commands), 2)
            self.assertIn(lookalike["command"], commands)
            self.assertNotIn(windows["command"], commands)
            self.assertEqual(len(list(Path(tmp).glob("hooks.json.bak-jiaobang-*"))), 2)

    def test_the_command_runs_as_written_in_sh_powershell_and_cmd(self):
        script = r"C:\Users\Ann Lee\.codex\jiaobang\handoff_guard.py"
        self.assertEqual(installer.hook_command("/x y/handoff_guard.py", "/usr/bin/python3", windows=False),
                         '"/usr/bin/python3" -X utf8 "/x y/handoff_guard.py"')
        # PowerShell reads a quoted first word as a string, so the interpreter goes first, bare.
        plain = r"C:\Users\ann\AppData\Local\Programs\Python\Python312\python.exe"
        self.assertEqual(installer.hook_command(script, plain, windows=True), '%s -X utf8 "%s"' % (plain, script))
        spaced = r"C:\Program Files\Python312\python.exe"
        with mock.patch.object(installer, "short_path", return_value=r"C:\PROGRA~1\PYTHON~1\python.exe"):
            self.assertTrue(installer.hook_command(script, spaced, windows=True).startswith(r"C:\PROGRA~1\PYTHON~1\python.exe -X utf8 "))
        with mock.patch.object(installer, "short_path", side_effect=lambda path: path), \
                mock.patch.object(installer.shutil, "which", side_effect=lambda name: name == "py" and r"C:\py.exe"):
            self.assertTrue(installer.hook_command(script, spaced, windows=True).startswith("py -X utf8 "))
        with mock.patch.object(installer, "short_path", side_effect=lambda path: path), \
                mock.patch.object(installer.shutil, "which", return_value=None):
            self.assertTrue(installer.hook_command(script, spaced, windows=True).startswith("python -X utf8 "))
        with mock.patch.object(installer, "short_path", return_value=r"C:\Users\ANNLEE~1\x.py") as short:
            self.assertTrue(installer.hook_command(r"C:\Users\a$b\x.py", plain, windows=True).endswith(r'"C:\Users\ANNLEE~1\x.py"'))
            short.assert_called_once()
        self.assertTrue(installer.ours({"command": installer.hook_command(script, plain, windows=True)}))

    def test_a_broken_hooks_file_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CODEX_HOME": tmp}), \
                mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", io.StringIO()):
            hooks = Path(tmp) / "hooks.json"
            for text in ("{not json", json.dumps({"hooks": []}), json.dumps({"hooks": {"Stop": [{"hooks": "x"}]}})):
                hooks.write_text(text, encoding="utf-8")
                self.assertEqual(installer.main([]), 1)
                self.assertEqual(hooks.read_text(encoding="utf-8"), text)
            self.assertFalse((Path(tmp) / "jiaobang").exists())

    def test_uninstall_leaves_files_it_did_not_put_there(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CODEX_HOME": tmp}), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            installer.main([])
            mine = Path(tmp) / "jiaobang" / "notes.txt"
            mine.write_text("keep", encoding="utf-8")
            installer.main(["--uninstall"])
            self.assertTrue(mine.is_file())
            self.assertFalse((Path(tmp) / "jiaobang" / "handoff_guard.py").exists())
            self.assertFalse((Path(tmp) / "skills" / "jiaobang-handoff").exists())


if __name__ == "__main__":
    unittest.main()
