import { describe, expect, it, vi } from 'vitest'
import { fixtures } from '../api/fixtures'
import { httpCatalog } from '../api/httpCatalog'
import { fixtureCatalog } from '../api/fixtureCatalog'

const signal = () => new AbortController().signal

describe('real API transport (prepared, not enabled in App)', () => {
  it('only makes same-origin GET calls with encoded filters and model context', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => fixtures.catalog_page })
    vi.stubGlobal('fetch', fetcher)
    const abort = signal()
    const query = {
      q: 'a&b / товар',
      kind: 'product' as const,
      model_id: fixtures.models.items[0]!.id,
      limit: 20,
      offset: 0,
    }
    await httpCatalog.getItems(query, abort)
    const [path, options] = fetcher.mock.calls[0]!
    expect(path).toBe(
      `/api/v1/catalog/items?${new URLSearchParams(Object.entries(query).map(([key, value]) => [key, String(value)]))}`,
    )
    expect(options).toEqual(
      expect.objectContaining({
        method: 'GET',
        signal: abort,
        credentials: 'same-origin',
        cache: 'no-store',
      }),
    )
    fetcher.mockResolvedValue({
      ok: true,
      json: async () => fixtures.catalog_item,
    })
    await httpCatalog.getItem(fixtures.catalog_item.id, query.model_id, abort)
    expect(fetcher.mock.calls[1]?.[0]).toBe(
      `/api/v1/catalog/items/${fixtures.catalog_item.id}?model_id=${query.model_id}`,
    )
    await httpCatalog.getItem(fixtures.catalog_item.id, null, abort)
    expect(fetcher.mock.calls[2]?.[0]).toBe(
      `/api/v1/catalog/items/${fixtures.catalog_item.id}`,
    )
  })

  it('loads all equipment model pages', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ ...fixtures.models, total: 2 }),
      })
      .mockResolvedValueOnce({ ok: true, json: async () => fixtures.models })
    vi.stubGlobal('fetch', fetcher)
    expect(await httpCatalog.getModels(signal())).toHaveLength(2)
    expect(fetcher.mock.calls.map((call) => call[0])).toEqual([
      '/api/v1/equipment-models?limit=100&offset=0',
      '/api/v1/equipment-models?limit=100&offset=100',
    ])
  })

  it.each([404, 422, 429, 503])(
    'rejects HTTP %s without a fixture fallback',
    async (status) => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status }))
      await expect(httpCatalog.getItems({}, signal())).rejects.toMatchObject({
        status,
      })
    },
  )

  it('rejects malformed JSON and propagates network/abort errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => {
          throw new SyntaxError()
        },
      }),
    )
    await expect(httpCatalog.getItems({}, signal())).rejects.toThrow(
      'некорректный ответ',
    )
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))
    await expect(httpCatalog.getItems({}, signal())).rejects.toThrow('offline')
  })

  it.each([
    null,
    {},
    { ...fixtures.catalog_page, items: null },
    {
      ...fixtures.catalog_page,
      items: [{ ...fixtures.catalog_item, price: 1200 }],
    },
    {
      ...fixtures.catalog_page,
      items: [{ ...fixtures.catalog_item, price: 'invalid' }],
    },
  ])(
    'rejects malformed response shapes and non-decimal prices',
    async (value) => {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({ ok: true, json: async () => value }),
      )
      await expect(httpCatalog.getItems({}, signal())).rejects.toThrow(
        'некорректный ответ',
      )
    },
  )
})

describe('fixture adapter', () => {
  it('returns null applicability without a model and only confirmed rows with model_id', async () => {
    expect(
      (await fixtureCatalog.getItems({}, signal())).items.every(
        (item) => item.applicability === null,
      ),
    ).toBe(true)
    const page = await fixtureCatalog.getItems(
      { model_id: fixtures.models.items[0]!.id },
      signal(),
    )
    expect(page.total).toBe(1)
    expect(page.items[0]?.sku).toBe('DEMO-CONFIRMED')
  })
  it('rejects a missing detail and cancelled request', async () => {
    await expect(
      fixtureCatalog.getItem(
        'ffffffff-ffff-ffff-ffff-ffffffffffff',
        null,
        signal(),
      ),
    ).rejects.toMatchObject({ status: 404 })
    const controller = new AbortController()
    controller.abort()
    await expect(fixtureCatalog.getModels(controller.signal)).rejects.toThrow()
  })
})
