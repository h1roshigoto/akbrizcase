import { Button } from './Button'
import { Icon } from './Icon'

export function StatusPanel({
  kind,
  onRetry,
  onReset,
}: {
  kind: 'loading' | 'empty' | 'error'
  onRetry?: () => void
  onReset?: () => void
}) {
  return (
    <div
      className="status-panel ui-stack"
      data-kind={kind}
      aria-atomic="true"
      role={kind === 'error' ? 'alert' : 'status'}
    >
      {kind === 'loading' ? (
        <span className="loader" aria-hidden="true" />
      ) : (
        <Icon name={kind === 'error' ? 'warning' : 'search'} />
      )}
      <h3>
        {kind === 'loading'
          ? 'Загружаем каталог'
          : kind === 'empty'
            ? 'Позиции не найдены'
            : 'Не удалось загрузить данные'}
      </h3>
      <p>
        {kind === 'loading'
          ? 'Подготавливаем товары и услуги…'
          : kind === 'empty'
            ? 'Попробуйте другой запрос или сбросьте фильтры.'
            : 'Проверьте соединение и повторите попытку.'}
      </p>
      {kind === 'error' && onRetry && (
        <Button onClick={onRetry}>Повторить</Button>
      )}
      {kind === 'empty' && onReset && (
        <Button onClick={onReset}>Сбросить фильтры</Button>
      )}
    </div>
  )
}
