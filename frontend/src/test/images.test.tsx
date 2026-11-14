import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { fixtures } from '../api/fixtures'
import type { CatalogCard } from '../api/types'
import { ItemImage } from '../components/ItemImage'

const product = fixtures.catalog_item
const path = (key: string) => `/api/v1/catalog/items/${product.id}/image/${key}.png`
const image = (url: string | null, kind: 'product' | 'service' = 'product'): CatalogCard => ({
  ...product, kind, image_url: url as CatalogCard['image_url'],
})

describe('server catalog image boundary', () => {
  it.each([null, path('a'.repeat(64))])('shows only На заказ for the exact left-post SKU, even with a photo (%s)', (url) => {
    const item = { ...image(url), sku: 'ЯИУШ.301421.011-02' }
    const snapshot = structuredClone(item)
    const { container } = render(<ItemImage item={item} />)
    expect(screen.getByRole('img', { name: 'На заказ' }).textContent).toBe('На заказ')
    expect(container.querySelector('img, svg, small')).toBeNull()
    expect(item).toEqual(snapshot)
  })

  it('keeps the usual placeholder for other products and matches by SKU, not name', () => {
    render(<ItemImage item={{ ...image(null), name: 'Стойка левая в сборе', sku: 'ЯИУШ.301421.011-01' }} />)
    expect(screen.getByRole('img', { name: 'Изображение отсутствует' })).toBeTruthy()
    expect(screen.queryByText('На заказ')).toBeNull()
  })

  it('uses a technical service icon without missing-image text, including after a failed image', () => {
    const { container, rerender } = render(<ItemImage item={image(null, 'service')} />)
    expect(screen.getByRole('img', { name: 'Сервисная услуга' }).querySelector('svg')).toBeTruthy()
    expect(container.textContent).toBe('')
    rerender(<ItemImage item={image(path('a'.repeat(64)), 'service')} />)
    expect(screen.getByAltText(product.name)).toBeTruthy()
    fireEvent.error(screen.getByAltText(product.name))
    expect(screen.getByRole('img', { name: 'Сервисная услуга' })).toBeTruthy()
    expect(container.textContent).toBe('')
  })

  it('does not apply the product SKU override to a service with a real photo', () => {
    render(<ItemImage item={{ ...image(path('a'.repeat(64)), 'service'), sku: 'ЯИУШ.301421.011-02' }} />)
    expect(screen.getByAltText(product.name)).toBeTruthy()
    expect(screen.queryByText('На заказ')).toBeNull()
  })

  it.each([
    null, 'https://example.invalid/a.png', '//example.invalid/a.png',
    'javascript:alert(1)', 'data:image/png;base64,AAAA',
    path('a'.repeat(64)) + '?external=true', path('a'.repeat(64)) + '#x',
    path('a'.repeat(64)) + '/extra', path('../other'),
    path('a'.repeat(64)).replace(product.id, '11111111-1111-4111-8111-111111111111'),
    path('not-a-server-key'), path('A'.repeat(64)),
  ])('never requests malformed or unrelated URL %s', (url) => {
    const { container } = render(<ItemImage item={image(url)} />)
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByRole('img', { name: 'Изображение отсутствует' })).toBeTruthy()
  })

  it.each(['product', 'service'] as const)('falls back after a network/404 error and loads a replaced %s image', (kind) => {
    const first = path('a'.repeat(64))
    const next = path('b'.repeat(64))
    const { rerender } = render(<ItemImage item={image(first, kind)} />)
    expect(screen.getByAltText(product.name).getAttribute('src')).toBe(first)
    fireEvent.error(screen.getByAltText(product.name))
    expect(screen.queryByAltText(product.name)).toBeNull()
    rerender(<ItemImage item={image(next, kind)} />)
    expect(screen.getByAltText(product.name).getAttribute('src')).toBe(next)
    rerender(<ItemImage item={image(null, kind)} />)
    expect(screen.queryByAltText(product.name)).toBeNull()
  })
})
