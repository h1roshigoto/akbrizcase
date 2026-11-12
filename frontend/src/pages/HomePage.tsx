import { ModelViewer } from '../design/ModelViewer'

export function HomePage() {
  return <main id="home">
    <section className="hero"><div className="wrap">
      <div><p className="eyebrow">ОБОРУДОВАНИЕ · ЗАПАСНЫЕ ЧАСТИ · СЕРВИС</p><h1>Стационарный арочный металлодетектор «МТД-КА»</h1>
        <p>Сервисный портал ООО «АК Бриз». Подбор товаров и услуг, помощь по опубликованным документам и обращение к специалисту — в одном месте.</p>
        <p>Выберите оборудование и сценарий. Технические ответы доступны только при наличии опубликованного источника.</p>
        <div className="cta"><a className="btn" href="#chat">Найти неисправность</a><a className="btn ghost" href="#catalog">Заказать запчасть</a></div>
      </div><ModelViewer />
    </div></section>
    <section className="sec"><div className="wrap"><div className="head"><h2>Чем поможет помощник</h2></div>
      <div className="grid3">
        <a className="card" href="#wizard"><h3>Найти неисправность</h3><p>Выберите модель и пройдите опубликованный диагностический сценарий. Каждый шаг приходит с сервера.</p><span className="more">Подбор по шагам →</span></a>
        <a className="card" href="#catalog"><h3>Заказать запчасть</h3><p>Найдите товар или услугу по названию и артикулу. Соберите заявку, которую проверит менеджер.</p><span className="more">Открыть каталог →</span></a>
        <a className="card" href="#docs"><h3>Нормативные документы</h3><p>Опубликованные материалы, источники и цитаты. Доступность документов проверяется при каждом открытии.</p><span className="more">Перейти к документам →</span></a>
      </div><div className="principles"><p>Нет подтверждённого источника — нет технической рекомендации</p><p>Доставка и окончательная стоимость согласуются менеджером</p><p>Если ответа нет, можно передать вопрос специалисту</p></div>
    </div></section>
    <section className="sec contacts" id="contacts"><div className="wrap"><h2>Связаться с нами</h2><p className="lead">Опишите задачу — специалист поможет с дальнейшими действиями.</p>
      <div className="contact-callout"><p>Для обращения используйте защищённую форму. В демонстрации вводите только вымышленные контакты.</p><a className="btn" href="#support">Открыть форму поддержки</a></div>
    </div></section>
    <section className="sec service"><div className="wrap"><h2>Сервисное обслуживание</h2><p className="say">Товары, услуги и сопровождение заявки — в едином сервисном портале.</p><div className="row"><div className="big">ООО «АК БРИЗ»</div><p className="desc">Состав работ, применимость деталей и условия обслуживания подтверждает специалист.</p></div></div></section>
  </main>
}
