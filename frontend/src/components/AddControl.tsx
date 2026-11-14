import type { CatalogCard } from '../api/types'
import type { Selection } from '../state/selection'

/** Original add/stepper presentation; the existing selection owns IDs and revisions. */
export function AddControl({ item, selection, large = false }: { item: CatalogCard; selection: Selection; large?: boolean }) {
  const row = selection.items.find(v => v.item_id === item.id)
  return <span className={`addctl ${large ? 'lg' : ''}`}>
    {row ? <span className={`stepper ${large ? '' : 'sm'}`} role="group" aria-label={`Количество в заявке: ${item.name}`}>
      <button type="button" className="st-b" aria-label={row.qty === 1 ? `Убрать из заявки ${item.sku}` : `Меньше ${item.sku}`} onClick={() => row.qty === 1 ? selection.remove(item.id) : selection.setQty(item.id, row.qty - 1)}>−</button>
      <span className="st-v" aria-live="polite">Добавлено — {row.qty} ед.</span>
      <button type="button" className="st-b" aria-label={`Больше ${item.sku}`} disabled={row.qty >= 999} onClick={() => selection.setQty(item.id, row.qty + 1)}>+</button>
    </span> : <button className={`btn ${large ? '' : 'sm'}`} disabled={selection.items.length >= 100} onClick={() => selection.add(item)}>Добавить в заявку</button>}
  </span>
}
