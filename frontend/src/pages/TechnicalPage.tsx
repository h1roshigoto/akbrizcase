import { Trainer } from '../design/Trainer'

export function TechnicalPage() {
  return <main><div className="wrap"><div className="wrap pg"><h1>Техинформация</h1><p className="lead">Как читать панель индикации и технические характеристики МТД-КА.</p></div>
    <Trainer />
    <section className="technical-docs"><h2>Технические характеристики</h2><p>Для фактических параметров используйте опубликованные документы выбранной модели. Размеры и поведение учебного макета не подтверждают характеристики оборудования.</p><a className="btn ghost" href="#docs">Открыть технические документы</a></section>
  </div></main>
}
