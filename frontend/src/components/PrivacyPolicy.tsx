import type { PrivacyPolicy as Policy } from '../api/types'

export function PrivacyPolicy({ policy }: { policy: Policy }) {
  return (
    <section aria-label="Политика обработки персональных данных">
      <h2>Политика обработки персональных данных</h2>
      {policy.synthetic && (
        <p role="note" className="inline-error">
          Только синтетическое демо. Политика не разрешает сбор реальных
          персональных данных. Не вводите реальные контакты.
        </p>
      )}
      <p>Версия политики: {policy.version}</p>
      <p className="consent-text">{policy.text}</p>
      <h3>Отзыв согласия и запрос удаления</h3>
      <p className="consent-text">{policy.withdrawal_instructions}</p>
    </section>
  )
}
