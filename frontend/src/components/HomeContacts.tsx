import type { FormEvent } from 'react'
import { followPortalLink } from './ReferenceFooter'
import { maskPhone, validateReferenceForm } from '../state/referenceForm'

export type QuickSupportDraft = {
  name: string
  phone: string
  comment: string
  email?: string
  organization_name?: string
}

export function HomeContacts({ onSupport, onAssistant }: {
  onSupport: (draft: QuickSupportDraft) => void
  onAssistant: () => void
}) {
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!validateReferenceForm(event.currentTarget)) return
    const data = new FormData(event.currentTarget)
    onSupport({
      name: String(data.get('name') ?? ''),
      phone: String(data.get('phone') ?? ''),
      comment: String(data.get('comment') ?? ''),
    })
  }
  return <>
<section className="sec contacts" id="contacts">
  <div className="wrap">
    <h2>Наши контакты</h2>
    <p className="lead">Готовы оказать поддержку и ответить на любые вопросы заказчика.</p>
    <div className="cgrid">
      <div className="cinfo">
<div className="person"><a className="tel" href="tel:+79094265396">+7 (909) 426-53-96</a><p>Александр, эксперт по технической эксплуатации</p></div>
<div className="person"><a className="tel" href="tel:+79185116899">+7 (918) 511-68-99</a><p>Дарья, эксперт по оформлению и отгрузке запасных частей</p></div>
<a className="tel" href="mailto:1968@bk.ru">1968@bk.ru</a>
        <address>344092, Ростовская область, город Ростов-на-Дону, пр-кт Космонавтов, дом 2/3, 2 этаж, кабинеты 3, 4, 5, 6</address>
      </div>
      <form className="form" id="quick" noValidate onSubmit={submit}><label className="f"><span className="sr">Имя</span><input className="in" name="name" placeholder="Имя" autoComplete="name" maxLength={100} data-req="" /></label><label className="f"><span className="sr">Номер телефона</span><input className="in" type="tel" name="phone" placeholder="Номер телефона" autoComplete="tel" inputMode="tel" maxLength={30} onInput={maskPhone} data-req="" /></label><label className="f"><span className="sr">Комментарий</span><textarea className="in" name="comment" placeholder="Комментарий" rows={3} maxLength={1000}></textarea></label><label className="agree"><input type="checkbox" data-req="" /><span>Согласен на обработку <a href="/privacy-policy" className="stub" target="_blank" rel="noopener noreferrer">персональных данных</a>.</span></label>
        <div><button className="btn wide" type="submit">Отправить</button></div>
      </form>
    </div>
  </div>
</section>
<section className="sec service">
  <div className="wrap">
    <h2>Гарантийное и постгарантийное сервисное обслуживание</h2>
    <p className="say">Существует <b>сообщество</b> поддержки всех пользователей в понятном и доступном, максимально удобном для всех сторон, информационном пространстве.</p>
    <div className="row">
      <div className="big">ООО «АК БРИЗ»</div>
      <p className="desc">Эксклюзивный дистрибьютор запасных частей на территории РФ. Агрегатор сервисных инцидентов на территории РФ.</p>
    </div>
    <div className="req">
      <p>Email: <a href="mailto:1968@bk.ru">1968@bk.ru</a></p>
      <p>Телефон: <a href="tel:+79094265396">+7(909)426-53-96</a></p>
      <p>Директор: Никоненко Александр Юрьевич</p>
      <p>Юридический адрес: 455000 г. Магнитогорск ул. Николая Шишка 28</p>
    </div>
  </div>
</section>
<div className="fab-rail"><a className="fab" href="pomoshchnik.html" onClick={(event) => followPortalLink(event, () => onAssistant())} aria-label="Открыть помощника"><span className="lbl">Помощник</span><span className="ico"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2a1 1 0 0 1 1 1v1.1A7 7 0 0 1 19 11v1h1a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1h-1v1a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3v-1H4a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1h1v-1a7 7 0 0 1 6-6.9V3a1 1 0 0 1 1-1Zm-3 9a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3Zm6 0a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3Z"/></svg></span></a></div>
  </>
}
