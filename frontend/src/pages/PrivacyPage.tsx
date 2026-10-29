import { useCallback } from 'react'
import { httpOrders, type OrdersSource } from '../api/httpOrders'
import { PrivacyPolicy } from '../components/PrivacyPolicy'
import { useResource } from '../state/useResource'

export function PrivacyPage({
  orders = httpOrders,
}: {
  orders?: OrdersSource
}) {
  const policy = useResource(
    useCallback((signal: AbortSignal) => orders.getPolicy(signal), [orders]),
  )
  return (
    <main className="page-shell request-shell">
      <a href="/">Вернуться в каталог</a>
      <h1>Политика обработки персональных данных</h1>
      {policy.status === 'loading' && <p role="status">Загружаем политику…</p>}
      {policy.status === 'error' && (
        <p role="alert">
          Утверждённая политика недоступна. Отправка новых заявок заблокирована.
        </p>
      )}
      {policy.data && <PrivacyPolicy policy={policy.data} />}
    </main>
  )
}
