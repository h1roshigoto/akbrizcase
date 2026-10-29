import { useEffect, useRef, useState } from 'react'
import type { CatalogCard, UUID } from '../api/types'

export const SELECTION_KEY = 'akbrizcase.selected-items'
export const MAX_REQUEST_ITEMS = 50
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
  const currentItems = useRef(items)
  // Update the in-memory selection before returning to the caller. Chat actions
  // can finish after another render or within the same React batch; their
  // success result must describe the actual committed selection, not a closure.
  const updateItems = (change: (current: SelectedItem[]) => SelectedItem[]) => {
    const next = change(currentItems.current)
    currentItems.current = next
    setItems(next)
  }
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
    addSuggestions(suggestions: { card: CatalogCard; qty: number }[]) {
      if (
        suggestions.length < 1 ||
        suggestions.length > MAX_REQUEST_ITEMS ||
        suggestions.some(({ qty }) => !validQty(qty)) ||
        new Set(suggestions.map(({ card }) => card.id)).size !==
          suggestions.length
      )
        return false
      const current = currentItems.current
      const additions = suggestions.filter(
        (s) => !current.some((i) => i.item_id === s.card.id),
      )
      if (current.length + additions.length > MAX_REQUEST_ITEMS) return false
      updateItems(() => [
        ...current,
        ...additions.map(({ card, qty }) => ({
          item_id: card.id,
          qty,
          revision: card.revision,
        })),
      ])
      return true
    },
    add(card: CatalogCard) {
      updateItems((current) =>
        current.some((row) => row.item_id === card.id) || current.length >= 100
          ? current
          : [...current, { item_id: card.id, qty: 1, revision: card.revision }],
      )
    },
    setQty(id: UUID, qty: number) {
      if (validQty(qty))
        updateItems((current) =>
          current.map((row) => (row.item_id === id ? { ...row, qty } : row)),
        )
    },
    acceptRevision(id: UUID, previous: number, revision: number) {
      updateItems((current) =>
        current.map((row) =>
          row.item_id === id && row.revision === previous
            ? { ...row, revision }
            : row,
        ),
      )
    },
    remove(id: UUID) {
      updateItems((current) => current.filter((row) => row.item_id !== id))
    },
  }
}

export type Selection = ReturnType<typeof useSelection>
