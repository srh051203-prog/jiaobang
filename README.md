# 交棒 jiaobang

> 接力赛里，跑完这一棒的人把棒交给下一棒。长对话也一样：上下文快满时，把目标、决定和进度交给下一个会话接着跑。

**交棒**是一个 Claude Code 插件（`handoff-guard`），带一个 `handoff` 交接技能：

- **按真实用量提醒**：读 Claude Code 自己报告的上下文 token 数和自动压缩线，到压缩线的 70% / 80% / 90% 各提醒一次。
  - 70%：会话有长期价值就悄悄保存一份 checkpoint，不打扰你；没有就跳过。
  - 80%：评估要不要交接，需要就在回复开头写「交接建议：」。
  - 90%：压缩前最后一个保存点，有价值就一律先刷新 checkpoint。
- **工具调用后也检查**：一轮里连着调用很多工具时，冲过档位或中途压缩，下一次工具调用后就会提醒，不用等你下一条消息。
- **压缩前后都照顾到**：压缩时给摘要器加一份「必须保留」清单（目标、决定、否决路线、文件、进度）；压缩后提醒先回读 checkpoint。
- **盯着 checkpoint 有没有写**：90% 那次提醒后一整轮都没写，就再提醒一次。
- **不重复唠叨**：已提醒过的档位按会话 ID 存下来（记最近 50 个会话），续接、重启后一般不会再提醒一遍。
- **一句话交接**：说「交接」，就保存正式交接、回读核对，并在 Claude 桌面 App 里直接开一个新会话接手。

插件只负责提醒，不会自己写交接、开会话，也不会拦截任何操作；出错就直接放行。

> **用 Codex？** 看 [codex/README.md](codex/README.md)：同一套规则、同一种交接文件，在 Claude Code 写的交接到 Codex 里也能接着做。

## 安装

需要 **Claude Code 2.1.303 或更新**的版本（在 2.1.303 命令行和 2.1.308 桌面版上测过；插件接口还在早期测试阶段，以后的版本可能会变）。

在 Claude Code 终端会话里输入：

```text
/plugin install handoff-guard --marketplace srh051203-prog/jiaobang
```

提示添加插件市场时输入 `y`，再选安装范围。

或者在 shell 里：

```bash
claude plugin install handoff-guard --marketplace srh051203-prog/jiaobang
```

Claude 桌面 App 里 `/plugin` 用不了：点输入框旁边的 **+** → **Plugins** → **Add marketplace**，填 `srh051203-prog/jiaobang`，再 **Add plugin**。

装好后新开一个会话（或输入 `/reload-plugins`）就生效。

### Codex

需要 Python 3.9 或更新（在 macOS 上的 Codex CLI 0.163.0 测过；Windows 上的命令在 PowerShell 里实测过，还没在真的 Windows 电脑上跑过完整流程）。

macOS / Linux，在终端里：

```bash
git clone https://github.com/srh051203-prog/jiaobang.git
python3 jiaobang/codex/install.py
```

Windows，在 PowerShell 或「命令提示符」里（提示找不到 `py` 就换成 `python`）：

```powershell
git clone https://github.com/srh051203-prog/jiaobang.git
py jiaobang\codex\install.py
```

重新打开 Codex 后生效（问是否信任新的 hooks 时选信任）。安装脚本做了什么、怎么更新和卸载，看 [codex/README.md](codex/README.md)。

## 用法

- **自动**：什么都不用做，到档位会提醒。
- **手动**：随时说「保存进度」（只存 checkpoint）、「交接」（正式交接并开新会话）、「只保存交接」（不开新会话），或「读取 <交接文件> 继续」。也可以直接调用技能 `/handoff-guard:handoff`。

交接材料存在任务目录下：

```text
.handoff/
├── latest.md                # 正式交接入口
├── history/<时间>-<标识>.md  # 每次交接的历史副本，不覆盖
└── checkpoints/<会话ID>.md   # 每个会话自己的 checkpoint
```

里面是会话细节，**建议把 `.handoff/` 加进 `.gitignore`**（第一次在 git 仓库里用时，技能也会提醒你）。

## 调压缩线（可选）

1M 窗口的模型默认约 96.7 万 token 才自动压缩，三档在约 50 / 77 / 87 万，实际很少压到，长会话每轮都很贵。想早点压缩，在 `~/.claude/settings.json` 顶层加：

```json
{ "autoCompactWindow": 533000 }
```

- 取值 10 万到 100 万，或 `"auto"`；只给某个模型设，就写在 `modelSettings["<模型>"].autoCompactWindow`，它优先于顶层。也可以在会话里用 `/autocompact <数字>`。
- 真正的压缩线约等于窗口减 3.4 万左右的摘要缓冲：53.3 万 → 约 50 万，三档约 35 / 40 / 45 万。
- 取舍：调低更省钱，压缩会真的发生；但每次压缩都会丢细节，靠 checkpoint 找回。

插件会自动读压缩线，档位跟着变，不用改插件。

## 限制

- 这个插件只能装在 Claude Code 里；Codex 用 [codex/](codex/) 里的版本。DeepSeek Harness 等其他工具暂不支持。
- **自动开新会话只在 Claude 桌面 App 里可用**；终端里会给出一段可复制的恢复开场白。
- 公司统一管理 Claude Code 的电脑上，压缩相关的 hook（压缩计数、摘要保留清单）不会运行，档位提醒照常。

## 开发

```bash
claude plugin validate .
claude plugin test .
```

## English

**jiaobang** ("passing the baton") is a Claude Code plugin, `handoff-guard`, with a `handoff` skill. It reads Claude Code's real context usage and auto-compaction line, reminds the model at 70% / 80% / 90% of that line to save a checkpoint or weigh a handoff, also checks after the model's own tool calls, keeps what a handoff needs through compaction, and remembers per session which reminders it already gave. Saying "交接" (handoff) saves a handoff under `.handoff/` and, in the Claude desktop app, opens a new session to continue. The reminders and the skill are written in Chinese. Requires Claude Code 2.1.303 or later. A Codex version with the same rules and handoff files lives in [codex/](codex/).

## 许可证

[MIT](LICENSE)
