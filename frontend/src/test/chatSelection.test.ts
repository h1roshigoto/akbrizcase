import { act, renderHook } from '@testing-library/react'
import { expect, it } from 'vitest'
import { useSelection } from '../state/selection'
import { fixtures } from '../api/fixtures'
import { id } from './chatFixtures'

it('validates chat suggestions atomically and preserves previously selected quantities/revisions for review', () => {
  const { result } = renderHook(useSelection)
  const card = fixtures.catalog_item
  act(() => result.current.add(card))
  act(() => result.current.setQty(card.id, 5))
  act(() => {
    expect(
      result.current.addSuggestions([
        { card: { ...card, revision: card.revision + 1 }, qty: 2 },
        { card: { ...card, id: id(99) }, qty: 999 },
      ]),
    ).toBe(true)
  })
  expect(result.current.items[0]).toEqual({
    item_id: card.id,
    qty: 5,
    revision: card.revision,
  })
  expect(result.current.items[1]?.qty).toBe(999)
  act(() => {
    expect(result.current.addSuggestions([{ card, qty: 1000 }])).toBe(false)
    expect(
      result.current.addSuggestions([
        { card, qty: 1 },
        { card, qty: 1 },
      ]),
    ).toBe(false)
  })
  expect(result.current.items).toHaveLength(2)
})
it('refuses additions above capacity without a partial merge', () => {
  const { result } = renderHook(useSelection)
  for (let n = 1; n <= 100; n++)
    act(() => result.current.add({ ...fixtures.catalog_item, id: id(n) }))
  act(() =>
    expect(
      result.current.addSuggestions([
        { card: { ...fixtures.catalog_item, id: id(101) }, qty: 1 },
      ]),
    ).toBe(false),
  )
  expect(result.current.items).toHaveLength(100)
})
