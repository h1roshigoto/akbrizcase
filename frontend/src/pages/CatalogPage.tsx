import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import type { CatalogSource } from '../api/catalog'
import type { CatalogQuery, UUID } from '../api/types'
import { useResource } from '../state/useResource'
import { useSelection } from '../state/selection'
import { CatalogCard } from '../components/CatalogCard'
import { SelectedItems } from '../components/SelectedItems'
import { StatusPanel } from '../components/StatusPanel'
import { Icon } from '../components/Icon'
import { ItemDetail } from './ItemDetail'
import { RequestPage } from './RequestPage'
import { httpOrders, type OrdersSource } from '../api/httpOrders'
import { Submission } from '../state/submission'
import { httpChat, type ChatSource } from '../api/httpChat'
import { ChatController } from '../chat/controller'
import { ChatPage } from '../chat/ChatPage'

export function CatalogPage({
  source,
  orders = httpOrders,
  chat = httpChat,
}: {
  source: CatalogSource
  orders?: OrdersSource
  chat?: ChatSource
}) {
  const [chatController] = useState(() => new ChatController(chat))
  const [chatOpen, setChatOpen] = useState(
    () => window.location.hash === '#chat',
  )
  useEffect(() => {
    const change = () => setChatOpen(window.location.hash === '#chat')
    window.addEventListener('hashchange', change)
    return () => window.removeEventListener('hashchange', change)
  }, [])
  const [submission] = useState(() => new Submission(orders))
  const [requestOpen, setRequestOpen] = useState(false)
  const [requestVisited, setRequestVisited] = useState(false)
  const openRequest = () => {
    setChatOpen(false)
    window.location.hash = 'request'
    setRequestVisited(true)
    setRequestOpen(true)
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
        onClick={() => setRequestOpen(false)}
      >
        Перейти к каталогу
      </a>
      <header className="site-header">
        <div className="header-inner">
          <a
            className="brand"
            href="#catalog"
            onClick={() => {
              setChatOpen(false)
              setActiveId(null)
              setRequestOpen(false)
            }}
          >
            <span className="brand-mark">
              <Icon name="box" />
            </span>
            <span>
              B2B-портал<small>Оборудование и сервис</small>
            </span>
          </a>
          <nav aria-label="Основная навигация">
            <a
              className={!chatOpen ? 'nav-active' : undefined}
              href="#catalog"
              onClick={() => {
                setChatOpen(false)
                setActiveId(null)
                setRequestOpen(false)
              }}
            >
              Каталог
            </a>
            <a
              href="#selection"
              onClick={() => {
                setChatOpen(false)
                setRequestOpen(false)
              }}
            >
              Выбранное <span>{selection.items.length}</span>
            </a>
            <a
              href="#chat"
              className={chatOpen ? 'nav-active' : undefined}
              onClick={() => {
                setRequestOpen(false)
                setChatOpen(true)
              }}
            >
              Помощник
            </a>
          </nav>
          <span className="header-note">Для вашего оборудования</span>
        </div>
      </header>
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
          catalog={source}
          selection={selection}
          onBack={() => {
            setChatOpen(false)
            window.location.hash = 'catalog'
          }}
          onItem={(id) => {
            setChatOpen(false)
            setRequestOpen(false)
            window.location.hash = 'catalog'
            openItem(id)
          }}
          onRequest={openRequest}
        />
      )}
      <main className="page-shell" hidden={requestOpen || chatOpen}>
        <div className="breadcrumb">
          Главная <span>/</span> Каталог
        </div>
        <section className="page-heading">
          <div>
            <p className="eyebrow">КАТАЛОГ РЕШЕНИЙ</p>
            <h1>Товары и услуги</h1>
            <p>
              Подберите нужные позиции для оборудования.
              <br className="desktop-break" /> Соберите товары и сервис в одном
              списке.
            </p>
          </div>
          <div className="heading-index" aria-hidden="true">
            01 <span>/ КАТАЛОГ</span>
          </div>
        </section>
        <div className="workspace-grid">
          <div
            className="catalog-column"
            id="catalog"
            ref={catalogAnchor}
            tabIndex={-1}
          >
            <section className="filter-panel" aria-label="Поиск и фильтры">
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
                <button className="primary-button" type="submit">
                  Найти
                  <Icon name="arrow" />
                </button>
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
                          <button
                            className="secondary-button"
                            disabled={catalog.data.offset === 0}
                            onClick={() =>
                              setQuery((current) => ({
                                ...current,
                                offset: Math.max(0, (current.offset ?? 0) - 20),
                              }))
                            }
                          >
                            Назад
                          </button>
                          <button
                            className="secondary-button"
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
                          </button>
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
          <SelectedItems
            source={source}
            selection={selection}
            onOpen={openItem}
            onRequest={openRequest}
          />
        </div>
      </main>
      {requestVisited && (
        <div hidden={!requestOpen || chatOpen}>
          <RequestPage
            source={source}
            orders={orders}
            selection={selection}
            submission={submission}
            active={requestOpen}
            onBack={() => setRequestOpen(false)}
          />
        </div>
      )}
      <footer className="site-footer">
        <span>
          B2B-портал <span className="footer-divider">/</span> Оборудование и
          сервис
        </span>
        <span>Каталог и заявки</span>
      </footer>
    </>
  )
}
