import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import type { CatalogSource } from '../api/catalog'
import type { CatalogQuery, UUID } from '../api/types'
import { useResource } from '../state/useResource'
import { useSelection } from '../state/selection'
import { CatalogCard } from '../components/CatalogCard'
import { SelectedItems } from '../components/SelectedItems'
import { StatusPanel } from '../components/StatusPanel'
import { ItemDetail } from './ItemDetail'
import { RequestPage, type SupportContext } from './RequestPage'
import { httpOrders, type OrdersSource } from '../api/httpOrders'
import { Submission } from '../state/submission'
import { httpChat, type ChatSource } from '../api/httpChat'
import { ChatController } from '../chat/controller'
import { ChatPage } from '../chat/ChatPage'
import { SiteHeader, SiteFooter, type PortalView, type PortalHash } from '../components/SiteHeader'
import { SelectionDrawer } from '../components/SelectionDrawer'
import { HomePage } from './HomePage'
import { DocumentsPage } from './DocumentsPage'
import { TechnicalPage } from './TechnicalPage'

const viewFromHash = (defaultView: PortalView): PortalView => {
  const key = window.location.hash.slice(1)
  if (/^part-[0-9a-f-]{36}$/i.test(key)) return 'catalog'
  if (/^wizard(?:=\w+)?$/.test(key)) return 'wizard'
  return ['home', 'catalog', 'chat', 'wizard', 'request', 'support', 'docs', 'tech'].includes(key)
    ? key as PortalView
    : key === 'selection' ? 'catalog' : defaultView
}

const itemFromHash = (): UUID | null => /^#part-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/i.exec(window.location.hash)?.[1]?.toLowerCase() as UUID ?? null

export function CatalogPage({
  source,
  orders = httpOrders,
  chat = httpChat,
  defaultView = 'catalog',
}: {
  source: CatalogSource
  orders?: OrdersSource
  chat?: ChatSource
  defaultView?: PortalView
}) {
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [chatController] = useState(() => new ChatController(chat))
  const [view, setView] = useState(() => viewFromHash(defaultView))
  const [requestVisited, setRequestVisited] = useState(
    () => viewFromHash(defaultView) === 'request',
  )
  useEffect(() => {
    const titles = {home:'Сервисный портал',catalog:'Запчасти',chat:'Помощник',wizard:'Помощник',request:'Заявка',support:'Обращение к специалисту',docs:'Нормативная база',tech:'Техинформация'}
    document.title = `${titles[view]} · МТД-КА`
  }, [view])
  const chatOpen = view === 'chat' || view === 'wizard'
  const requestOpen = view === 'request'
  const supportOpen = view === 'support'
  const [independentRequested, setIndependentRequested] = useState(false)
  const [requestGeneration, setRequestGeneration] = useState(0)
  const [supportGeneration, setSupportGeneration] = useState(0)
  const [supportContext, setSupportContext] = useState<SupportContext | null>(
    () => (viewFromHash(defaultView) === 'support' ? { draft: null, owner: null } : null),
  )
  const syncView = useCallback(() => {
    const next = viewFromHash(defaultView)
    setView(next)
    setActiveId(itemFromHash())
    if (next !== 'support') setIndependentRequested(false)
    if (next === 'request') setRequestVisited(true)
    if (next === 'support')
      setSupportContext((current) => current ?? { draft: null, owner: null })
  }, [defaultView])
  useEffect(() => {
    window.addEventListener('hashchange', syncView)
    window.addEventListener('popstate', syncView)
    return () => {
      window.removeEventListener('hashchange', syncView)
      window.removeEventListener('popstate', syncView)
    }
  }, [syncView])
  const navigate = (
    hash: PortalHash,
  ) => {
    setDrawerOpen(false)
    window.location.hash = hash
    syncView()
  }
  const [submission] = useState(() => new Submission(orders))
  const startNew = (kind: 'commercial' | 'support') => {
    const completedSupport = submission.succeededFor('support')
    const completedCommercial = submission.succeededFor('commercial')
    if (!submission.beginNew()) return
    if (kind === 'support' || completedSupport) {
      setSupportContext({ draft: null, owner: null })
      setSupportGeneration((n) => n + 1)
      setIndependentRequested(false)
    }
    if (kind === 'commercial' || completedCommercial)
      setRequestGeneration((n) => n + 1)
  }
  const openRequest = () => navigate('#request')
  const openSiteSupport = () => {
    if (supportContext?.owner) setIndependentRequested(true)
    navigate('#support')
  }
  const selection = useSelection()
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState<CatalogQuery>({ limit: 20, offset: 0 })
  useEffect(() => {
    const timer = setTimeout(() => setQuery(v => (v.q ?? '') === search.trim() ? v : ({ ...v, q: search.trim(), offset: 0 })), 120)
    return () => clearTimeout(timer)
  }, [search])
  const [activeId, setActiveId] = useState<UUID | null>(itemFromHash)
  const [attempt, setAttempt] = useState(0)
  const [modelAttempt, setModelAttempt] = useState(0)
  const catalogAnchor = useRef<HTMLDivElement>(null)
  const loadModels = useCallback(
    (signal: AbortSignal) => source.getModels(signal),
    [source, modelAttempt],
  )
  const models = useResource(loadModels)
  const loadItems = useCallback(
    (signal: AbortSignal) => source.getItems(query, signal),
    [source, query, attempt],
  )
  const catalog = useResource(loadItems)
  const reset = () => {
    setSearch('')
    setQuery({ limit: 20, offset: 0 })
  }
  const openItem = (id: UUID) => {
    window.location.hash = `part-${id}`
    setActiveId(id)
    window.scrollTo({ top: 0, behavior: 'instant' })
    catalogAnchor.current?.focus({ preventScroll: true })
  }
  const submitSearch = (event: FormEvent) => {
    event.preventDefault()
    setActiveId(null)
    setQuery((current) => ({ ...current, q: search.trim(), offset: 0 }))
  }

  return (
    <>
      <a
        className="skip-link"
        href="#catalog"
        onClick={() => navigate('#catalog')}
      >
        Перейти к каталогу
      </a>
      <SiteHeader view={view} count={selection.items.length} navigate={navigate} onSupport={openSiteSupport} onCart={() => setDrawerOpen(true)} />
      {view === 'home' && <HomePage onSupport={openSiteSupport} />}
      {view === 'docs' && <DocumentsPage />}
      {view === 'tech' && <TechnicalPage />}
      {source.mode === 'fixtures' && (
        <div className="demo-banner">
          <span className="demo-tag">ДЕМО</span>
          <p>
            Интерфейс на синтетических fixtures. Реальный каталог и сохранение
            заявок в БД ещё не подключены.
          </p>
        </div>
      )}
      {chatOpen && (
        <ChatPage
          controller={chatController}
          presentation={view === 'wizard' ? 'steps' : 'chat'}
          onPresentation={(next) => navigate(next === 'steps' ? '#wizard' : '#chat')}
          catalog={source}
          selection={selection}
          onBack={() => {
            navigate('#catalog')
          }}
          onItem={(id) => {
            navigate('#catalog')
            openItem(id)
          }}
          onRequest={openRequest}
          onSupport={(draft, owner) => {
            const kind = submission.snapshot().kind
            if (kind !== 'editing') {
              navigate('#support')
              return
            }
            setIndependentRequested(false)
            setSupportContext({ draft, owner })
            navigate('#support')
          }}
        />
      )}
      <main hidden={view !== 'catalog'}>
        <div className="wrap restore-catalog">
          <div className="wrap pg"><h1>Запчасти</h1><p className="lead">Подберите деталь по названию, обозначению или модели. Наличие и срок поставки подтвердим после заявки.</p></div>
          {activeId ? <ItemDetail key={activeId} source={source} id={activeId} modelId={query.model_id ?? null} selection={selection} onBack={() => navigate('#catalog')} /> : <section id="list">
            <form className="bar" aria-label="Поиск и фильтры" onSubmit={submitSearch}>
              <label className="sr" htmlFor="search">Поиск по названию или SKU</label>
              <input className="in" id="search" type="search" placeholder="Название детали или обозначение" maxLength={1000} value={search} onChange={e => setSearch(e.target.value)} />
              <fieldset className="choice sm" id="model"><legend className="sr">Модификация</legend>
                <label><input type="radio" name="m" checked={!query.model_id} onChange={() => setQuery(v => ({...v, model_id:null, offset:0}))} /><span>Все модели</span></label>
                {models.data?.map(m => <label key={m.id}><input type="radio" name="m" checked={query.model_id === m.id} onChange={() => setQuery(v => ({...v, model_id:m.id, offset:0}))} /><span>{m.name}</span></label>)}
              </fieldset>
              <button className="btn sm" type="button" data-cart="" aria-label={`Открыть корзину, позиций: ${selection.items.length}`} onClick={() => setDrawerOpen(true)}>Заявка ({selection.items.reduce((n,i) => n+i.qty,0)})</button>
            </form>
            {models.status === 'success' && !models.data.length && <p className="tip">Модели оборудования пока не опубликованы.</p>}
            {models.status === 'error' && <p className="inline-error" role="alert">Не удалось загрузить модели. <button onClick={() => setModelAttempt(v => v+1)}>Повторить загрузку моделей</button></p>}
            <section aria-label="Результаты каталога" aria-busy={catalog.status === 'loading'} ref={catalogAnchor} tabIndex={-1}>
              {catalog.status !== 'success' ? <StatusPanel kind={catalog.status} onRetry={() => setAttempt(v => v+1)} /> : catalog.data.items.length === 0 ? <StatusPanel kind="empty" onReset={reset} /> : <>
                <div className="pcards" id="pcards">{catalog.data.items.map(item => <CatalogCard key={item.id} item={item} selection={selection} onOpen={openItem} />)}</div>
                {catalog.data.total > catalog.data.limit && <div className="catalog-pagination"><p role="status">Показано {catalog.data.offset+1}–{catalog.data.offset+catalog.data.items.length} из {catalog.data.total}</p><button className="btn ghost sm" disabled={catalog.data.offset===0} onClick={() => setQuery(v=>({...v,offset:Math.max(0,(v.offset??0)-20)}))}>Назад</button><button className="btn ghost sm" disabled={catalog.data.offset+catalog.data.limit>=catalog.data.total || catalog.data.offset+20>10000} onClick={()=>setQuery(v=>({...v,offset:(v.offset??0)+20}))}>Далее</button></div>}
              </>}
            </section>
          </section>}
        </div>
      </main>
      {requestVisited && (
        <div hidden={!requestOpen || chatOpen}>
          <RequestPage
            key={requestGeneration}
            source={source}
            orders={orders}
            selection={selection}
            submission={submission}
            active={requestOpen}
            onBack={() => navigate('#catalog')}
            onNew={() => startNew('commercial')}
          />
        </div>
      )}
      {supportContext && (
        <div hidden={!supportOpen}>
          <RequestPage
            key={supportGeneration}
            source={source}
            orders={orders}
            selection={selection}
            submission={submission}
            active={supportOpen}
            support={supportContext}
            onBack={() => navigate('#catalog')}
            onChat={() => navigate('#chat')}
            onNew={() => startNew('support')}
            independentRequested={independentRequested}
            onIndependent={() => {
              if (submission.snapshot().kind !== 'editing') return
              setSupportContext({ draft: null, owner: null })
              setIndependentRequested(false)
            }}
            onKeepChat={() => setIndependentRequested(false)}
          />
        </div>
      )}
      {drawerOpen && <SelectionDrawer onClose={() => setDrawerOpen(false)}>
        <SelectedItems source={source} selection={selection} onOpen={id => { navigate('#catalog'); openItem(id) }} onRequest={openRequest} />
      </SelectionDrawer>}
      {!chatOpen && <div className="fab-rail"><a className="fab" href="#chat" aria-label="Открыть помощника"><span className="lbl">Помощник</span><span className="ico"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2a1 1 0 0 1 1 1v1.1A7 7 0 0 1 19 11v1h1a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1h-1v1a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3v-1H4a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1h1v-1a7 7 0 0 1 6-6.9V3a1 1 0 0 1 1-1Zm-3 9a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3Zm6 0a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3Z" /></svg></span></a></div>}
      <SiteFooter />
    </>
  )
}
