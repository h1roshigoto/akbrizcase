import { useCallback, useState, type FormEvent } from 'react'
import { documentURL, httpKnowledge } from '../api/httpKnowledge'
import type { DocumentPage, DocumentQuery, KnowledgePage } from '../api/types'
import { SourceLink } from '../chat/SourceLink'
import { useResource } from '../state/useResource'

export function DocumentsPage() {
  const [text, setText] = useState('')
  const [q, setQ] = useState('')
  const [kind, setKind] = useState<NonNullable<DocumentQuery['kind']> | null>(null)
  const [offset, setOffset] = useState(0)
  const [attempt, setAttempt] = useState(0)
  const load = useCallback((signal: AbortSignal): Promise<{ type: 'search'; data: KnowledgePage } | { type: 'documents'; data: DocumentPage }> => q
    ? httpKnowledge.search(q, offset, signal).then(data => ({ type: 'search' as const, data }))
    : httpKnowledge.documents({ kind, limit: 20, offset }, signal).then(data => ({ type: 'documents' as const, data })), [q, kind, offset, attempt])
  const result = useResource(load)
  const submit = (e: FormEvent) => { e.preventDefault(); setQ(text.trim()); setOffset(0) }
  return <main className="wrap restore-documents">
    <div className="wrap pg"><h1>Нормативная база</h1><p className="lead">Нормативные акты и документы завода, на которые опираются ответы помощника.</p></div>
    <form className="bar" onSubmit={submit}><label className="document-search"><span className="visually-hidden">Поиск по документам</span><input className="in" id="dsearch" type="search" maxLength={1000} value={text} onChange={e => setText(e.target.value)} placeholder="Поиск по опубликованным фрагментам" /></label><button className="btn sm">Найти</button></form>
    <label className="document-kind">Вид документа<select value={kind ?? ''} disabled={!!q} onChange={e => { setKind(e.target.value ? e.target.value as NonNullable<DocumentQuery['kind']> : null); setOffset(0) }}><option value="">Все документы</option><option value="regulation">Нормативные акты</option><option value="manual">Руководства</option><option value="article">Статьи</option><option value="other">Другие материалы</option></select></label>
    {q && <p className="filter-note">Поиск по содержимому: {q}. <button className="text-button" onClick={() => { setQ(''); setText(''); setOffset(0) }}>Показать список документов</button></p>}
    <section aria-label="Опубликованные материалы" aria-busy={result.status === 'loading'}>
      {result.status === 'loading' && <p role="status" className="status-panel">Загружаем материалы…</p>}
      {result.status === 'error' && <div role="alert" className="status-panel"><p>Не удалось загрузить материалы.</p><button className="secondary-button" onClick={() => setAttempt(n => n + 1)}>Повторить загрузку документов</button></div>}
      {result.status === 'success' && <>
        {!result.data.data.items.length && <p className="status-panel" role="status">Опубликованные материалы не найдены.</p>}
        {result.data.type === 'documents'
          ? result.data.data.items.map(d => <article className="docrow" key={d.id}><div><h2>{d.title}</h2><p className="tip">{d.code} · Редакция {d.version}</p></div><a className="btn ghost sm" href={documentURL(d)!} target="_blank" rel="noopener noreferrer">Открыть документ</a></article>)
          : result.data.data.items.map(hit => <article className="docrow knowledge-hit" key={hit.source.section_id}><h2>{hit.source.title}</h2><p>{hit.excerpt}</p><SourceLink source={hit.source} /></article>)}
        <div className="catalog-pagination"><p>Найдено: {result.data.data.total}</p><div><button className="secondary-button" disabled={offset === 0} onClick={() => setOffset(n => Math.max(0, n - result.data.data.limit))}>Предыдущие документы</button><button className="secondary-button" disabled={offset + result.data.data.limit >= result.data.data.total || offset + result.data.data.limit > 10000} onClick={() => setOffset(n => n + result.data.data.limit)}>Следующие документы</button></div></div>
      </>}
    </section><p className="tip docs-note">Доступность каждой версии проверяет сервер. Отозванные и закрытые материалы не публикуются. Сведения не заменяют консультацию специалиста.</p>
  </main>
}
