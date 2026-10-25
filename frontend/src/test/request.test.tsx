import { StrictMode, useState } from 'react'
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { fixtures } from '../api/fixtures'
import { fixtureCatalog } from '../api/fixtureCatalog'
import { CatalogError, type CatalogSource } from '../api/catalog'
import type { CatalogItem, OrderReceipt } from '../api/types'
import { httpOrders, type OrdersSource } from '../api/httpOrders'
import { RequestPage } from '../pages/RequestPage'
import { CatalogPage } from '../pages/CatalogPage'
import { PENDING_KEY, Submission } from '../state/submission'
import { SELECTION_KEY, useSelection } from '../state/selection'
import App from '../App'

const product = fixtures.catalog_item
const service = {
  ...product,
  ...fixtures.catalog_page.items[1]!,
  description: '',
  attributes: {},
} as CatalogItem
const consent = fixtures.consent_text
const source: CatalogSource = { ...fixtureCatalog, mode: 'api' }
const orders = (): OrdersSource => ({
  getConsent: vi.fn().mockResolvedValue(consent),
  submit: vi.fn().mockResolvedValue({
    kind: 'success',
    status: 201,
    receipt: fixtures.receipt,
  }),
})
const saved = (items: CatalogItem[] = [product]) =>
  sessionStorage.setItem(
    SELECTION_KEY,
    JSON.stringify(
      items.map((item) => ({
        item_id: item.id,
        qty: 2,
        revision: item.revision,
      })),
    ),
  )
function Harness({
  api,
  catalog = source,
}: {
  api: OrdersSource
  catalog?: CatalogSource
}) {
  const selection = useSelection()
  const [submission] = useState(() => new Submission(api))
  return (
    <RequestPage
      source={catalog}
      orders={api}
      selection={selection}
      submission={submission}
      active
      onBack={() => {}}
    />
  )
}
const send = () =>
  screen.getByRole('button', { name: 'Подтвердить и отправить заявку' })
const accept = () => screen.getByRole('checkbox', { name: /Я прочитал/ })
const confirm = () =>
  screen.getByRole('checkbox', { name: /Подтверждаю показанные/ })
async function fill() {
  await waitFor(() =>
    expect((send() as HTMLButtonElement).disabled).toBe(false),
  )
  fireEvent.change(screen.getByLabelText(/Контактное имя/), {
    target: { value: '  Synthetic Buyer  ' },
  })
  fireEvent.change(screen.getByLabelText('Email'), {
    target: { value: 'buyer@example.invalid' },
  })
  fireEvent.change(screen.getByLabelText(/Комментарий/), {
    target: { value: '  exact\ncomment  ' },
  })
}
async function approve() {
  fireEvent.click(accept())
  fireEvent.click(confirm())
  fireEvent.click(send())
}
beforeEach(() => {
  vi.stubGlobal('crypto', {
    randomUUID: vi.fn().mockReturnValue('00000000-0000-4000-8000-000000000001'),
    getRandomValues: vi.fn((b: Uint8Array) => b.fill(7)),
  })
})
it.each([
  { items: [product] },
  { items: [product, service] },
  { items: [service] },
])('submits real commercial contract for selection %#', async ({ items }) => {
  saved(items)
  const api = orders()
  render(
    <StrictMode>
      <Harness api={api} />
    </StrictMode>,
  )
  await fill()
  await approve()
  await screen.findByRole('heading', {
    name: `Заявка №${fixtures.receipt.number}`,
  })
  expect(api.submit).toHaveBeenCalledTimes(1)
  const body = JSON.parse(vi.mocked(api.submit).mock.calls[0]![0])
  expect(body).toMatchObject({
    kind: 'commercial',
    source: 'site',
    confirmed: true,
    consent: { version: consent.version, accepted: true },
    contact: { name: '  Synthetic Buyer  ', email: 'buyer@example.invalid' },
    comment: '  exact\ncomment  ',
  })
  expect(body.items).toEqual(
    items.map((item) => ({
      item_id: item.id,
      qty: 2,
      expected_revision: item.revision,
    })),
  )
  expect(Object.keys(body).sort()).toEqual([
    'comment',
    'confirmed',
    'consent',
    'contact',
    'items',
    'kind',
    'source',
  ])
  expect(screen.queryByLabelText('Email')).toBeNull()
  expect(screen.queryByText('buyer@example.invalid')).toBeNull()
})
it('requires both explicit consent and confirmation, preserving inputs on validation', async () => {
  saved()
  const api = orders()
  render(<Harness api={api} />)
  await fill()
  fireEvent.click(send())
  expect(api.submit).not.toHaveBeenCalled()
  expect(screen.getByText(consent.text)).toBeTruthy()
  expect(api.getConsent).toHaveBeenCalled()
  fireEvent.click(accept())
  fireEvent.click(send())
  expect(api.submit).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: '' } })
  fireEvent.click(confirm())
  fireEvent.click(send())
  expect(screen.getByText('Укажите email или телефон.')).toBeTruthy()
  expect(api.submit).not.toHaveBeenCalled()
})
it('reconciles stale revisions only after explicit acceptance', async () => {
  saved()
  const api = orders()
  const catalog = {
    ...source,
    getItem: vi.fn().mockResolvedValue({
      ...product,
      revision: product.revision + 1,
      price: '0.00',
    }),
  }
  render(<Harness api={api} catalog={catalog} />)
  await screen.findByText(/Позиция изменилась:/)
  expect((send() as HTMLButtonElement).disabled).toBe(true)
  expect(JSON.parse(sessionStorage.getItem(SELECTION_KEY)!)[0].revision).toBe(
    product.revision,
  )
  fireEvent.click(screen.getByRole('button', { name: /Принять обновление/ }))
  await fill()
  await approve()
  const body = JSON.parse(vi.mocked(api.submit).mock.calls[0]![0])
  expect(body.items[0].expected_revision).toBe(product.revision + 1)
})
it('blocks unavailable positions and distinguishes backend outages', async () => {
  saved()
  const api = orders()
  const catalog = {
    ...source,
    getItem: vi.fn().mockRejectedValue(new CatalogError('hidden', 404)),
  }
  render(<Harness api={api} catalog={catalog} />)
  await screen.findByText(/Её нельзя отправить/)
  expect((send() as HTMLButtonElement).disabled).toBe(true)
  expect(api.submit).not.toHaveBeenCalled()
})
it.each(['catalog', 'consent'])(
  'blocks %s outage and retries real loading without fallback',
  async (resource) => {
    saved()
    const api = orders()
    const catalog = { ...source, getItem: vi.fn(source.getItem) }
    if (resource === 'consent')
      vi.mocked(api.getConsent).mockRejectedValueOnce(new Error('down'))
    else catalog.getItem.mockRejectedValueOnce(new Error('down'))
    render(<Harness api={api} catalog={catalog} />)
    await screen.findByText(
      resource === 'consent'
        ? /Не удалось загрузить согласие/
        : /Каталог недоступен/,
    )
    expect((send() as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(
      screen.getByRole('button', {
        name:
          resource === 'consent'
            ? 'Повторить загрузку согласия'
            : 'Повторить загрузку каталога',
      }),
    )
    await fill()
    expect(api.submit).not.toHaveBeenCalled()
  },
)
it.each(['catalog_changed', 'consent_changed', 'validation'] as const)(
  'requires explicit review, fresh acceptance and confirmation after %s',
  async (reason) => {
    saved()
    const api = orders()
    vi.mocked(api.submit).mockResolvedValueOnce({ kind: reason })
    render(<Harness api={api} />)
    await fill()
    await approve()
    await screen.findByRole('button', { name: 'Пересмотреть заявку' })
    expect((send() as HTMLButtonElement).disabled).toBe(true)
    if (reason === 'consent_changed')
      vi.mocked(api.getConsent).mockResolvedValue({
        ...consent,
        version: 'effective-v2',
        text: 'Changed plain text <script>literal</script>',
      })
    fireEvent.click(screen.getByRole('button', { name: 'Пересмотреть заявку' }))
    await waitFor(() =>
      expect((send() as HTMLButtonElement).disabled).toBe(false),
    )
    expect((accept() as HTMLInputElement).checked).toBe(false)
    expect((confirm() as HTMLInputElement).checked).toBe(false)
    expect(
      (screen.getByLabelText(/Контактное имя/) as HTMLInputElement).value,
    ).toBe('  Synthetic Buyer  ')
    if (reason === 'consent_changed')
      await screen.findByText('Changed plain text <script>literal</script>')
    await approve()
    await screen.findByRole('heading', { name: /Заявка №/ })
    expect(api.submit).toHaveBeenCalledTimes(2)
    expect(crypto.randomUUID).toHaveBeenCalledTimes(2)
    if (reason === 'consent_changed')
      expect(
        JSON.parse(vi.mocked(api.submit).mock.calls[1]![0]).consent.version,
      ).toBe('effective-v2')
  },
)
it.each(['not_found', 'idempotency_conflict'] as const)(
  'has neutral recovery and no key rotation on %s',
  async (kind) => {
    saved()
    const api = orders()
    vi.mocked(api.submit).mockResolvedValue({ kind })
    render(<Harness api={api} />)
    await fill()
    await approve()
    await screen.findByText(/Обратитесь к менеджеру/)
    expect(
      screen.queryByRole('button', { name: 'Пересмотреть заявку' }),
    ).toBeNull()
    expect(api.submit).toHaveBeenCalledTimes(1)
  },
)
it('locks inputs and double clicks, retries original request after uncertain failure', async () => {
  saved()
  const api = orders()
  vi.mocked(api.submit).mockResolvedValueOnce({ kind: 'uncertain' })
  render(<Harness api={api} />)
  await fill()
  fireEvent.click(accept())
  fireEvent.click(confirm())
  act(() => {
    fireEvent.click(send())
    fireEvent.click(send())
  })
  await screen.findByRole('button', { name: 'Повторить отправку' })
  expect(screen.getByLabelText('Email').matches(':disabled')).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Повторить отправку' }))
  await screen.findByRole('heading', { name: /Заявка №/ })
  expect(vi.mocked(api.submit).mock.calls[0]).toEqual(
    vi.mocked(api.submit).mock.calls[1],
  )
  expect(crypto.randomUUID).toHaveBeenCalledTimes(1)
})
it.each([200, 201] as const)(
  'renders safe receipt identically for %s, retaining null versus zero',
  async (status) => {
    saved()
    const api = orders()
    const receipt: OrderReceipt = {
      ...fixtures.receipt,
      known_subtotal: '0.00',
    }
    vi.mocked(api.submit).mockResolvedValue({
      kind: 'success',
      status,
      receipt,
    })
    render(<Harness api={api} />)
    await fill()
    await approve()
    const confirmation = within(
      await screen.findByRole('region', { name: 'Подтверждение заявки' }),
    )
    expect(confirmation.getByText('0,00 RUB')).toBeTruthy()
    expect(confirmation.getAllByText('Уточняется')).toHaveLength(3)
    expect(confirmation.getByText('new')).toBeTruthy()
  },
)
it('blocks >50 selections without dropping positions', async () => {
  const rows = Array.from({ length: 51 }, (_, i) => ({
    item_id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`,
    qty: 1,
    revision: 1,
  }))
  sessionStorage.setItem(SELECTION_KEY, JSON.stringify(rows))
  const api = orders()
  render(
    <Harness
      api={api}
      catalog={{ ...source, getItem: vi.fn().mockResolvedValue(product) }}
    />,
  )
  await screen.findByText(/Для заявки нужно от 1 до 50/)
  expect((send() as HTMLButtonElement).disabled).toBe(true)
  expect(JSON.parse(sessionStorage.getItem(SELECTION_KEY)!)).toHaveLength(51)
  expect(api.submit).not.toHaveBeenCalled()
})
it('shows unknown prices and known zero without inventing a shipping cost', async () => {
  const free = { ...product, price: '0.00' as const }
  saved([free, service])
  const api = orders()
  render(
    <Harness
      api={api}
      catalog={{
        ...source,
        getItem: vi.fn(async (id) => (id === product.id ? free : service)),
      }}
    />,
  )
  await screen.findByText('Цена по запросу')
  expect(screen.getByText(/Позиций без известной цены: 1/)).toBeTruthy()
  expect(
    screen.getByText(/Стоимость доставки и общая сумма уточняются/),
  ).toBeTruthy()
  expect(screen.getByText('0,00 RUB')).toBeTruthy()
})
it('retains form and selected values across catalog navigation while pending', async () => {
  saved()
  const api = orders()
  vi.mocked(api.submit).mockResolvedValue({ kind: 'uncertain' })
  render(<CatalogPage source={source} orders={api} />)
  fireEvent.click(screen.getByRole('button', { name: 'Оформить заявку' }))
  await fill()
  await approve()
  await screen.findByRole('button', { name: 'Повторить отправку' })
  fireEvent.click(screen.getByRole('button', { name: /Вернуться в каталог/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Оформить заявку' }))
  expect(
    (screen.getByLabelText(/Контактное имя/) as HTMLInputElement).value,
  ).toBe('  Synthetic Buyer  ')
  expect(api.submit).toHaveBeenCalledTimes(1)
})
it('explains ambiguous reload and never starts another request', async () => {
  saved()
  sessionStorage.setItem(PENDING_KEY, '{}')
  const api = orders()
  render(<Harness api={api} />)
  await screen.findByText(/Исход предыдущей отправки неизвестен/)
  expect((send() as HTMLButtonElement).disabled).toBe(true)
  expect(api.submit).not.toHaveBeenCalled()
})
it('connects App catalog → form → real same-origin consent and POST adapters', async () => {
  saved()
  const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
    if (path === '/api/v1/orders')
      return { status: 201, json: async () => fixtures.receipt }
    if (path === '/api/v1/consent-text')
      return { ok: true, json: async () => consent }
    if (path.startsWith('/api/v1/catalog/items/'))
      return { ok: true, json: async () => product }
    if (path.startsWith('/api/v1/catalog/items?'))
      return { ok: true, json: async () => fixtures.catalog_page }
    void options
    return { ok: true, json: async () => fixtures.models }
  })
  vi.stubGlobal('fetch', fetcher)
  render(<App />)
  await userEvent.click(screen.getByRole('button', { name: 'Оформить заявку' }))
  await fill()
  await approve()
  await screen.findByRole('heading', { name: /Заявка №/ })
  expect(
    fetcher.mock.calls.filter(([path]) => path === '/api/v1/orders'),
  ).toHaveLength(1)
  expect(httpOrders).toBeTruthy()
})

it('shows zero service totals and no delivery city for a service-only request', async () => {
  saved([service])
  const api = orders()
  const freeService = {
    ...service,
    price_mode: 'fixed' as const,
    price: '0.00' as const,
    requires_shipping: false,
  }
  vi.mocked(api.submit).mockResolvedValue({
    kind: 'success',
    status: 201,
    receipt: {
      number: 99,
      status: 'new',
      known_subtotal: '0.00',
      items_total: '0.00',
      delivery_status: 'not_required',
      delivery_cost: '0.00',
      total_amount: '0.00',
      currency: 'RUB',
    },
  })
  render(
    <Harness
      api={api}
      catalog={{ ...source, getItem: vi.fn().mockResolvedValue(freeService) }}
    />,
  )
  await fill()
  expect(screen.queryByLabelText(/Город доставки/)).toBeNull()
  await approve()
  const receipt = within(
    await screen.findByRole('region', { name: 'Подтверждение заявки' }),
  )
  expect(receipt.getAllByText('0,00 RUB')).toHaveLength(4)
  expect(receipt.getByText('Не требуется')).toBeTruthy()
})

it('shows actual price changes after a catalog conflict before revision acceptance', async () => {
  saved()
  const api = orders()
  const catalog = { ...source, getItem: vi.fn().mockResolvedValue(product) }
  vi.mocked(api.submit).mockResolvedValueOnce({ kind: 'catalog_changed' })
  render(<Harness api={api} catalog={catalog} />)
  await fill()
  catalog.getItem.mockResolvedValue({
    ...product,
    revision: product.revision + 1,
    price: '1500.00',
  })
  await approve()
  await screen.findByText(/Цена: 1.200,00 RUB.*→ 1.500,00 RUB/)
  expect((send() as HTMLButtonElement).disabled).toBe(true)
  expect(JSON.parse(sessionStorage.getItem(SELECTION_KEY)!)[0].revision).toBe(
    product.revision,
  )
})
