import { expect, it, vi } from 'vitest'
import {
  chatScenarios,
  httpChat,
  isChatInput,
  isReply,
  sourceURL,
} from '../api/httpChat'
import { id, page, reply, result, source, stamp } from './chatFixtures'

it.each([
  null,
  'Safe editable draft',
  '<img src=x onerror=alert(1)>',
  'x'.repeat(2000),
])('accepts bounded support draft as plain text: %#', (draft) => {
  expect(
    isReply(
      reply('diagnose', {
        actions: [
          {
            type: 'contact_specialist',
            payload: { kind: 'support', comment_draft: draft },
          },
        ],
      }),
    ),
  ).toBe(true)
  expect(
    isReply(
      reply('diagnose', {
        actions: [
          {
            type: 'open_request_form',
            payload: { kind: 'support', items: [], comment_draft: draft },
          },
        ],
      }),
    ),
  ).toBe(true)
})
it.each(['', '  ', 'x'.repeat(2001), {}, 42])(
  'rejects malformed support draft: %#',
  (draft) => {
    const value = {
      ...reply(),
      actions: [
        {
          type: 'contact_specialist',
          payload: { kind: 'support', comment_draft: draft },
        },
      ],
    }
    expect(isReply(value)).toBe(false)
  },
)

it('uses same-origin paths, no cookies, no cache, and the owner header only', async () => {
  const outputs = [
    {
      session_id: id(1),
      session_token: 'b'.repeat(64),
      state_version: 0,
      expires_at: stamp,
      reply: reply(),
    },
    page(),
    result(),
    { items: [{ key: 'approved', title: 'Сценарий', model_id: id(8) }] },
  ]
  const fetcher = vi.fn(async () => ({
    ok: true,
    json: async () => outputs.shift(),
  }))
  vi.stubGlobal('fetch', fetcher)
  const created = await httpChat.create('diagnose')
  await httpChat.read(created.session_id, created.session_token, id(9))
  const body = JSON.stringify({
    session_id: id(1),
    request_id: id(2),
    expected_version: 0,
    input: { type: 'button', option_id: 'a' },
  })
  await httpChat.send(body, created.session_token)
  await chatScenarios(id(8), new AbortController().signal)
  expect(fetcher.mock.calls.length).toBe(4)
  // Intentionally test boolean properties so raw tokens cannot appear in diffs.
  const calls = fetcher.mock.calls as unknown as [string, RequestInit][]
  expect(
    calls.every(
      ([path, options]) =>
        path.startsWith('/api/v1/') &&
        options.credentials === 'omit' &&
        options.cache === 'no-store',
    ),
  ).toBe(true)
  expect(calls[1]![0]).toBe(`/api/v1/chat/sessions/${id(1)}?before=${id(9)}`)
  expect(new Headers(calls[1]![1].headers).has('X-Chat-Token')).toBe(true)
  expect(calls[2]![1].body === body).toBe(true)
  expect(calls.every(([path]) => !path.includes(created.session_token))).toBe(
    true,
  )
})
it.each([404, 410, 409, 422, 503])(
  'preserves HTTP %s error classification without exposing the body',
  async (status) => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status,
        json: async () => ({
          error: { code: 'STATE_CONFLICT', message: 'UNTRUSTED BODY' },
        }),
      })),
    )
    await expect(httpChat.read(id(1), 'a'.repeat(64))).rejects.toMatchObject({
      status,
      code: 'STATE_CONFLICT',
      message: 'Chat request failed',
    })
  },
)
it('treats timeout, malformed success and invalid action types as uncertainty', async () => {
  const fetcher = vi
    .fn()
    .mockRejectedValueOnce(new Error())
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        ...result(),
        reply: reply('knowledge', { actions: [{ type: 'execute' }] as never }),
      }),
    })
  vi.stubGlobal('fetch', fetcher)
  await expect(httpChat.send('{}', 'a'.repeat(64))).rejects.toMatchObject({
    status: 0,
  })
  await expect(httpChat.send('{}', 'a'.repeat(64))).rejects.toMatchObject({
    status: 0,
  })
})
it('allows only the exact document/version link and bounded public inputs', () => {
  expect(sourceURL(source)).toBe(source.url)
  for (const url of [
    'javascript:alert(1)',
    '//evil.invalid/file',
    `${source.url}?token=x`,
    `/api/v1/documents/${id(4)}/versions/${id(7)}/file`,
  ])
    expect(sourceURL({ ...source, url: url as SourceRefURL })).toBeNull()
  expect(isChatInput({ type: 'text', value: ' '.repeat(3) })).toBe(false)
  expect(isChatInput({ type: 'text', value: 'x'.repeat(1001) })).toBe(false)
  expect(isChatInput({ type: 'button', option_id: 'a', next: 'b' })).toBe(false)
})
type SourceRefURL = typeof source.url
