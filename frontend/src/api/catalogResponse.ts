import type { CatalogItem, CatalogPage, EquipmentModelsPage } from './types'

// Minimal wire checks at the HTTP boundary prevent malformed JSON from crashing
// rendering. Backend schemas remain authoritative for all business constraints.
type RecordValue = Record<string, unknown>
const record = (value: unknown): value is RecordValue =>
  typeof value === 'object' && value !== null
const integer = (value: unknown, minimum: number) =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum
const uuid = (value: unknown) =>
  typeof value === 'string' &&
  /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value)
const strings = (row: RecordValue, keys: string[]) =>
  keys.every((key) => typeof row[key] === 'string')

function card(value: unknown): boolean {
  if (!record(value)) return false
  return (
    uuid(value.id) &&
    strings(value, ['name', 'sku', 'unit', 'tax_note']) &&
    ['product', 'service'].includes(String(value.kind)) &&
    value.currency === 'RUB' &&
    integer(value.revision, 1) &&
    typeof value.requires_shipping === 'boolean' &&
    (value.image_url === null || typeof value.image_url === 'string') &&
    ((value.price_mode === 'fixed' &&
      typeof value.price === 'string' &&
      /^\d+(\.\d{1,2})?$/.test(value.price)) ||
      (value.price_mode === 'on_request' && value.price === null)) &&
    (value.applicability === null ||
      (record(value.applicability) &&
        uuid(value.applicability.model_id) &&
        ['confirmed', 'unknown'].includes(String(value.applicability.status)) &&
        Array.isArray(value.applicability.sources)))
  )
}

function page(value: unknown): value is RecordValue & { items: unknown[] } {
  return (
    record(value) &&
    Array.isArray(value.items) &&
    integer(value.total, 0) &&
    integer(value.limit, 1) &&
    integer(value.offset, 0)
  )
}

export function isCatalogPage(value: unknown): value is CatalogPage {
  return page(value) && value.items.every(card)
}

export function isCatalogItem(value: unknown): value is CatalogItem {
  return (
    record(value) &&
    card(value) &&
    typeof value.description === 'string' &&
    record(value.attributes)
  )
}

export function isModelsPage(value: unknown): value is EquipmentModelsPage {
  return (
    page(value) &&
    value.items.every(
      (model) =>
        record(model) &&
        uuid(model.id) &&
        strings(model, ['code', 'name', 'description']),
    )
  )
}
