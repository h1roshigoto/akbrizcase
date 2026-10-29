import {
  newOrderHeaders,
  orderSnapshot,
  type OrderResult,
  type OrdersSource,
} from '../api/httpOrders'
import type {
  ChatHeaders,
  OrderCreate,
  OrderHeaders,
  OrderReceipt,
} from '../api/types'

export const PENDING_KEY = 'akbrizcase.pending-order'
type Rejection = Exclude<OrderResult['kind'], 'success' | 'uncertain'>
export type SubmissionState =
  | { kind: 'editing' }
  | { kind: 'sending' | 'uncertain' | 'recovered' | 'storage_error' }
  | { kind: 'rejected'; reason: Rejection }
  | { kind: 'success'; receipt: OrderReceipt }

/** Lives above the catalog/form view switch. No POST from effects or mount. */
export class Submission {
  private state: SubmissionState
  private pending: {
    body: string
    headers: OrderHeaders
    chat?: ChatHeaders
  } | null = null
  private listeners = new Set<() => void>()
  constructor(private source: OrdersSource) {
    try {
      this.state =
        sessionStorage.getItem(PENDING_KEY) === null
          ? { kind: 'editing' }
          : { kind: 'recovered' }
    } catch {
      this.state = { kind: 'storage_error' }
    }
  }
  snapshot = () => this.state
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }
  private set(state: SubmissionState) {
    this.state = state
    this.listeners.forEach((listener) => listener())
  }
  async confirm(body: OrderCreate, chat?: ChatHeaders) {
    if (this.state.kind !== 'editing') return
    // Lock synchronously before generation/storage/fetch, including rapid clicks.
    this.set({ kind: 'sending' })
    try {
      if (body.source === 'chat' && !chat) throw new Error('Missing owner')
      const pending = {
        body: orderSnapshot(body),
        headers: newOrderHeaders(),
        ...(body.source === 'chat' && chat ? { chat: { ...chat } } : {}),
      }
      sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending.headers))
      this.pending = pending
    } catch {
      this.set({ kind: 'storage_error' })
      return
    }
    await this.send()
  }
  async retry() {
    if (this.state.kind !== 'uncertain' || !this.pending) return
    this.set({ kind: 'sending' })
    await this.send()
  }
  private async send() {
    if (!this.pending) return
    let result: OrderResult
    try {
      result = await this.source.submit(
        this.pending.body,
        this.pending.headers,
        this.pending.chat,
      )
    } catch {
      result = { kind: 'uncertain' }
    }
    if (result.kind === 'success') {
      this.pending = null
      try {
        sessionStorage.removeItem(PENDING_KEY)
      } catch {
        /* Reload remains conservatively blocked. */
      }
      this.set({ kind: 'success', receipt: result.receipt })
    } else if (result.kind === 'uncertain') this.set({ kind: 'uncertain' })
    else this.set({ kind: 'rejected', reason: result.kind })
  }
  review(expiredFallback = false) {
    if (
      this.state.kind !== 'rejected' ||
      ![
        'validation',
        'catalog_changed',
        'consent_changed',
        ...(expiredFallback ? ['chat_expired'] : []),
      ].includes(this.state.reason)
    )
      return false
    try {
      sessionStorage.removeItem(PENDING_KEY)
    } catch {
      this.set({ kind: 'storage_error' })
      return false
    }
    this.pending = null
    this.set({ kind: 'editing' })
    return true
  }
}
