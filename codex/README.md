# 交棒 · Codex 版

和 Claude Code 版同一套规则、同一种 `.handoff/` 交接文件：在 Claude Code 里写的交接，换到 Codex 里也能读着接着做，反过来也一样。

- **按真实用量提醒**：读 Codex 会话记录里的真实上下文用量，到自动压缩线的 70%（最晚 50 万）/ 80% / 90% 各提醒一次。
- **压缩线自动读**：优先用 Codex 自己记录的压缩线；其次是 `config.toml` 里的 `model_auto_compact_token_limit`；都没有才按窗口的 90% 估计，提醒里会标「估计」。
- **工具调用后也检查**，压缩后提醒先回读 checkpoint，从第 2 次压缩起提醒「摘要叠在摘要上」。
- **盯着 checkpoint**：90% 那次提醒后一整轮，任务目录的 `.handoff/checkpoints/` 里都没有新文件，就再提醒一次。
- **不重复唠叨**：状态按会话 ID 存在 `~/.codex/state/jiaobang/`（留最近 50 个会话）。
- 脚本只读不写交接、不开对话、不拦任何操作；出错时不附提醒、照常放行（错误只写到 stderr）。

## 安装

需要 Python 3.9 或更新，只用 Python 自带的库：macOS 自带的就行；Windows 从 [python.org](https://www.python.org/downloads/) 或 Microsoft Store 装一个。

macOS / Linux，在终端里：

```bash
git clone https://github.com/srh051203-prog/jiaobang.git
python3 jiaobang/codex/install.py
```

Windows，在 PowerShell 或「命令提示符」里：

```powershell
git clone https://github.com/srh051203-prog/jiaobang.git
py jiaobang\codex\install.py
```

提示找不到 `py` 就换成 `python`。没装 git 的话，在仓库页面点 **Code → Download ZIP**，解压后把命令里的 `jiaobang` 换成解压出来的文件夹名（一般是 `jiaobang-main`）。

测过的情况：在 macOS 上的 Codex CLI 0.163.0 里，真的 `codex exec` 会调用这四个 hook，提醒会送到模型。Windows 上 Codex 用 PowerShell（或 cmd）运行 hook，安装脚本生成的是两种都能直接跑的命令，这条命令在 PowerShell 7 里实测过；还没在真的 Windows 电脑上跑过完整流程。脚本读的会话记录和日志格式不是 Codex 的公开接口，以后的版本可能会变。

安装脚本会：

- 把提醒脚本复制到 `~/.codex/jiaobang/handoff_guard.py`
- 把技能复制到 `~/.codex/skills/jiaobang-handoff/`
- 先备份 `~/.codex/hooks.json`（`hooks.json.bak-jiaobang-<时间>`），再加上 UserPromptSubmit、PostToolUse、PreCompact、PostCompact 四个 hook，**原有的 hook 一个不动**；`hooks.json` 读不懂时什么都不改，让你先修好

重新打开 Codex 后生效；如果 Codex 询问是否信任新的 hooks，选信任。再跑一次就是更新，不会重复添加。如果 `hooks.json` 里已经有别的交接类 hook，安装脚本会列出来提醒你：两套同时开会重复提醒。

卸载（Windows 上同样把 `python3` 换成 `py`）：

```bash
python3 jiaobang/codex/install.py --uninstall
```

用了别的 `CODEX_HOME`，安装和卸载都会跟着它走。

## 用法

- **自动**：什么都不用做，到档位会提醒。
- **手动**：说「保存进度」（只存 checkpoint）、「交接」（写正式交接），或「读取 <交接文件> 继续」；也可以直接点名 `jiaobang-handoff` 技能。
- Codex 里模型不能自己开新对话：交接写好后，它会给你一段开场白，新开一个对话粘贴进去就能接着做。

## 测试

```bash
cd jiaobang/codex
python3 -m unittest discover -s tests -v
```
