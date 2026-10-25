import type { CatalogCard as Card, UUID } from '../api/types'
import { ItemImage } from './ItemImage'
import { ApplicabilityStatus, ItemPrice } from './ItemFacts'
import { Icon } from './Icon'

export function CatalogCard({
  item,
  selected,
  atCapacity,
  onAdd,
  onOpen,
}: {
  item: Card
  selected: boolean
  atCapacity: boolean
  onAdd: (item: Card) => void
  onOpen: (id: UUID) => void
}) {
  return (
    <article className="catalog-card" aria-label={`${item.name}, ${item.sku}`}>
      <div className="card-visual">
        <ItemImage item={item} />
        <span className={`kind-badge ${item.kind}`}>
          {item.kind === 'product' ? 'Товар' : 'Услуга'}
        </span>
      </div>
      <div className="card-body">
        <p className="sku">{item.sku}</p>
        <h3>
          <button className="title-button" onClick={() => onOpen(item.id)}>
            {item.name}
            <Icon name="arrow" />
          </button>
        </h3>
        <ApplicabilityStatus item={item} />
        <ItemPrice item={item} />
        <button
          className={`add-button ${selected ? 'is-selected' : ''}`}
          disabled={selected || atCapacity}
          onClick={() => onAdd(item)}
        >
          <Icon name={selected ? 'check' : 'plus'} />
          {selected ? 'В выбранном' : 'Добавить в список'}
        </button>
      </div>
    </article>
  )
}
