import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { CatalogPage } from '../pages/CatalogPage'
import { fixtureCatalog } from '../api/fixtureCatalog'
import { fixtures } from '../api/fixtures'
import type { CatalogSource } from '../api/catalog'
import type {
  CatalogCard,
  CatalogItem,
  CatalogPage as Page,
} from '../api/types'
import { SELECTION_KEY } from '../state/selection'
import { ItemImage } from '../components/ItemImage'

const product = fixtures.catalog_item
const service = fixtures.catalog_page.items[1]!
const confirmed = fixtures.catalog_page.items[2]!
const model = fixtures.models.items[0]!
const productLabel = `${product.name}, ${product.sku}`
const serviceLabel = `${service.name}, ${service.sku}`
const selected = () =>
  within(screen.getByRole('complementary', { name: 'Выбранные позиции' }))
const saved = () =>
  JSON.parse(sessionStorage.getItem(SELECTION_KEY) ?? '[]') as unknown[]
const ready = () => screen.findByRole('article', { name: productLabel })

describe('catalog UI on approved fixtures', () => {
  it('searches by SKU and name, filters kinds, resets an empty result', async () => {
    const user = userEvent.setup()
    render(<CatalogPage source={fixtureCatalog} />)
    await ready()
    expect(screen.getAllByRole('article')).toHaveLength(3)
    await user.click(screen.getByRole('button', { name: 'Услуги' }))
    await screen.findByRole('article', { name: serviceLabel })
    expect(screen.getAllByRole('article')).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Товары' }))
    await ready()
    expect(screen.getAllByRole('article')).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: 'Все позиции' }))
    const search = screen.getByRole('searchbox')
    await user.type(search, 'demo-service')
    await user.click(screen.getByRole('button', { name: 'Найти' }))
    await screen.findByRole('article', { name: serviceLabel })
    expect(screen.getAllByRole('article')).toHaveLength(1)
    await user.clear(search)
    await user.type(search, 'демо-товар{Enter}')
    await ready()
    expect(screen.getAllByRole('article')).toHaveLength(2)
    await user.clear(search)
    await user.type(search, 'нет такой позиции{Enter}')
    await screen.findByRole('heading', { name: 'Позиции не найдены' })
    await user.click(
      within(screen.getByRole('status')).getByRole('button', {
        name: 'Сбросить фильтры',
      }),
    )
    await ready()
    expect(screen.getAllByRole('article')).toHaveLength(3)
  })

  it('shows decimal price, tax note, unit and on_request without a zero price', async () => {
    render(<CatalogPage source={fixtureCatalog} />)
    const card = within(await ready())
    expect(card.getByText('1 200,00', { exact: false }).textContent).toContain(
      'RUB',
    )
    expect(card.getByText('за шт.')).toBeTruthy()
    expect(card.getByText(product.tax_note)).toBeTruthy()
    const serviceCard = within(
      screen.getByRole('article', { name: serviceLabel }),
    )
    expect(serviceCard.getByText(/Цена по запросу/)).toBeTruthy()
    expect(serviceCard.queryByText(/^0,00/)).toBeNull()
  })

  it('opens the approved description and keeps selection across detail/list/filter navigation', async () => {
    const user = userEvent.setup()
    render(<CatalogPage source={fixtureCatalog} />)
    const card = within(await ready())
    await user.click(card.getByRole('button', { name: product.name }))
    await screen.findByText(product.description)
    await user.click(screen.getByRole('button', { name: 'Добавить в список' }))
    await selected().findByRole('button', { name: product.name })
    await user.click(
      screen.getByRole('button', { name: /Вернуться в каталог/ }),
    )
    expect(
      (
        within(await ready()).getByRole('button', {
          name: 'В выбранном',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Услуги' }))
    await screen.findByRole('article', { name: serviceLabel })
    expect(selected().getByRole('button', { name: product.name })).toBeTruthy()
  })

  it('filters models by confirmed only, shows unknown in detail without inventing incompatibility', async () => {
    const user = userEvent.setup()
    render(<CatalogPage source={fixtureCatalog} />)
    await ready()
    await screen.findByRole('option', { name: /Демо-модель/ })
    await user.selectOptions(screen.getByRole('combobox'), model.id)
    await screen.findByText('Применимость подтверждена')
    expect(screen.getAllByRole('article')).toHaveLength(1)
    expect(screen.getByRole('article').textContent).toContain('DEMO-CONFIRMED')
    await user.selectOptions(screen.getByRole('combobox'), '')
    await ready()
    await user.click(
      within(screen.getByRole('article', { name: serviceLabel })).getByRole(
        'button',
        { name: service.name },
      ),
    )
    await screen.findByText('Описание не предоставлено в контрактном fixture.')
    await user.selectOptions(screen.getByRole('combobox'), model.id)
    await screen.findByText('Применимость не подтверждена')
    expect(screen.queryByText(/несовместим/i)).toBeNull()
  })

  it('handles both absent and broken images, and rejects external image URLs', async () => {
    const { rerender } = render(<ItemImage item={product} />)
    expect(
      screen.getByRole('img', { name: 'Изображение отсутствует' }),
    ).toBeTruthy()
    rerender(<ItemImage item={confirmed} />)
    fireEvent.error(screen.getByRole('img', { name: confirmed.name }))
    expect(
      screen.getByRole('img', { name: 'Изображение отсутствует' }),
    ).toBeTruthy()
    rerender(
      <ItemImage
        item={{
          ...product,
          image_url:
            'https://example.org/image.png' as CatalogCard['image_url'],
        }}
      />,
    )
    expect(
      screen.getByRole('img', { name: 'Изображение отсутствует' }),
    ).toBeTruthy()
  })

  it('adds a mixed list, prevents duplicates, changes qty, restores and removes', async () => {
    const user = userEvent.setup()
    const view = render(<CatalogPage source={fixtureCatalog} />)
    await user.click(
      within(await ready()).getByRole('button', { name: 'Добавить в список' }),
    )
    await user.click(
      within(screen.getByRole('article', { name: serviceLabel })).getByRole(
        'button',
        { name: 'Добавить в список' },
      ),
    )
    await selected().findByRole('textbox', {
      name: `Количество ${service.sku}`,
    })
    expect(selected().getAllByRole('listitem')).toHaveLength(2)
    expect(
      (
        within(await ready()).getByRole('button', {
          name: 'В выбранном',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
    expect(selected().getByText('Известная часть суммы')).toBeTruthy()
    const qty = selected().getByRole('textbox', {
      name: `Количество ${product.sku}`,
    })
    await user.clear(qty)
    await user.type(qty, '2')
    await waitFor(() =>
      expect(selected().getByText('2 400,00', { exact: false })).toBeTruthy(),
    )
    expect(selected().getByText(/Полная сумма пока не определена/)).toBeTruthy()
    expect(saved()).toEqual([
      { item_id: product.id, qty: 2, revision: product.revision },
      { item_id: service.id, qty: 1, revision: service.revision },
    ])
    view.unmount()
    render(<CatalogPage source={fixtureCatalog} />)
    await selected().findByRole('textbox', {
      name: `Количество ${product.sku}`,
    })
    expect(
      (
        selected().getByRole('textbox', {
          name: `Количество ${product.sku}`,
        }) as HTMLInputElement
      ).value,
    ).toBe('2')
    await user.click(
      selected().getByRole('button', { name: `Удалить ${product.sku}` }),
    )
    expect(selected().getAllByRole('listitem')).toHaveLength(1)
    expect(selected().getByText('Пока не определена')).toBeTruthy()
    await user.click(
      selected().getByRole('button', { name: `Удалить ${service.sku}` }),
    )
    expect(selected().getByText('Начните с каталога')).toBeTruthy()
    expect(saved()).toEqual([])
  })

  it('rejects invalid quantities and enforces 1–999 without mutating storage', async () => {
    const user = userEvent.setup()
    sessionStorage.setItem(
      SELECTION_KEY,
      JSON.stringify([
        { item_id: product.id, qty: 1, revision: product.revision },
      ]),
    )
    render(<CatalogPage source={fixtureCatalog} />)
    const qty = await selected().findByRole('textbox', {
      name: `Количество ${product.sku}`,
    })
    for (const bad of ['0', '-1', '1.5', '1000', 'abc']) {
      fireEvent.change(qty, { target: { value: bad } })
      expect(qty.getAttribute('aria-invalid')).toBe('true')
      expect(saved()).toEqual([
        { item_id: product.id, qty: 1, revision: product.revision },
      ])
      fireEvent.blur(qty)
      expect((qty as HTMLInputElement).value).toBe('1')
    }
    fireEvent.change(qty, { target: { value: '999' } })
    await waitFor(() =>
      expect(saved()).toEqual([
        { item_id: product.id, qty: 999, revision: product.revision },
      ]),
    )
    expect(
      (
        selected().getByRole('button', {
          name: `Увеличить количество ${product.sku}`,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
    await user.click(
      selected().getByRole('button', {
        name: `Уменьшить количество ${product.sku}`,
      }),
    )
    expect((qty as HTMLInputElement).value).toBe('998')
  })

  it('restores only whitelisted fields, discards duplicates/invalid rows, uses fresh prices', async () => {
    sessionStorage.setItem(
      SELECTION_KEY,
      JSON.stringify([
        {
          item_id: product.id,
          qty: 2,
          revision: 1,
          price: '0.01',
          token: 'secret',
          contact: { name: 'Private' },
        },
        { item_id: product.id, qty: 9, revision: 1 },
        { item_id: service.id, qty: -1, revision: 1 },
      ]),
    )
    render(<CatalogPage source={fixtureCatalog} />)
    await selected().findByText('2 400,00', { exact: false })
    expect(saved()).toEqual([{ item_id: product.id, qty: 2, revision: 1 }])
    expect(selected().getByText(/Позиция обновилась/)).toBeTruthy()
    expect(localStorage.length).toBe(0)
  })

  it('remains usable when browser storage is unavailable', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const user = userEvent.setup()
    render(<CatalogPage source={fixtureCatalog} />)
    await user.click(
      within(await ready()).getByRole('button', { name: 'Добавить в список' }),
    )
    await selected().findByRole('button', { name: product.name })
    expect(selected().getByRole('alert').textContent).toContain(
      'не разрешил сохранение',
    )
  })

  it('renders loading, empty, rejected list, retries, and model errors', async () => {
    let finish!: (page: Page) => void
    const getItems = vi
      .fn<CatalogSource['getItems']>()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve
          }),
      )
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(fixtures.catalog_page)
    const getModels = vi
      .fn<CatalogSource['getModels']>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue([])
    const user = userEvent.setup()
    render(<CatalogPage source={{ ...fixtureCatalog, getItems, getModels }} />)
    expect(
      screen.getByRole('heading', { name: 'Загружаем каталог' }),
    ).toBeTruthy()
    await waitFor(() => expect(getItems).toHaveBeenCalledOnce())
    await act(async () => finish({ items: [], total: 0, limit: 20, offset: 0 }))
    expect(
      screen.getByRole('heading', { name: 'Позиции не найдены' }),
    ).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain(
      'Не удалось загрузить модели',
    )
    await user.click(
      screen.getByRole('button', { name: 'Повторить загрузку моделей' }),
    )
    await screen.findByText('Модели оборудования пока не опубликованы.')
    await user.click(
      screen.getAllByRole('button', { name: 'Сбросить фильтры' })[0]!,
    )
    await screen.findByRole('heading', { name: 'Не удалось загрузить данные' })
    await user.click(screen.getByRole('button', { name: 'Повторить' }))
    await ready()
  })

  it('shows detail and selected-item errors, keeps unavailable IDs removable, retries', async () => {
    const user = userEvent.setup()
    const getItem = vi
      .fn<CatalogSource['getItem']>()
      .mockRejectedValue(new Error('404'))
    sessionStorage.setItem(
      SELECTION_KEY,
      JSON.stringify([{ item_id: product.id, qty: 1, revision: 3 }]),
    )
    render(<CatalogPage source={{ ...fixtureCatalog, getItem }} />)
    await selected().findByText('Позиция недоступна')
    expect(selected().getByText('Пока не определена')).toBeTruthy()
    await user.click(
      within(await ready()).getByRole('button', { name: product.name }),
    )
    await screen.findByRole('heading', { name: 'Не удалось загрузить данные' })
    getItem.mockResolvedValue(product)
    await user.click(screen.getByRole('button', { name: 'Повторить' }))
    await screen.findByText(product.description)
    await user.click(
      selected().getByRole('button', { name: 'Повторить загрузку' }),
    )
    await selected().findByRole('button', { name: product.name })
    await user.click(
      selected().getByRole('button', { name: `Удалить ${product.sku}` }),
    )
    expect(selected().getByText('Начните с каталога')).toBeTruthy()
  })

  it('ignores a stale search response that finishes after the latest query', async () => {
    let finish!: (page: Page) => void
    const getItems = vi
      .fn<CatalogSource['getItems']>()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve
          }),
      )
      .mockResolvedValue({
        ...fixtures.catalog_page,
        items: [service],
        total: 1,
      })
    const user = userEvent.setup()
    render(<CatalogPage source={{ ...fixtureCatalog, getItems }} />)
    await waitFor(() => expect(getItems).toHaveBeenCalledOnce())
    await user.click(screen.getByRole('button', { name: 'Услуги' }))
    await screen.findByRole('article', { name: serviceLabel })
    await act(async () => finish(fixtures.catalog_page))
    expect(screen.getAllByRole('article')).toHaveLength(1)
  })

  it('escapes names, SKU, descriptions and tax notes as text', async () => {
    const attack =
      '<img src=x onerror="window.pwned=true"><script>alert(1)</script>'
    const item: CatalogItem = {
      ...product,
      name: attack,
      sku: attack,
      description: attack,
      tax_note: attack,
    }
    const user = userEvent.setup()
    const { container } = render(
      <CatalogPage
        source={{
          ...fixtureCatalog,
          getItems: async () => ({
            items: [item],
            total: 1,
            limit: 20,
            offset: 0,
          }),
          getItem: async () => item,
        }}
      />,
    )
    const card = await screen.findByRole('article')
    expect(card.textContent).toContain(attack)
    await user.click(within(card).getByRole('button', { name: attack }))
    await waitFor(() =>
      expect(container.querySelector('.item-description')?.textContent).toBe(
        attack,
      ),
    )
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('[onerror]')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
  })

  it('supports pagination with the contract limit and offset', async () => {
    const getItems = vi
      .fn<CatalogSource['getItems']>()
      .mockImplementation(async (query) => ({
        items: [product],
        total: 21,
        limit: 20,
        offset: query.offset ?? 0,
      }))
    const user = userEvent.setup()
    render(<CatalogPage source={{ ...fixtureCatalog, getItems }} />)
    await ready()
    expect(
      (screen.getByRole('button', { name: 'Назад' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Далее' }))
    await waitFor(() => expect(getItems.mock.calls.at(-1)?.[0].offset).toBe(20))
    await screen.findByText('Показано 21–21 из 21')
    expect(
      (screen.getByRole('button', { name: 'Далее' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true)
  })
})
