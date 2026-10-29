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

it('rejects 50 existing positions plus one chat suggestion atomically', () => {
  const original = Array.from({ length: 50 }, (_, n) => ({
    item_id: id(n + 1),
    qty: n + 1,
    revision: n + 2,
  }))
  sessionStorage.setItem('akbrizcase.selected-items', JSON.stringify(original))
  const { result } = renderHook(useSelection)
  act(() =>
    expect(
      result.current.addSuggestions([
        { card: { ...fixtures.catalog_item, id: id(51) }, qty: 1 },
      ]),
    ).toBe(false),
  )
  expect(result.current.items).toEqual(original)
  expect(
    JSON.parse(sessionStorage.getItem('akbrizcase.selected-items')!),
  ).toEqual(original)
})
it('rejects the entire proposal if only part would fit and preserves existing duplicates at the limit', () => {
  const { result } = renderHook(useSelection)
  act(() => {
    for (let n = 1; n <= 49; n++)
      result.current.add({ ...fixtures.catalog_item, id: id(n) })
    expect(
      result.current.addSuggestions(
        [50, 51].map((n) => ({
          card: { ...fixtures.catalog_item, id: id(n) },
          qty: 1,
        })),
      ),
    ).toBe(false)
  })
  expect(result.current.items).toHaveLength(49)
  act(() => {
    result.current.add({ ...fixtures.catalog_item, id: id(50), revision: 3 })
    result.current.setQty(id(50), 7)
    expect(
      result.current.addSuggestions([
        { card: { ...fixtures.catalog_item, id: id(50), revision: 4 }, qty: 1 },
      ]),
    ).toBe(true)
  })
  expect(result.current.items.at(-1)).toEqual({
    item_id: id(50),
    qty: 7,
    revision: 3,
  })
})
it('reports the actual limit for stale callbacks and multiple proposals within a single batch', () => {
  const { result } = renderHook(useSelection)
  const staleAddSuggestions = result.current.addSuggestions
  act(() => {
    for (let n = 1; n <= 49; n++)
      result.current.add({ ...fixtures.catalog_item, id: id(n) })
    expect(
      staleAddSuggestions([
        { card: { ...fixtures.catalog_item, id: id(50) }, qty: 2 },
      ]),
    ).toBe(true)
    expect(
      staleAddSuggestions([
        { card: { ...fixtures.catalog_item, id: id(51) }, qty: 1 },
      ]),
    ).toBe(false)
  })
  expect(result.current.items).toHaveLength(50)
  expect(result.current.items.at(-1)).toEqual({
    item_id: id(50),
    qty: 2,
    revision: fixtures.catalog_item.revision,
  })
})
