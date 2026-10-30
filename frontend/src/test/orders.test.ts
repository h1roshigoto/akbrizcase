import { policyFixture } from './privacyFixture'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fixtures } from '../api/fixtures'
import {
  httpOrders,
  isReceipt,
  newOrderHeaders,
  type OrdersSource,
  type OrderResult,
} from '../api/httpOrders'
import { PENDING_KEY, Submission } from '../state/submission'
import { emptyForm, validateForm } from '../state/requestForm'
import type { OrderCreate } from '../api/types'

const body = fixtures.commercial_order as OrderCreate
const receipt = fixtures.receipt
const makeSource = (
  result: OrderResult = { kind: 'success', status: 201, receipt },
): OrdersSource => ({
  getPolicy: vi.fn().mockResolvedValue(policyFixture),
  getConsent: vi.fn(),
  submit: vi.fn().mockResolvedValue(result),
})
beforeEach(() => {
  vi.stubGlobal('crypto', {
    randomUUID: vi.fn().mockReturnValue('00000000-0000-4000-8000-000000000001'),
    getRandomValues: vi.fn((bytes: Uint8Array) => bytes.fill(7)),
  })
})

describe('exact logical submission lifecycle', () => {
  it('keeps chat ownership and the immutable support body through uncertain retry, never allowing site fallback', async () => {
    const source = makeSource({ kind: 'uncertain' })
    const submission = new Submission(source)
    const support: OrderCreate = {
      ...fixtures.commercial_order,
      kind: 'support',
      items: [],
      comment: 'Synthetic exact support',
      source: 'chat',
      chat_session_id: fixtures.catalog_item.id,
    }
    const chat = { 'X-Chat-Token': newOrderHeaders()['X-Request-Token'] }
    await submission.confirm(support, chat)
    const original = structuredClone(vi.mocked(source.submit).mock.calls[0]!)
    expect(submission.review(true)).toBe(false)
    expect(submission.beginNew()).toBe(false)
    chat['X-Chat-Token'] = newOrderHeaders()['X-Request-Token']
    support.comment = 'Changed'
    await submission.confirm({ ...support, source: 'site' } as OrderCreate)
    expect(source.submit).toHaveBeenCalledTimes(1)
    await submission.retry()
    expect(vi.mocked(source.submit).mock.calls[1]).toEqual(original)
    expect(sessionStorage.getItem(PENDING_KEY)).not.toContain(
      'Synthetic exact support',
    )
    expect(sessionStorage.getItem(PENDING_KEY)).not.toContain('X-Chat-Token')
  })
  it('locks rapid double clicks and keeps only independent crypto headers in session storage', async () => {
    let resolve!: (result: OrderResult) => void
    const source = makeSource()
    vi.mocked(source.submit).mockReturnValue(
      new Promise((r) => {
        resolve = r
      }),
    )
    const submission = new Submission(source)
    const sending = submission.confirm(body)
    expect(submission.beginNew()).toBe(false)
    await submission.confirm(body)
    expect(source.submit).toHaveBeenCalledTimes(1)
    expect(crypto.randomUUID).toHaveBeenCalledTimes(1)
    expect(crypto.getRandomValues).toHaveBeenCalledTimes(1)
    const stored = JSON.parse(sessionStorage.getItem(PENDING_KEY)!)
    expect(Object.keys(stored).sort()).toEqual([
      'Idempotency-Key',
      'X-Request-Token',
    ])
    expect(stored['X-Request-Token']).toMatch(/^[0-9a-f]{64}$/)
    expect(localStorage.length).toBe(0)
    resolve({ kind: 'success', status: 201, receipt })
    await sending
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
    expect(submission.snapshot()).toEqual({ kind: 'success', receipt })
  })
  it('retries byte-identical original body and headers after uncertainty, regardless of subsequent edits', async () => {
    const source = makeSource({ kind: 'uncertain' })
    const submission = new Submission(source)
    const input = structuredClone(body)
    await submission.confirm(input)
    const original = vi.mocked(source.submit).mock.calls[0]!
    input.contact.name = 'Changed after approval'
    input.items.reverse()
    await submission.confirm(input)
    vi.mocked(source.submit).mockResolvedValue({
      kind: 'success',
      status: 200,
      receipt,
    })
    await Promise.all([submission.retry(), submission.retry()])
    expect(source.submit).toHaveBeenCalledTimes(2)
    expect(vi.mocked(source.submit).mock.calls[1]).toEqual(original)
    expect(crypto.randomUUID).toHaveBeenCalledTimes(1)
    expect(submission.snapshot()).toEqual({ kind: 'success', receipt })
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
  })
  it('blocks all submission after unresolved reload without reconstructing the body', async () => {
    sessionStorage.setItem(PENDING_KEY, '{}')
    const source = makeSource()
    const submission = new Submission(source)
    expect(submission.snapshot().kind).toBe('recovered')
    expect(submission.beginNew()).toBe(false)
    await submission.confirm(body)
    await submission.retry()
    expect(submission.review()).toBe(false)
    expect(source.submit).not.toHaveBeenCalled()
    expect(crypto.randomUUID).not.toHaveBeenCalled()
  })
  it.each(['not_found', 'idempotency_conflict'] as const)(
    'retains a conservative lock on %s',
    async (kind) => {
      const source = makeSource({ kind })
      const submission = new Submission(source)
      await submission.confirm(body)
      expect(submission.review()).toBe(false)
      expect(submission.beginNew()).toBe(false)
      await submission.confirm(body)
      expect(source.submit).toHaveBeenCalledTimes(1)
      expect(sessionStorage.getItem(PENDING_KEY)).not.toBeNull()
    },
  )
  it.each(['validation', 'catalog_changed', 'consent_changed'] as const)(
    'requires explicit review after %s before a fresh crypto pair',
    async (kind) => {
      const source = makeSource({ kind })
      const submission = new Submission(source)
      await submission.confirm(body)
      await submission.confirm(body)
      expect(source.submit).toHaveBeenCalledTimes(1)
      expect(submission.review()).toBe(true)
      expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
      await submission.confirm(body)
      expect(crypto.randomUUID).toHaveBeenCalledTimes(2)
    },
  )
  it('fails closed when pending storage cannot be written', async () => {
    const source = makeSource()
    const submission = new Submission(source)
    const spy = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => {
        throw new Error('blocked')
      })
    await submission.confirm(body)
    expect(source.submit).not.toHaveBeenCalled()
    expect(submission.snapshot().kind).toBe('storage_error')
    expect(submission.beginNew()).toBe(false)
    spy.mockRestore()
  })
  it('releases only a proven success and creates no headers or POST on beginNew', async () => {
    const source = makeSource()
    const submission = new Submission(source)
    expect(submission.beginNew()).toBe(false)
    await submission.confirm(body)
    expect(submission.succeededFor('commercial')).toBe(true)
    expect(submission.succeededFor('support')).toBe(false)
    expect(submission.beginNew()).toBe(true)
    expect(submission.snapshot()).toEqual({ kind: 'editing' })
    expect(submission.succeededFor('commercial')).toBe(false)
    expect(source.submit).toHaveBeenCalledTimes(1)
    expect(crypto.randomUUID).toHaveBeenCalledTimes(1)
    expect(submission.beginNew()).toBe(false)
  })
  it('blocks beginNew if completed-marker cleanup fails', async () => {
    const submission = new Submission(makeSource())
    await submission.confirm(body)
    const spy = vi
      .spyOn(Storage.prototype, 'removeItem')
      .mockImplementation(() => {
        throw new Error('blocked')
      })
    expect(submission.beginNew()).toBe(false)
    expect(submission.snapshot().kind).toBe('storage_error')
    spy.mockRestore()
  })
})

describe('same-origin real orders adapter', () => {
  const headers = () => newOrderHeaders()
  it('loads effective backend consent with no-store and AbortSignal', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => fixtures.consent_text })
    vi.stubGlobal('fetch', fetcher)
    const signal = new AbortController().signal
    expect(await httpOrders.getConsent(signal)).toEqual(fixtures.consent_text)
    expect(fetcher).toHaveBeenCalledWith(
      '/api/v1/consent-text',
      expect.objectContaining({
        signal,
        cache: 'no-store',
        credentials: 'same-origin',
      }),
    )
  })
  it.each([200, 201] as const)(
    'accepts %s original safe receipt, projecting extra fields out',
    async (status) => {
      const fetcher = vi.fn().mockResolvedValue({
        status,
        json: async () => ({ ...receipt, unexpected: 'do not render' }),
      })
      vi.stubGlobal('fetch', fetcher)
      const wire = JSON.stringify(body)
      expect(await httpOrders.submit(wire, headers())).toEqual({
        kind: 'success',
        status,
        receipt,
      })
      expect(fetcher).toHaveBeenCalledWith(
        '/api/v1/orders',
        expect.objectContaining({
          body: wire,
          headers: expect.objectContaining({
            'Content-Type': 'application/json',
            'X-Request-Token': expect.stringMatching(/^[0-9a-f]{64}$/),
          }),
        }),
      )
    },
  )
  it.each([
    [409, 'CATALOG_CHANGED', 'catalog_changed'],
    [409, 'CONSENT_CHANGED', 'consent_changed'],
    [409, 'IDEMPOTENCY_CONFLICT', 'idempotency_conflict'],
    [404, 'NOT_FOUND', 'not_found'],
    [410, 'SESSION_EXPIRED', 'chat_expired'],
    [422, 'VALIDATION_ERROR', 'validation'],
    [503, 'UNAVAILABLE', 'uncertain'],
    [500, 'INTERNAL_ERROR', 'uncertain'],
    [409, 'UNKNOWN', 'uncertain'],
  ])(
    'maps %s %s without exposing server detail',
    async (status, code, kind) => {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({
          status,
          json: async () => ({
            error: {
              code,
              message: 'sensitive',
              details: { input: 'sensitive' },
            },
            request_id: 'id',
          }),
        }),
      )
      expect(await httpOrders.submit('{}', headers())).toEqual({ kind })
    },
  )
  it('treats lost responses, timeout, invalid JSON and malformed success as uncertainty', async () => {
    for (const value of [
      Promise.reject(new Error('sensitive')),
      Promise.resolve({ status: 201, json: async () => ({}) }),
      Promise.resolve({
        status: 200,
        json: async () => {
          throw new Error('sensitive')
        },
      }),
    ]) {
      vi.stubGlobal('fetch', vi.fn().mockReturnValue(value))
      expect(await httpOrders.submit('{}', headers())).toEqual({
        kind: 'uncertain',
      })
    }
  })
  it.each([{}, { ...fixtures.consent_text, checksum: 'bad' }])(
    'rejects malformed consent without fixtures',
    async (value) => {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({ ok: true, json: async () => value }),
      )
      await expect(
        httpOrders.getConsent(new AbortController().signal),
      ).rejects.toThrow()
    },
  )
  it('checks null/zero amounts and receipt arithmetic exactly', () => {
    expect(isReceipt(receipt)).toBe(true)
    expect(
      isReceipt({
        ...receipt,
        items_total: '0.00',
        known_subtotal: '0.00',
        delivery_status: 'not_required',
        delivery_cost: '0.00',
        total_amount: '0.00',
      }),
    ).toBe(true)
    expect(isReceipt({ ...receipt, known_subtotal: 0 })).toBe(false)
    expect(isReceipt({ ...receipt, total_amount: '0.00' })).toBe(false)
    expect(isReceipt({ ...receipt, status: 'closed' })).toBe(false)
  })
})

describe('browser field constraints preserve exact input', () => {
  const valid = {
    ...emptyForm,
    name: '  Synthetic Name  ',
    email: 'buyer@example.invalid',
  }
  it('uses Unicode character bounds for support without trimming the final text', () => {
    expect(
      validateForm({ ...valid, comment: '🙂'.repeat(2000) }, true),
    ).toEqual({})
    expect(
      validateForm({ ...valid, comment: '🙂'.repeat(2001) }, true),
    ).toHaveProperty('comment')
    expect(
      validateForm({ ...valid, comment: ' ' + 'x'.repeat(2000) }, true),
    ).toHaveProperty('comment')
    expect(validateForm({ ...valid, comment: ' \n\t' }, true)).toHaveProperty(
      'comment',
    )
  })
  it('requires a channel and nonblank name', () => {
    expect(validateForm(emptyForm)).toHaveProperty('name')
    expect(validateForm(emptyForm)).toHaveProperty('channel')
    expect(validateForm(valid)).toEqual({})
    expect(valid.name).toBe('  Synthetic Name  ')
  })
  it.each([
    ['name', ' '.repeat(3)],
    ['name', 'x'.repeat(201)],
    ['email', 'bad@'],
    ['email', 'x'.repeat(255)],
    ['phone', '+7<script>'],
    ['phone', '123456'],
    ['phone', '1'.repeat(16)],
    ['phone', '1'.repeat(33)],
    ['organization_name', 'x'.repeat(201)],
    ['city', ' '.repeat(2)],
    ['comment', 'x'.repeat(2001)],
  ] as const)('rejects invalid %s', (key, value) => {
    expect(validateForm({ ...valid, [key]: value })).toHaveProperty(key)
  })
})
