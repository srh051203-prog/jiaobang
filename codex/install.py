"""Install (or remove) handoff-guard for Codex.

Copies the hook script to $CODEX_HOME/jiaobang/, the skill to
$CODEX_HOME/skills/jiaobang-handoff/, and adds four hooks to
$CODEX_HOME/hooks.json, keeping every hook already there. hooks.json is
backed up first. Running it again replaces only its own entries.

    python3 install.py            # install or update
    python3 install.py --uninstall
    py install.py                 # on Windows
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

HERE = Path(__file__).resolve().parent
EVENTS = {
    "UserPromptSubmit": None,
    "PostToolUse": ".*",
    "PreCompact": "^(auto|manual)$",
    "PostCompact": "^(auto|manual)$",
}
# Our own entries, wherever CODEX_HOME is and whichever path separator.
OURS = re.compile(r"[\\/]jiaobang[\\/]handoff_guard\.py\b")


# What PowerShell and cmd both take as a bare word, and inside double quotes.
BARE = re.compile(r"^[\w.:\\/~-]+$")
QUOTABLE = re.compile(r'^[^$`%"]+$')


def ours(hook):
    return bool(OURS.search(str(hook.get("command", ""))))


def short_path(path):
    """The 8.3 form of a Windows path (no spaces), or the path as it was."""
    try:
        import ctypes
        get = ctypes.windll.kernel32.GetShortPathNameW
        size = get(path, None, 0)
        buffer = ctypes.create_unicode_buffer(size)
        return buffer.value if size and get(path, buffer, size) else path
    except (AttributeError, ImportError, OSError):
        return path


def hook_command(script, python=sys.executable, windows=os.name == "nt"):
    """The hook's command line. Codex hands it to the user's shell: sh, zsh or
    bash on macOS and Linux; PowerShell, or cmd /C, on Windows. PowerShell runs
    a program only when it is named by a bare word (a quoted first word is just
    a string), so on Windows the interpreter goes first, unquoted."""
    script = str(script)
    if not windows:
        return '"%s" -X utf8 "%s"' % (python, script)
    program = python if BARE.match(python) else short_path(python)
    if not BARE.match(program):
        program = "py" if shutil.which("py") else "python"
    if not QUOTABLE.match(script):
        script = short_path(script)
    return '%s -X utf8 "%s"' % (program, script)


def without_ours(hooks):
    """hooks.json's hooks with every entry of ours taken out."""
    kept = {}
    for event, groups in hooks.items():
        left = []
        for group in groups:
            entries = [h for h in group.get("hooks", []) if not ours(h)]
            if entries:
                left.append(dict(group, hooks=entries))
        if left:
            kept[event] = left
    return kept


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install handoff-guard for Codex")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)

    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    script = home / "jiaobang" / "handoff_guard.py"
    skill = home / "skills" / "jiaobang-handoff"
    hooks_file = home / "hooks.json"

    config = {"hooks": {}}
    if hooks_file.exists():
        try:
            config = json.loads(hooks_file.read_text(encoding="utf-8"))
            hooks = config.get("hooks", {})
            if not isinstance(hooks, dict) or not all(
                    isinstance(groups, list) and all(isinstance(g, dict) and isinstance(g.get("hooks", []), list)
                                                     and all(isinstance(h, dict) for h in g.get("hooks", []))
                                                     for g in groups)
                    for groups in hooks.values()):
                raise ValueError("hooks 的结构不对")
        except (OSError, ValueError, AttributeError) as error:
            print("没有改动：读不懂 %s（%s），先修好它再运行。" % (hooks_file, error), file=sys.stderr)
            return 1
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = hooks_file.with_name("hooks.json.bak-jiaobang-%s" % stamp)
        number = 1
        while backup.exists():
            number += 1
            backup = hooks_file.with_name("hooks.json.bak-jiaobang-%s-%d" % (stamp, number))
        shutil.copy2(hooks_file, backup)
        print("已备份 hooks.json → %s" % backup)
    hooks = without_ours(config.get("hooks", {}))

    if args.uninstall:
        for path in (script, skill / "SKILL.md"):
            if path.exists():
                path.unlink()
        for folder in (script.parent, skill):
            try:
                folder.rmdir()  # only when nothing else is left in it
            except OSError:
                pass
        print("已删除提醒脚本和 jiaobang-handoff 技能")
    else:
        script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / "handoff_guard.py", script)
        skill.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / "skills" / "jiaobang-handoff" / "SKILL.md", skill / "SKILL.md")
        command = hook_command(script)
        for event, matcher in EVENTS.items():
            group = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
            if event in ("UserPromptSubmit", "PostToolUse"):
                group["hooks"][0]["additionalContextLimit"] = 4000
            if matcher:
                group["matcher"] = matcher
            hooks.setdefault(event, []).append(group)
        print("已安装：%s\n        %s" % (script, skill / "SKILL.md"))

    config["hooks"] = hooks
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = hooks_file.with_name("hooks.json.tmp-jiaobang")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, hooks_file)
    print("已更新 %s" % hooks_file)

    others = sorted({h.get("command", "") for groups in hooks.values() for g in groups for h in g.get("hooks", [])
                     if "handoff" in str(h.get("command", "")).lower() and not ours(h)})
    if others and not args.uninstall:
        print("\n注意：hooks.json 里还有别的交接类 hook，两套同时开会重复提醒：")
        for command in others:
            print("  - " + command)
    if not args.uninstall:
        print("\n重新打开 Codex 后生效；如果 Codex 询问是否信任新的 hooks，选信任。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
