import type {
  CatalogItem,
  CatalogPage,
  CatalogQuery,
  EquipmentModel,
  UUID,
} from './types'

/** Implementations must keep prices as decimal strings and honour AbortSignal. */
export interface CatalogSource {
  readonly mode: 'fixtures' | 'api'
  getModels(signal: AbortSignal): Promise<EquipmentModel[]>
  getItems(query: CatalogQuery, signal: AbortSignal): Promise<CatalogPage>
  getItem(
    id: UUID,
    modelId: UUID | null,
    signal: AbortSignal,
  ): Promise<CatalogItem>
}

export class CatalogError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message)
    this.name = 'CatalogError'
  }
}
