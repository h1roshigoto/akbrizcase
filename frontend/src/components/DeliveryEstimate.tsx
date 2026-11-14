import { useId, useRef, useState } from 'react'
import { cities } from '../design/cities'

/** Original offline city suggestions and explicit manager-confirmed quote, without invented tariffs. */
export function DeliveryEstimate({ value, onChange, disabled = false }: { value?: string; onChange?: (city: string) => void; disabled?: boolean }) {
  const [local, setLocal] = useState('')
  const [result, setResult] = useState('')
  const [open, setOpen] = useState(false)
  const [current, setCurrent] = useState(-1)
  const listId = useId()
  const list = useRef<HTMLUListElement>(null)
  const city = value ?? local
  const normalized = city.trim().toLowerCase().replace(/ё/g,'е')
  const matches = normalized ? [...cities.filter(c => c.n.toLowerCase().replace(/ё/g,'е').startsWith(normalized)), ...cities.filter(c => {const n=c.n.toLowerCase().replace(/ё/g,'е');return !n.startsWith(normalized) && n.includes(normalized)})].slice(0,7) : []
  const shown = open && matches.length > 0 && !disabled
  const change = (next: string) => {setLocal(next);onChange?.(next);setResult('');setCurrent(-1)}
  const pick = (index: number) => {if(matches[index]) change(matches[index].n);setOpen(false)}
  return <div className="calc plain"><div className="rw"><input className="in" role="combobox" aria-label="Город получателя" aria-autocomplete="list" aria-expanded={shown} aria-controls={listId} aria-activedescendant={shown && current >= 0 ? `${listId}-${current}` : undefined} placeholder="Город получателя" autoComplete="off" maxLength={80} value={city} disabled={disabled}
    onChange={e=>{change(e.target.value);setOpen(true)}} onFocus={()=>{if(city)setOpen(true)}} onBlur={()=>setOpen(false)}
    onKeyDown={e=>{
      if(e.key==='Escape'){setOpen(false);setCurrent(-1);return}
      if(e.key==='Tab' && shown && matches.length===1){pick(0);return}
      if(e.key==='Enter' && shown && current>=0){e.preventDefault();pick(current);return}
      if((e.key==='ArrowDown'||e.key==='ArrowUp') && matches.length){e.preventDefault();setOpen(true);const next=current<0 ? (e.key==='ArrowDown'?0:matches.length-1) : (current+(e.key==='ArrowDown'?1:matches.length-1)+matches.length)%matches.length;setCurrent(next);list.current?.children[next]?.scrollIntoView?.({block:'nearest'})}
    }} /><button className="btn sm calc-go" type="button" disabled={disabled} onClick={()=>{setOpen(false);setResult(city.trim() ? `Доставка в г. ${city.trim()}: стоимость и срок доставки уточним при подтверждении заявки.` : 'Укажите город получателя.')}}>Рассчитать</button>
    <ul className="suggest" id={listId} ref={list} role="listbox" hidden={!shown}>{matches.map((c,i)=><li key={c.n+c.r} role="option" id={`${listId}-${i}`} aria-selected={i===current} onMouseDown={e=>{e.preventDefault();pick(i)}}><span>{c.n}</span><small>{c.r}</small></li>)}</ul>
  </div><p className="tip res" aria-live="polite">{result}</p></div>
}
