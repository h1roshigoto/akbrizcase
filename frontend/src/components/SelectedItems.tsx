import { useCallback, useState } from 'react'
import type { CatalogSource } from '../api/catalog'
import type { CatalogItem, UUID } from '../api/types'
import { type Selection, validQty } from '../state/selection'
import { useResource } from '../state/useResource'
import { formatKopecks, formatPrice, toKopecks } from '../state/money'
import { Icon } from './Icon'
import { ItemImage } from './ItemImage'

function QuantityInput({
  qty,
  label,
  onChange,
}: {
  qty: number
  label: string
  onChange: (qty: number) => void
}) {
  const [draft, setDraft] = useState(String(qty))
  const valid = /^\d+$/.test(draft) && validQty(Number(draft))
  return (
    <div className="quantity-control">
      <button
        aria-label={`Уменьшить количество ${label}`}
        disabled={qty <= 1}
        onClick={() => {
          setDraft(String(qty - 1))
          onChange(qty - 1)
        }}
      >
        −
      </button>
      <input
        aria-label={`Количество ${label}`}
        aria-invalid={!valid}
        inputMode="numeric"
        type="text"
        value={draft}
        maxLength={4}
        onChange={(event) => {
          const value = event.target.value
          setDraft(value)
          if (/^\d+$/.test(value) && validQty(Number(value)))
            onChange(Number(value))
        }}
        onBlur={() => setDraft(String(qty))}
      />
      <button
        aria-label={`Увеличить количество ${label}`}
        disabled={qty >= 999}
        onClick={() => {
          setDraft(String(qty + 1))
          onChange(qty + 1)
        }}
      >
        +
      </button>
      {!valid && (
        <small className="quantity-error" role="status">
          Введите целое число от 1 до 999
        </small>
      )}
    </div>
  )
}

export function SelectedItems({
  source,
  selection,
  onOpen,
  onRequest,
}: {
  source: CatalogSource
  selection: Selection
  onOpen: (id: UUID) => void
  onRequest?: () => void
}) {
  const [attempt, setAttempt] = useState(0)
  const idsKey = selection.items.map((row) => row.item_id).join(',')
  const load = useCallback(
    async (signal: AbortSignal) => {
      const ids = idsKey ? (idsKey.split(',') as UUID[]) : []
      const results = await Promise.allSettled(
        ids.map((id) => source.getItem(id, null, signal)),
      )
      return new Map<UUID, CatalogItem | null>(
        ids.map((id, index) => {
          const result = results[index]
          return [id, result?.status === 'fulfilled' ? result.value : null]
        }),
      )
    },
    [source, idsKey, attempt],
  )
  const details = useResource(load)
  let knownSubtotal = 0n
  let knownCount = 0
  let unknownCount = 0
  let unavailable = false
  for (const row of selection.items) {
    const item = details.data?.get(row.item_id)
    if (item?.price_mode === 'fixed' && item.price !== null) {
      knownSubtotal += toKopecks(item.price) * BigInt(row.qty)
      knownCount++
    } else {
      unknownCount++
      if (!item && details.status === 'success') unavailable = true
    }
  }
  return (
    <aside
      className="selection-panel"
      id="selection"
      aria-label="Выбранные позиции"
    >
      <div className="selection-title">
        <Icon name="bag" />
        <h2>Выбранные позиции</h2>
        <span className="count-badge">{selection.items.length}</span>
      </div>
      <p className="selection-intro">Товары и услуги в одном списке</p>
      {selection.storageFailed && (
        <p role="alert" className="inline-error">
          Браузер не разрешил сохранение. После обновления список может
          потеряться.
        </p>
      )}
      {selection.items.length === 0 ? (
        <div className="selection-empty">
          <span>
            <Icon name="plus" />
          </span>
          <h3>Начните с каталога</h3>
          <p>
            Добавьте нужные позиции.
            <br />
            Количество можно изменить здесь.
          </p>
        </div>
      ) : (
        <>
          <ul className="selected-list">
            {selection.items.map((row) => {
              const item = details.data?.get(row.item_id)
              const label = item?.sku ?? row.item_id
              return (
                <li className="selected-row" key={row.item_id}>
                  <div className="selected-row-title">
                    {item && <div className="selected-image"><ItemImage item={item} /></div>}
                    <div>
                      <p className="sku">{label}</p>
                      {item ? (
                        <button
                          className="selected-name"
                          onClick={() => onOpen(row.item_id)}
                        >
                          {item.name}
                        </button>
                      ) : (
                        <p>
                          {details.status === 'loading'
                            ? 'Загружаем позицию…'
                            : 'Позиция недоступна'}
                        </p>
                      )}
                    </div>
                    <button
                      className="remove-button"
                      aria-label={`Удалить ${label}`}
                      onClick={() => selection.remove(row.item_id)}
                    >
                      <Icon name="close" />
                    </button>
                  </div>
                  {item && (
                    <>
                      <p className="selected-price">
                        {item.price_mode === 'fixed' && item.price !== null
                          ? `${formatPrice(item.price)} ${item.currency} / ${item.unit}`
                          : `Цена по запросу · ${item.currency} / ${item.unit}`}
                      </p>
                      {item.revision !== row.revision && (
                        <p className="revision-note">
                          Позиция обновилась. Показаны текущие данные;
                          потребуется повторная проверка перед заявкой.
                        </p>
                      )}
                    </>
                  )}
                  <QuantityInput
                    qty={row.qty}
                    label={label}
                    onChange={(qty) => selection.setQty(row.item_id, qty)}
                  />
                </li>
              )
            })}
          </ul>
          {unavailable && (
            <div className="inline-error" role="alert">
              Часть позиций недоступна. Их стоимость не включена в сумму.{' '}
              <button
                className="text-button"
                onClick={() => setAttempt((value) => value + 1)}
              >
                Повторить загрузку
              </button>
            </div>
          )}
          <div className="subtotal" aria-live="polite">
            <p>
              {unknownCount > 0 ? 'Известная часть суммы' : 'Стоимость позиций'}
            </p>
            <strong>
              {details.status === 'loading' ? (
                'Считаем…'
              ) : knownCount ? (
                <>
                  {formatKopecks(knownSubtotal)} <span>RUB</span>
                </>
              ) : (
                'Пока не определена'
              )}
            </strong>
            {unknownCount > 0 && details.status === 'success' && (
              <p className="subtotal-note">
                Позиций без известной цены: {unknownCount}. Полная сумма пока не
                определена.
              </p>
            )}
          </div>
          <p className="selection-footnote">
            Цены предварительные. Доставка, если требуется, согласуется
            отдельно.
          </p>
        </>
      )}
      {selection.items.length >= 100 && (
        <p role="status" className="selection-footnote">
          Достигнут лимит: 100 позиций.
        </p>
      )}
      {onRequest && (
        <button className="primary-button request-action" onClick={onRequest}>
          Оформить заявку
        </button>
      )}
      <div className="next-stage">
        <span className="stage-dot" />
        <p>Список сохраняется в этой вкладке.</p>
      </div>
    </aside>
  )
}
