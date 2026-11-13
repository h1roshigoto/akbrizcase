import { useState } from 'react'

/** The original calculator explicitly quotes no tariff; the manager confirms it. */
export function DeliveryEstimate({ value, onChange, disabled = false }: { value?: string; onChange?: (city: string) => void; disabled?: boolean }) {
  const [local, setLocal] = useState('')
  const [result, setResult] = useState('')
  const city = value ?? local
  return <div className="calc plain"><div className="rw"><input className="in" aria-label="Город получателя" placeholder="Город получателя" autoComplete="off" maxLength={80} value={city} disabled={disabled} onChange={e=>{setLocal(e.target.value);onChange?.(e.target.value);setResult('')}} /><button className="btn sm calc-go" type="button" disabled={disabled} onClick={()=>setResult(city.trim() ? `Доставка в г. ${city.trim()}: стоимость и срок доставки уточним при подтверждении заявки.` : 'Укажите город получателя.')}>Рассчитать</button></div><p className="tip res" aria-live="polite">{result}</p></div>
}
