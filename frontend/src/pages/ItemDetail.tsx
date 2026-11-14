import { useCallback, useState } from 'react'
import type { CatalogSource } from '../api/catalog'
import type { UUID } from '../api/types'
import { useResource } from '../state/useResource'
import type { Selection } from '../state/selection'
import { ItemImage } from '../components/ItemImage'
import { ApplicabilityStatus, ItemPrice } from '../components/ItemFacts'
import { StatusPanel } from '../components/StatusPanel'
import { DeliveryEstimate } from '../components/DeliveryEstimate'
import { AddControl } from '../components/AddControl'

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
  return (
    <section aria-label="Карточка позиции" className="detail-section" id="detail">
      {result.status !== 'success' ? (
        <StatusPanel
          kind={result.status}
          onRetry={() => setAttempt((value) => value + 1)}
        />
      ) : (
        item && (
          <article className="panel detail item-detail">
            <ItemImage item={item} />
            <div className="detail-body">
              <h2 id="d-name">{item.name}</h2><p className="meta">{item.sku}</p>
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
              {item.requires_shipping && <><b className="calc-h">Доставка СДЭК</b><DeliveryEstimate /></>}
              <div className="d-acts"><AddControl item={item} selection={selection} large /><button className="btn ghost" id="back" onClick={onBack}>Назад к списку</button></div>
            </div>
          </article>
        )
      )}
    </section>
  )
}
