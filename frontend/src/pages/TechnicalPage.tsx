import { Trainer } from '../design/Trainer'

export function TechnicalPage() {
  return <main className="page-shell technical-page"><div className="page-heading"><div><p className="eyebrow">ОБОРУДОВАНИЕ И ОБУЧЕНИЕ</p><h1>Техинформация</h1><p>Визуальный тренажёр и опубликованные технические материалы.</p></div></div>
    <Trainer />
    <section className="technical-docs"><h2>Технические характеристики</h2><p>Для фактических параметров используйте опубликованные документы выбранной модели. Размеры и поведение учебного макета не подтверждают характеристики оборудования.</p><a className="btn ghost" href="#docs">Открыть технические документы</a></section>
  </main>
}
