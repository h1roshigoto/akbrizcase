import { describe, expect, it, vi } from 'vitest'
import { ChatController } from '../chat/controller'
import { CHAT_KEY, parseOwner } from '../chat/storage'
import { ChatError } from '../api/httpChat'
import {
  deferred,
  id,
  page,
  reply,
  result,
  source,
  transport,
} from './chatFixtures'

const command = { type: 'button', option_id: 'a' } as const

describe('chat owner and exact request lifecycle', () => {
  it('creates once across concurrent opens and persists only the owner metadata', async () => {
    const api = transport(),
      controller = new ChatController(api)
    await Promise.all([controller.open(), controller.open()])
    expect(api.create).toHaveBeenCalledTimes(1)
    const stored = parseOwner(sessionStorage.getItem(CHAT_KEY))!
    expect(Object.keys(stored).sort()).toEqual([
      'expires_at',
      'pending',
      'session_id',
      'session_token',
    ])
    expect(stored.session_token.length).toBe(64)
    expect(controller.snapshot().ready).toBe(true)
    expect(localStorage.length).toBe(0)
  })
  it('restores through authorized GET and never reconstructs input text', async () => {
    const api = transport()
    await new ChatController(api).open()
    const read = page(2, reply('knowledge'))
    read.messages = [
      { ...result(2, reply('knowledge')), created_at: read.created_at },
    ]
    api.read.mockResolvedValue(read)
    const restored = new ChatController(api)
    await restored.open()
    expect(api.create).toHaveBeenCalledTimes(1)
    expect(api.read).toHaveBeenCalledTimes(1)
    expect(restored.snapshot().version).toBe(2)
    expect(restored.snapshot().messages[0]).not.toHaveProperty('input')
  })
  it.each(['knowledge', 'commerce', 'diagnose'] as const)(
    'confirms %s on the server and never switches optimistically',
    async (mode) => {
      const api = transport(),
        controller = new ChatController(api)
      await controller.open()
      const pending = deferred<ReturnType<typeof result>>()
      api.send.mockReturnValue(pending.promise)
      const sending = controller.send({ type: 'select_mode', mode })
      expect(controller.snapshot().reply?.mode).toBe('diagnose')
      pending.resolve(result(1, reply(mode)))
      await sending
      expect(controller.snapshot().reply?.mode).toBe(mode)
      expect(controller.snapshot().notice).toMatch(
        mode === 'diagnose' ? /сохранён/ : /сброшен/,
      )
      expect(JSON.parse(api.send.mock.calls[0]![0]).input).toEqual({
        type: 'select_mode',
        mode,
      })
    },
  )
  it('blocks independent sends, double clicks and mode switches while POST is unresolved', async () => {
    const api = transport(),
      controller = new ChatController(api)
    await controller.open()
    const pending = deferred<ReturnType<typeof result>>()
    api.send.mockReturnValue(pending.promise)
    const sending = controller.send(command)
    await controller.send(command)
    await controller.send({ type: 'select_mode', mode: 'commerce' })
    expect(api.send).toHaveBeenCalledTimes(1)
    pending.resolve(result())
    await sending
  })
  it.each([new ChatError(503, 'SERVICE_UNAVAILABLE'), new Error('network')])(
    'retains one frozen body on an uncertain POST, including after reload',
    async (error) => {
      const api = transport(),
        controller = new ChatController(api)
      await controller.open()
      api.send.mockRejectedValueOnce(error)
      await controller.send({ type: 'text', value: 'SYNTHETIC_PENDING' })
      const original = api.send.mock.calls[0]![0]
      expect(
        parseOwner(sessionStorage.getItem(CHAT_KEY))!.pending === original,
      ).toBe(true)
      await controller.send(command)
      const restored = new ChatController(api)
      await restored.open()
      expect(api.send).toHaveBeenCalledTimes(1)
      await restored.retry()
      expect(api.send.mock.calls[1]![0] === original).toBe(true)
      expect(restored.snapshot().pending).toBe(false)
      expect(parseOwner(sessionStorage.getItem(CHAT_KEY))!.pending).toBeNull()
      expect(
        sessionStorage.getItem(CHAT_KEY)?.includes('SYNTHETIC_PENDING'),
      ).toBe(false)
      expect(localStorage.length).toBe(0)
    },
  )
  it('updates a replayed exchange projection at the same ID instead of duplicating it', async () => {
    const api = transport(),
      controller = new ChatController(api)
    await controller.open()
    api.send.mockRejectedValueOnce(new ChatError(503, 'SERVICE_UNAVAILABLE'))
    await controller.send(command)
    const read = page(1, reply('knowledge', { sources: [source] }))
    read.messages = [{ ...result(1, read.reply), created_at: read.created_at }]
    api.read.mockResolvedValue(read)
    await controller.refresh()
    api.send.mockResolvedValue(
      result(1, reply('knowledge', { resolution: 'handoff' })),
    )
    await controller.retry()
    expect(controller.snapshot().messages).toHaveLength(1)
    expect(controller.snapshot().messages[0]!.reply.sources).toEqual([])
    expect(controller.snapshot().reply?.resolution).toBe('handoff')
  })
  it('does not roll back a newer GET when replay returns an older exchange', async () => {
    const api = transport(),
      controller = new ChatController(api)
    await controller.open()
    api.send.mockRejectedValueOnce(new Error())
    await controller.send(command)
    api.read.mockResolvedValue(page(5, reply('commerce')))
    await controller.refresh()
    api.send.mockResolvedValue(result(1, reply('diagnose')))
    await controller.retry()
    expect(controller.snapshot().version).toBe(5)
    expect(controller.snapshot().reply?.mode).toBe('commerce')
    expect(api.read).toHaveBeenCalledTimes(2)
  })
  it('handles STATE_CONFLICT with a GET and no automatic replay of the old button', async () => {
    const api = transport(),
      controller = new ChatController(api)
    await controller.open()
    api.send.mockRejectedValue(new ChatError(409, 'STATE_CONFLICT'))
    api.read.mockResolvedValue(page(4, reply('commerce')))
    await controller.send(command)
    expect(api.read).toHaveBeenCalledTimes(1)
    expect(api.send).toHaveBeenCalledTimes(1)
    expect(controller.snapshot().version).toBe(4)
    expect(controller.snapshot().pending).toBe(false)
    expect(controller.snapshot().notice).toMatch(/Выберите действие заново/)
  })
  it('keeps old controls inactive if conflict recovery GET fails', async () => {
    const api = transport(),
      controller = new ChatController(api)
    await controller.open()
    api.send.mockRejectedValue(new ChatError(409, 'STATE_CONFLICT'))
    api.read.mockRejectedValue(new ChatError(503, 'SERVICE_UNAVAILABLE'))
    await controller.send(command)
    expect(controller.snapshot().reply).toBeNull()
    expect(controller.snapshot().ready).toBe(false)
  })
  it('reports IDEMPOTENCY_CONFLICT without a new automatic UUID/POST', async () => {
    const api = transport(),
      controller = new ChatController(api)
    await controller.open()
    api.send.mockRejectedValue(new ChatError(409, 'IDEMPOTENCY_CONFLICT'))
    await controller.send(command)
    await controller.retry()
    await controller.send(command)
    expect(api.send).toHaveBeenCalledTimes(1)
    expect(api.read).not.toHaveBeenCalled()
    expect(controller.snapshot().ready).toBe(false)
    expect(controller.snapshot().notice).toMatch(/идентичности/)
  })
  it.each([404, 410])(
    'clears the owner and pending on %s, and requires a new explicit start',
    async (status) => {
      const api = transport(),
        controller = new ChatController(api)
      await controller.open()
      api.send.mockRejectedValue(
        new ChatError(status, status === 410 ? 'SESSION_EXPIRED' : 'NOT_FOUND'),
      )
      await controller.send(command)
      expect(sessionStorage.getItem(CHAT_KEY)).toBeNull()
      expect(controller.snapshot().terminal).toBe(true)
      await controller.retry()
      await controller.open()
      expect(api.create).toHaveBeenCalledTimes(1)
      await controller.start()
      expect(api.create).toHaveBeenCalledTimes(2)
      expect(api.send).toHaveBeenCalledTimes(1)
    },
  )
  it('shows GET 503 without cached technical replies and supports refresh', async () => {
    const api = transport(),
      controller = new ChatController(api)
    await controller.open()
    api.read.mockRejectedValueOnce(new ChatError(503, 'SERVICE_UNAVAILABLE'))
    await controller.refresh()
    expect(controller.snapshot().reply).toBeNull()
    expect(controller.snapshot().ready).toBe(false)
    await controller.refresh()
    expect(controller.snapshot().ready).toBe(true)
  })
  it('replaces all previously loaded source history on refresh after revoke', async () => {
    const api = transport(),
      controller = new ChatController(api)
    await controller.open()
    const old = page(
      1,
      reply('knowledge', { sources: [source], text: 'SYNTHETIC_QUOTE' }),
    )
    old.messages = [{ ...result(1, old.reply), created_at: old.created_at }]
    api.read
      .mockResolvedValueOnce(old)
      .mockResolvedValue(page(1, reply('knowledge', { resolution: 'handoff' })))
    await controller.refresh()
    await controller.refresh()
    expect(
      JSON.stringify(controller.snapshot()).includes('SYNTHETIC_QUOTE'),
    ).toBe(false)
    expect(controller.snapshot().messages).toEqual([])
  })
  it('merges pages by message_id and orders chronologically; refresh discards the page cache', async () => {
    const api = transport(),
      controller = new ChatController(api)
    await controller.open()
    const latest = page(3)
    latest.messages = [{ ...result(3), created_at: latest.created_at }]
    latest.next_before = id(103)
    const previous = page(3)
    previous.messages = [2, 1].map((n) => ({
      ...result(n),
      created_at: previous.created_at,
    }))
    api.read
      .mockResolvedValueOnce(latest)
      .mockResolvedValueOnce(latest)
      .mockResolvedValueOnce(previous)
      .mockResolvedValue(page(3))
    await controller.refresh()
    await controller.more()
    expect(api.read.mock.calls[2]![2]).toBe(id(103))
    expect(controller.snapshot().messages.map((m) => m.state_version)).toEqual([
      1, 2, 3,
    ])
    await controller.refresh()
    expect(controller.snapshot().messages).toEqual([])
  })
  it('rejects a late stale read instead of overwriting a newer state', async () => {
    const api = transport(),
      controller = new ChatController(api)
    await controller.open()
    await controller.send(command)
    await controller.refresh()
    expect(controller.snapshot().version).toBe(1)
    expect(controller.snapshot().ready).toBe(false)
  })
  it('reports unavailable sessionStorage and never falsely promises recovery', async () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error()
    })
    const api = transport(),
      controller = new ChatController(api)
    await controller.open()
    await controller.send(command)
    expect(controller.snapshot().storageFailed).toBe(true)
    expect(api.send).toHaveBeenCalledTimes(1)
    expect(localStorage.length).toBe(0)
  })
  it('does not execute corrupted stored inputs', async () => {
    sessionStorage.setItem(
      CHAT_KEY,
      JSON.stringify({
        session_id: id(1),
        session_token: 'a'.repeat(64),
        expires_at: '2099-01-01',
        pending: JSON.stringify({
          input: { type: 'execute', next: 'anything' },
        }),
      }),
    )
    const api = transport(),
      controller = new ChatController(api)
    await controller.open()
    expect(api.send).not.toHaveBeenCalled()
    expect(controller.snapshot().storageFailed).toBe(true)
  })
})

it('reopening during POST schedules authoritative refresh after it completes', async () => {
  const api = transport(),
    controller = new ChatController(api)
  await controller.open()
  const pending = deferred<ReturnType<typeof result>>()
  api.send.mockReturnValue(pending.promise)
  api.read.mockResolvedValue(page(2, reply('commerce')))
  const sending = controller.send(command)
  await controller.open()
  pending.resolve(result(1))
  await sending
  await vi.waitFor(() => expect(controller.snapshot().version).toBe(2))
  expect(api.read).toHaveBeenCalledTimes(1)
  expect(controller.snapshot().reply?.mode).toBe('commerce')
})
it('clears expired restored ownership after authorized GET without creating another session', async () => {
  const api = transport()
  await new ChatController(api).open()
  api.read.mockRejectedValue(new ChatError(410, 'SESSION_EXPIRED'))
  const restored = new ChatController(api)
  await restored.open()
  expect(sessionStorage.getItem(CHAT_KEY)).toBeNull()
  expect(restored.snapshot().terminal).toBe(true)
  expect(api.create).toHaveBeenCalledTimes(1)
})
it('refreshes already visible pages before appending history after source revocation', async () => {
  const api = transport(),
    controller = new ChatController(api)
  await controller.open()
  const old = page(
    3,
    reply('knowledge', { text: 'REVOKED_QUOTE', sources: [source] }),
  )
  old.messages = [{ ...result(3, old.reply), created_at: old.created_at }]
  old.next_before = id(103)
  const fresh = {
    ...old,
    reply: reply('knowledge', { resolution: 'handoff' }),
    messages: [
      {
        ...result(3, reply('knowledge', { resolution: 'handoff' })),
        created_at: old.created_at,
      },
    ],
  }
  const older = page(3, fresh.reply)
  older.messages = [{ ...result(2), created_at: old.created_at }]
  api.read
    .mockResolvedValueOnce(old)
    .mockResolvedValueOnce(fresh)
    .mockResolvedValueOnce(older)
  await controller.refresh()
  await controller.more()
  expect(controller.snapshot().messages).toHaveLength(2)
  expect(JSON.stringify(controller.snapshot()).includes('REVOKED_QUOTE')).toBe(
    false,
  )
})
