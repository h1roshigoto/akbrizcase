import { policyFixture } from './privacyFixture'
import { StrictMode } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { CatalogPage } from '../pages/CatalogPage'
import { fixtureCatalog } from '../api/fixtureCatalog'
import { fixtures } from '../api/fixtures'
import { ChatController } from '../chat/controller'
import { ChatPage } from '../chat/ChatPage'
import { ReplyView } from '../chat/ReplyView'
import { useSelection, SELECTION_KEY } from '../state/selection'
import { ChatError } from '../api/httpChat'
import { id, page, reply, result, source, transport } from './chatFixtures'

const synthetic = '<img src=x onerror="window.chatInjected=true">'
afterEach(() => {
  window.location.hash = ''
})
function mount(
  api = transport(),
  options: {
    onItem?: (id: string) => void
    onRequest?: () => void
    onSupport?: (...args: unknown[]) => void
  } = {},
) {
  const controller = new ChatController(api)
  const catalog = { ...fixtureCatalog, getItem: vi.fn(fixtureCatalog.getItem) }
  function Host() {
    const selection = useSelection()
    return (
      <ChatPage
        controller={controller}
        catalog={catalog}
        selection={selection}
        onBack={() => {}}
        onItem={options.onItem ?? (() => {})}
        onRequest={options.onRequest ?? (() => {})}
        {...(options.onSupport ? { onSupport: options.onSupport } : {})}
      />
    )
  }
  const view = render(
    <StrictMode>
      <Host />
    </StrictMode>,
  )
  return { api, controller, catalog, ...view }
}

it('uses real model/scenario selection and only server buttons for transitions and feedback', async () => {
  const api = transport()
  const fetcher = vi.fn(async (path: string) => ({
    ok: true,
    json: async () =>
      path.includes('equipment-models')
        ? fixtures.models
        : {
            items: [
              {
                key: 'PRIVATE_KEY',
                title: 'Проверка модели',
                model_id: fixtures.models.items[0]!.id,
              },
            ],
          },
  }))
  vi.stubGlobal('fetch', fetcher)
  api.send.mockImplementation(async (wire) => {
    const input = JSON.parse(wire).input
    if (input.type === 'start_diagnosis')
      return result(
        1,
        reply('diagnose', {
          resolution: 'question',
          text: 'Синтетический вопрос',
          buttons: [
            {
              id: 'q',
              label: 'Наблюдение',
              input: { type: 'button', option_id: 'approved' },
            },
          ],
        }),
      )
    if (input.type === 'button')
      return result(
        2,
        reply('diagnose', {
          resolution: 'results',
          text: 'Синтетический результат',
          buttons: [
            {
              id: 'yes',
              label: 'Помогло',
              input: { type: 'feedback', useful: true },
            },
            {
              id: 'no',
              label: 'Не помогло',
              input: { type: 'feedback', useful: false },
            },
          ],
        }),
      )
    return result(3, reply('diagnose', { resolution: 'completed' }))
  })
  mount(api)
  const user = userEvent.setup()
  await screen.findByRole('article', { name: 'Актуальный ответ' })
  const select = await screen.findByLabelText('Модель для диагностики')
  await waitFor(() =>
    expect((select as HTMLSelectElement).disabled).toBe(false),
  )
  await user.selectOptions(select, fixtures.models.items[0]!.id)
  await user.click(
    await screen.findByRole('button', { name: 'Проверка модели' }),
  )
  expect(screen.queryByText('PRIVATE_KEY')).toBeNull()
  await user.click(await screen.findByRole('button', { name: 'Наблюдение' }))
  await user.click(await screen.findByRole('button', { name: 'Помогло' }))
  await screen.findByText('Диагностика завершена')
  expect(api.send.mock.calls.map(([wire]) => JSON.parse(wire).input)).toEqual([
    {
      type: 'start_diagnosis',
      model_id: fixtures.models.items[0]!.id,
      tree_key: 'PRIVATE_KEY',
    },
    { type: 'button', option_id: 'approved' },
    { type: 'feedback', useful: true },
  ])
  expect(api.create).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('button', { name: 'Наблюдение' })).toBeNull()
  expect(
    screen.getByRole('button', { name: 'Начать другой сценарий' }),
  ).toBeTruthy()
})
it('shows empty scenario state and resets the menu when the model changes', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: string) => ({
      ok: true,
      json: async () =>
        path.includes('equipment-models') ? fixtures.models : { items: [] },
    })),
  )
  const { api } = mount()
  const user = userEvent.setup()
  const select = await screen.findByLabelText('Модель для диагностики')
  await waitFor(() =>
    expect((select as HTMLSelectElement).disabled).toBe(false),
  )
  await user.selectOptions(select, fixtures.models.items[0]!.id)
  await screen.findByText(
    /Для этой модели нет доступных опубликованных сценариев/,
  )
  await user.selectOptions(select, '')
  expect(
    screen.queryByText(
      /Для этой модели нет доступных опубликованных сценариев/,
    ),
  ).toBeNull()
  expect(api.send).not.toHaveBeenCalled()
})
it('knowledge accepts nonblank bounded queries and shows actual no_match', async () => {
  const api = transport()
  api.create.mockResolvedValue({
    ...(await api.create('diagnose')),
    reply: reply('knowledge'),
  })
  api.create.mockClear()
  api.send.mockResolvedValue(
    result(
      1,
      reply('knowledge', {
        text: 'Подтверждения не найдено',
        resolution: 'no_match',
      }),
    ),
  )
  mount(api)
  const query = await screen.findByLabelText('Запрос по документации')
  fireEvent.change(query, { target: { value: '   ' } })
  expect(
    (
      screen.getByRole('button', {
        name: 'Отправить запрос',
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true)
  fireEvent.change(query, { target: { value: 'Синтетический запрос' } })
  await userEvent.click(
    screen.getByRole('button', { name: 'Отправить запрос' }),
  )
  await screen.findByText('Подтверждения не найдено')
  expect(JSON.parse(api.send.mock.calls[0]![0]).input.value).toBe(
    'Синтетический запрос',
  )
})
it('renders source titles, text, labels and commerce cards as safe text', () => {
  const r = reply('commerce', {
    text: synthetic,
    sources: [{ ...source, title: synthetic }],
    items: [{ ...fixtures.catalog_item, name: synthetic, applicability: null }],
    buttons: [
      { id: 'a', label: synthetic, input: { type: 'feedback', useful: false } },
    ],
    actions: [{ type: 'open_document', payload: source }],
  })
  const { container } = render(<ReplyView reply={r} current disabled={false} />)
  expect(container.querySelector('[onerror]')).toBeNull()
  expect(container.querySelector('script')).toBeNull()
  expect(screen.getByRole('button', { name: synthetic })).toBeTruthy()
  expect(screen.getAllByText(/1 200,00/).length).toBeGreaterThan(0)
  expect(screen.queryByText('Применимость подтверждена')).toBeNull()
  const links = screen.getAllByRole('link')
  expect(
    links.every(
      (a) =>
        a.getAttribute('href') === source.url &&
        a.getAttribute('rel') === 'noopener noreferrer',
    ),
  ).toBe(true)
  expect(screen.getByText(/предыдущая редакция/)).toBeTruthy()
})
it('does not create an unsafe or mismatched document link', () => {
  render(
    <ReplyView
      reply={reply('knowledge', {
        sources: [
          { ...source, url: 'javascript:alert(1)' as typeof source.url },
        ],
      })}
    />,
  )
  expect(screen.queryByRole('link')).toBeNull()
})
it.each(['contact_specialist', 'open_request_form'] as const)(
  'handles %s support honestly without orders',
  async (type) => {
    const api = transport(),
      fetcher = vi.fn()
    vi.stubGlobal('fetch', fetcher)
    const r = reply('knowledge', {
      resolution: 'handoff',
      actions: [
        type === 'contact_specialist'
          ? { type, payload: { kind: 'support' } }
          : { type, payload: { kind: 'support', items: [] } },
      ],
    })
    api.create.mockResolvedValue({
      ...(await api.create('diagnose')),
      reply: r,
    })
    const onSupport = vi.fn()
    mount(api, { onSupport })
    await userEvent.click(
      await screen.findByRole('button', {
        name:
          type === 'contact_specialist'
            ? 'Связаться со специалистом'
            : 'Обращение в поддержку',
      }),
    )
    expect(onSupport).toHaveBeenCalledTimes(1)
    expect(onSupport.mock.calls[0]![1]).toMatchObject({ session_id: id(1) })
    expect(fetcher).not.toHaveBeenCalled()
    expect(api.send).not.toHaveBeenCalled()
  },
)
it('rechecks open_item availability through catalog before navigation', async () => {
  const api = transport(),
    onItem = vi.fn()
  api.create.mockResolvedValue({
    ...(await api.create('diagnose')),
    reply: reply('commerce', {
      actions: [
        { type: 'open_item', payload: { item_id: fixtures.catalog_item.id } },
      ],
      items: [fixtures.catalog_item],
    }),
  })
  const { catalog } = mount(api, { onItem })
  await userEvent.click(
    await screen.findByRole('button', {
      name: `Карточка ${fixtures.catalog_item.name}`,
    }),
  )
  await waitFor(() =>
    expect(onItem).toHaveBeenCalledWith(fixtures.catalog_item.id),
  )
  expect(catalog.getItem).toHaveBeenCalledTimes(1)
})
it('commercial action refreshes prices/revisions and prepares selection, never POST orders', async () => {
  const api = transport(),
    onRequest = vi.fn(),
    fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  api.create.mockResolvedValue({
    ...(await api.create('diagnose')),
    reply: reply('commerce', {
      actions: [
        {
          type: 'open_request_form',
          payload: {
            kind: 'commercial',
            items: [{ item_id: fixtures.catalog_item.id, qty: 3 }],
          },
        },
      ],
    }),
  })
  const { catalog } = mount(api, { onRequest })
  catalog.getItem.mockResolvedValue({
    ...fixtures.catalog_item,
    revision: 9,
    price: '99.00',
  })
  await userEvent.click(
    await screen.findByRole('button', {
      name: 'Проверить коммерческую заявку',
    }),
  )
  await waitFor(() => expect(onRequest).toHaveBeenCalledTimes(1))
  expect(JSON.parse(sessionStorage.getItem(SELECTION_KEY)!)).toEqual([
    { item_id: fixtures.catalog_item.id, qty: 3, revision: 9 },
  ])
  expect(fetcher).not.toHaveBeenCalled()
  expect(localStorage.length).toBe(0)
})
it('clears revoked history on refresh and leaves old actions inactive', async () => {
  const api = transport()
  api.create.mockResolvedValue({
    ...(await api.create('diagnose')),
    reply: reply('knowledge', { text: 'OLD_QUOTE', sources: [source] }),
  })
  const { controller } = mount(api)
  await screen.findByText('OLD_QUOTE')
  api.read.mockResolvedValue(
    page(0, reply('knowledge', { resolution: 'handoff' })),
  )
  await act(() => controller.refresh())
  expect(screen.queryByText('OLD_QUOTE')).toBeNull()
  expect(screen.queryByRole('link')).toBeNull()
})
it('shows storage limitation and 503 retry without pretending success', async () => {
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new Error()
  })
  const api = transport()
  api.create.mockResolvedValue({
    ...(await api.create('diagnose')),
    reply: reply('knowledge'),
  })
  api.send.mockRejectedValue(new ChatError(503, 'SERVICE_UNAVAILABLE'))
  mount(api)
  await screen.findByText(
    /Восстановление после обновления страницы.*не гарантируются/,
  )
  fireEvent.change(await screen.findByLabelText('Запрос по документации'), {
    target: { value: 'Синтетический текст' },
  })
  await userEvent.click(
    screen.getByRole('button', { name: 'Отправить запрос' }),
  )
  await screen.findByText(/Исход запроса неизвестен/)
  expect(
    screen.getByRole('button', { name: 'Повторить тот же запрос' }),
  ).toBeTruthy()
})
it('catalog creates no chat until opening; navigation preserves the selected items', async () => {
  const api = transport()
  api.create.mockResolvedValue({
    ...(await api.create('diagnose')),
    reply: reply('knowledge'),
  })
  api.create.mockClear()
  render(
    <StrictMode>
      <CatalogPage source={fixtureCatalog} chat={api} />
    </StrictMode>,
  )
  const user = userEvent.setup()
  await screen.findByRole('article', {
    name: `${fixtures.catalog_item.name}, ${fixtures.catalog_item.sku}`,
  })
  expect(api.create).not.toHaveBeenCalled()
  await user.click(
    screen.getAllByRole('button', { name: 'Добавить в список' })[0]!,
  )
  await user.click(screen.getByRole('link', { name: 'Помощник' }))
  await screen.findByRole('heading', { name: 'Помощник по оборудованию' })
  await screen.findByRole('article', { name: 'Актуальный ответ' })
  expect(api.create).toHaveBeenCalledTimes(1)
  await user.click(screen.getByRole('button', { name: 'Вернуться в каталог' }))
  expect(JSON.parse(sessionStorage.getItem(SELECTION_KEY)!)).toHaveLength(1)
  expect(screen.getByRole('heading', { name: 'Товары и услуги' })).toBeTruthy()
})

it('keeps the chat open and the selection unchanged when 50 positions plus an action exceed the request limit', async () => {
  const existing = Array.from({ length: 50 }, (_, n) => ({
    item_id: id(n + 1000),
    qty: n + 1,
    revision: n + 2,
  }))
  sessionStorage.setItem(SELECTION_KEY, JSON.stringify(existing))
  const api = transport(),
    onRequest = vi.fn()
  api.create.mockResolvedValue({
    ...(await api.create('diagnose')),
    reply: reply('commerce', {
      actions: [
        {
          type: 'open_request_form',
          payload: {
            kind: 'commercial',
            items: [{ item_id: fixtures.catalog_item.id, qty: 1 }],
          },
        },
      ],
    }),
  })
  const { catalog } = mount(api, { onRequest })
  await userEvent.click(
    await screen.findByRole('button', {
      name: 'Проверить коммерческую заявку',
    }),
  )
  await screen.findByText(/не более 50 уникальных позиций/)
  expect(catalog.getItem).toHaveBeenCalledWith(
    fixtures.catalog_item.id,
    null,
    expect.any(AbortSignal),
  )
  expect(onRequest).not.toHaveBeenCalled()
  expect(JSON.parse(sessionStorage.getItem(SELECTION_KEY)!)).toEqual(existing)
  expect(
    screen.getByRole('heading', { name: 'Помощник по оборудованию' }),
  ).toBeTruthy()
})

it('synchronizes catalog, chat and commercial form on browser Back/Forward and hash changes', async () => {
  window.history.replaceState(null, '', '#catalog')
  const existing = [
    {
      item_id: fixtures.catalog_item.id,
      qty: 2,
      revision: fixtures.catalog_item.revision,
    },
  ]
  sessionStorage.setItem(SELECTION_KEY, JSON.stringify(existing))
  const api = transport()
  const commerceReply = reply('commerce', {
    actions: [
      {
        type: 'open_request_form',
        payload: {
          kind: 'commercial',
          items: [{ item_id: fixtures.catalog_item.id, qty: 3 }],
        },
      },
    ],
  })
  api.create.mockResolvedValue({
    ...(await api.create('diagnose')),
    reply: commerceReply,
  })
  api.create.mockClear()
  api.read.mockResolvedValue(page(0, commerceReply))
  const orders = {
    getPolicy: vi.fn().mockResolvedValue(policyFixture),
    getConsent: vi.fn().mockResolvedValue(fixtures.consent_text),
    submit: vi.fn(),
  }
  render(
    <StrictMode>
      <CatalogPage source={fixtureCatalog} chat={api} orders={orders} />
    </StrictMode>,
  )
  const user = userEvent.setup()
  await screen.findByRole('heading', { name: 'Товары и услуги' })
  await user.click(screen.getByRole('link', { name: 'Помощник' }))
  await user.click(
    await screen.findByRole('button', {
      name: 'Проверить коммерческую заявку',
    }),
  )
  await screen.findByRole('heading', { name: 'Оформление заявки' })
  expect(window.location.hash).toBe('#request')
  fireEvent.change(screen.getByLabelText(/Контактное имя/), {
    target: { value: 'SYNTHETIC NAVIGATION' },
  })
  const checkView = async (hash: string, title: string) => {
    await waitFor(() => expect(window.location.hash).toBe(hash))
    await screen.findByRole('heading', { name: title })
    expect(screen.getAllByRole('main')).toHaveLength(1)
    expect(JSON.parse(sessionStorage.getItem(SELECTION_KEY)!)).toEqual(existing)
  }
  act(() => window.history.back())
  await checkView('#chat', 'Помощник по оборудованию')
  await screen.findByRole('article', { name: 'Актуальный ответ' })
  act(() => window.history.back())
  await checkView('#catalog', 'Товары и услуги')
  act(() => window.history.forward())
  await checkView('#chat', 'Помощник по оборудованию')
  await screen.findByRole('article', { name: 'Актуальный ответ' })
  act(() => window.history.forward())
  await checkView('#request', 'Оформление заявки')
  expect(
    (screen.getByLabelText(/Контактное имя/) as HTMLInputElement).value,
  ).toBe('SYNTHETIC NAVIGATION')
  await user.click(screen.getByRole('button', { name: /Вернуться в каталог/ }))
  await checkView('#catalog', 'Товары и услуги')
  act(() => {
    window.location.hash = '#request'
  })
  await checkView('#request', 'Оформление заявки')
  act(() => {
    window.location.hash = '#selection'
  })
  await checkView('#selection', 'Товары и услуги')
  await user.click(screen.getByRole('link', { name: 'Помощник' }))
  await screen.findByRole('article', { name: 'Актуальный ответ' })
  expect(api.create).toHaveBeenCalledTimes(1)
  expect(api.read.mock.calls.length).toBeGreaterThanOrEqual(3)
  expect(orders.submit).not.toHaveBeenCalled()
})
