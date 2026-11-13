import { useCallback, useState } from 'react'
import { chatModels, chatScenarios, type ChatInput } from '../api/httpChat'
import type { UUID } from '../api/types'
import { useResource } from '../state/useResource'

function Scenarios({
  model,
  disabled,
  onStart,
}: {
  model: UUID
  disabled: boolean
  onStart: (input: ChatInput) => void
}) {
  const [attempt, setAttempt] = useState(0)
  const load = useCallback(
    (signal: AbortSignal) => chatScenarios(model, signal),
    [model, attempt],
  )
  const scenarios = useResource(load)
  return (
    <div aria-label="Опубликованные сценарии">
      {scenarios.status === 'loading' && (
        <p role="status">Загружаем сценарии…</p>
      )}
      {scenarios.status === 'error' && (
        <p role="alert">
          Не удалось загрузить сценарии.{' '}
          <button onClick={() => setAttempt((n) => n + 1)} disabled={disabled}>
            Повторить загрузку сценариев
          </button>
        </p>
      )}
      {scenarios.status === 'success' &&
        (scenarios.data.items.length ? (
          <div className="wz-opts">
            {scenarios.data.items.map((s) => (
              <button
                className="wz-o"
                disabled={disabled}
                key={s.key}
                onClick={() =>
                  onStart({
                    type: 'start_diagnosis',
                    model_id: s.model_id,
                    tree_key: s.key,
                  })
                }
              >
                {s.title}
              </button>
            ))}
          </div>
        ) : (
          <p>
            Для этой модели нет доступных опубликованных сценариев. Можно
            выбрать другую модель или обратиться к специалисту.
          </p>
        ))}
      <button
        className="text-button"
        disabled={disabled}
        onClick={() => setAttempt((n) => n + 1)}
      >
        Обновить сценарии
      </button>
    </div>
  )
}
export function DiagnosisMenu({
  disabled,
  onStart,
  symptom,
}: {
  symptom?: string | null
  disabled: boolean
  onStart: (input: ChatInput) => void
}) {
  const [model, setModel] = useState<UUID | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [selecting, setSelecting] = useState(true)
  const load = useCallback(chatModels, [attempt])
  const models = useResource(load)
  if (!selecting)
    return (
      <button
        className="wz-o"
        disabled={disabled}
        onClick={() => setSelecting(true)}
      >
        Начать другой сценарий
      </button>
    )
  return (
    <section className="chat-panel" aria-label="Начало диагностики">
      <h3 className="wz-q">{model ? 'Что происходит с изделием?' : 'Какая модификация у изделия?'}</h3>{symptom && <p className="wz-lead">Неисправность: {symptom}.</p>}
      {model ? <nav className="wz-path" aria-label="Сделанный выбор">Модификация <button type="button" disabled={disabled} onClick={() => setModel(null)} aria-label="Изменить модификацию">{models.data?.find(m => m.id === model)?.name}</button></nav> : <div className="wz-opts" aria-label="Модель для диагностики">
        {models.data?.map(m => <button className="wz-o" key={m.id} type="button" disabled={disabled} onClick={() => setModel(m.id)}>{m.name}</button>)}
        <button className="wz-o" type="button" disabled={disabled} onClick={() => onStart({type:'text',value:'Не знаю модификацию изделия'})}>Не знаю</button>
      </div>}
      {models.status === 'loading' && <p role="status">Загружаем модели…</p>}
      {models.status === 'error' && (
        <p role="alert">
          Не удалось загрузить модели.{' '}
          <button disabled={disabled} onClick={() => setAttempt((n) => n + 1)}>
            Повторить загрузку моделей
          </button>
        </p>
      )}
      {models.status === 'success' && !models.data.length && (
        <p>Модели оборудования пока не опубликованы.</p>
      )}
      {model && (
        <Scenarios
          key={model}
          model={model}
          disabled={disabled}
          onStart={(input) => {
            onStart(input)
            setSelecting(false)
          }}
        />
      )}
    </section>
  )
}
