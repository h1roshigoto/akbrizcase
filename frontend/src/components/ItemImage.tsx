import { useState } from 'react'
import type { CatalogCard } from '../api/types'
import { Icon } from './Icon'

export function ItemImage({ item }: { item: CatalogCard }) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  // This exact SKU has a presentation override, independent of its commercial data.
  const onRequest = item.kind === 'product' && item.sku === 'ЯИУШ.301421.011-02'
  const service = item.kind === 'service'
  // Only accept the contracted same-origin image path belonging to this item.
  const url = item.image_url
  const safe =
    url?.startsWith(`/api/v1/catalog/items/${item.id}/image/`) &&
    /^\/api\/v1\/catalog\/items\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\/image\/[0-9a-f]{64}\.(?:jpg|png)$/.test(
      url,
    )
  return (
    <div className={`item-image ${item.kind}`}>
      {onRequest ? (
        <div className="on-request-illustration" role="img" aria-label="На заказ">
          На заказ
        </div>
      ) : url && safe && failedUrl !== url ? (
        <img
          key={url}
          src={url}
          alt={item.name}
          loading="lazy"
          onError={() => setFailedUrl(url)}
        />
      ) : service ? (
        <div className="image-placeholder service-illustration" role="img" aria-label="Сервисная услуга">
          <Icon name="service" />
        </div>
      ) : (
        <div
          className="image-placeholder"
          role="img"
          aria-label="Изображение отсутствует"
        >
          <Icon name="box" />
          <span>Оборудование и детали</span>
          <small>Изображение отсутствует</small>
        </div>
      )}
    </div>
  )
}
