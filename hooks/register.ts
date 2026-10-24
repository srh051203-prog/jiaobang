// handoff-guard: tells the model, beside the user's prompt and after the
// model's own tool calls, when this session's real context use or a
// compaction makes it time to protect the session with the handoff skill. It
// hands over facts and the tier reached; the skill and the model decide what
// is worth saving. Nothing here writes a handoff, opens a session or blocks
// anything.
import type { EngineInterface, Register } from 'claude-code'

import type { HandoffGuardCompaction, HandoffGuardSaved, HandoffGuardTier } from '../types'

const fired = { plugin: 'handoff-guard', key: 'fired' } as const
const threshold = { plugin: 'handoff-guard', key: 'threshold' } as const
const compactions = { plugin: 'handoff-guard', key: 'compactions' } as const
const pending = { plugin: 'handoff-guard', key: 'pending' } as const
const before = { plugin: 'handoff-guard', key: 'before' } as const
const owner = { plugin: 'handoff-guard', key: 'owner' } as const
const awaiting = { plugin: 'handoff-guard', key: 'awaiting' } as const

// Only when the engine cannot report its own compaction point: models with a
// native 1M window compact at about 967K tokens by default.
const FALLBACK_RATIO = 0.967

// The engine's compaction point is read again after this many checks, so a
// mid-session /autocompact reaches the tiers before the next compaction does.
const REREAD_AFTER = 20

// `$.state` lives as long as the process; `$.store` keeps what each session's
// tiers and compactions came to, so a resumed or restarted session does not
// speak again for what it already said. The newest sessions are kept.
const SAVED = 'sessions'
const SAVED_MAX = 50

// Each tier speaks once per stretch of context; falling back under the first
// one (a compaction, /clear, a rewind) arms them all again.
const TIERS: { id: HandoffGuardTier; at: (compactAt: number) => number }[] = [
  { id: 'checkpoint', at: compactAt => Math.min(500_000, compactAt * 0.7) },
  { id: 'evaluate', at: compactAt => compactAt * 0.8 },
  { id: 'reevaluate', at: compactAt => compactAt * 0.9 },
]

// From this many compactions on, a summary is built on a summary and early
// detail fades generation by generation. It is told to the model as a reason
// to weigh, never as the answer: the model still judges each time.
const STACKED_AT = 2

const STACKED =
  '摘要叠在摘要上，早期细节会逐代丢失；把这点算进评估，但是否建议交接由你自己判断，不是默认结论。'

const KEEP =
  '为了之后能交接或恢复，摘要里务必保留：长期目标与完成标准；用户已确认的决定及理由（区分用户确认与助手建议）；' +
  '已否决的路线及原因；关键文件的绝对路径和状态；已完成、未完成、未验证的事项和下一步；' +
  '学习会话里已掌握的内容、纠正过的误区和练习进度；已有 handoff checkpoint 或交接文件的路径。'

const VIA_TOOL =
  '（这条是 handoff-guard 插件跟着工具结果附上的提醒，不是工具输出，也不是用户的新消息；先做完手上这一步再按它处理。）'

const TRIGGER_LABEL: Record<HandoffGuardCompaction['trigger'], string> = {
  auto: '自动压缩',
  manual: '手动 /compact',
}

const CHECKPOINTS = /\.handoff[\\/]checkpoints[\\/]/
// A shell command that writes rather than reads: a redirect into a file (not
// `2>&1` or `2>/dev/null`), or a program that edits or copies files.
const SHELL_WRITE = /(^|[^0-9&>])>>?\s*[^&\s]|\btee\b|\b(cp|mv|install)\s|\bsed\s+-i|\b(python3?|node|perl|ruby)\b/

/** The compaction instructions with what a later handoff needs kept added last. */
export function withKeep(instructions?: string): string {
  return instructions ? `${instructions}\n\n${KEEP}` : KEEP
}

/** Whether a tool call writes a handoff checkpoint, wherever the task directory is. */
export function isCheckpointWrite(tool: string, args: { file_path?: unknown; command?: unknown }): boolean {
  if (tool === 'Write' || tool === 'Edit') {
    return typeof args.file_path === 'string' && CHECKPOINTS.test(args.file_path)
  }
  if (tool === 'Bash') {
    return typeof args.command === 'string' && CHECKPOINTS.test(args.command) && SHELL_WRITE.test(args.command)
  }
  return false
}

export const register: Register = on => {
  on('prompt.submit', async ($, e, next) => {
    // A slash command (the handoff skill itself included) runs as typed.
    if (e.command || e.isMeta) {
      return next(e)
    }

    const notes = await notesFor($, 'prompt')

    return next(notes.length ? { ...e, context: [...(e.context ?? []), ...notes] } : e)
  })

  // The model hears it after its own tool calls too, so a long run of them
  // cannot carry the session past a tier, or past a compaction, unheard.
  on('tool.call', async ($, e, next) => {
    const ran = await next(e)
    if (ran.deny !== undefined) {
      return ran
    }

    if (!ran.isError && isCheckpointWrite(e.tool, e as { file_path?: unknown; command?: unknown })) {
      await $.state.set(awaiting, null)
    }

    // A subagent's loop is not this session's context.
    if (e.agentId) {
      return ran
    }

    const notes = await notesFor($, 'tool')

    return notes.length ? { ...ran, context: [...(ran.context ?? []), ...notes] } : ran
  })

  on('session.compact', ($, e, next) => {
    // A subagent's own transcript is not this session's context.
    if (e.agentId || !Array.isArray(e.messages)) {
      return next(e)
    }

    return next({ ...e, messages: e.messages, instructions: withKeep(e.instructions) })
  })

  // The settings hooks' own compaction events, subagents' left out: the size
  // before, then the count once the compaction stands.
  on('classic.PreCompact', async ($, e, next) => {
    if (!e.agent_id) {
      const { context } = await $.session.usage()
      if (context?.tokens !== undefined) {
        await $.state.set(before, context.tokens)
      }
    }

    return next(e)
  })

  on('classic.PostCompact', async ($, e, next) => {
    if (!e.agent_id) {
      const { sid, count } = await sync($)
      const { value: tokensBefore } = await $.state.get(before)
      await $.state.set(compactions, count + 1)
      await $.state.set(pending, { count: count + 1, trigger: e.trigger, ...(tokensBefore ? { tokensBefore } : {}) })
      await $.state.set(before, 0)
      await $.state.set(fired, [])
      await $.state.set(awaiting, null)
      await persist($, sid, [], count + 1)
    }

    return next(e)
  })
}

type Synced = { sid: string; done: HandoffGuardTier[]; count: number; isFresh: boolean }

// The values for the session running now. `$.state` reads one moment per
// dispatch, so what this sets is handed back rather than read again.
async function sync($: EngineInterface): Promise<Synced> {
  const sid = await $.session.id()
  const { value: current } = await $.state.get(owner)
  const { value: liveDone = [] } = await $.state.get(fired)
  const { value: liveCount = 0 } = await $.state.get(compactions)
  if (current === sid) {
    return { sid, done: liveDone, count: liveCount, isFresh: false }
  }

  // A process meeting this session for the first time: what an earlier one
  // saved for it, else (an older copy of this code just reloaded) what the
  // state already holds; a session that changed under it (/clear) starts over.
  const entry = (await saved($))[sid]
  const done = entry?.fired ?? (current === undefined ? liveDone : [])
  const count = entry?.compactions ?? (current === undefined ? liveCount : 0)
  await $.state.set(fired, done)
  await $.state.set(compactions, count)
  await $.state.set(awaiting, null)
  await $.state.set(owner, sid)

  return { sid, done, count, isFresh: true }
}

async function saved($: EngineInterface): Promise<HandoffGuardSaved> {
  try {
    const value = await $.store.get(SAVED)
    return value && typeof value === 'object' ? { ...(value as HandoffGuardSaved) } : {}
  } catch {
    return {}
  }
}

async function persist($: EngineInterface, sid: string, done: HandoffGuardTier[], count: number): Promise<void> {
  try {
    const all = await saved($)
    delete all[sid]
    all[sid] = { fired: done, compactions: count }
    const ids = Object.keys(all)
    for (const id of ids.slice(0, Math.max(0, ids.length - SAVED_MAX))) {
      delete all[id]
    }
    await $.store.set(SAVED, all)
  } catch {
    // Kept for this process alone; a restart may speak once more.
  }
}

async function notesFor($: EngineInterface, via: 'prompt' | 'tool'): Promise<string[]> {
  const { sid, done, count, isFresh } = await sync($)
  const notes: string[] = []

  // A whole turn went by after the last saving point asked for a checkpoint
  // and none was written: ask once more, never again.
  if (via === 'prompt' && !isFresh) {
    const { value: wait = null } = await $.state.get(awaiting)
    if (wait) {
      notes.push(nudgeNote(wait.tokens, sid))
      await $.state.set(awaiting, null)
    }
  }

  const { value: compacted = null } = await $.state.get(pending)
  if (compacted) {
    notes.push(compactNote(compacted, sid))
    await $.state.set(pending, null)
  }

  const { context } = await $.session.usage()
  if (context?.window && context.tokens !== undefined) {
    const tokens = context.tokens
    const compactAt = await compactPoint($, context.window, count)
    const reached = TIERS.filter(tier => tokens >= tier.at(compactAt))
    const top = reached.at(-1)

    if (!top) {
      if (done.length) {
        await $.state.set(fired, [])
        await persist($, sid, [], count)
      }
    } else if (!done.includes(top.id)) {
      const now = [...new Set([...done, ...reached.map(tier => tier.id)])]
      await $.state.set(fired, now)
      await persist($, sid, now, count)
      if (top.id === 'reevaluate') {
        await $.state.set(awaiting, { tokens })
      }
      notes.push(tierNote(top.id, tokens, compactAt, count, sid))
    }
  }

  return via === 'tool' ? notes.map(note => note + VIA_TOOL) : notes
}

// The engine's own compaction point for this session (it follows /autocompact,
// autoCompactWindow and CLAUDE_CODE_AUTO_COMPACT_WINDOW), read once per model
// window and compaction, and again every REREAD_AFTER checks.
async function compactPoint($: EngineInterface, window: number, count: number): Promise<number> {
  const { value: cached } = await $.state.get(threshold)
  if (cached && cached.window === window && cached.compactions === count && (cached.uses ?? 0) < REREAD_AFTER) {
    await $.state.set(threshold, { ...cached, uses: (cached.uses ?? 0) + 1 })
    return cached.value
  }

  try {
    const { context } = await $.session.usage({ breakdown: 'summary' })
    const breakdown = context.breakdown
    if (breakdown) {
      const value = breakdown.isAutoCompactEnabled && breakdown.autoCompactThreshold
        ? breakdown.autoCompactThreshold
        : window
      await $.state.set(threshold, { window, value, compactions: count, uses: 1 })
      return value
    }
  } catch {
    // Fall through to the last reading or the default point; the next check asks again.
  }

  return cached && cached.window === window ? cached.value : Math.round(window * FALLBACK_RATIO)
}

function where(sid: string): string {
  return `checkpoint 写到任务目录下的 .handoff/checkpoints/${sid}.md（本会话 ID）；` +
    '上下文里其他 ID 的 checkpoint（比如分叉前的会话）只读不写，要沿用就复制到本会话的文件，并注明从哪个会话分叉而来。'
}

function tierNote(tier: HandoffGuardTier, tokens: number, compactAt: number, count: number, sid: string): string {
  const head =
    `[handoff-guard] 当前上下文约 ${wan(tokens)} token，自动压缩线约 ${wan(compactAt)}` +
    `（已到 ${Math.round((tokens / compactAt) * 100)}%）。` +
    (count >= STACKED_AT ? `本会话已压缩 ${count} 次，${STACKED}` : '')

  switch (tier) {
    case 'checkpoint':
      return head +
        '先完成手上最小的工作单元，再按 handoff 技能综合评估整个会话：有长期价值（目标、决定及理由、否决路线、文件状态、学习进度）' +
        '就保存或刷新 checkpoint 并回读确认；确实没有就跳过。' + where(sid) +
        '这一档只做 checkpoint，不写正式交接，也不新建、发送或切换会话。' +
        '回复里不要提这条提醒，也不要汇报 checkpoint，用户问什么就答什么。'
    case 'evaluate':
      return head +
        '请按 handoff 技能评估整个会话是否需要交接：需要时先保存或刷新 checkpoint（' + where(sid) + '），' +
        '并在最终回复正文最前面单独一段以「交接建议：」写明原因、接续第一步，并说明确认后会保存交接、直接开新会话接手；' +
        '不需要时用一句话说明理由，到压缩线 90% 会再提醒。用户确认前不写正式交接、不开新会话。'
    case 'reevaluate':
      return head +
        (tokens < compactAt ? `距离自动压缩约 ${wan(compactAt - tokens)}。` : '已经超过自动压缩线，随时可能压缩。') +
        '这是压缩前最后一个保存点：会话有长期价值（目标、决定及理由、否决路线、文件状态、学习进度、未完成事项）' +
        '就一律先保存或刷新 checkpoint 并回读确认，压缩后要靠它找回细节；确实没有就跳过。' + where(sid) +
        '然后请再评估一次交接，并在最终回复正文最前面用一句话给出结论：需要就以「交接建议：」写明原因，' +
        '用户确认后按 handoff 技能保存交接并直接开新会话接手；暂时不需要就用一句话说明理由。' +
        '结论由你按整个会话的价值自己判断，不要为了完成提醒而硬提交接。用户确认前不写正式交接、不开新会话。'
  }
}

function compactNote(compacted: HandoffGuardCompaction, sid: string): string {
  const before = compacted.tokensBefore ? `，压缩前约 ${wan(compacted.tokensBefore)} token` : ''

  return `[handoff-guard] 本会话刚完成第 ${compacted.count} 次压缩（${TRIGGER_LABEL[compacted.trigger]}${before}）。` +
    '压缩前的细节可能已丢失：如果会话有长期价值，先回读已有 checkpoint，按 handoff 技能用压缩摘要和可核实的文件刷新它，再继续手上的事；' +
    '没有值得恢复的信息就不用刷新。' + where(sid) +
    (compacted.count >= STACKED_AT ? `这已是第 ${compacted.count} 次压缩，${STACKED}` : '') +
    '最后在最终回复正文最前面用一句话给出交接结论：需要就以「交接建议：」写明原因，暂时不需要就说一句理由。' +
    '用户确认前不写正式交接、不开新会话。'
}

function nudgeNote(tokens: number, sid: string): string {
  return `[handoff-guard] 上下文约 ${wan(tokens)} token 时那次「压缩前最后一个保存点」提醒之后，` +
    '一整轮过去了，还没看到写入 .handoff/checkpoints/ 的动作。会话有长期价值就现在先保存或刷新 checkpoint 并回读确认；' +
    '判断确实不需要就忽略这条，回复里也不用提它。' + where(sid) + '这条只提醒一次。'
}

function wan(tokens: number): string {
  return `${(tokens / 10_000).toFixed(1)} 万`
}
