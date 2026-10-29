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
          <div className="chat-controls">
            {scenarios.data.items.map((s) => (
              <button
                className="secondary-button"
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
}: {
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
        className="secondary-button"
        disabled={disabled}
        onClick={() => setSelecting(true)}
      >
        Начать другой сценарий
      </button>
    )
  return (
    <section className="chat-panel" aria-label="Начало диагностики">
      <h2>Выбор сценария</h2>
      <p>
        Выбор модели обновляет меню. Текущая диагностика заменится только после
        явного запуска сценария.
      </p>
      <label>
        Модель для диагностики
        <select
          value={model ?? ''}
          disabled={disabled || models.status !== 'success'}
          onChange={(e) =>
            setModel(e.target.value ? (e.target.value as UUID) : null)
          }
        >
          <option value="">Выберите модель</option>
          {models.data?.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
            </option>
          ))}
        </select>
      </label>
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
