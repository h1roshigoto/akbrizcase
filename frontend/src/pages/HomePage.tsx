import { ModelViewer } from '../design/ModelViewer'
import { HomeContacts, type QuickSupportDraft } from '../components/HomeContacts'

export function HomePage({ onSupport, onAssistant }: {
  onSupport: (draft: QuickSupportDraft) => void
  onAssistant: () => void
}) {
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
    <HomeContacts onSupport={onSupport} onAssistant={onAssistant} />
  </main>
}
