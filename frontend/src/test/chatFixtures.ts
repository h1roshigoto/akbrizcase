import { vi } from 'vitest'
import type { ChatSource, ChatMode } from '../api/httpChat'
import type {
  ChatReply,
  ChatSessionRead,
  ChatMessageResult,
  SourceRef,
  UUID,
} from '../api/types'
import { secretToken } from '../api/types'
export const id = (n: number) =>
  `00000000-0000-4000-8000-${String(n).padStart(12, '0')}` as UUID
export const stamp = '2099-01-01T00:00:00Z' as const
export const source: SourceRef = {
  section_id: id(3),
  document_id: id(4),
  document_version_id: id(5),
  title: 'Синтетический документ (предыдущая редакция)',
  section: 'Раздел',
  page_from: 2,
  page_to: 3,
  url: `/api/v1/documents/${id(4)}/versions/${id(5)}/file`,
}
export const reply = (
  mode: ChatMode = 'diagnose',
  patch: Partial<ChatReply> = {},
): ChatReply => ({
  mode,
  text: 'Синтетический ответ',
  resolution: 'no_match',
  buttons: [],
  actions: [],
  sources: [],
  items: [],
  ...patch,
})
export const result = (version = 1, r = reply()): ChatMessageResult => ({
  message_id: id(100 + version),
  state_version: version,
  reply: r,
})
export const page = (version = 0, r = reply()): ChatSessionRead => ({
  session_id: id(1),
  mode: r.mode,
  state_version: version,
  reply: r,
  messages: [],
  next_before: null,
  outcome: null,
  created_at: stamp,
  last_activity_at: stamp,
  expires_at: stamp,
})
export function transport() {
  return {
    create: vi
      .fn<ChatSource['create']>()
      .mockResolvedValue({
        session_id: id(1),
        session_token: secretToken('a'.repeat(64)),
        state_version: 0,
        expires_at: stamp,
        reply: reply(),
      }),
    read: vi.fn<ChatSource['read']>().mockResolvedValue(page()),
    send: vi.fn<ChatSource['send']>().mockResolvedValue(result()),
  }
}
export function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}
