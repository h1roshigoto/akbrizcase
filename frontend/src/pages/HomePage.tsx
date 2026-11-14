import { ModelViewer } from '../design/ModelViewer'

export function HomePage({ onSupport }: { onSupport?: () => void }) {
  return <main id="home">
    <section className="hero"><div className="wrap">
      <div><h1>Стационарный арочный металлодетектор «МТД-КА»</h1>
        <p>Предназначен для обнаружения металлических предметов, запрещенных в местах массового скопления людей, используется для обеспечения <b>общественной безопасности</b>.</p>
        <p>Помощник подскажет, что делать при неисправности металлодетектора, поможет заказать запчасть и покажет документы, на которые опираются ответы.</p>
        <div className="cta"><a className="btn" href="#chat">Найти неисправность</a><a className="btn ghost" href="#catalog">Заказать запчасть</a></div>
      </div><ModelViewer />
    </div></section>
    <section className="sec"><div className="wrap"><div className="head"><h2>Чем поможет помощник</h2></div>
      <div className="grid3">
        <a className="card" href="#chat"><h3>Найти неисправность</h3><p>Опишите, что происходит с прибором, или выберите, что горит на панели. Помощник проведёт по опубликованным шагам и покажет документ-источник.</p><span className="more">Открыть помощника</span></a>
        <a className="card" href="#catalog"><h3>Заказать запчасть</h3><p>Подберите деталь по названию, артикулу или по результату диагностики и отправьте заявку. Доставку согласует менеджер.</p><span className="more">Перейти в каталог</span></a>
        <a className="card" href="#docs"><h3>Нормативные документы</h3><p>Нормативные акты, регламенты обслуживания и инструкции. Помощник отвечает только со ссылкой на опубликованный документ.</p><span className="more">Открыть документы</span></a>
      </div><div className="principles"><p>Отвечает только по опубликованным документам</p><p>Каждый ответ со ссылкой на документ и раздел</p><p>Если ответа нет, передаёт вопрос специалисту</p></div>
    </div></section>
    <section className="sec contacts" id="contacts"><div className="wrap"><h2>Наши контакты</h2><p className="lead">Готовы оказать поддержку и ответить на любые вопросы заказчика.</p>
      <div className="cgrid"><div className="cinfo"><div className="person"><p>Техническая поддержка</p><small>Обращение по эксплуатации оборудования</small></div><div className="person"><p>Запасные части</p><small>Оформление и отгрузка</small></div></div>
      <div className="form"><p>Опишите задачу в форме обращения.</p><a className="btn wide" href="#support" onClick={e => {if(onSupport){e.preventDefault();onSupport()}}}>Обратиться к специалисту</a></div></div>
    </div></section>
    <section className="sec service"><div className="wrap"><h2>Гарантийное и постгарантийное сервисное обслуживание</h2><p className="say">Существует <b>сообщество</b> поддержки всех пользователей в понятном и доступном, максимально удобном для всех сторон, информационном пространстве.</p><div className="row"><div className="big">ООО «АК БРИЗ»</div><p className="desc">Состав работ и условия обслуживания подтверждает специалист.</p></div></div></section>
  </main>
}
