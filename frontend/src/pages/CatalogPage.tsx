import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import type { CatalogSource } from '../api/catalog'
import type { CatalogQuery, UUID } from '../api/types'
import { useResource } from '../state/useResource'
import { useSelection } from '../state/selection'
import { CatalogCard } from '../components/CatalogCard'
import { SelectedItems } from '../components/SelectedItems'
import { Button } from '../components/Button'
import { StatusPanel } from '../components/StatusPanel'
import { Icon } from '../components/Icon'
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
  return ['home', 'catalog', 'chat', 'wizard', 'request', 'support', 'docs', 'tech'].includes(key)
    ? key as PortalView
    : key === 'selection' ? 'catalog' : defaultView
}

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
  const [activeId, setActiveId] = useState<UUID | null>(null)
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
    setActiveId(id)
    catalogAnchor.current?.scrollIntoView?.({
      block: 'start',
      behavior: 'instant',
    })
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
      {view === 'home' && <HomePage />}
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
      <main
        className="page-shell"
        hidden={view !== 'catalog'}
      >
        <div className="breadcrumb">
          Главная <span>/</span> Каталог
        </div>
        <section className="page-heading">
          <div>
            <p className="eyebrow">ЗАПАСНЫЕ ЧАСТИ И СЕРВИС</p>
            <h1>Товары и услуги</h1>
            <p>
              Подберите нужные позиции для оборудования.
              <br className="desktop-break" /> Соберите товары и сервис в одном
              списке.
            </p>
          </div>
          <div className="heading-index" aria-hidden="true">
            МТД-КА <span>/ КАТАЛОГ</span>
          </div>
        </section>
        <div className="workspace-grid">
          <div
            className="catalog-column"
            id="catalog"
            ref={catalogAnchor}
            tabIndex={-1}
          >
            <section className="filter-panel catalog-controls" aria-label="Поиск и фильтры">
              <form className="search-form" onSubmit={submitSearch}>
                <label className="search-input">
                  <Icon name="search" />
                  <input
                    type="search"
                    aria-label="Поиск по названию или SKU"
                    placeholder="Название или SKU"
                    value={search}
                    maxLength={1000}
                    onChange={(event) => setSearch(event.target.value)}
                  />
                </label>
                <Button variant="primary" type="submit">
                  Найти
                  <Icon name="arrow" />
                </Button>
              </form>
              <div className="filter-row">
                <fieldset className="kind-filter">
                  <legend className="visually-hidden">Тип позиции</legend>
                  {(
                    [
                      [null, 'Все позиции'],
                      ['product', 'Товары'],
                      ['service', 'Услуги'],
                    ] as const
                  ).map(([kind, label]) => (
                    <button
                      key={label}
                      aria-pressed={(query.kind ?? null) === kind}
                      onClick={() => {
                        setActiveId(null)
                        setQuery((current) => ({
                          ...current,
                          kind,
                          offset: 0,
                        }))
                      }}
                    >
                      {label}
                    </button>
                  ))}
                </fieldset>
                <label className="model-filter">
                  <span>Модель оборудования</span>
                  <select
                    aria-label="Модель оборудования"
                    disabled={models.status !== 'success'}
                    value={query.model_id ?? ''}
                    onChange={(event) =>
                      setQuery((current) => ({
                        ...current,
                        model_id: event.target.value
                          ? (event.target.value as UUID)
                          : null,
                        offset: 0,
                      }))
                    }
                  >
                    <option value="">
                      {models.status === 'loading'
                        ? 'Загружаем модели…'
                        : 'Все модели'}
                    </option>
                    {models.data?.map((model) => (
                      <option key={model.id} value={model.id}>
                        {model.name} · {model.code}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              {models.status === 'error' && (
                <p className="inline-error" role="alert">
                  Не удалось загрузить модели.{' '}
                  <button
                    className="text-button"
                    onClick={() => setModelAttempt((value) => value + 1)}
                  >
                    Повторить загрузку моделей
                  </button>
                </p>
              )}
              {models.status === 'success' && models.data.length === 0 && (
                <p className="filter-note">
                  Модели оборудования пока не опубликованы.
                </p>
              )}
              {query.model_id && (
                <p className="filter-note">
                  В списке — только позиции с подтверждённой применимостью. В
                  открытой карточке показан статус для выбранной модели.
                </p>
              )}
            </section>
            {activeId ? (
              <ItemDetail
                key={activeId}
                source={source}
                id={activeId}
                modelId={query.model_id ?? null}
                selection={selection}
                onBack={() => setActiveId(null)}
              />
            ) : (
              <section
                aria-label="Результаты каталога"
                aria-busy={catalog.status === 'loading'}
              >
                <div className="results-heading">
                  <h2>
                    Каталог <span>{catalog.data?.total ?? '—'}</span>
                  </h2>
                  <button className="text-button reset-button" onClick={reset}>
                    Сбросить фильтры
                  </button>
                </div>
                {catalog.status !== 'success' ? (
                  <StatusPanel
                    kind={catalog.status}
                    onRetry={() => setAttempt((value) => value + 1)}
                  />
                ) : catalog.data.items.length === 0 ? (
                  <StatusPanel kind="empty" onReset={reset} />
                ) : (
                  <>
                    <div className="catalog-grid">
                      {catalog.data.items.map((item) => (
                        <CatalogCard
                          key={item.id}
                          item={item}
                          selected={selection.items.some(
                            (row) => row.item_id === item.id,
                          )}
                          atCapacity={selection.items.length >= 100}
                          onAdd={selection.add}
                          onOpen={openItem}
                        />
                      ))}
                    </div>
                    <div className="catalog-pagination">
                      <p role="status">
                        Показано {catalog.data.offset + 1}–
                        {catalog.data.offset + catalog.data.items.length} из{' '}
                        {catalog.data.total}
                      </p>
                      {catalog.data.total > catalog.data.limit && (
                        <div>
                          <Button
                            disabled={catalog.data.offset === 0}
                            onClick={() =>
                              setQuery((current) => ({
                                ...current,
                                offset: Math.max(0, (current.offset ?? 0) - 20),
                              }))
                            }
                          >
                            Назад
                          </Button>
                          <Button
                            disabled={
                              catalog.data.offset + catalog.data.limit >=
                                catalog.data.total ||
                              catalog.data.offset + 20 > 10000
                            }
                            onClick={() =>
                              setQuery((current) => ({
                                ...current,
                                offset: (current.offset ?? 0) + 20,
                              }))
                            }
                          >
                            Далее
                          </Button>
                        </div>
                      )}
                    </div>
                  </>
                )}
              </section>
            )}
            <div className="catalog-note">
              <span className="note-symbol">i</span>
              <p>
                Не уверены в применимости? Выберите модель оборудования. Если
                подтверждения нет, совместимость потребуется уточнить.
              </p>
            </div>
          </div>
          <div hidden={drawerOpen}>
            <SelectedItems source={source} selection={selection} onOpen={openItem} onRequest={openRequest} />
          </div>
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
      <SiteFooter />
    </>
  )
}
