import { useEffect, useState } from 'react'
import type { CatalogCard, UUID } from '../api/types'

export const SELECTION_KEY = 'akbrizcase.selected-items'
export type SelectedItem = { item_id: UUID; qty: number; revision: number }
const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
export const validQty = (qty: number) =>
  Number.isInteger(qty) && qty >= 1 && qty <= 999

export function parseSelection(value: string | null): SelectedItem[] {
  try {
    const input: unknown = JSON.parse(value ?? '[]')
    if (!Array.isArray(input)) return []
    const seen = new Set<string>()
    return input
      .flatMap((row: unknown): SelectedItem[] => {
        if (!row || typeof row !== 'object') return []
        const { item_id, qty, revision } = row as Record<string, unknown>
        if (
          typeof item_id !== 'string' ||
          !uuidPattern.test(item_id) ||
          typeof qty !== 'number' ||
          !validQty(qty) ||
          typeof revision !== 'number' ||
          !Number.isSafeInteger(revision) ||
          revision < 1
        )
          return []
        const normalized = item_id.toLowerCase() as UUID
        if (seen.has(normalized)) return []
        seen.add(normalized)
        return [{ item_id: normalized, qty, revision }]
      })
      .slice(0, 100)
  } catch {
    return []
  }
}

function restore(): SelectedItem[] {
  try {
    return parseSelection(sessionStorage.getItem(SELECTION_KEY))
  } catch {
    return []
  }
}

export function useSelection() {
  const [items, setItems] = useState(restore)
  const [storageFailed, setStorageFailed] = useState(false)
  useEffect(() => {
    try {
      // Explicit projection: never persist names, prices, contacts or other fields.
      sessionStorage.setItem(
        SELECTION_KEY,
        JSON.stringify(
          items.map(({ item_id, qty, revision }) => ({
            item_id,
            qty,
            revision,
          })),
        ),
      )
      setStorageFailed(false)
    } catch {
      setStorageFailed(true)
    }
  }, [items])
  return {
    items,
    storageFailed,
    add(card: CatalogCard) {
      setItems((current) =>
        current.some((row) => row.item_id === card.id) || current.length >= 100
          ? current
          : [...current, { item_id: card.id, qty: 1, revision: card.revision }],
      )
    },
    setQty(id: UUID, qty: number) {
      if (validQty(qty))
        setItems((current) =>
          current.map((row) => (row.item_id === id ? { ...row, qty } : row)),
        )
    },
    acceptRevision(id: UUID, previous: number, revision: number) {
      setItems((current) =>
        current.map((row) =>
          row.item_id === id && row.revision === previous
            ? { ...row, revision }
            : row,
        ),
      )
    },
    remove(id: UUID) {
      setItems((current) => current.filter((row) => row.item_id !== id))
    },
  }
}

export type Selection = ReturnType<typeof useSelection>
