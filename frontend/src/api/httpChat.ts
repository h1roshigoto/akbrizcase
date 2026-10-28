import type {
  ChatMessageResult,
  ChatSessionCreated,
  ChatSessionRead,
  ChatScenarios,
  ChatSessionCreate,
  ChatReply,
  SourceRef,
  UUID,
  ChatMessageCreate,
} from './types'
import { isCatalogCard, isModelsPage } from './catalogResponse'

export type ChatInput = ChatMessageCreate['input']
export type ChatMode = ChatReply['mode']
export const record = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)
export const uuid = (v: unknown): v is UUID =>
  typeof v === 'string' &&
  /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(v)
const integer = (v: unknown, min = 0) =>
  Number.isSafeInteger(v) && Number(v) >= min
const keys = (v: Record<string, unknown>, expected: string[]) =>
  Object.keys(v).length === expected.length && expected.every((k) => k in v)
const code = (v: unknown) =>
  typeof v === 'string' && /^[A-Za-z0-9_.-]{1,100}$/.test(v)
export const isMode = (v: unknown): v is ChatMode =>
  v === 'diagnose' || v === 'knowledge' || v === 'commerce'
export function isChatInput(v: unknown): v is ChatInput {
  if (!record(v)) return false
  switch (v.type) {
    case 'text':
      return (
        keys(v, ['type', 'value']) &&
        typeof v.value === 'string' &&
        !!v.value.trim() &&
        v.value.length <= 1000
      )
    case 'select_mode':
      return keys(v, ['type', 'mode']) && isMode(v.mode)
    case 'start_diagnosis':
      return (
        keys(v, ['type', 'model_id', 'tree_key']) &&
        uuid(v.model_id) &&
        code(v.tree_key)
      )
    case 'button':
      return keys(v, ['type', 'option_id']) && code(v.option_id)
    case 'feedback':
      return keys(v, ['type', 'useful']) && typeof v.useful === 'boolean'
    default:
      return false
  }
}
export function sourceURL(source: SourceRef): string | null {
  return uuid(source.document_id) &&
    uuid(source.document_version_id) &&
    source.url ===
      `/api/v1/documents/${source.document_id}/versions/${source.document_version_id}/file`
    ? source.url
    : null
}
function isSource(v: unknown): v is SourceRef {
  return (
    record(v) &&
    uuid(v.section_id) &&
    uuid(v.document_id) &&
    uuid(v.document_version_id) &&
    typeof v.title === 'string' &&
    typeof v.section === 'string' &&
    ((v.page_from === null && v.page_to === null) ||
      (integer(v.page_from, 1) && integer(v.page_to, Number(v.page_from)))) &&
    typeof v.url === 'string' &&
    sourceURL(v as SourceRef) !== null
  )
}
function isAction(v: unknown): boolean {
  if (!record(v) || !record(v.payload)) return false
  const p = v.payload
  switch (v.type) {
    case 'open_item':
      return keys(p, ['item_id']) && uuid(p.item_id)
    case 'open_document':
      return isSource(p)
    case 'contact_specialist':
      return (
        p.kind === 'support' &&
        (p.comment_draft === undefined || p.comment_draft === null)
      )
    case 'open_request_form':
      if (!Array.isArray(p.items)) return false
      if (p.kind === 'support')
        return (
          p.items.length === 0 &&
          (p.comment_draft === undefined || p.comment_draft === null)
        )
      return (
        p.kind === 'commercial' &&
        p.items.length >= 1 &&
        p.items.length <= 50 &&
        p.items.every(
          (i) =>
            record(i) &&
            uuid(i.item_id) &&
            integer(i.qty, 1) &&
            Number(i.qty) <= 999,
        ) &&
        new Set(p.items.map((i) => i.item_id)).size === p.items.length
      )
    default:
      return false
  }
}
export function isReply(v: unknown): v is ChatReply {
  return (
    record(v) &&
    isMode(v.mode) &&
    typeof v.text === 'string' &&
    v.text.length > 0 &&
    v.text.length <= 10000 &&
    ['question', 'results', 'no_match', 'handoff', 'completed'].includes(
      String(v.resolution),
    ) &&
    Array.isArray(v.buttons) &&
    v.buttons.every(
      (b) =>
        record(b) &&
        typeof b.id === 'string' &&
        typeof b.label === 'string' &&
        isChatInput(b.input),
    ) &&
    Array.isArray(v.sources) &&
    v.sources.every(isSource) &&
    Array.isArray(v.items) &&
    v.items.every(isCatalogCard) &&
    Array.isArray(v.actions) &&
    v.actions.every(isAction)
  )
}
export class ChatError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super('Chat request failed')
  }
}
export interface ChatSource {
  create(mode: ChatMode): Promise<ChatSessionCreated>
  read(id: UUID, token: string, before?: UUID): Promise<ChatSessionRead>
  send(body: string, token: string): Promise<ChatMessageResult>
}
async function request<T>(
  path: string,
  validate: (v: unknown) => v is T,
  options: RequestInit = {},
): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api/v1/${path}`, {
      ...options,
      credentials: 'omit',
      cache: 'no-store',
      signal: options.signal ?? AbortSignal.timeout(30000),
      headers: { Accept: 'application/json', ...options.headers },
    })
  } catch {
    throw new ChatError(0, 'UNCERTAIN')
  }
  if (!response.ok) {
    let code = 'UNAVAILABLE'
    try {
      const v: unknown = await response.json()
      if (record(v) && record(v.error) && typeof v.error.code === 'string')
        code = v.error.code
    } catch {
      /* Never expose a transport body. */
    }
    throw new ChatError(response.status, code)
  }
  try {
    const v: unknown = await response.json()
    if (validate(v)) return v
  } catch {
    /* Invalid success after POST is also an uncertain outcome. */
  }
  throw new ChatError(0, 'UNCERTAIN')
}
const date = (v: unknown) =>
  typeof v === 'string' && Number.isFinite(Date.parse(v))
export const httpChat: ChatSource = {
  create: (mode: ChatSessionCreate['mode']) =>
    request(
      'chat/sessions',
      (v): v is ChatSessionCreated =>
        record(v) &&
        uuid(v.session_id) &&
        typeof v.session_token === 'string' &&
        /^[0-9a-f]{64}$/.test(v.session_token) &&
        v.state_version === 0 &&
        date(v.expires_at) &&
        isReply(v.reply),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      },
    ),
  read: (id, token, before) =>
    request(
      `chat/sessions/${encodeURIComponent(id)}${before ? `?before=${encodeURIComponent(before)}` : ''}`,
      (v): v is ChatSessionRead =>
        record(v) &&
        v.session_id === id &&
        isMode(v.mode) &&
        integer(v.state_version) &&
        date(v.expires_at) &&
        isReply(v.reply) &&
        v.mode === v.reply.mode &&
        Array.isArray(v.messages) &&
        v.messages.length <= 50 &&
        v.messages.every(
          (m) =>
            record(m) &&
            uuid(m.message_id) &&
            integer(m.state_version, 1) &&
            date(m.created_at) &&
            isReply(m.reply),
        ) &&
        (v.next_before === null || uuid(v.next_before)),
      { headers: { 'X-Chat-Token': token } },
    ),
  send: (body, token) =>
    request(
      'chat/messages',
      (v): v is ChatMessageResult =>
        record(v) &&
        uuid(v.message_id) &&
        integer(v.state_version, 1) &&
        isReply(v.reply),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Chat-Token': token },
        body,
      },
    ),
}
export const chatScenarios = (model: UUID, signal: AbortSignal) =>
  request(
    `chat/scenarios?model_id=${encodeURIComponent(model)}`,
    (v): v is ChatScenarios =>
      record(v) &&
      Array.isArray(v.items) &&
      v.items.length <= 10 &&
      v.items.every(
        (s) =>
          record(s) &&
          code(s.key) &&
          typeof s.title === 'string' &&
          s.model_id === model,
      ),
    { signal },
  )
export const chatModels = async (signal: AbortSignal) => {
  const models = []
  for (let offset = 0; offset <= 10000; offset += 100) {
    const page = await request(
      `equipment-models?limit=100&offset=${offset}`,
      isModelsPage,
      { signal },
    )
    models.push(...page.items)
    if (models.length >= page.total) return models
    if (!page.items.length) break
  }
  throw new ChatError(0, 'UNAVAILABLE')
}
