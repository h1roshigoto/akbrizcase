import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from 'react'
import type { CatalogSource } from '../api/catalog'
import type { ChatReply, UUID } from '../api/types'
import { MAX_REQUEST_ITEMS, type Selection } from '../state/selection'
import { ChatController } from './controller'
import { DiagnosisMenu } from './DiagnosisMenu'
import { ReplyView } from './ReplyView'

export function ChatPage({
  controller,
  catalog,
  selection,
  onItem,
  onRequest,
  onSupport,
  onBack,
}: {
  controller: ChatController
  catalog: CatalogSource
  selection: Selection
  onItem: (id: UUID) => void
  onRequest: () => void
  onSupport?: (
    draft: string | null,
    owner: { session_id: UUID; session_token: string },
  ) => void
  onBack: () => void
}) {
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot)
  const [opened, setOpened] = useState(false)
  const [text, setText] = useState('')
  const [actionBusy, setActionBusy] = useState(false)
  const actionLock = useRef(false)
  const alive = useRef<AbortController | null>(null)
  const [actionNotice, setActionNotice] = useState('')
  useEffect(() => {
    const lifetime = new AbortController()
    alive.current = lifetime
    void controller.open().then(() => {
      if (!lifetime.signal.aborted) setOpened(true)
    })
    return () => {
      lifetime.abort()
    }
  }, [controller])
  const disabled =
    state.busy || !state.ready || state.pending || actionBusy || !opened
  const reply = opened ? state.reply : null
  const act = async (action: ChatReply['actions'][number]) => {
    const current = controller.snapshot()
    if (
      actionLock.current ||
      disabled ||
      current.reply !== reply ||
      current.busy ||
      current.pending ||
      !reply?.actions.includes(action)
    )
      return
    if (
      action.type === 'contact_specialist' ||
      (action.type === 'open_request_form' && action.payload.kind === 'support')
    ) {
      const owner = controller.supportContext(action)
      if (owner && onSupport)
        onSupport(
          'comment_draft' in action.payload
            ? (action.payload.comment_draft ?? null)
            : null,
          owner,
        )
      return
    }
    const signal = alive.current!.signal
    actionLock.current = true
    setActionBusy(true)
    setActionNotice('')
    try {
      if (action.type === 'open_item') {
        await catalog.getItem(action.payload.item_id, null, signal)
        if (!signal.aborted) onItem(action.payload.item_id)
      } else if (
        action.type === 'open_request_form' &&
        action.payload.kind === 'commercial'
      ) {
        const suggestions = action.payload.items
        // Reload every public position. Server prices/revisions never come from chat/storage.
        const cards = await Promise.all(
          suggestions.map((s) => catalog.getItem(s.item_id, null, signal)),
        )
        if (!signal.aborted) {
          const result = selection.addSuggestions(
            cards.map((card, i) => ({ card, qty: suggestions[i]!.qty })),
          )
          if (!result) {
            setActionNotice(
              `Не удалось подготовить заявку. В одной заявке допускается не более ${MAX_REQUEST_ITEMS} уникальных позиций. Проверьте выбранное. Список не изменён, заявка не отправлена.`,
            )
            return
          }
          onRequest()
        }
      }
    } catch {
      if (!signal.aborted)
        setActionNotice(
          'Не удалось подготовить действие. Проверьте доступность позиций и лимит выбранного, затем повторите. Заявка не отправлена.',
        )
    } finally {
      actionLock.current = false
      if (!signal.aborted) setActionBusy(false)
    }
  }
  const openItem = (id: UUID) => {
    const action = reply?.actions.find(
      (a) => a.type === 'open_item' && a.payload.item_id === id,
    )
    if (action) void act(action)
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (disabled || !text.trim() || text.length > 1000) return
    void controller.send({ type: 'text', value: text })
    setText('')
  }
  return (
    <main className="page-shell chat-page" id="chat">
      <button className="text-button" onClick={onBack}>
        Вернуться в каталог
      </button>
      <h1>Помощник по оборудованию</h1>
      <p className="chat-privacy">
        Не указывайте в чате ФИО, телефон, email и другие персональные данные.
        Для обращения используйте предназначенную для этого форму.
      </p>
      <p>
        Ответы основаны на опубликованных материалах и не заменяют консультацию
        специалиста.
      </p>
      {state.storageFailed && (
        <p className="inline-error" role="alert">
          Хранилище сессии недоступно или запись не удалась. Восстановление
          после обновления страницы и сохранение запроса для повтора не
          гарантируются.
        </p>
      )}
      {state.notice && <p role="status">{state.notice}</p>}
      {state.error && (
        <p className="inline-error" role="alert">
          {state.error}
        </p>
      )}
      {state.busy && <p role="status">Загружаем ответ…</p>}
      {state.terminal && (
        <button
          className="primary-button"
          disabled={state.busy}
          onClick={() => void controller.start()}
        >
          Создать новую сессию
        </button>
      )}
      {!state.terminal && (
        <div className="chat-controls">
          <button
            className="secondary-button"
            disabled={state.busy || actionBusy}
            onClick={() => {
              setActionNotice('')
              void controller.refresh()
            }}
          >
            Перечитать сессию
          </button>
          {state.pending && (
            <button
              className="primary-button"
              disabled={state.busy || actionBusy}
              onClick={() => void controller.retry()}
            >
              Повторить тот же запрос
            </button>
          )}
        </div>
      )}
      {reply && (
        <>
          <fieldset className="chat-modes" disabled={disabled}>
            <legend>Режим помощника</legend>
            {(
              [
                ['diagnose', 'Диагностика'],
                ['knowledge', 'Документация'],
                ['commerce', 'Товары и услуги'],
              ] as const
            ).map(([mode, label]) => (
              <button
                className="secondary-button"
                key={mode}
                aria-pressed={reply.mode === mode}
                onClick={() => {
                  setText('')
                  setActionNotice('')
                  void controller.send({ type: 'select_mode', mode })
                }}
              >
                {label}
              </button>
            ))}
          </fieldset>
          {reply.mode === 'diagnose' && (
            <DiagnosisMenu
              disabled={disabled}
              onStart={(input) => {
                setActionNotice('')
                void controller.send(input)
              }}
            />
          )}
          <section aria-label="История ответов">
            {state.next && (
              <button
                className="secondary-button"
                disabled={disabled}
                onClick={() => void controller.more()}
              >
                Загрузить предыдущие ответы
              </button>
            )}
            {state.messages
              .filter((m) => m.state_version !== state.version)
              .map((m) => (
                <ReplyView key={m.message_id} reply={m.reply} />
              ))}
          </section>
          <ReplyView
            reply={reply}
            current
            disabled={disabled}
            onInput={(input) => {
              setActionNotice('')
              void controller.send(input)
            }}
            onAction={(action) => void act(action)}
            onItem={openItem}
          />
          {reply.mode !== 'diagnose' && (
            <form className="chat-panel" onSubmit={submit}>
              <label htmlFor="chat-query">
                {reply.mode === 'knowledge'
                  ? 'Запрос по документации'
                  : 'Поиск товаров и услуг'}
              </label>
              <textarea
                id="chat-query"
                value={text}
                disabled={disabled}
                minLength={1}
                maxLength={1000}
                onChange={(e) => setText(e.target.value)}
              />
              <button
                className="primary-button"
                disabled={disabled || !text.trim() || text.length > 1000}
              >
                Отправить запрос
              </button>
            </form>
          )}
        </>
      )}
      {actionNotice && (
        <p className="chat-panel" role="status">
          {actionNotice}
        </p>
      )}
    </main>
  )
}
