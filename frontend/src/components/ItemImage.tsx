import { useState } from 'react'
import type { CatalogCard } from '../api/types'
import { Icon } from './Icon'

export function ItemImage({ item }: { item: CatalogCard }) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  // Only accept the contracted same-origin image path belonging to this item.
  const url = item.image_url
  const safe =
    url?.startsWith(`/api/v1/catalog/items/${item.id}/image/`) &&
    /^\/api\/v1\/catalog\/items\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\/image\/[0-9a-f]{64}\.(?:jpg|png)$/.test(
      url,
    )
  // Presentation exception requested for this exact existing SKU. This is not
  // availability, stock, price_mode, compatibility or a delivery-date promise.
  const onRequest = item.kind === 'product' && item.sku === 'ЯИУШ.301421.011-02'
  const service = item.kind === 'service'
  return (
    <div className={`item-image ${item.kind}`}>
      {url && safe && failedUrl !== url ? (
        <img
          key={url}
          src={url}
          alt={item.name}
          loading="lazy"
          onError={() => setFailedUrl(url)}
        />
      ) : (
        <div
          className={`image-placeholder ${service ? 'service-illustration' : onRequest ? 'on-request-illustration' : ''}`}
          role="img"
          aria-label={service ? 'Сервисная услуга' : onRequest ? 'На заказ' : 'Изображение отсутствует'}
        >
          {service ? (
            <svg className="service-art" viewBox="0 0 160 96" fill="none" aria-hidden="true">
              <rect x="22" y="12" width="92" height="68" rx="8" stroke="currentColor" strokeWidth="2" />
              <path d="M34 30h36M34 42h24M34 66h35M28 86h80" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              <circle cx="110" cy="55" r="27" fill="var(--surface)" stroke="currentColor" strokeWidth="2" />
              <path d="m114 38-9 8a10 10 0 0 0 12 12l9-8-9 2-5-5 2-9Zm-9 20-13 13 4 4 13-13" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
            </svg>
          ) : <Icon name="box" />}
          <span>
            {service ? 'Сервис и поддержка' : onRequest ? 'На заказ' : 'Оборудование и детали'}
          </span>
          {!service && <small>{onRequest ? 'Поставка по согласованию' : 'Изображение отсутствует'}</small>}
        </div>
      )}
    </div>
  )
}
