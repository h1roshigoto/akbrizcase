import { isChatInput, record, uuid } from '../api/httpChat'
import type { UUID } from '../api/types'

export const CHAT_KEY = 'akbrizcase.chat.v1'
export type Owner = {
  session_id: UUID
  session_token: string
  expires_at: string
  pending: string | null
}
export function parseOwner(raw: string | null): Owner | null {
  if (raw === null) return null
  if (raw.length > 18000) throw new Error('Invalid stored session')
  const v: unknown = JSON.parse(raw)
  if (
    !record(v) ||
    !uuid(v.session_id) ||
    typeof v.session_token !== 'string' ||
    !/^[0-9a-f]{64}$/.test(v.session_token) ||
    typeof v.expires_at !== 'string' ||
    !Number.isFinite(Date.parse(v.expires_at))
  )
    throw new Error('Invalid stored session')
  if (v.pending !== null) {
    if (typeof v.pending !== 'string' || v.pending.length > 16000)
      throw new Error('Invalid pending')
    const p: unknown = JSON.parse(v.pending)
    if (
      !record(p) ||
      Object.keys(p).length !== 4 ||
      p.session_id !== v.session_id ||
      !uuid(p.request_id) ||
      !Number.isSafeInteger(p.expected_version) ||
      Number(p.expected_version) < 0 ||
      !isChatInput(p.input)
    )
      throw new Error('Invalid pending')
  }
  return {
    session_id: v.session_id,
    session_token: v.session_token,
    expires_at: v.expires_at,
    pending: v.pending as string | null,
  }
}
