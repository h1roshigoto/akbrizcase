import { useCallback, useState } from 'react'
import type { CatalogSource } from '../api/catalog'
import { useResource } from '../state/useResource'
import { formatPrice } from '../state/money'

/** The original service table reads the same published assortment as catalog cards. */
export function ServicePrices({source}: {source: CatalogSource}) {
  const [attempt,setAttempt]=useState(0)
  const load=useCallback(async(signal:AbortSignal)=>{
    const page=await source.getItems({kind:'service',limit:100,offset:0},signal)
    return Promise.all(page.items.filter(item=>item.kind==='service').map(item=>source.getItem(item.id,null,signal)))
  },[source,attempt])
  const result=useResource(load)
  return <section className="prices" id="prices" aria-labelledby="pr-h"><h2 id="pr-h">Стоимость сервисного обслуживания</h2>
    {result.status==='loading' && <p role="status">Загружаем услуги…</p>}
    {result.status==='error' && <p role="alert">Не удалось загрузить стоимость обслуживания. <button type="button" className="link" onClick={()=>setAttempt(n=>n+1)}>Повторить загрузку услуг</button></p>}
    {result.status==='success' && (result.data.length ? <table className="spec-t svc-t"><thead><tr><th scope="col">Услуга</th><th scope="col">Что входит</th><th scope="col">Цена</th></tr></thead><tbody>{result.data.map(item=><tr key={item.id}><td>{item.name}</td><td>{item.description || '—'}</td><td>{item.price_mode==='fixed' && item.price!==null ? `${formatPrice(item.price)} ${item.currency}` : 'Цена по запросу'}<small>{item.unit} · {item.tax_note}</small></td></tr>)}</tbody></table> : <p role="status">Услуги пока не опубликованы.</p>)}
  </section>
}
