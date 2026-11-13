import type { CatalogCard as Card, UUID } from '../api/types'
import type { Selection } from '../state/selection'
import { ItemImage } from './ItemImage'
import { ApplicabilityStatus, ItemPrice } from './ItemFacts'
import { AddControl } from './AddControl'

export function CatalogCard({ item, selection, onOpen }: { item: Card; selection: Selection; onOpen: (id: UUID) => void }) {
  return <article className="card pcard" aria-label={`${item.name}, ${item.sku}`}>
    <ItemImage item={item} />
    <h3>{item.name}</h3><small>{item.sku}</small>
    <ApplicabilityStatus item={item} /><ItemPrice item={item} />
    <div className="acts"><AddControl item={item} selection={selection} /><button className="btn ghost sm" onClick={() => onOpen(item.id)}>Подробнее</button></div>
  </article>
}
