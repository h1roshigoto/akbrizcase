import { CatalogError, type CatalogSource } from './catalog'
import { isCatalogItem, isCatalogPage, isModelsPage } from './catalogResponse'
import type {
  CatalogItem,
  CatalogPage,
  CatalogQuery,
  EquipmentModelsPage,
} from './types'

/** Same-origin transport only. No fixture fallback or business endpoint emulation. */
async function get<T>(
  path: string,
  signal: AbortSignal,
  validate: (value: unknown) => value is T,
): Promise<T> {
  const response = await fetch(`/api/v1/${path}`, {
    method: 'GET',
    signal,
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
    cache: 'no-store',
  })
  if (!response.ok) {
    throw new CatalogError(
      'Не удалось получить данные каталога. Попробуйте ещё раз.',
      response.status,
    )
  }
  try {
    const value: unknown = await response.json()
    if (!validate(value)) throw new Error('Invalid response shape')
    return value
  } catch {
    throw new CatalogError('Сервер вернул некорректный ответ.')
  }
}

export function catalogQuery(query: CatalogQuery): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== null && value !== undefined && value !== '')
      params.set(key, String(value))
  }
  return params.toString()
}

export const httpCatalog: CatalogSource = {
  mode: 'api',
  async getModels(signal) {
    const models: EquipmentModelsPage['items'] = []
    for (let offset = 0; offset <= 10000; offset += 100) {
      const page = await get<EquipmentModelsPage>(
        `equipment-models?limit=100&offset=${offset}`,
        signal,
        isModelsPage,
      )
      models.push(...page.items)
      if (models.length >= page.total) return models
      if (page.items.length === 0) break
    }
    throw new CatalogError('Не удалось загрузить весь список моделей.')
  },
  getItems: (query, signal) =>
    get<CatalogPage>(
      `catalog/items?${catalogQuery(query)}`,
      signal,
      isCatalogPage,
    ),
  getItem: (id, modelId, signal) =>
    get<CatalogItem>(
      `catalog/items/${encodeURIComponent(id)}${modelId ? `?model_id=${encodeURIComponent(modelId)}` : ''}`,
      signal,
      isCatalogItem,
    ),
}
