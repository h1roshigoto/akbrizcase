import { CatalogError, type CatalogSource } from './catalog'
import { fixtures } from './fixtures'
import type { Applicability, CatalogCard, UUID } from './types'

function applicability(
  card: CatalogCard,
  modelId: UUID | null | undefined,
): Applicability | null {
  if (!modelId) return null
  if (card.applicability?.model_id === modelId) return card.applicability
  return { model_id: modelId, status: 'unknown', sources: [] }
}

/** In-memory adapter for approved synthetic catalog fixtures; never intercepts HTTP. */
export const fixtureCatalog: CatalogSource = {
  mode: 'fixtures',
  async getModels(signal) {
    signal.throwIfAborted()
    return structuredClone(fixtures.models.items)
  },
  async getItems(query, signal) {
    signal.throwIfAborted()
    const search = (query.q ?? '').trim().toLocaleLowerCase('ru')
    const items = fixtures.catalog_page.items
      .filter((card) => !query.kind || card.kind === query.kind)
      .filter((card) =>
        `${card.name} ${card.sku}`.toLocaleLowerCase('ru').includes(search),
      )
      .map((card) => ({
        ...card,
        applicability: applicability(card, query.model_id),
      }))
      .filter(
        (card) => !query.model_id || card.applicability?.status === 'confirmed',
      )
    const limit = query.limit ?? 20
    const offset = query.offset ?? 0
    return structuredClone({
      items: items.slice(offset, offset + limit),
      total: items.length,
      limit,
      offset,
    })
  },
  async getItem(id, modelId, signal) {
    signal.throwIfAborted()
    const card = fixtures.catalog_page.items.find((item) => item.id === id)
    if (!card)
      throw new CatalogError('Позиция не найдена или больше недоступна.', 404)
    // Only catalog_item has approved description/attributes. Do not invent others.
    const detail =
      id === fixtures.catalog_item.id
        ? fixtures.catalog_item
        : { ...card, description: '', attributes: {} }
    return structuredClone({
      ...detail,
      applicability: applicability(card, modelId),
    })
  },
}
