import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
  type ChangeEvent,
} from 'react'
import { CatalogError, type CatalogSource } from '../api/catalog'
import type { OrdersSource } from '../api/httpOrders'
import type {
  CatalogItem,
  OrderCreate,
  OrderItemInput,
  OrderReceipt,
  UUID,
} from '../api/types'
import { formatKopecks, formatPrice, toKopecks } from '../state/money'
import { emptyForm, validateForm, type FormValues } from '../state/requestForm'
import { MAX_REQUEST_ITEMS, validQty, type Selection } from '../state/selection'
import { Submission } from '../state/submission'
import { useResource } from '../state/useResource'
import { PrivacyPolicy } from '../components/PrivacyPolicy'

function CatalogChanges({
  previous,
  current,
}: {
  previous: CatalogItem | null | undefined
  current: CatalogItem
}) {
  if (!previous) return null
  const price = (item: CatalogItem) =>
    item.price === null
      ? 'Цена по запросу'
      : `${formatPrice(item.price)} RUB / ${item.unit}`
  const changes: string[] = []
  if (previous.name !== current.name)
    changes.push(`Название: ${previous.name} → ${current.name}`)
  if (previous.sku !== current.sku)
    changes.push(`SKU: ${previous.sku} → ${current.sku}`)
  if (price(previous) !== price(current))
    changes.push(`Цена: ${price(previous)} → ${price(current)}`)
  if (previous.kind !== current.kind)
    changes.push(`Тип: ${current.kind === 'product' ? 'Товар' : 'Услуга'}`)
  if (previous.requires_shipping !== current.requires_shipping)
    changes.push(
      `Доставка: ${current.requires_shipping ? 'требуется' : 'не требуется'}`,
    )
  return changes.length ? (
    <ul aria-label="Изменения позиции">
      {changes.map((change) => (
        <li key={change}>{change}</li>
      ))}
    </ul>
  ) : null
}

function Receipt({ receipt }: { receipt: OrderReceipt }) {
  const amount = (v: OrderReceipt['items_total']) =>
    v === null ? 'Уточняется' : `${formatPrice(v)} RUB`
  return (
    <section className="request-panel" aria-label="Подтверждение заявки">
      <p className="eyebrow">ЗАЯВКА ПОЛУЧЕНА</p>
      <h1>Заявка №{receipt.number}</h1>
      <dl className="request-totals">
        <dt>Начальный статус</dt>
        <dd>{receipt.status}</dd>
        <dt>Известная часть стоимости</dt>
        <dd>{amount(receipt.known_subtotal)}</dd>
        <dt>Стоимость всех позиций</dt>
        <dd>{amount(receipt.items_total)}</dd>
        <dt>Доставка</dt>
        <dd>
          {receipt.delivery_status === 'not_required'
            ? 'Не требуется'
            : 'Требует расчёта'}
        </dd>
        <dt>Стоимость доставки</dt>
        <dd>{amount(receipt.delivery_cost)}</dd>
        <dt>Общая сумма</dt>
        <dd>{amount(receipt.total_amount)}</dd>
        <dt>Валюта</dt>
        <dd>{receipt.currency}</dd>
      </dl>
      <p>
        Суммы предварительные. Неизвестные цены и доставку уточнит менеджер.
        Подтверждение заявки не означает заключение договора, выставление счёта,
        оплату или гарантированную цену доставки.
      </p>
    </section>
  )
}
const messages = {
  chat_expired:
    'Срок вашей сессии истёк. Заявка не создана. Введённые данные сохранены в открытой форме. Можно оформить обращение независимо от чата.',
  catalog_changed:
    'Каталог изменился. Перечитываем позиции; проверьте актуальные данные и подтвердите заявку заново.',
  consent_changed:
    'Текст согласия изменился. Прочитайте актуальный текст и примите его заново.',
  validation:
    'Сервер отклонил данные заявки. Проверьте имя, контакты, длину полей и количество позиций. Введённые значения сохранены в открытой форме.',
  idempotency_conflict:
    'Не удалось безопасно сопоставить запрос. Повторная отправка с новым ключом заблокирована. Обратитесь к менеджеру для проверки предыдущей заявки.',
  not_found:
    'Не удалось получить подтверждение заявки. Обратитесь к менеджеру для проверки. Автоматическая новая отправка недоступна.',
}

export type SupportContext = {
  draft: string | null
  owner: { session_id: UUID; session_token: string } | null
}

export function RequestPage({
  source,
  orders,
  selection,
  submission,
  active,
  onBack,
  support,
  onChat,
  onNew,
  independentRequested = false,
  onIndependent,
  onKeepChat,
}: {
  source: CatalogSource
  orders: OrdersSource
  selection: Selection
  submission: Submission
  active: boolean
  onBack: () => void
  support?: SupportContext
  onChat?: () => void
  onNew?: () => void
  independentRequested?: boolean
  onIndependent?: () => void
  onKeepChat?: () => void
}) {
  const state = useSyncExternalStore(submission.subscribe, submission.snapshot)
  const anchor = useRef<HTMLElement>(null)
  const [values, setValues] = useState<FormValues>(() => ({
    ...emptyForm,
    comment: support?.draft ?? '',
  }))
  const [siteFallback, setSiteFallback] = useState(false)
  const owner = siteFallback ? null : support?.owner
  const isSupport = support !== undefined
  useEffect(() => {
    setSiteFallback(false)
    setAccepted(false)
    setConfirmed(false)
  }, [support?.owner?.session_id])
  const [accepted, setAccepted] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [touched, setTouched] = useState(false)
  useEffect(() => {
    if (active) anchor.current?.focus()
  }, [active, state.kind === 'success'])
  const idsKey = selection.items.map((row) => row.item_id).join(',')
  const load = useCallback(
    async (signal: AbortSignal) => {
      if (!active || isSupport) return []
      return Promise.all(
        selection.items.map(async (row) => {
          try {
            return {
              id: row.item_id,
              item: await source.getItem(row.item_id, null, signal),
            }
          } catch (error) {
            if (error instanceof CatalogError && error.status === 404)
              return { id: row.item_id, item: null }
            throw error
          }
        }),
      )
    },
    [source, idsKey, attempt, active, isSupport],
  )
  const details = useResource(load)
  const previousReview = useRef<typeof details.data>(null)
  const consent = useResource(
    useCallback(
      (signal: AbortSignal) => orders.getConsent(signal),
      [orders, attempt, active],
    ),
  )
  const policy = useResource(
    useCallback(
      (signal: AbortSignal) => orders.getPolicy(signal),
      [orders, attempt, active],
    ),
  )
  const documentsReady =
    consent.status === 'success' &&
    policy.status === 'success' &&
    policy.data.collection_enabled &&
    policy.data.consent_version === consent.data.version
  useEffect(() => {
    setAccepted(false)
    setConfirmed(false)
  }, [
    active,
    attempt,
    consent.data?.version,
    consent.data?.checksum,
    policy.data?.version,
    policy.data?.checksum,
  ])
  // Conflict responses prove rejection of this logical request. Refresh while
  // retaining the rejected marker until the user explicitly starts a review.
  useEffect(() => {
    if (
      state.kind === 'rejected' &&
      (state.reason === 'catalog_changed' || state.reason === 'consent_changed')
    )
      setAttempt((n) => n + 1)
  }, [state])
  useEffect(() => {
    if (!['sending', 'uncertain', 'recovered'].includes(state.kind)) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [state.kind])
  const editing = state.kind === 'editing'
  let subtotal = 0n
  let unknown = 0
  let shipping = false
  let needsReview = false
  let unavailable = false
  for (const row of isSupport ? [] : selection.items) {
    const detail = details.data?.find((v) => v.id === row.item_id)
    const item = detail?.item
    if (!item) unavailable = true
    if (item && item.revision !== row.revision) needsReview = true
    if (item?.price_mode === 'fixed' && item.price !== null)
      subtotal += toKopecks(item.price) * BigInt(row.qty)
    else unknown++
    shipping ||= item?.requires_shipping ?? false
  }
  const errors = validateForm(
    { ...values, city: shipping ? values.city : '' },
    isSupport,
  )
  const validSelection =
    selection.items.length >= 1 &&
    selection.items.length <= MAX_REQUEST_ITEMS &&
    new Set(selection.items.map((row) => row.item_id)).size ===
      selection.items.length &&
    selection.items.every((row) => validQty(row.qty))
  const ready =
    editing &&
    !(independentRequested && owner) &&
    source.mode === 'api' &&
    (isSupport || validSelection) &&
    details.status === 'success' &&
    !unavailable &&
    !needsReview &&
    documentsReady
  const submit = (event: FormEvent) => {
    event.preventDefault()
    setTouched(true)
    if (
      !ready ||
      !accepted ||
      !confirmed ||
      Object.keys(errors).length ||
      !consent.data
    )
      return
    const items = selection.items.map(({ item_id, qty, revision }) => ({
      item_id,
      qty,
      expected_revision: revision,
    })) as [OrderItemInput, ...OrderItemInput[]]
    const body: OrderCreate = {
      ...(isSupport
        ? { kind: 'support' as const, items: [] as [], comment: values.comment }
        : { kind: 'commercial' as const, items }),
      ...(isSupport && owner
        ? { source: 'chat' as const, chat_session_id: owner.session_id }
        : { source: 'site' as const }),
      contact: {
        name: values.name,
        ...(values.email !== '' ? { email: values.email } : {}),
        ...(values.phone !== '' ? { phone: values.phone } : {}),
      },
      ...(values.organization_name !== ''
        ? { organization_name: values.organization_name }
        : {}),
      ...(shipping && values.city !== '' ? { city: values.city } : {}),
      ...(values.comment !== '' ? { comment: values.comment } : {}),
      consent: { version: consent.data.version, accepted: true },
      confirmed: true,
    }
    previousReview.current = details.data
    void submission.confirm(
      body,
      isSupport && owner
        ? {
            'X-Chat-Token':
              owner.session_token as import('../api/types').SecretToken,
          }
        : undefined,
    )
  }
  const refresh = () => {
    setAccepted(false)
    setConfirmed(false)
    setAttempt((n) => n + 1)
  }
  if (state.kind === 'success')
    return (
      <main className="page-shell request-shell" ref={anchor} tabIndex={-1}>
        {submission.succeededFor(isSupport ? 'support' : 'commercial') ? (
          <Receipt receipt={state.receipt} />
        ) : (
          <p role="status">
            Предыдущая заявка успешно сохранена. Новая заявка ещё не создана.
          </p>
        )}
        {onNew && (
          <button className="primary-button" onClick={onNew}>
            {isSupport
              ? 'Начать новое независимое обращение с сайта'
              : 'Начать новую коммерческую заявку'}
          </button>
        )}
        <button className="secondary-button" onClick={onBack}>
          Вернуться в каталог
        </button>
      </main>
    )
  return (
    <main className="page-shell request-shell" ref={anchor} tabIndex={-1}>
      <button className="text-button" onClick={onBack}>
        ← Вернуться в каталог
      </button>
      <section
        className="request-panel"
        aria-labelledby={isSupport ? 'support-title' : 'request-title'}
      >
        <p className="eyebrow">ОБЗОР И ПОДТВЕРЖДЕНИЕ</p>
        <h1 id={isSupport ? 'support-title' : 'request-title'}>
          {isSupport ? 'Обращение к специалисту' : 'Оформление заявки'}
        </h1>
        {isSupport ? (
          <>
            <p>
              Опишите проблему и проверьте окончательный текст. Предложенный
              черновик можно полностью изменить. Обращение будет создано только
              после подтверждения и отправки.
            </p>
            <p>
              {owner
                ? 'Обращение связано с вашей сессией чата.'
                : 'Самостоятельное обращение с сайта.'}
            </p>
            {onChat && (
              <button className="text-button" onClick={onChat}>
                Вернуться в чат
              </button>
            )}
          </>
        ) : (
          <p>
            Проверьте позиции и предварительные цены. Выбранное сохраняется при
            возвращении в каталог.
          </p>
        )}
        {isSupport && independentRequested && owner && (
          <section aria-label="Новое независимое обращение">
            <p>
              Сейчас открыт черновик из чата. Для независимого обращения
              подтвердите отделение от чата. Введённые текст и контакты
              сохранятся; согласие и отправку потребуется подтвердить заново.
            </p>
            <button type="button" disabled={!editing} onClick={onIndependent}>
              Продолжить независимо от чата
            </button>
            <button type="button" onClick={onKeepChat}>
              Вернуться к черновику чата
            </button>
          </section>
        )}
        {state.kind === 'recovered' && (
          <p role="alert" className="inline-error">
            Исход предыдущей отправки неизвестен. После обновления страницы
            исходное тело с контактами не сохраняется; безопасно повторить его
            невозможно. Не создавайте новую заявку до проверки у менеджера:
            предыдущая могла быть принята. Если сохранился номер, сообщите его
            менеджеру. Не передавайте секреты из браузера.
          </p>
        )}
        {state.kind === 'storage_error' && (
          <p role="alert" className="inline-error">
            Не удалось обеспечить безопасное сохранение отправки. Отправка
            заблокирована. Проверьте разрешения браузера; если уже отправляли
            заявку, уточните результат у менеджера перед новой попыткой.
          </p>
        )}
        {state.kind === 'uncertain' && (
          <div role="alert" className="inline-error">
            <p>
              Результат отправки пока неизвестен: заявка могла быть принята.
              Повтор отправит тот же подтверждённый запрос. Не обновляйте
              страницу и не создавайте новую заявку; при длительной
              недоступности обратитесь к менеджеру.
            </p>
            <button
              className="primary-button"
              onClick={() => void submission.retry()}
            >
              Повторить отправку
            </button>
          </div>
        )}
        {state.kind === 'sending' && (
          <p role="status">Отправляем заявку… Дождитесь подтверждения.</p>
        )}
        {state.kind === 'rejected' && (
          <div role="alert" className="inline-error">
            <p>{messages[state.reason]}</p>
            {isSupport && owner && state.reason === 'chat_expired' && (
              <button
                type="button"
                className="secondary-button"
                onClick={() => {
                  if (submission.review(true)) {
                    setSiteFallback(true)
                    onIndependent?.()
                    refresh()
                  }
                }}
              >
                Оформить независимо от чата
              </button>
            )}
            {['catalog_changed', 'consent_changed', 'validation'].includes(
              state.reason,
            ) && (
              <button
                className="secondary-button"
                onClick={() => {
                  if (submission.review()) refresh()
                }}
              >
                Пересмотреть заявку
              </button>
            )}
          </div>
        )}
        {source.mode !== 'api' && (
          <p role="alert">Отправка на демонстрационных fixtures недоступна.</p>
        )}
        {!isSupport && !validSelection && (
          <p role="alert" className="inline-error">
            Для заявки нужно от 1 до 50 уникальных позиций, количество каждой —
            1–999. Вернитесь в каталог и измените выбранное. Позиции не будут
            удалены автоматически.
          </p>
        )}
        {!isSupport && (
          <section
            aria-label="Обзор позиций"
            aria-busy={details.status === 'loading'}
          >
            <h2>Выбранные товары и услуги</h2>
            {details.status === 'loading' && (
              <p role="status">Загружаем актуальные позиции…</p>
            )}
            {details.status === 'error' && (
              <p role="alert">
                Каталог недоступен. Отправка заблокирована.{' '}
                <button
                  className="text-button"
                  onClick={refresh}
                  disabled={!editing}
                >
                  Повторить загрузку каталога
                </button>
              </p>
            )}
            {details.status === 'success' && (
              <ul className="request-items">
                {selection.items.map((row) => {
                  const item = details.data.find(
                    (v) => v.id === row.item_id,
                  )?.item
                  return (
                    <li key={row.item_id}>
                      {item ? (
                        <>
                          <h3>{item.name}</h3>
                          <p>
                            {item.sku} ·{' '}
                            {item.kind === 'product' ? 'Товар' : 'Услуга'} ·{' '}
                            {row.qty} {item.unit}
                          </p>
                          <p>
                            {item.price === null
                              ? 'Цена по запросу'
                              : `${formatPrice(item.price)} RUB / ${item.unit}`}
                          </p>
                          <p>
                            {item.requires_shipping
                              ? 'Требуется доставка, стоимость уточняется'
                              : 'Доставка не требуется'}
                          </p>
                          <CatalogChanges
                            previous={
                              previousReview.current?.find(
                                (v) => v.id === row.item_id,
                              )?.item
                            }
                            current={item}
                          />
                          {item.revision !== row.revision && (
                            <div role="alert" className="revision-note">
                              <p>
                                Позиция изменилась: выбранная ревизия{' '}
                                {row.revision}, текущая {item.revision}. Выше
                                показаны актуальные название, цена и доставка.
                                Проверьте их.
                              </p>
                              <button
                                className="secondary-button"
                                disabled={!editing}
                                onClick={() => {
                                  selection.acceptRevision(
                                    row.item_id,
                                    row.revision,
                                    item.revision,
                                  )
                                  setConfirmed(false)
                                }}
                              >
                                Принять обновление {item.sku}
                              </button>
                            </div>
                          )}
                        </>
                      ) : (
                        <p role="alert">
                          Позиция {row.item_id} недоступна. Её нельзя отправить;
                          удалите её в каталоге.
                        </p>
                      )}
                    </li>
                  )
                })}
              </ul>
            )}
            {details.status === 'success' && (
              <>
                <p>
                  Известная часть стоимости:{' '}
                  <strong>{formatKopecks(subtotal)} RUB</strong>
                </p>
                <p>
                  Позиций без известной цены: {unknown}.{' '}
                  {unknown > 0
                    ? 'Полная стоимость позиций уточняется.'
                    : 'Все цены позиций известны.'}
                </p>
                <p>
                  {shipping
                    ? 'Требуется доставка. Стоимость доставки и общая сумма уточняются менеджером.'
                    : 'Доставка не требуется; стоимость доставки 0,00 RUB.'}
                </p>
              </>
            )}
          </section>
        )}
        <form noValidate onSubmit={submit}>
          <fieldset disabled={!editing} className="request-fields">
            <legend>Контактные данные</legend>
            <p>Укажите хотя бы один контакт: email или телефон.</p>
            {(Object.keys(emptyForm) as (keyof FormValues)[])
              .filter((key) => key !== 'city' || shipping)
              .map((key) => {
                const labels = {
                  name: 'Контактное имя',
                  email: 'Email',
                  phone: 'Телефон',
                  organization_name: 'Организация (необязательно)',
                  city: 'Город доставки (необязательно)',
                  comment: isSupport
                    ? 'Описание проблемы *'
                    : 'Комментарий (необязательно)',
                }
                const id = `${isSupport ? 'support' : 'request'}-${key}`
                const props = {
                  id,
                  value: values[key],
                  // DOM maxLength counts UTF-16 units. Validate support's 2000
                  // Unicode characters separately, without clipping emoji text.
                  maxLength:
                    key === 'comment'
                      ? isSupport
                        ? 4000
                        : 2000
                      : key === 'email'
                        ? 254
                        : key === 'phone'
                          ? 32
                          : 200,
                  'aria-invalid': touched && !!errors[key],
                  'aria-describedby':
                    touched && errors[key] ? `${id}-error` : undefined,
                  onChange: (
                    event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>,
                  ) => {
                    setValues((v) => ({ ...v, [key]: event.target.value }))
                    setConfirmed(false)
                  },
                }
                return (
                  <label key={key} htmlFor={id}>
                    {labels[key]}
                    {key === 'name' && ' *'}
                    {key === 'comment' ? (
                      <textarea {...props} required={isSupport} rows={4} />
                    ) : (
                      <input
                        {...props}
                        type={
                          key === 'email'
                            ? 'email'
                            : key === 'phone'
                              ? 'tel'
                              : 'text'
                        }
                        required={key === 'name'}
                        autoComplete={
                          key === 'name'
                            ? 'name'
                            : key === 'email'
                              ? 'email'
                              : key === 'phone'
                                ? 'tel'
                                : key === 'organization_name'
                                  ? 'organization'
                                  : 'address-level2'
                        }
                      />
                    )}
                    {touched && errors[key] && (
                      <span id={`${id}-error`} role="alert">
                        {errors[key]}
                      </span>
                    )}
                  </label>
                )
              })}
            {touched && errors.channel && (
              <p role="alert" className="inline-error">
                {errors.channel}
              </p>
            )}
          </fieldset>
          <section aria-label="Политика до отправки">
            <a href="/privacy-policy" target="_blank" rel="noopener noreferrer">
              Открыть политику обработки персональных данных
            </a>
            {policy.status === 'loading' && (
              <p role="status">Загружаем политику…</p>
            )}
            {(policy.status === 'error' ||
              (policy.status === 'success' &&
                consent.data &&
                policy.data.consent_version !== consent.data.version)) && (
              <p role="alert">
                Политика недоступна или документы изменились. Отправка
                заблокирована.
                <button type="button" onClick={refresh} disabled={!editing}>
                  Обновить документы
                </button>
              </p>
            )}
            {policy.data && !policy.data.collection_enabled && (
              <p role="status">
                Приём новых заявок временно недоступен. Политику можно прочитать
                ниже.
              </p>
            )}
            {policy.data && (
              <details>
                <summary>Прочитать политику и порядок обращения</summary>
                <PrivacyPolicy policy={policy.data} />
              </details>
            )}
          </section>
          <section
            className="consent-panel"
            aria-label="Согласие на обработку данных"
            aria-busy={consent.status === 'loading'}
          >
            <h2>Согласие на обработку данных</h2>
            {consent.status === 'loading' && (
              <p role="status">Загружаем действующий текст…</p>
            )}
            {consent.status === 'error' && (
              <p role="alert">
                Не удалось загрузить согласие. Отправка заблокирована.{' '}
                <button
                  type="button"
                  className="text-button"
                  disabled={!editing}
                  onClick={refresh}
                >
                  Повторить загрузку согласия
                </button>
              </p>
            )}
            {consent.data && (
              <>
                {consent.data.version.startsWith('SYNTHETIC-') && (
                  <p className="inline-error">
                    Только синтетическое демо. Этот текст не утверждён юристом;
                    не вводите реальные персональные данные.
                  </p>
                )}
                <p>Версия: {consent.data.version}</p>
                <p>Цель: {consent.data.purpose}</p>
                <p className="consent-text">{consent.data.text}</p>
              </>
            )}
            <label className="check-label">
              <input
                type="checkbox"
                disabled={!editing || !documentsReady}
                checked={accepted}
                onChange={(event) => {
                  setAccepted(event.target.checked)
                  setConfirmed(false)
                }}
              />
              Я прочитал(а) текст и явно принимаю согласие этой версии
            </label>
            {touched && !accepted && (
              <p role="alert">Необходимо явно принять действующее согласие.</p>
            )}
          </section>
          {isSupport && (
            <section aria-label="Окончательное описание">
              <h2>Окончательное описание проблемы</h2>
              <p className="consent-text">
                {values.comment || 'Описание пока не заполнено.'}
              </p>
            </section>
          )}
          <label className="check-label">
            <input
              type="checkbox"
              disabled={!ready}
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            {isSupport
              ? 'Подтверждаю окончательное описание проблемы, контакты и отправку обращения'
              : 'Подтверждаю показанные позиции, количества, предварительные суммы и введённые данные'}
          </label>
          {touched && !confirmed && (
            <p role="alert">Подтвердите показанную заявку.</p>
          )}
          <p>
            Неизвестные цены и доставка уточняются менеджером. Отправка не
            означает договор, счёт или оплату.
          </p>
          <button className="primary-button" disabled={!ready} type="submit">
            Подтвердить и отправить заявку
          </button>
        </form>
      </section>
    </main>
  )
}
