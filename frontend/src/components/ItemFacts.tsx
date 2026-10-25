import type { CatalogCard } from '../api/types'
import { formatPrice } from '../state/money'
import { Icon } from './Icon'

export function ItemPrice({ item }: { item: CatalogCard }) {
  return (
    <div className="price-block">
      <p className="price">
        {item.price_mode === 'fixed' && item.price !== null ? (
          <>
            {formatPrice(item.price)} <span>{item.currency}</span>
          </>
        ) : (
          <>
            Цена по запросу <span>{item.currency}</span>
          </>
        )}
      </p>
      <p className="price-caption">за {item.unit}</p>
      <p className="tax-note">{item.tax_note}</p>
    </div>
  )
}

export function ApplicabilityStatus({ item }: { item: CatalogCard }) {
  const confirmed = item.applicability?.status === 'confirmed'
  return (
    <p className={`applicability ${confirmed ? 'confirmed' : ''}`}>
      {confirmed && <Icon name="check" />}
      {item.applicability
        ? confirmed
          ? 'Применимость подтверждена'
          : 'Применимость не подтверждена'
        : 'Выберите модель для проверки применимости'}
    </p>
  )
}
