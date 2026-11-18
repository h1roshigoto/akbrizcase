import { useRef, useState, type FormEvent } from 'react'
import type { FormValues } from '../state/requestForm'
import { formatReferencePhone, validateReferenceForm } from '../state/referenceForm'

export type Institution = { type: string; address: string }

export function ReferenceRequestForm({ values, onField, checked, onCheck, onReview, disabled }: {
  values: FormValues
  onField: <K extends keyof FormValues>(field: K, value: FormValues[K]) => void
  disabled: boolean
  checked: boolean
  onCheck: (checked: boolean) => void
  onReview: (institution: Institution) => void
}) {
  const [type, setType] = useState('Образовательное учреждение')
  const other = useRef<HTMLInputElement>(null)
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!validateReferenceForm(event.currentTarget)) return
    const data = new FormData(event.currentTarget)
    onReview({
      type: type === 'other' ? String(data.get('type_other') ?? '').trim() : type,
      address: String(data.get('addr') ?? '').trim(),
    })
  }

  return (
    <form className="panel form r" id="reqform" noValidate onSubmit={submit}>
      <h3>Данные учреждения</h3>
      <div className="f2">
        <label className="f">Название учреждения *<input className="in" name="org" placeholder="Например, школа или колледж" autoComplete="organization" maxLength={200} data-req="" disabled={disabled} value={values.organization_name} onChange={(e) => onField('organization_name', e.target.value)} /></label>
        <fieldset className="f choice-f">
          <legend>Тип учреждения</legend>
          <div className="choice" role="radiogroup">
            <label><input type="radio" name="type" value="Образовательное учреждение" disabled={disabled} checked={type === 'Образовательное учреждение'} onChange={(e) => setType(e.target.value)} /><span>Образовательное</span></label>
            <label><input type="radio" name="type" value="Государственное учреждение" disabled={disabled} checked={type === 'Государственное учреждение'} onChange={(e) => setType(e.target.value)} /><span>Государственное</span></label>
            <label><input type="radio" name="type" value="other" disabled={disabled} checked={type === 'other'} onChange={(e) => {
              setType(e.target.value)
              requestAnimationFrame(() => other.current?.focus())
            }} /><span>Другое</span></label>
          </div>
          <input ref={other} className="in choice-other" disabled={disabled} name="type_other" placeholder="Укажите тип учреждения" aria-label="Другой тип учреждения" maxLength={100} hidden={type !== 'other'} />
        </fieldset>
      </div>
      <div className="f2">
        <label className="f">Контактное лицо *<input className="in" name="fio" placeholder="Фамилия, имя, отчество" autoComplete="name" maxLength={150} data-req="" disabled={disabled} value={values.name} onChange={(e) => onField('name', e.target.value)} /></label>
        <label className="f">Телефон *<input className="in" type="tel" name="phone" placeholder="+7 (___) ___-__-__" autoComplete="tel" inputMode="tel" maxLength={30} data-req="" disabled={disabled} value={values.phone} onChange={(e) => onField('phone', formatReferencePhone(e.target.value))} /></label>
      </div>
      <div className="f2">
        <label className="f">E-mail *<input className="in" type="email" name="email" placeholder="name@example.ru" autoComplete="email" maxLength={254} data-req="" disabled={disabled} value={values.email} onChange={(e) => onField('email', e.target.value)} /></label>
        <label className="f">Адрес доставки *<input className="in" name="addr" placeholder="Город, улица, дом" autoComplete="street-address" maxLength={300} data-req="" disabled={disabled} /></label>
      </div>
      <label className="f">Комментарий<textarea className="in" name="comment" placeholder="Что случилось с прибором, какая деталь нужна" maxLength={1000} disabled={disabled} value={values.comment} onChange={(e) => onField('comment', e.target.value)} /></label>
      <label className="agree"><input type="checkbox" data-req="" disabled={disabled} checked={checked} onChange={(e) => onCheck(e.target.checked)} /><span>Согласен на обработку <a href="/privacy-policy" className="stub" target="_blank" rel="noopener noreferrer">персональных данных</a>.</span></label>
      <div><button className="btn" type="submit" disabled={disabled}>Отправить заявку</button></div>
    </form>
  )
}
