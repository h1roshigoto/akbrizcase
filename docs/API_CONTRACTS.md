# MVP-002 — API-контракты и клиентские типы

Основание: `ARCHITECTURE.md` v2.1, §5.1–5.4, §6.1–6.4 и карточка
MVP-002. База PR: `e30e0df380c13f3b713e97163e8270f78d513855`.
Архитектура и план не изменены. Этот PR содержит DTO и чистые функции;
business endpoints, хранение ключей, HMAC, БД, миграции и UoW не реализованы.

## Файлы и DTO

| Модуль backend | Контракты |
| --- | --- |
| `app/schemas/common.py` | Строгая база DTO (`extra=forbid`), UUID, UTC, Money, токены, ErrorEnvelope/ErrorDetail, Pagination/KnowledgePagination/Page, OrderHeaders/ChatHeaders |
| `app/schemas/catalog.py` | EquipmentModel, EquipmentModelsPage, CatalogQuery, CatalogCard (без description/attributes), CatalogItem, CatalogPage |
| `app/schemas/sources.py` | SourceRef, Applicability, KnowledgeQuery/KnowledgeHit/KnowledgePage |
| `app/schemas/consent.py` | ConsentText, ConsentAcceptance |
| `app/schemas/orders.py` | Contact, OrderItemInput, OrderCreate, OrderReceipt |
| `app/schemas/chat.py` | Пять вариантов ChatInput, ChatButton, четыре типа ChatAction и их payload, ChatReply, ChatSessionCreate/Created, ChatMessageCreate/Result |
| `app/schemas/admin.py` | OrderStatusChange, OrderStatusResult |
| `app/contracts/canonical.py` | parse_order_json, canonical_order_body |
| `app/contracts/export_types.py` | Экспорт TypeScript и проверяемых клиентских fixtures |

Клиентский вход: `frontend/src/api/index.ts`. Типы находятся в `types.ts`,
единые исходные примеры — в `fixtures.json`, их сгенерированная версия с
`satisfies` — в `fixtures.ts`. Backend валидирует **этот же JSON** и проверяет,
что оба сгенерированных файла актуальны. `contracts.typecheck.ts` содержит
отрицательные проверки типов через `@ts-expect-error`.

После изменения схем или примеров:

```sh
cd backend
uv run --locked python -m app.contracts.export_types
uv run --locked python -m app.contracts.export_types --check
```

Генератор сохраняет дискриминируемые union для chat и отдельно отражает
межполевые правила OrderCreate: support требует comment и пустой items,
commercial — непустой items, source=chat требует session ID, source=site
запрещает это поле. OrderSubmission связывает source=chat с ChatHeaders.
Числовые диапазоны, форматы строк, уникальность позиций и другие межполевые
инварианты всегда проверяются backend; TypeScript не заменяет валидацию API.

## Представление данных

- Входные и выходные модели строгие. Неизвестные поля запрещены и во вложенных
  моделях. Строки не превращаются в числа, bool — в int, `1` — в `true`.
- UUID в JSON — строка с дефисами. Каноническое представление — lowercase;
  принимается также uppercase. Номер receipt — положительное целое из sequence.
- Деньги — неотрицательные decimal-строки с максимум двумя знаками после точки;
  JSON number, exponent, NaN/Infinity запрещены. В Python допустим Decimal,
  сериализация всегда даёт строку с двумя знаками. Неизвестная цена/сумма — null.
  Ненужная доставка — `not_required`/`"0.00"`, требующая уточнения —
  `pending_quote`/null; неизвестная общая сумма остаётся null.
- `price`, `image_url`, `applicability`, поля сумм receipt и страницы SourceRef
  **обязательны**, даже когда равны null. Массивы обязательны и всегда массивы.
  В странице обязательны items/total/limit/offset. Значения по умолчанию относятся
  к query: limit=20, offset=0; для knowledge limit=5 (его максимум — 5).
- contact.email/phone, organization_name, city, коммерческий comment могут
  отсутствовать или быть null. Хотя бы один контактный канал обязателен.
  Support.comment обязателен, не null и содержит непробельный текст.
  source=site требует **отсутствия** chat_session_id, в том числе запрещает null;
  source=chat требует UUID. Предложенный comment_draft и reason могут быть
  пропущены/null; закрытие/отмена требуют содержательный reason.
- accepted и confirmed — только явное true. Текст не обрезается и не заменяется:
  `.strip()` используется исключительно для проверки содержательности.
- Токен — 32 случайных байта в lowercase hex (64 символа). Тип SecretToken
  создаётся клиентской функцией `secretToken`; это проверка кодирования,
  не генератор случайности и не проверка права доступа. UUID не является токеном.
  Токены передаются только в предусмотренных полях/заголовках, никогда в URL.
- Обязательны Idempotency-Key и X-Request-Token. Для chat-запросов и
  source=chat дополнительно обязателен X-Chat-Token. DTO заголовков валидируют
  выделенную проекцию заголовков, а не все технические HTTP-заголовки.
  Будущий endpoint обязан проверить эту зависимость и владение/срок сессии.
- SourceRef.url — только `/api/v1/documents/{document_id}/versions/{version_id}/file`
  с ID **того же** SourceRef. Страницы либо обе null, либо 1 ≤ from ≤ to.
  ImageURL — только `/api/v1/catalog/items/{id}/image/{key}` своего элемента;
  ключ — 1–128 букв/цифр/`_`/`-`, опционально `.jpg`/`.jpeg`/`.png`.
  Внешние URL, query, fragment, traversal и кодированные сегменты отвергаются.
  ACL, существование раздела в версии и актуальную публикацию проверяет сервис.
- unknown applicability всегда имеет sources=[]; confirmed может иметь
  sources=[] при внутреннем основании. Это не подтверждение доступности файла.
- ErrorDetail.code допускает расширяемые UPPER_SNAKE_CASE коды; HTTP-статусы
  и безопасное содержание details определяет будущий endpoint по §6.1.

Нормативные пределы: query/chat text ≤1000, contact.name ≤200,
email ≤254, phone ≤32, comment/reason/draft ≤2000, items 1–50 уникальных
для commercial и пустой для support, qty 1–999, revision >0,
limit ≤100 (knowledge ≤5), offset ≤10000. Телефон допускает `+`, цифры,
пробелы/скобки/дефисы и 7–15 цифр. Email проверяется как адрес, не display name.
Дополнительные явные границы строк DTO: code/tree_key/option_id ≤100,
названия/sku/unit/tax_note/версии/разделы/город/организация ≤200,
description/excerpt/reply ≤10000, текст согласия ≤100000,
attributes ≤200 ключей. Checksum представлен lowercase SHA-256 hex;
сверку с опубликованным текстом выполняет сервис согласий.
Общий лимит HTTP body, ACL, текущие catalog revision/consent version,
допустимость кнопки в текущем состоянии и перехода статуса — проверки будущих
сервисов, а не доказанные этим PR бизнес-свойства.

## Канонизация POST /orders, версия 1

Будущий HMAC получает **байты**, возвращённые `canonical_order_body` для
проверенного `OrderCreate`. Сервис не должен логировать эти байты: они содержат
контакты. Функция не вычисляет и не хранит ни HMAC, ни обычный хэш ПДн.

1. Исходный UTF-8 JSON разобрать через `parse_order_json`: повторяющиеся имена
   ключей на любом уровне и нестандартные NaN/Infinity запрещены. Затем провести
   структурную валидацию DTO; lone surrogates запрещены как невалидный UTF-8.
   Проверка текущих цен/версии согласия сюда не входит.
2. Получить `model_dump(mode="json", exclude_unset=True)`. UUID нормализуются в
   lowercase. Все явно переданные допустимые поля сохраняются. Пропуск optional
   поля и явный null **различаются**, в том числе внутри contact. Значения по
   умолчанию не добавляются. Не применять exclude_none/exclude_defaults.
3. Ключи всех объектов рекурсивно сортируются лексикографически по Unicode
   code point. JSON выводится без отступов/пробелов-разделителей: `,` и `:`.
   JSON escapes кавычек, обратной косой черты и control characters — стандартные
   Python json; остальные Unicode-символы выводятся напрямую (`ensure_ascii=False`).
4. Порядок **всех массивов сохраняется**. Позиции не сортируются и не объединяются:
   другой порядок items считается другим каноническим телом. Порядок блокировок
   БД по ID — отдельное требование, не порядок сериализации.
5. Текст не trim/casefold и не проходит Unicode normalization: различия пробелов,
   регистра и NFC/NFD сохраняются. Email/phone не нормализуются. Никаких float
   в POST orders нет: количества/ревизии — строгие JSON integer.
6. Результат кодируется UTF-8 без BOM и завершающего перевода строки.
   Имена/кодирование ключей JSON и пробелы вне строк исходного запроса не влияют
   на результат. Заголовки ключа/токенов и request_id трассировки в тело не входят.

Этот алгоритм закреплён тестами до реализации HMAC. Изменение этих правил
потребует явной совместимости с уже сохранёнными request_hash.

## Примеры fixtures

Все названия, UUID, описания и email в fixtures синтетические; реальных контактов,
токенов, файлов документов и технических инструкций там нет. Токены создаются
только в тестах из заведомо фиктивных нулевых байтов и не сохраняются в fixtures.

| Пример в fixtures.json | Проверяемая семантика |
| --- | --- |
| catalog_item / catalog_page | fixed product, on_request service, null image, безопасный image URL, applicability=null/unknown/confirmed без раскрытия internal-источника |
| commercial_order | Несколько позиций с expected_revision, видимое согласие/подтверждение |
| support_order / chat_order | Самостоятельное описание, items=[], site/chat |
| receipt | known_subtotal="2400.00", items_total=null, доставка/итог неизвестны |
| service_receipt / support_receipt | Известная полная сумма услуги; support с нулевой известной частью и null итогом |
| consent_text | Синтетические version/purpose/text и checksum этого текста |
| errors | STATE_CONFLICT/current_version=3, CATALOG_CHANGED, CONSENT_CHANGED, IDEMPOTENCY_CONFLICT, SESSION_EXPIRED, VALIDATION_ERROR |
| chat_messages / chat_result | Все пять inputs, кнопки, все четыре actions, редактируемый draft; нет confidence и автоматической отправки |
| knowledge_page | Точная ссылка на версию документа, null-страницы |
| admin_status_change/result | Закрытие с reason и expected_version, результат с новой version |

## AQ-01 — принято владельцем, PR #21 слит в main

Исходная §6.1 требовала 410 после стирания ПДн, хотя §10.3 удаляет
receipt_token_hash и HMAC request_hash. [Принятое AQ-01](decisions/AQ-01.md)
и уточнение §6.1 задают общую 404 `NOT_FOUND` для любого корректно сформированного токена
при сохранённом стёртом ключе; отсутствующие/некорректные обязательные заголовки
по-прежнему дают 422. Ключ остаётся UNIQUE до удаления строки; хранение
материала проверки токена после стирания не продлевается.

Владелец одобрил и слил decision PR #21 с изменениями архитектуры в main;
контракт нормативный, gate MVP-010 снят. Реализация и проверки описаны в
[MVP-010](MVP-010.md); DTO и fixtures сохраняют исходные правила канонизации.
SESSION_EXPIRED/410 для истёкшей собственной chat-сессии сохраняется.
Остальные DTO и UI-fixtures независимы.

## Проверки

В PR выполняются `make backend-test backend-lint backend-types` и
`make frontend-typecheck frontend-build`; точные результаты и ссылки на
существующие GitHub Actions приведены в описании PR. Контрактные тесты покрывают
положительные примеры §6, неизвестные поля/цену/статус, строгие числа и bool,
support.comment, decimal/null, chat inputs/actions, ссылки, пагинацию,
детерминированность канонизации и синхронизацию генерируемых файлов.

Pydantic 2.14.1 объявлен прямой зависимостью для прямых импортов. До этого эта
же версия уже была в uv.lock как зависимость FastAPI. Изменение lockfile — только
две записи корневого проекта; версии, хэши и набор установленных пакетов прежние.
Файлы инфраструктуры, main/config, workflows и миграции не менялись.
