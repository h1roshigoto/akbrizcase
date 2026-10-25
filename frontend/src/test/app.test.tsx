import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import App from '../App'
import { fixtures } from '../api/fixtures'

it('composes the real HTTP source and retries a 503 without fixture fallback', async () => {
  const user = userEvent.setup()
  let unavailable = true
  const fetcher = vi.fn(async (path: string) => {
    if (path.startsWith('/api/v1/equipment-models'))
      return { ok: true, json: async () => fixtures.models }
    return unavailable
      ? { ok: false, status: 503 }
      : { ok: true, json: async () => fixtures.catalog_page }
  })
  vi.stubGlobal('fetch', fetcher)
  render(<App />)
  const results = within(screen.getByRole('region', { name: 'Результаты каталога' }))
  await results.findByRole('alert')
  expect(results.queryAllByRole('article')).toHaveLength(0)
  expect(screen.queryByText(/Интерфейс на синтетических fixtures/)).toBeNull()
  expect(fetcher.mock.calls.map(([path]) => path)).toEqual(
    expect.arrayContaining([
      '/api/v1/equipment-models?limit=100&offset=0',
      '/api/v1/catalog/items?limit=20&offset=0',
    ]),
  )
  unavailable = false
  await user.click(results.getByRole('button', { name: 'Повторить' }))
  await results.findByRole('article', {
    name: `${fixtures.catalog_item.name}, ${fixtures.catalog_item.sku}`,
  })
})

it('shows an error for an invalid HTTP response instead of rendering fixtures', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }))
  render(<App />)
  const results = within(screen.getByRole('region', { name: 'Результаты каталога' }))
  await results.findByRole('alert')
  expect(results.queryAllByRole('article')).toHaveLength(0)
})
