"""handoff-guard for Codex: the same reminders as the Claude Code plugin.

Codex runs this once per hook event, with the event as JSON on stdin. It
reads the session's real context use from the session's own rollout file and
the auto-compaction line Codex reports, and prints a reminder for the model
when a tier is reached or a compaction happened. It never writes a handoff,
opens a thread or blocks anything; on any error it prints nothing.

Standard library only; runs on the Python 3.9 macOS ships with.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

try:
    import fcntl
except ImportError:  # Windows: state writes go unlocked.
    fcntl = None

CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
DEFAULT_STATE_DIR = CODEX_HOME / "state" / "jiaobang"
SKILL = "jiaobang-handoff"

# Tiers as fractions of the compaction line; the first is capped at 500K.
TIERS = (
    ("checkpoint", lambda line: min(500_000, line * 0.7)),
    ("evaluate", lambda line: line * 0.8),
    ("reevaluate", lambda line: line * 0.9),
)
# Only when Codex reports no line of its own and config.toml sets none.
FALLBACK_RATIO = 0.9
# The compaction line is read again after this many checks.
REREAD_AFTER = 20
STACKED_AT = 2
KEEP_SESSIONS = 50
TAIL_BYTES = 2 * 1024 * 1024
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

STACKED = "摘要叠在摘要上，早期细节会逐代丢失；把这点算进评估，但是否建议交接由你自己判断，不是默认结论。"
VIA_TOOL = "（这条是 handoff-guard 跟着工具结果附上的提醒，不是工具输出，也不是用户的新消息；先做完手上这一步再按它处理。）"
TRIGGER_LABEL = {"auto": "自动压缩", "manual": "手动 /compact"}


def wan(tokens):
    return "%.1f 万" % (tokens / 10_000)


def where(sid):
    return ("checkpoint 写到任务目录下的 .handoff/checkpoints/%s.md（本会话 ID）；"
            "上下文里其他 ID 的 checkpoint（比如分叉前的会话）只读不写，要沿用就复制到本会话的文件，"
            "并注明从哪个会话分叉而来。" % sid)


# --- reading what Codex reports -------------------------------------------

def is_subagent(event):
    if event.get("agent_id") or event.get("agent_transcript_path"):
        return True
    meta = session_meta(event.get("transcript_path"))
    if meta is None:
        return False
    source = meta.get("source")
    return meta.get("id") != event.get("session_id") or (isinstance(source, dict) and "subagent" in source)


def session_meta(transcript):
    if not isinstance(transcript, str) or not os.path.isabs(transcript):
        return None
    try:
        with open(transcript, "rb") as handle:
            first = json.loads(handle.readline(65536))
        return first.get("payload") if first.get("type") == "session_meta" else None
    except (OSError, ValueError, AttributeError):
        return None


def usage(event):
    """The context the last model request carried, and the model's window."""
    transcript = event.get("transcript_path")
    meta = session_meta(transcript)
    if meta is None or meta.get("id") != event.get("session_id"):
        return None
    latest = None
    with open(transcript, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        handle.seek(max(0, size - TAIL_BYTES))
        if handle.tell():
            handle.readline()
        for line in handle:
            try:
                row = json.loads(line)
                data = row.get("payload") or {}
                if row.get("type") != "event_msg" or data.get("type") != "token_count":
                    continue
                info = data.get("info") or {}
                last = info.get("last_token_usage") or {}
                tokens = last.get("total_tokens")
                window = info.get("model_context_window")
                if isinstance(tokens, int) and isinstance(window, int) and window > 0 and tokens >= 0:
                    latest = (tokens, window)
            except (ValueError, AttributeError):
                continue
    return latest


def runtime_line(session):
    """The auto-compaction line Codex logged for this thread, if it logged one."""
    database = CODEX_HOME / "logs_2.sqlite"
    if not database.is_file():
        return None
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.2) as conn:
            row = conn.execute(
                "SELECT feedback_log_body FROM logs WHERE thread_id=? "
                "AND feedback_log_body LIKE '%auto_compact_scope_limit%' "
                "ORDER BY ts DESC, ts_nanos DESC LIMIT 1", (session,)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    match = re.search(r"auto_compact_scope_limit=(?:Some\()?(\d+)", row[0])
    return int(match.group(1)) if match else None


def configured_line(window):
    """`model_auto_compact_token_limit` at the top of config.toml, if set."""
    try:
        for line in (CODEX_HOME / "config.toml").read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if text.startswith("["):
                break
            match = re.fullmatch(r"model_auto_compact_token_limit\s*=\s*([0-9_]+)\s*(#.*)?", text)
            if match:
                value = int(match.group(1).replace("_", ""))
                return value if 0 < value <= window else None
    except (OSError, ValueError):
        pass
    return None


def compaction_line(state, session, window):
    cached = state.get("threshold")
    if (cached and cached.get("window") == window and cached.get("compactions") == state["compactions"]
            and cached.get("uses", 0) < REREAD_AFTER):
        cached["uses"] = cached.get("uses", 0) + 1
        return cached["value"], cached["source"]
    value, source = runtime_line(session), "runtime"
    if not value:
        value, source = configured_line(window), "config"
    if not value:
        value, source = round(window * FALLBACK_RATIO), "estimate"
    state["threshold"] = {"window": window, "value": value, "source": source,
                          "compactions": state["compactions"], "uses": 1}
    return value, source


def checkpoint_written_since(cwd, since):
    folder = Path(cwd) / ".handoff" / "checkpoints" if isinstance(cwd, str) and cwd else None
    if folder is None or not folder.is_dir():
        return False
    return any(path.stat().st_mtime >= since for path in folder.glob("*.md"))


# --- state, one file per session ---------------------------------------

class State:
    def __init__(self, folder, session):
        self.folder = Path(folder)
        self.path = self.folder / ("%s.json" % session)
        self.lock = None

    def __enter__(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        if fcntl:
            self.lock = open(str(self.path) + ".lock", "w")
            fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("fired", [])
        data.setdefault("compactions", 0)
        data.setdefault("pending", None)
        data.setdefault("before", 0)
        data.setdefault("awaiting", None)
        self.data = data
        return data

    def __exit__(self, kind, value, trace):
        if kind is None:
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
            prune(self.folder)
        if self.lock:
            self.lock.close()
        return False


def prune(folder):
    files = sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in files[KEEP_SESSIONS:]:
        for path in (stale, Path(str(stale) + ".lock")):
            try:
                path.unlink()
            except OSError:
                pass


# --- the notes -----------------------------------------------------------

def tier_note(tier, tokens, line, source, count, sid):
    head = ("[handoff-guard] 当前上下文约 %s token，自动压缩线约 %s%s（已到 %d%%）。"
            % (wan(tokens), wan(line), "（估计）" if source == "estimate" else "", round(tokens / line * 100)))
    if count >= STACKED_AT:
        head += "本会话已压缩 %d 次，%s" % (count, STACKED)
    if tier == "checkpoint":
        return (head + "先完成手上最小的工作单元，再按 %s 技能综合评估整个会话：有长期价值（目标、决定及理由、否决路线、"
                "文件状态、学习进度）就保存或刷新 checkpoint 并回读确认；确实没有就跳过。" % SKILL + where(sid) +
                "这一档只做 checkpoint，不写正式交接，也不开新对话。回复里不要提这条提醒，也不要汇报 checkpoint，用户问什么就答什么。")
    if tier == "evaluate":
        return (head + "请按 %s 技能评估整个会话是否需要交接：需要时先保存或刷新 checkpoint（" % SKILL + where(sid) + "），"
                "并在最终回复正文最前面单独一段以「交接建议：」写明原因、接续第一步，并说明确认后会保存交接、给出新对话的开场白；"
                "不需要时用一句话说明理由，到压缩线 90% 会再提醒。用户确认前不写正式交接。")
    distance = ("距离自动压缩约 %s。" % wan(line - tokens)) if tokens < line else "已经超过自动压缩线，随时可能压缩。"
    return (head + distance + "这是压缩前最后一个保存点：会话有长期价值（目标、决定及理由、否决路线、文件状态、学习进度、未完成事项）"
            "就一律先保存或刷新 checkpoint 并回读确认，压缩后要靠它找回细节；确实没有就跳过。" + where(sid) +
            "然后请再评估一次交接，并在最终回复正文最前面用一句话给出结论：需要就以「交接建议：」写明原因，"
            "用户确认后按 %s 技能保存交接并给出新对话的开场白；暂时不需要就用一句话说明理由。" % SKILL +
            "结论由你按整个会话的价值自己判断，不要为了完成提醒而硬提交接。用户确认前不写正式交接。")


def compact_note(compacted, sid):
    before = "，压缩前约 %s token" % wan(compacted["tokensBefore"]) if compacted.get("tokensBefore") else ""
    text = ("[handoff-guard] 本会话刚完成第 %d 次压缩（%s%s）。" % (
        compacted["count"], TRIGGER_LABEL.get(compacted["trigger"], compacted["trigger"]), before) +
        "压缩前的细节可能已丢失：如果会话有长期价值，先回读已有 checkpoint，按 %s 技能用压缩摘要和可核实的文件刷新它，" % SKILL +
        "再继续手上的事；没有值得恢复的信息就不用刷新。" + where(sid))
    if compacted["count"] >= STACKED_AT:
        text += "这已是第 %d 次压缩，%s" % (compacted["count"], STACKED)
    return text + "最后在最终回复正文最前面用一句话给出交接结论：需要就以「交接建议：」写明原因，暂时不需要就说一句理由。用户确认前不写正式交接。"


def nudge_note(tokens, sid):
    return ("[handoff-guard] 上下文约 %s token 时那次「压缩前最后一个保存点」提醒之后，一整轮过去了，"
            "任务目录下的 .handoff/checkpoints/ 里没有新写入的文件。会话有长期价值就现在先保存或刷新 checkpoint 并回读确认；"
            "判断确实不需要就忽略这条，回复里也不用提它。" % wan(tokens) + where(sid) + "这条只提醒一次。")


# --- the events ----------------------------------------------------------

def notes_for(state, event, via):
    sid = event["session_id"]
    notes = []
    wait = state.get("awaiting")
    if via == "prompt" and wait:
        if not checkpoint_written_since(event.get("cwd"), wait["at"]):
            notes.append(nudge_note(wait["tokens"], sid))
        state["awaiting"] = None
    if state.get("pending"):
        notes.append(compact_note(state["pending"], sid))
        state["pending"] = None

    seen = usage(event)
    if seen:
        tokens, window = seen
        line, source = compaction_line(state, sid, window)
        reached = [name for name, at in TIERS if tokens >= at(line)]
        if not reached:
            state["fired"] = []
        elif reached[-1] not in state["fired"]:
            state["fired"] = sorted(set(state["fired"]) | set(reached), key=[name for name, _ in TIERS].index)
            if reached[-1] == "reevaluate":
                state["awaiting"] = {"tokens": tokens, "at": time.time()}
            notes.append(tier_note(reached[-1], tokens, line, source, state["compactions"], sid))
    return [note + VIA_TOOL for note in notes] if via == "tool" else notes


def handle(event, state_dir):
    name = event.get("hook_event_name")
    sid = event.get("session_id")
    if not isinstance(sid, str) or not SESSION_ID.match(sid) or is_subagent(event):
        return None
    if name == "UserPromptSubmit" and str(event.get("prompt", "")).lstrip().startswith("/"):
        return None
    with State(state_dir, sid) as state:
        if name == "PreCompact":
            seen = usage(event)
            state["before"] = seen[0] if seen else 0
            return None
        if name == "PostCompact":
            state["compactions"] += 1
            state["pending"] = {"count": state["compactions"], "trigger": event.get("trigger") or "auto"}
            if state["before"]:
                state["pending"]["tokensBefore"] = state["before"]
            state["before"], state["fired"], state["awaiting"] = 0, [], None
            return None
        if name in ("UserPromptSubmit", "PostToolUse"):
            notes = notes_for(state, event, "prompt" if name == "UserPromptSubmit" else "tool")
            if notes:
                return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": "\n\n".join(notes)}}
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    args = parser.parse_args(argv)
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        result = handle(event, args.state_dir)
    except Exception as error:  # Never stand in Codex's way.
        print("handoff-guard: %s" % error, file=sys.stderr)
        return 0
    if result:
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
