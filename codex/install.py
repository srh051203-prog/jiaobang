"""Install (or remove) handoff-guard for Codex.

Copies the hook script to $CODEX_HOME/jiaobang/, the skill to
$CODEX_HOME/skills/jiaobang-handoff/, and adds four hooks to
$CODEX_HOME/hooks.json, keeping every hook already there. hooks.json is
backed up first. Running it again replaces only its own entries.

    python3 install.py            # install or update
    python3 install.py --uninstall
"""
import argparse
import json
import os
from pathlib import Path
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
MARK = "jiaobang/handoff_guard.py"


def without_ours(hooks):
    """hooks.json's hooks with every entry of ours taken out."""
    kept = {}
    for event, groups in hooks.items():
        left = []
        for group in groups:
            entries = [h for h in group.get("hooks", []) if MARK not in str(h.get("command", ""))]
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
        config = json.loads(hooks_file.read_text(encoding="utf-8"))
        backup = hooks_file.with_name("hooks.json.bak-jiaobang-%s" % time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(hooks_file, backup)
        print("已备份 hooks.json → %s" % backup)
    hooks = without_ours(config.get("hooks", {}))

    if args.uninstall:
        shutil.rmtree(script.parent, ignore_errors=True)
        shutil.rmtree(skill, ignore_errors=True)
        print("已删除提醒脚本和 jiaobang-handoff 技能")
    else:
        script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / "handoff_guard.py", script)
        skill.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / "skills" / "jiaobang-handoff" / "SKILL.md", skill / "SKILL.md")
        command = '"%s" -X utf8 "%s"' % (sys.executable, script)
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
    hooks_file.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已更新 %s" % hooks_file)

    others = sorted({h.get("command", "") for groups in hooks.values() for g in groups for h in g.get("hooks", [])
                     if "handoff" in str(h.get("command", "")).lower() and MARK not in str(h.get("command", ""))})
    if others and not args.uninstall:
        print("\n注意：hooks.json 里还有别的交接类 hook，两套同时开会重复提醒：")
        for command in others:
            print("  - " + command)
    if not args.uninstall:
        print("\n重新打开 Codex 后生效；如果 Codex 询问是否信任新的 hooks，选信任。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
