import { useState } from 'react'
import type { CatalogCard } from '../api/types'
import { Icon } from './Icon'

export function ItemImage({ item }: { item: CatalogCard }) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  // Only accept the contracted same-origin image path belonging to this item.
  const url = item.image_url
  const safe =
    url?.startsWith(`/api/v1/catalog/items/${item.id}/image/`) &&
    /^\/api\/v1\/catalog\/items\/[0-9a-f-]{36}\/image\/[A-Za-z0-9_-]+(?:\.(?:jpg|jpeg|png))?$/.test(
      url,
    )
  return (
    <div className={`item-image ${item.kind}`}>
      {url && safe && failedUrl !== url ? (
        <img
          src={url}
          alt={item.name}
          loading="lazy"
          onError={() => setFailedUrl(url)}
        />
      ) : (
        <div
          className="image-placeholder"
          role="img"
          aria-label="Изображение отсутствует"
        >
          <Icon name={item.kind === 'service' ? 'service' : 'box'} />
          <span>
            {item.kind === 'service'
              ? 'Услуги и сервис'
              : 'Оборудование и детали'}
          </span>
          <small>Изображение отсутствует</small>
        </div>
      )}
    </div>
  )
}
