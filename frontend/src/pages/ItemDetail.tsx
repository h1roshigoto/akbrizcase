import { useCallback, useState } from 'react'
import type { CatalogSource } from '../api/catalog'
import type { UUID } from '../api/types'
import { useResource } from '../state/useResource'
import type { Selection } from '../state/selection'
import { ItemImage } from '../components/ItemImage'
import { ApplicabilityStatus, ItemPrice } from '../components/ItemFacts'
import { StatusPanel } from '../components/StatusPanel'
import { Icon } from '../components/Icon'

export function ItemDetail({
  source,
  id,
  modelId,
  selection,
  onBack,
}: {
  source: CatalogSource
  id: UUID
  modelId: UUID | null
  selection: Selection
  onBack: () => void
}) {
  const [attempt, setAttempt] = useState(0)
  const load = useCallback(
    (signal: AbortSignal) => source.getItem(id, modelId, signal),
    [source, id, modelId, attempt],
  )
  const result = useResource(load)
  const item = result.data
  const selected = selection.items.some((row) => row.item_id === id)
  return (
    <section aria-label="Карточка позиции" className="detail-section">
      <button className="back-button" onClick={onBack}>
        ← Вернуться в каталог
      </button>
      {result.status !== 'success' ? (
        <StatusPanel
          kind={result.status}
          onRetry={() => setAttempt((value) => value + 1)}
        />
      ) : (
        item && (
          <article className="item-detail">
            <ItemImage item={item} />
            <div className="detail-body">
              <span className={`kind-label ${item.kind}`}>
                {item.kind === 'product' ? 'Товар' : 'Услуга'}
              </span>
              <p className="sku">{item.sku}</p>
              <h2>{item.name}</h2>
              <p className="item-description">
                {item.description ||
                  (source.mode === 'fixtures'
                    ? 'Описание не предоставлено в контрактном fixture.'
                    : 'Описание не предоставлено.')}
              </p>
              <ApplicabilityStatus item={item} />
              <ItemPrice item={item} />
              <p className="shipping-note">
                {item.requires_shipping
                  ? 'Требуется доставка. Условия согласует менеджер.'
                  : 'Доставка не требуется.'}
              </p>
              <button
                className={`add-button ${selected ? 'is-selected' : ''}`}
                disabled={selected || selection.items.length >= 100}
                onClick={() => selection.add(item)}
              >
                <Icon name={selected ? 'check' : 'plus'} />
                {selected ? 'В выбранном' : 'Добавить в список'}
              </button>
            </div>
          </article>
        )
      )}
    </section>
  )
}
