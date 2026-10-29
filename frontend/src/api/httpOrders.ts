import {
  secretToken,
  type ConsentText,
  type PrivacyPolicy,
  type ChatHeaders,
  type OrderCreate,
  type OrderHeaders,
  type OrderReceipt,
} from './types'
import { toKopecks } from '../state/money'

export type OrderResult =
  | { kind: 'success'; status: 200 | 201; receipt: OrderReceipt }
  | {
      kind:
        | 'catalog_changed'
        | 'consent_changed'
        | 'idempotency_conflict'
        | 'not_found'
        | 'validation'
        | 'chat_expired'
    }
  | { kind: 'uncertain' }

export interface OrdersSource {
  getConsent(signal: AbortSignal): Promise<ConsentText>
  getPolicy(signal: AbortSignal): Promise<PrivacyPolicy>
  submit(
    body: string,
    headers: OrderHeaders,
    chat?: ChatHeaders,
  ): Promise<OrderResult>
}

const record = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)
const money = (v: unknown): v is OrderReceipt['known_subtotal'] =>
  typeof v === 'string' && /^\d+(\.\d{1,2})?$/.test(v)
export function isReceipt(v: unknown): v is OrderReceipt {
  if (
    !record(v) ||
    !Number.isSafeInteger(v.number) ||
    Number(v.number) < 1 ||
    v.status !== 'new' ||
    v.currency !== 'RUB' ||
    !money(v.known_subtotal)
  )
    return false
  if (
    ![v.items_total, v.delivery_cost, v.total_amount].every(
      (x) => x === null || money(x),
    )
  )
    return false
  if (v.delivery_status === 'not_required') {
    if (!money(v.delivery_cost) || toKopecks(v.delivery_cost) !== 0n)
      return false
  } else if (v.delivery_status !== 'pending_quote' || v.delivery_cost !== null)
    return false
  if (
    money(v.items_total) &&
    toKopecks(v.items_total) !== toKopecks(v.known_subtotal)
  )
    return false
  const total =
    money(v.items_total) && money(v.delivery_cost)
      ? toKopecks(v.items_total) + toKopecks(v.delivery_cost)
      : null
  return total === null
    ? v.total_amount === null
    : money(v.total_amount) && toKopecks(v.total_amount) === total
}

export function newOrderHeaders(): OrderHeaders {
  const bytes = crypto.getRandomValues(new Uint8Array(32))
  return {
    'Idempotency-Key': crypto.randomUUID(),
    'X-Request-Token': secretToken(
      Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join(''),
    ),
  }
}

/** Freeze the approved wire body once; retries never reserialize form state. */
export const orderSnapshot = (body: OrderCreate): string => JSON.stringify(body)

export const httpOrders: OrdersSource = {
  async getPolicy(signal) {
    const response = await fetch('/api/v1/privacy-policy', {
      method: 'GET',
      signal,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    })
    if (!response.ok) throw new Error('Политика недоступна.')
    const v: unknown = await response.json()
    if (
      !record(v) ||
      !['version', 'text', 'withdrawal_instructions', 'consent_version'].every(
        (key) => typeof v[key] === 'string' && v[key].trim().length > 0,
      ) ||
      typeof v.checksum !== 'string' ||
      !/^[0-9a-f]{64}$/.test(v.checksum) ||
      typeof v.synthetic !== 'boolean'
    )
      throw new Error('Некорректная политика.')
    return {
      version: v.version,
      text: v.text,
      checksum: v.checksum,
      withdrawal_instructions: v.withdrawal_instructions,
      consent_version: v.consent_version,
      synthetic: v.synthetic,
    } as PrivacyPolicy
  },
  async getConsent(signal) {
    const response = await fetch('/api/v1/consent-text', {
      method: 'GET',
      signal,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    })
    if (!response.ok) throw new Error('Не удалось загрузить согласие.')
    const v: unknown = await response.json()
    if (
      !record(v) ||
      !['version', 'purpose', 'text'].every(
        (key) => typeof v[key] === 'string' && v[key].length > 0,
      ) ||
      typeof v.checksum !== 'string' ||
      !/^[0-9a-f]{64}$/.test(v.checksum)
    )
      throw new Error('Некорректный ответ согласия.')
    return {
      version: v.version,
      purpose: v.purpose,
      text: v.text,
      checksum: v.checksum,
    } as ConsentText
  },
  async submit(body, headers, chat) {
    try {
      const response = await fetch('/api/v1/orders', {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        signal: AbortSignal.timeout(30000),
        headers: {
          ...headers,
          ...chat,
          'Content-Type': 'application/json',
          Accept: 'application/json',
        },
        body,
      })
      const value: unknown = await response.json()
      if (
        (response.status === 200 || response.status === 201) &&
        isReceipt(value)
      ) {
        // Project only the safe receipt, even if an unexpected server adds fields.
        const {
          number,
          status,
          known_subtotal,
          items_total,
          delivery_status,
          delivery_cost,
          total_amount,
          currency,
        } = value
        return {
          kind: 'success',
          status: response.status,
          receipt: {
            number,
            status,
            known_subtotal,
            items_total,
            delivery_status,
            delivery_cost,
            total_amount,
            currency,
          },
        }
      }
      if (
        !record(value) ||
        !record(value.error) ||
        typeof value.error.code !== 'string' ||
        typeof value.error.message !== 'string' ||
        !record(value.error.details) ||
        typeof value.request_id !== 'string'
      )
        return { kind: 'uncertain' }
      const code = value.error.code
      if (response.status === 409) {
        if (code === 'CATALOG_CHANGED') return { kind: 'catalog_changed' }
        if (code === 'CONSENT_CHANGED') return { kind: 'consent_changed' }
        if (code === 'IDEMPOTENCY_CONFLICT')
          return { kind: 'idempotency_conflict' }
      }
      if (response.status === 404 && code === 'NOT_FOUND')
        return { kind: 'not_found' }
      if (response.status === 410 && code === 'SESSION_EXPIRED')
        return { kind: 'chat_expired' }
      if (response.status === 422 && code === 'VALIDATION_ERROR')
        return { kind: 'validation' }
      return { kind: 'uncertain' }
    } catch {
      // Fetch errors and timeout may happen after COMMIT. Never expose raw errors.
      return { kind: 'uncertain' }
    }
  },
}
