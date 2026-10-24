import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On, SessionUsage } from 'claude-code'

import { isCheckpointWrite, withKeep } from '../hooks/register'

type World = {
  tokens: number
  window?: number
  compactAt?: number
  isAutoCompactEnabled?: boolean
  hasBreakdown?: boolean
  sid?: string
  store?: Record<string, unknown>
  isStoreBroken?: boolean
  denies?: boolean
  failsWrites?: boolean
  turns?: number
  isTurnsBroken?: boolean
}

// Stands for the engine: the live context figures, and the prompt as it lands.
function world(on: On, init: World) {
  const live = { sid: 'sid-1', ...init, breakdownReads: 0 }

  on('session.id', () => ({ value: live.sid }))
  on('session.turns', () => {
    if (live.isTurnsBroken) {
      throw new Error('turns unavailable')
    }
    return { value: live.turns ?? 0 }
  })
  if (init.isStoreBroken) {
    on('store.get', () => {
      throw new Error('store unavailable')
    })
    on('store.set', () => {
      throw new Error('store unavailable')
    })
  } else {
    mock.store(on, init.store)
  }
  // Stands for the tool itself: an answer, a refusal when `denies`, an error
  // from Write when `failsWrites`.
  on('tool.call', (_$, e) =>
    live.denies
      ? { deny: 'no' }
      : live.failsWrites && e.tool === 'Write'
        ? { result: {}, isError: true as const }
        : { result: {} },
  )

  on('session.usage', (_$, e) => {
    const window = live.window ?? 1_000_000
    const context: Record<string, unknown> = { tokens: live.tokens, window, percent: Math.round((live.tokens / window) * 100) }
    if (e?.breakdown) {
      live.breakdownReads += 1
      if (live.hasBreakdown !== false) {
        context.breakdown = { autoCompactThreshold: live.compactAt ?? 967_000, isAutoCompactEnabled: live.isAutoCompactEnabled ?? true }
      }
    }
    return { value: { startedAt: 0, context, rateLimits: [] } as unknown as SessionUsage }
  })
  on('prompt.submit', (_$, e) => ({ text: e.text, context: e.context }))
  on('classic.PreCompact', () => ({}))
  on('classic.PostCompact', () => ({}))

  return live
}

async function notes($: Engine, text = '继续') {
  // The person's own prompt: the engine stamps the rest of the input.
  const typed = { text } as Parameters<Engine['prompt']['submit']>[0]
  return (await $.prompt.submit(typed)).context ?? []
}

// What the model reads after one of its own tool calls.
async function afterTool($: Engine, agentId?: string) {
  const ran = await $.tool.call({ tool: 'Bash', command: 'ls', ...(agentId ? { agentId } : {}) })
  return ran.context ?? []
}

async function saveCheckpoint($: Engine, sid = 'sid-1') {
  await $.tool.call({ tool: 'Write', file_path: `/work/.handoff/checkpoints/${sid}.md`, content: '# checkpoint' })
}

describe('handoff-guard tiers', () => {
  test('stays quiet below the first tier', async ($, on) => {
    world(on, { tokens: 300_000 })
    expect(await notes($)).toEqual([])
  })

  test('the checkpoint tier speaks once', async ($, on) => {
    const live = world(on, { tokens: 510_000 })
    const first = await notes($)
    expect(first.length).toBe(1)
    expect(first[0]).toContain('只做 checkpoint')
    expect(first[0]).toContain('96.7 万')

    live.tokens = 560_000
    expect(await notes($)).toEqual([])
  })

  test('80% asks for a handoff evaluation, 90% asks again', async ($, on) => {
    const live = world(on, { tokens: 510_000 })
    await notes($)

    live.tokens = 780_000
    const evaluate = await notes($)
    expect(evaluate.length).toBe(1)
    expect(evaluate[0]).toContain('是否需要交接')
    expect(evaluate[0]).toContain('交接建议：')
    expect(evaluate[0]).toContain('到压缩线 90% 会再提醒')

    live.tokens = 880_000
    const again = await notes($)
    expect(again.length).toBe(1)
    expect(again[0]).toContain('再评估一次交接')
    expect(again[0]).toContain('距离自动压缩约')
    expect(again[0]).toContain('自己判断')
    expect(again[0]).toContain('不要为了完成提醒而硬提交接')
    expect(again[0]).toContain('最后一个保存点')
    expect(again[0]).toContain('一律先保存或刷新 checkpoint')
    expect(again[0]).toContain('压缩后要靠它找回细节')

    await saveCheckpoint($)
    live.tokens = 900_000
    expect(await notes($)).toEqual([])
  })

  test('a jump past several tiers speaks only for the highest', async ($, on) => {
    const live = world(on, { tokens: 900_000 })
    const jumped = await notes($)
    expect(jumped.length).toBe(1)
    expect(jumped[0]).toContain('再评估一次交接')

    await saveCheckpoint($)
    live.tokens = 910_000
    expect(await notes($)).toEqual([])
  })

  test('past the compaction line it says so instead of a negative distance', async ($, on) => {
    world(on, { tokens: 690_000, compactAt: 67_000 })
    const past = (await notes($))[0]
    expect(past).toContain('已经超过自动压缩线')
    expect(past).not.toContain('约 -')
  })

  test('the tiers follow a lowered compaction line (about 500K)', async ($, on) => {
    const live = world(on, { tokens: 340_000, window: 533_000, compactAt: 500_000 })
    expect(await notes($)).toEqual([])

    live.tokens = 355_000
    const checkpoint = await notes($)
    expect(checkpoint[0]).toContain('只做 checkpoint')
    expect(checkpoint[0]).toContain('50.0 万')

    live.tokens = 405_000
    expect((await notes($))[0]).toContain('是否需要交接')

    live.tokens = 455_000
    expect((await notes($))[0]).toContain('再评估一次交接')
  })

  test('reads the engine compaction point once', async ($, on) => {
    const live = world(on, { tokens: 100_000, compactAt: 400_000 })
    await notes($)
    await notes($)
    expect(live.breakdownReads).toBe(1)

    live.tokens = 330_000
    const evaluate = await notes($)
    expect(evaluate[0]).toContain('是否需要交接')
    expect(evaluate[0]).toContain('40.0 万')
  })

  test('with auto-compaction off the window is the line', async ($, on) => {
    const live = world(on, { tokens: 690_000, isAutoCompactEnabled: false })
    expect((await notes($))[0]).toContain('只做 checkpoint')

    live.tokens = 810_000
    expect((await notes($))[0]).toContain('是否需要交接')
  })

  test('falls back to about 967K when the engine has no breakdown', async ($, on) => {
    world(on, { tokens: 780_000, hasBreakdown: false })
    const fallback = await notes($)
    expect(fallback[0]).toContain('是否需要交接')
    expect(fallback[0]).toContain('96.7 万')
  })
})

describe('handoff-guard compactions', () => {
  test('counts a compaction, notes it once and arms the tiers again', async ($, on) => {
    const live = world(on, { tokens: 900_000 })
    await notes($)

    await $.classic.PreCompact({ trigger: 'auto', custom_instructions: null })
    live.tokens = 40_000
    await $.classic.PostCompact({ trigger: 'auto', compact_summary: '摘要' })

    const after = await notes($)
    expect(after.length).toBe(1)
    expect(after[0]).toContain('第 1 次压缩（自动压缩，压缩前约 90.0 万 token）')
    expect(await notes($)).toEqual([])

    live.tokens = 510_000
    expect((await notes($))[0]).toContain('只做 checkpoint')
  })

  test('a second manual compaction is counted as the second', async ($, on) => {
    world(on, { tokens: 100_000 })
    await $.classic.PostCompact({ trigger: 'auto', compact_summary: '一' })
    const first = (await notes($))[0]
    expect(first).toContain('第 1 次压缩')
    expect(first).not.toContain('摘要叠在摘要上')
    expect(first).toContain('用一句话给出交接结论')

    await $.classic.PostCompact({ trigger: 'manual', compact_summary: '二' })
    const second = (await notes($))[0]
    expect(second).toContain('第 2 次压缩（手动 /compact）')
    expect(second).toContain('摘要叠在摘要上')
    expect(second).toContain('由你自己判断')
    expect(second).not.toContain('建议用户交接')
  })

  test('from the second compaction on the tier notes carry the count but still leave the call to the model', async ($, on) => {
    const live = world(on, { tokens: 100_000 })
    await $.classic.PostCompact({ trigger: 'auto', compact_summary: '一' })
    await $.classic.PostCompact({ trigger: 'auto', compact_summary: '二' })
    await notes($)

    live.tokens = 510_000
    const checkpoint = (await notes($))[0]
    expect(checkpoint).toContain('本会话已压缩 2 次')
    expect(checkpoint).toContain('由你自己判断')

    live.tokens = 880_000
    const again = (await notes($))[0]
    expect(again).toContain('本会话已压缩 2 次')
    expect(again).toContain('不要为了完成提醒而硬提交接')
  })

  test('one compaction is not enough to mention stacked summaries in the tiers', async ($, on) => {
    const live = world(on, { tokens: 100_000 })
    await $.classic.PostCompact({ trigger: 'auto', compact_summary: '一' })
    await notes($)

    live.tokens = 510_000
    expect((await notes($))[0]).not.toContain('已压缩 1 次')
  })

  test('a subagent compacting its own transcript is not counted', async ($, on) => {
    world(on, { tokens: 100_000 })
    await $.classic.PostCompact({ trigger: 'auto', compact_summary: 'sub', agent_id: 'agent-1', agent_type: 'Explore' })
    expect(await notes($)).toEqual([])
  })

  test('adds what a handoff needs after the person\'s own /compact instructions', () => {
    expect(withKeep()).toContain('已否决的路线及原因')
    const mine = withKeep('只保留 SQL 部分')
    expect(mine.startsWith('只保留 SQL 部分\n\n')).toBe(true)
    expect(mine).toContain('handoff checkpoint')
  })
})

describe('handoff-guard after tool calls', () => {
  test('a tier reached during a run of tool calls is told after the tool result', async ($, on) => {
    const live = world(on, { tokens: 300_000, window: 533_000, compactAt: 500_000 })
    expect(await notes($)).toEqual([])

    live.tokens = 455_000
    const told = await afterTool($)
    expect(told.length).toBe(1)
    expect(told[0]).toContain('再评估一次交接')
    expect(told[0]).toContain('跟着工具结果附上')
    expect(told[0]).toContain('不是用户的新消息')
  })

  test('a tier told on one path is not told again on the other', async ($, on) => {
    const live = world(on, { tokens: 355_000, window: 533_000, compactAt: 500_000 })
    expect((await notes($))[0]).toContain('只做 checkpoint')
    expect(await afterTool($)).toEqual([])

    live.tokens = 405_000
    expect((await afterTool($))[0]).toContain('是否需要交接')
    expect(await notes($)).toEqual([])
  })

  test('the beginning of a long run of tool calls stays quiet', async ($, on) => {
    world(on, { tokens: 200_000 })
    expect(await afterTool($)).toEqual([])
    expect(await afterTool($)).toEqual([])
  })

  test('a subagent\'s tool call carries nothing and leaves the tier for the main loop', async ($, on) => {
    world(on, { tokens: 455_000, window: 533_000, compactAt: 500_000 })
    expect(await afterTool($, 'agent-1')).toEqual([])
    expect((await notes($))[0]).toContain('再评估一次交接')
  })

  test('a refused call is handed on as it was', async ($, on) => {
    world(on, { tokens: 455_000, window: 533_000, compactAt: 500_000, denies: true })
    const ran = await $.tool.call({ tool: 'Bash', command: 'ls' })
    expect(ran.deny).toBe('no')
    expect(ran.context).toBeUndefined()
    expect((await notes($))[0]).toContain('再评估一次交接')
  })

  test('a compaction in the middle of a turn is told at the next tool call', async ($, on) => {
    const live = world(on, { tokens: 480_000, window: 533_000, compactAt: 500_000 })
    await notes($)

    await $.classic.PreCompact({ trigger: 'auto', custom_instructions: null })
    live.tokens = 60_000
    await $.classic.PostCompact({ trigger: 'auto', compact_summary: '摘要' })

    const told = await afterTool($)
    expect(told.length).toBe(1)
    expect(told[0]).toContain('第 1 次压缩（自动压缩，压缩前约 48.0 万 token）')
    expect(told[0]).toContain('先回读已有 checkpoint')
    expect(await notes($)).toEqual([])
  })
})

describe('handoff-guard checkpoint watch', () => {
  test('the last saving point asks once more after a turn with no checkpoint written', async ($, on) => {
    world(on, { tokens: 455_000, window: 533_000, compactAt: 500_000 })
    expect((await notes($))[0]).toContain('最后一个保存点')
    expect(await afterTool($)).toEqual([])

    const nudge = await notes($)
    expect(nudge.length).toBe(1)
    expect(nudge[0]).toContain('45.5 万')
    expect(nudge[0]).toContain('还没看到写入 .handoff/checkpoints/')
    expect(nudge[0]).toContain('判断确实不需要就忽略这条')
    expect(nudge[0]).toContain('只提醒一次')
    expect(await notes($)).toEqual([])
  })

  test('a checkpoint written after the last saving point leaves nothing owed', async ($, on) => {
    world(on, { tokens: 455_000, window: 533_000, compactAt: 500_000 })
    await notes($)
    await saveCheckpoint($)
    expect(await notes($)).toEqual([])
  })

  test('a checkpoint written by a shell command counts too', async ($, on) => {
    world(on, { tokens: 455_000, window: 533_000, compactAt: 500_000 })
    await notes($)
    await $.tool.call({ tool: 'Bash', command: "cat > /work/.handoff/checkpoints/sid-1.md <<'EOF'\n# checkpoint\nEOF" })
    expect(await notes($)).toEqual([])
  })

  test('a failed write is not a checkpoint', async ($, on) => {
    world(on, { tokens: 455_000, window: 533_000, compactAt: 500_000, failsWrites: true })
    await notes($)
    await saveCheckpoint($)
    expect((await notes($))[0]).toContain('还没看到写入')
  })

  test('a subagent writing a checkpoint does not stand for the main session', async ($, on) => {
    world(on, { tokens: 455_000, window: 533_000, compactAt: 500_000 })
    await notes($)
    await $.tool.call({ tool: 'Write', file_path: '/work/.handoff/checkpoints/other.md', content: '#', agentId: 'agent-1' })
    expect((await notes($))[0]).toContain('还没看到写入')
  })

  test('the earlier tiers ask for no second look', async ($, on) => {
    const live = world(on, { tokens: 355_000, window: 533_000, compactAt: 500_000 })
    await notes($)
    expect(await notes($)).toEqual([])

    live.tokens = 405_000
    await notes($)
    expect(await notes($)).toEqual([])
  })

  test('tells writes from reads', () => {
    expect(isCheckpointWrite('Write', { file_path: '/a/.handoff/checkpoints/s.md' })).toBe(true)
    expect(isCheckpointWrite('Edit', { file_path: '/a/.handoff/checkpoints/s.md' })).toBe(true)
    expect(isCheckpointWrite('Write', { file_path: '/a/.handoff/latest.md' })).toBe(false)
    expect(isCheckpointWrite('Read', { file_path: '/a/.handoff/checkpoints/s.md' })).toBe(false)
    expect(isCheckpointWrite('Bash', { command: 'ls -la .handoff/checkpoints/s.md 2>&1 | head' })).toBe(false)
    expect(isCheckpointWrite('Bash', { command: 'sed -n 1,20p .handoff/checkpoints/s.md 2>/dev/null' })).toBe(false)
    expect(isCheckpointWrite('Bash', { command: 'echo note >> .handoff/checkpoints/s.md' })).toBe(true)
    expect(isCheckpointWrite('Bash', { command: "python3 -I - <<'EOF'\np='.handoff/checkpoints/s.md'\nEOF" })).toBe(true)
    expect(isCheckpointWrite('Bash', { command: 'cp a.md .handoff/checkpoints/s.md' })).toBe(true)
    expect(isCheckpointWrite('Bash', { command: 'mv /tmp/s.md "/w/.handoff/checkpoints/s.md" && ls' })).toBe(true)
    expect(isCheckpointWrite('Bash', { command: 'printf x | tee -a .handoff/checkpoints/s.md' })).toBe(true)
    expect(isCheckpointWrite('Bash', { command: "sed -i '' 's/a/b/' .handoff/checkpoints/s.md" })).toBe(true)
    // Copying a checkpoint out, or comparing inside quotes, reads it.
    expect(isCheckpointWrite('Bash', { command: 'cp .handoff/checkpoints/s.md backup.md' })).toBe(false)
    expect(isCheckpointWrite('Bash', { command: "awk '$1 > 5' .handoff/checkpoints/s.md" })).toBe(false)
    expect(isCheckpointWrite('Bash', { command: 'cat .handoff/checkpoints/s.md > /tmp/copy.md' })).toBe(false)
  })

  test('the notes name this session\'s own checkpoint and keep the first tier out of the reply', async ($, on) => {
    world(on, { tokens: 510_000, sid: 'abc-123' })
    const checkpoint = (await notes($))[0]
    expect(checkpoint).toContain('.handoff/checkpoints/abc-123.md')
    expect(checkpoint).toContain('只读不写')
    expect(checkpoint).toContain('回复里不要提这条提醒')
  })
})

describe('handoff-guard sessions', () => {
  test('a session saved by an earlier process does not speak again for its tiers', async ($, on) => {
    const live = world(on, {
      tokens: 360_000,
      window: 533_000,
      compactAt: 500_000,
      store: { sessions: { 'sid-1': { fired: ['checkpoint'], compactions: 2 } } },
    })
    expect(await notes($)).toEqual([])

    live.tokens = 405_000
    const evaluate = (await notes($))[0]
    expect(evaluate).toContain('是否需要交接')
    expect(evaluate).toContain('本会话已压缩 2 次')
  })

  test('a new session id starts the tiers over, and the old one is remembered', async ($, on) => {
    const live = world(on, { tokens: 355_000, window: 533_000, compactAt: 500_000 })
    expect((await notes($))[0]).toContain('checkpoints/sid-1.md')

    live.sid = 'sid-2'
    const fresh = (await notes($))[0]
    expect(fresh).toContain('只做 checkpoint')
    expect(fresh).toContain('checkpoints/sid-2.md')

    live.sid = 'sid-1'
    expect(await notes($)).toEqual([])
  })

  test('a conversation carried over under a new id does not hear again what it reached', async ($, on) => {
    const live = world(on, { tokens: 360_000, window: 533_000, compactAt: 500_000, sid: 'reopened', turns: 12 })
    expect(await notes($)).toEqual([])
    expect(await afterTool($)).toEqual([])

    live.tokens = 405_000
    expect((await notes($))[0]).toContain('是否需要交接')
  })

  test('a conversation carried over past the last saving point still hears it', async ($, on) => {
    world(on, { tokens: 455_000, window: 533_000, compactAt: 500_000, sid: 'reopened', turns: 12 })
    expect((await notes($))[0]).toContain('最后一个保存点')
  })

  test('a new conversation that starts big still hears its tier', async ($, on) => {
    world(on, { tokens: 360_000, window: 533_000, compactAt: 500_000, sid: 'fresh', turns: 1 })
    expect((await notes($))[0]).toContain('只做 checkpoint')
  })

  test('a turn count that fails still lets the tier through', async ($, on) => {
    world(on, { tokens: 360_000, window: 533_000, compactAt: 500_000, sid: 'unknown', isTurnsBroken: true })
    expect((await notes($))[0]).toContain('只做 checkpoint')
  })

  test('a store that fails still lets the notes through', async ($, on) => {
    const live = world(on, { tokens: 355_000, window: 533_000, compactAt: 500_000, isStoreBroken: true })
    expect((await notes($))[0]).toContain('只做 checkpoint')
    expect(await notes($)).toEqual([])

    live.tokens = 405_000
    expect((await afterTool($))[0]).toContain('是否需要交接')
  })

  test('reads the compaction point again after 20 checks', async ($, on) => {
    const live = world(on, { tokens: 100_000 })
    for (let i = 0; i < 20; i++) {
      await notes($)
    }
    expect(live.breakdownReads).toBe(1)

    live.compactAt = 400_000
    live.tokens = 330_000
    const evaluate = (await notes($))[0]
    expect(live.breakdownReads).toBe(2)
    expect(evaluate).toContain('是否需要交接')
    expect(evaluate).toContain('40.0 万')
  })
})
