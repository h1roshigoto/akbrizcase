import type { ChatReply, UUID } from '../api/types'
import { SourceLink } from './SourceLink'
import { ItemPrice, ApplicabilityStatus } from '../components/ItemFacts'
import { ItemImage } from '../components/ItemImage'

const status = {
  question: 'Вопрос',
  results: 'Результаты',
  no_match: 'Выберите действие или уточните запрос',
  completed: 'Диагностика завершена',
  handoff: 'Обратитесь к специалисту',
}
export function ReplyView({
  reply,
  current = false,
  disabled = true,
  onInput,
  onAction,
  onItem,
}: {
  reply: ChatReply
  current?: boolean
  disabled?: boolean
  onInput?: (input: ChatReply['buttons'][number]['input']) => void
  onAction?: (action: ChatReply['actions'][number]) => void
  onItem?: (id: UUID) => void
}) {
  return (
    <article
      className={`chat-reply ${reply.resolution}`}
      aria-label={current ? 'Актуальный ответ' : 'Ответ из истории'}
    >
      <p className="eyebrow">{status[reply.resolution]}</p>
      <p className="chat-text">{reply.text}</p>
      {reply.sources.length > 0 && (
        <ul aria-label="Источники">
          {reply.sources.map((s) => (
            <li key={s.section_id}>
              <SourceLink source={s} disabled={current && disabled} />
            </li>
          ))}
        </ul>
      )}
      {reply.items.length > 0 && (
        <div className="catalog-grid">
          {reply.items.map((item) => (
            <article className="catalog-card" key={item.id}>
              <ItemImage item={item} />
              <div className="card-body">
                <p className="sku">{item.sku}</p>
                <h3>{item.name}</h3>
                <ApplicabilityStatus item={item} />
                <ItemPrice item={item} />
                <p>Редакция позиции: {item.revision}</p>
                {current && (
                  <button
                    className="secondary-button"
                    disabled={disabled}
                    onClick={() => onItem?.(item.id)}
                  >
                    Открыть карточку: {item.name}
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
      {current && (
        <>
          <div className="chat-controls" aria-label="Текущий шаг">
            {reply.buttons
              .filter((b) => b.input.type !== 'select_mode')
              .map((b) => (
                <button
                  key={b.id}
                  className="secondary-button"
                  disabled={disabled}
                  onClick={() => onInput?.(b.input)}
                >
                  {b.label}
                </button>
              ))}
          </div>
          <div className="chat-controls" aria-label="Действия ответа">
            {reply.actions.map((action, index) => {
              switch (action.type) {
                case 'open_document':
                  return (
                    <SourceLink
                      key={index}
                      source={action.payload}
                      disabled={disabled}
                    />
                  )
                case 'open_item':
                  return (
                    <button
                      key={index}
                      className="secondary-button"
                      disabled={disabled}
                      onClick={() => onAction?.(action)}
                    >
                      Карточка{' '}
                      {reply.items.find((i) => i.id === action.payload.item_id)
                        ?.name ?? 'позиции'}
                    </button>
                  )
                case 'open_request_form':
                  return (
                    <button
                      key={index}
                      className="secondary-button"
                      disabled={disabled}
                      onClick={() => onAction?.(action)}
                    >
                      {action.payload.kind === 'commercial'
                        ? 'Проверить коммерческую заявку'
                        : 'Обращение в поддержку'}
                    </button>
                  )
                case 'contact_specialist':
                  return (
                    <button
                      key={index}
                      className="secondary-button"
                      disabled={disabled}
                      onClick={() => onAction?.(action)}
                    >
                      Связаться со специалистом
                    </button>
                  )
                default:
                  return null
              }
            })}
          </div>
        </>
      )}
    </article>
  )
}
