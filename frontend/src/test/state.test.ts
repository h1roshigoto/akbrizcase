import { describe, expect, it } from 'vitest'
import { fixtures } from '../api/fixtures'
import { parseSelection } from '../state/selection'
import { formatKopecks, formatPrice, toKopecks } from '../state/money'

describe('storage boundary', () => {
  it.each([null, '', '{bad', '{}', 'null', '42', '[null,1,"x"]'])(
    'discards corrupt storage %s',
    (raw) => {
      expect(parseSelection(raw)).toEqual([])
    },
  )
  it.each([0, -1, 1.2, 1000, '2', null, true])(
    'rejects invalid qty %s',
    (qty) => {
      expect(
        parseSelection(
          JSON.stringify([
            { item_id: fixtures.catalog_item.id, qty, revision: 1 },
          ]),
        ),
      ).toEqual([])
    },
  )
  it.each([0, -1, 1.2, '1', Number.MAX_SAFE_INTEGER + 1])(
    'rejects invalid revision %s',
    (revision) => {
      expect(
        parseSelection(
          JSON.stringify([
            { item_id: fixtures.catalog_item.id, qty: 1, revision },
          ]),
        ),
      ).toEqual([])
    },
  )
  it('rejects a malformed ID', () => {
    expect(parseSelection('[{"item_id":"bad","qty":1,"revision":1}]')).toEqual(
      [],
    )
  })
})

describe('exact decimal money', () => {
  it('sums decimal strings without floating point rounding', () => {
    expect(formatKopecks(toKopecks('0.10') * 3n + toKopecks('0.20'))).toBe(
      '0,50',
    )
    expect(formatPrice('9007199254740993.01')).toBe(
      '9\u00a0007\u00a0199\u00a0254\u00a0740\u00a0993,01',
    )
    expect(formatPrice('0')).toBe('0,00')
  })
})
