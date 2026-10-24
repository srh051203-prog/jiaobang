# 交棒 · Codex 版

和 Claude Code 版同一套规则、同一种 `.handoff/` 交接文件：在 Claude Code 里写的交接，换到 Codex 里也能读着接着做，反过来也一样。

- **按真实用量提醒**：读 Codex 会话记录里的真实上下文用量，到自动压缩线的 70%（最晚 50 万）/ 80% / 90% 各提醒一次。
- **压缩线自动读**：优先用 Codex 自己记录的压缩线；其次是 `config.toml` 里的 `model_auto_compact_token_limit`；都没有才按窗口的 90% 估计，提醒里会标「估计」。
- **工具调用后也检查**，压缩后提醒先回读 checkpoint，从第 2 次压缩起提醒「摘要叠在摘要上」。
- **盯着 checkpoint**：90% 那次提醒后一整轮，任务目录的 `.handoff/checkpoints/` 里都没有新文件，就再提醒一次。
- **不重复唠叨**：状态按会话 ID 存在 `~/.codex/state/jiaobang/`（留最近 50 个会话）。
- 脚本只读不写交接、不开对话、不拦任何操作；出错就什么都不输出。

## 安装

需要 Python 3.9 或更新（macOS 自带的就行），只用 Python 自带的库。

```bash
git clone https://github.com/srh051203-prog/jiaobang.git
python3 jiaobang/codex/install.py
```

安装脚本会：

- 把提醒脚本复制到 `~/.codex/jiaobang/handoff_guard.py`
- 把技能复制到 `~/.codex/skills/jiaobang-handoff/`
- 先备份 `~/.codex/hooks.json`（`hooks.json.bak-jiaobang-<时间>`），再加上 UserPromptSubmit、PostToolUse、PreCompact、PostCompact 四个 hook，**原有的 hook 一个不动**

重新打开 Codex 后生效；如果 Codex 询问是否信任新的 hooks，选信任。再跑一次就是更新，不会重复添加。如果 `hooks.json` 里已经有别的交接类 hook，安装脚本会列出来提醒你：两套同时开会重复提醒。

卸载：

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
