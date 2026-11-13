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


describe('catalog presentation fallbacks', () => {
  const stand = { ...product, sku: 'ЯИУШ.301421.011-02', image_url: null }
  it('marks only the exact left-post SKU and preserves its commercial fields', () => {
    const { rerender } = render(<ItemImage item={stand} />)
    expect(screen.getByRole('img', { name: 'На заказ' })).toBeTruthy()
    expect(screen.getByText('Поставка по согласованию')).toBeTruthy()
    expect(screen.queryByText('Изображение отсутствует')).toBeNull()
    rerender(<ItemImage item={{ ...stand, sku: stand.sku + '-other' }} />)
    expect(screen.getByRole('img', { name: 'Изображение отсутствует' })).toBeTruthy()
    expect(screen.queryByText('На заказ')).toBeNull()
    expect(stand.price).toBe(product.price)
    expect(stand.price_mode).toBe(product.price_mode)
  })
  it('uses a neutral local illustration for services without an image', () => {
    const { container } = render(<ItemImage item={image(null, 'service')} />)
    expect(screen.getByRole('img', { name: 'Сервисная услуга' })).toBeTruthy()
    expect(container.querySelector('svg[aria-hidden="true"]')).toBeTruthy()
    expect(container.querySelector('img')).toBeNull()
    expect(screen.queryByText('Изображение отсутствует')).toBeNull()
    expect(screen.queryByText('На заказ')).toBeNull()
  })
  it.each(['product', 'service'] as const)('keeps real validated %s images ahead of design assets', (kind) => {
    const url = path('a'.repeat(64))
    render(<ItemImage item={{ ...stand, kind, image_url: url as CatalogCard['image_url'] }} />)
    expect(screen.getByAltText(product.name).getAttribute('src')).toBe(url)
    expect(screen.queryByText('На заказ')).toBeNull()
    expect(screen.queryByText('Сервис и поддержка')).toBeNull()
  })
  it.each(['product', 'service'] as const)('restores the appropriate %s design after an image error', (kind) => {
    render(<ItemImage item={{ ...stand, kind, image_url: path('b'.repeat(64)) as CatalogCard['image_url'] }} />)
    fireEvent.error(screen.getByAltText(product.name))
    expect(screen.getByRole('img', { name: kind === 'service' ? 'Сервисная услуга' : 'На заказ' })).toBeTruthy()
    expect(screen.queryByText('Изображение отсутствует')).toBeNull()
  })
})
