import { policyFixture } from './privacyFixture'
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { CatalogPage } from '../pages/CatalogPage'
import { fixtureCatalog } from '../api/fixtureCatalog'
import { fixtures } from '../api/fixtures'
import type { OrderResult, OrdersSource } from '../api/httpOrders'
import { PENDING_KEY } from '../state/submission'
import { SELECTION_KEY } from '../state/selection'
import { id, page, reply, transport } from './chatFixtures'

const catalog = { ...fixtureCatalog, mode: 'api' as const }
const active = () => within(screen.getByRole('main'))
const send = () =>
  active().getByRole('button', { name: 'Подтвердить и отправить заявку' })
const receipt = (n: number) =>
  screen.findByRole('heading', { name: `Заявка №${n}` })
const nav = (name: string) =>
  userEvent.click(screen.getByRole('link', { name }))
const route = (hash: string) =>
  act(() => {
    window.history.replaceState(null, '', hash)
    fireEvent.popState(window)
  })
function api() {
  let number = 0
  return {
    getPolicy: vi.fn().mockResolvedValue(policyFixture),
    getConsent: vi.fn().mockResolvedValue(fixtures.consent_text),
    submit: vi.fn<OrdersSource['submit']>().mockImplementation(async () => ({
      kind: 'success',
      status: 201,
      receipt: { ...fixtures.receipt, number: ++number },
    })),
  }
}
function mount(orders = api(), hash = '#support', chat = transport()) {
  window.history.replaceState(null, '', hash)
  sessionStorage.setItem(
    SELECTION_KEY,
    JSON.stringify([
      {
        item_id: fixtures.catalog_item.id,
        qty: 1,
        revision: fixtures.catalog_item.revision,
      },
    ]),
  )
  render(<CatalogPage source={catalog} orders={orders} chat={chat} />)
  return orders
}
async function edit(text: string) {
  await waitFor(() =>
    expect((send() as HTMLButtonElement).disabled).toBe(false),
  )
  fireEvent.change(active().getByLabelText(/Контактное имя/), {
    target: { value: 'Synthetic lifecycle buyer' },
  })
  fireEvent.change(active().getByLabelText('Email'), {
    target: { value: 'lifecycle@example.invalid' },
  })
  fireEvent.change(active().getByLabelText(/Комментарий|Описание проблемы/), {
    target: { value: text },
  })
}
async function submit() {
  await userEvent.click(active().getByRole('checkbox', { name: /Я прочитал/ }))
  expect(
    (active().getByRole('checkbox', { name: /Я прочитал/ }) as HTMLInputElement)
      .checked,
  ).toBe(true)
  await userEvent.click(active().getByRole('checkbox', { name: /Подтверждаю/ }))
  expect(
    (
      active().getByRole('checkbox', {
        name: /Подтверждаю/,
      }) as HTMLInputElement
    ).checked,
  ).toBe(true)
  await userEvent.click(send())
}
function chatSource() {
  const chat = transport()
  const handoff = reply('knowledge', {
    resolution: 'handoff',
    actions: [
      {
        type: 'contact_specialist',
        payload: { kind: 'support', comment_draft: 'Synthetic proposed draft' },
      },
    ],
  })
  chat.create.mockResolvedValue({
    session_id: id(1),
    session_token: 'a'.repeat(64) as Awaited<
      ReturnType<typeof chat.create>
    >['session_token'],
    state_version: 0,
    expires_at: '2099-01-01T00:00:00Z',
    reply: handoff,
  })
  chat.read.mockResolvedValue(page(0, handoff))
  return chat
}
async function openChatDraft(orders = api()) {
  mount(orders, '#chat', chatSource())
  fireEvent.click(
    await screen.findByRole('button', { name: 'Связаться со специалистом' }),
  )
  await edit('Synthetic edited chat draft')
  return orders
}
beforeEach(() => {
  let sequence = 0
  vi.stubGlobal('crypto', {
    randomUUID: vi.fn(() => id(++sequence)),
    getRandomValues: vi.fn((bytes: Uint8Array) => bytes.fill(++sequence)),
  })
})
it.each(['support', 'commercial'] as const)(
  'starts a separate request after %s success and allocates fresh secrets only on submit',
  async (first) => {
    const orders = mount(api(), first === 'support' ? '#support' : '#request')
    await edit('Synthetic first request')
    await submit()
    await receipt(1)
    await nav('Каталог')
    if (first === 'support') {
      // Raw hash navigation must not attribute the previous receipt to this form.
      route('#request')
      expect(screen.queryByRole('heading', { name: 'Заявка №1' })).toBeNull()
      fireEvent.click(
        screen.getByRole('button', {
          name: 'Начать новую коммерческую заявку',
        }),
      )
    } else {
      await nav('Обратиться к специалисту')
      expect(screen.queryByRole('heading', { name: 'Заявка №1' })).toBeNull()
      fireEvent.click(
        screen.getByRole('button', {
          name: 'Начать новое независимое обращение с сайта',
        }),
      )
    }
    await edit('Synthetic second request')
    expect(orders.submit).toHaveBeenCalledTimes(1)
    expect(crypto.randomUUID).toHaveBeenCalledTimes(1)
    await submit()
    await receipt(2)
    const [a, b] = orders.submit.mock.calls
    expect(JSON.parse(b![0])).toMatchObject({
      kind: first === 'support' ? 'commercial' : 'support',
      source: 'site',
      comment: 'Synthetic second request',
    })
    expect(b![1]['Idempotency-Key']).not.toBe(a![1]['Idempotency-Key'])
    expect(b![1]['X-Request-Token']).not.toBe(a![1]['X-Request-Token'])
    expect(b![2]).toBeUndefined()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
    expect(localStorage.length).toBe(0)
  },
)
it('starts two independent site supports explicitly after chat support success', async () => {
  const orders = await openChatDraft()
  await submit()
  await receipt(1)
  expect(JSON.parse(orders.submit.mock.calls[0]![0]).source).toBe('chat')
  for (const number of [2, 3]) {
    fireEvent.click(
      screen.getByRole('button', {
        name: 'Начать новое независимое обращение с сайта',
      }),
    )
    expect(
      screen.queryByRole('heading', { name: `Заявка №${number - 1}` }),
    ).toBeNull()
    expect(
      (active().getByLabelText(/Описание проблемы/) as HTMLTextAreaElement)
        .value,
    ).toBe('')
    await edit(`Synthetic independent support ${number}`)
    expect(orders.submit).toHaveBeenCalledTimes(number - 1)
    await submit()
    await receipt(number)
    const call = orders.submit.mock.calls[number - 1]!
    const previous = orders.submit.mock.calls[number - 2]!
    expect(JSON.parse(call[0])).toMatchObject({
      source: 'site',
      items: [],
      comment: `Synthetic independent support ${number}`,
    })
    expect(JSON.parse(call[0])).not.toHaveProperty('chat_session_id')
    expect(call[2]).toBeUndefined()
    expect(call[1]).not.toEqual(previous[1])
  }
})
it('preserves chat draft/context on history navigation and requires explicit site detachment without clearing edits', async () => {
  const orders = await openChatDraft()
  route('#catalog')
  route('#support')
  await waitFor(() =>
    expect((send() as HTMLButtonElement).disabled).toBe(false),
  )
  expect(
    active().getByText('Обращение связано с вашей сессией чата.'),
  ).toBeTruthy()
  expect(
    (active().getByLabelText(/Описание проблемы/) as HTMLTextAreaElement).value,
  ).toBe('Synthetic edited chat draft')
  await nav('Обратиться к специалисту')
  expect((send() as HTMLButtonElement).disabled).toBe(true)
  expect(orders.submit).not.toHaveBeenCalled()
  fireEvent.click(
    screen.getByRole('button', { name: 'Продолжить независимо от чата' }),
  )
  await waitFor(() =>
    expect((send() as HTMLButtonElement).disabled).toBe(false),
  )
  expect(
    (active().getByLabelText(/Описание проблемы/) as HTMLTextAreaElement).value,
  ).toBe('Synthetic edited chat draft')
  expect((active().getByLabelText('Email') as HTMLInputElement).value).toBe(
    'lifecycle@example.invalid',
  )
  expect(
    (
      active().getByRole('checkbox', {
        name: /Подтверждаю/,
      }) as HTMLInputElement
    ).checked,
  ).toBe(false)
  await submit()
  await receipt(1)
  const [body, , chat] = orders.submit.mock.calls[0]!
  expect(JSON.parse(body).source).toBe('site')
  expect(JSON.parse(body)).not.toHaveProperty('chat_session_id')
  expect(chat).toBeUndefined()
})
it('Back/Forward keeps the chat source until a separate site action', async () => {
  const orders = await openChatDraft()
  route('#request')
  route('#support')
  await waitFor(() =>
    expect((send() as HTMLButtonElement).disabled).toBe(false),
  )
  await submit()
  await receipt(1)
  expect(JSON.parse(orders.submit.mock.calls[0]![0])).toMatchObject({
    source: 'chat',
    chat_session_id: id(1),
    comment: 'Synthetic edited chat draft',
  })
  expect(orders.submit.mock.calls[0]![2]).toEqual({
    'X-Chat-Token': 'a'.repeat(64),
  })
})
it.each(['support', 'commercial'] as const)(
  'keeps uncertain %s immutable and blocks the other form after navigation',
  async (first) => {
    const orders = api()
    orders.submit.mockResolvedValueOnce({ kind: 'uncertain' })
    mount(orders, first === 'support' ? '#support' : '#request')
    await edit('Synthetic immutable request')
    await submit()
    await active().findByText(/Результат отправки пока неизвестен/)
    const original = structuredClone(orders.submit.mock.calls[0]!)
    route(first === 'support' ? '#request' : '#support')
    await active().findByText(/Результат отправки пока неизвестен/)
    expect((send() as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByRole('button', { name: /Начать нов/ })).toBeNull()
    fireEvent.click(send())
    expect(orders.submit).toHaveBeenCalledTimes(1)
    orders.submit.mockResolvedValueOnce({
      kind: 'success',
      status: 200,
      receipt: { ...fixtures.receipt, number: 1 },
    })
    fireEvent.click(
      active().getByRole('button', { name: 'Повторить отправку' }),
    )
    await screen.findByText(/Предыдущая заявка успешно сохранена/)
    expect(screen.queryByRole('heading', { name: 'Заявка №1' })).toBeNull()
    expect(orders.submit.mock.calls[1]).toEqual(original)
    route(first === 'support' ? '#support' : '#request')
    await receipt(1)
  },
)
it.each([
  'sending',
  'recovered',
  'storage_error',
  'not_found',
  'idempotency_conflict',
] as const)('does not start another form from %s', async (blocked) => {
  const orders = api()
  let storageSpy: ReturnType<typeof vi.spyOn> | undefined
  if (blocked === 'recovered') sessionStorage.setItem(PENDING_KEY, '{}')
  mount(orders)
  if (blocked !== 'recovered') {
    await edit('Synthetic protected request')
    if (blocked === 'sending')
      orders.submit.mockReturnValueOnce(new Promise<OrderResult>(() => {}))
    else if (blocked === 'storage_error')
      storageSpy = vi
        .spyOn(Storage.prototype, 'setItem')
        .mockImplementation(() => {
          throw new Error('blocked')
        })
    else orders.submit.mockResolvedValueOnce({ kind: blocked })
    await submit()
    await waitFor(() =>
      expect((send() as HTMLButtonElement).disabled).toBe(true),
    )
  }
  route('#request')
  expect((send() as HTMLButtonElement).disabled).toBe(true)
  expect(screen.queryByRole('button', { name: /Начать нов/ })).toBeNull()
  fireEvent.click(send())
  expect(orders.submit).toHaveBeenCalledTimes(
    blocked === 'storage_error' || blocked === 'recovered' ? 0 : 1,
  )
  storageSpy?.mockRestore()
})
