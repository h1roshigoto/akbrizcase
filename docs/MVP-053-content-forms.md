# MVP-053 — защищённые формы публикации и отзыва документов

Исходная база: `adeab944e23aca5d75f76993001e1fa8689ffaf5`. Отдельная ветка
`feat/mvp-053-content-forms`; незавершённая MVP-052 не используется.
Это интерфейс над существующим lifecycle, а не новый способ согласования.
Нормативные процедуры: [MVP-021](MVP-021.md), [MVP-023](MVP-023.md),
[ACL MVP-022](MVP-022.md), [операторский runbook, §4–5](MVP-043-operator-runbook.md).

## Роли и маршруты

Все страницы — `StaffBaseView`, `required_role="admin"`. PostgreSQL staff-сессия
и активная роль проверяются общей административной границей при каждом запросе.
Manager получает 403, anonymous/disabled/revoked/expired session — 401,
включая прямые POST. POST требует стандартные same-origin Origin и CSRF;
формы дополнительно отвергают дубли, неизвестные поля, query-параметры,
несовпадающие/повторные CSRF headers. URL не принимает actor/password/force/skip.
Ответы имеют `Cache-Control: no-store` и прежние security headers.

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/admin/documents` | Документы, status, visibility |
| GET | `/admin/documents/{document}` | Версии, status, reviewer UUID, recorded/published UTC |
| POST | `/admin/documents/{document}/preview` | Технические зависимости и форма подтверждения |
| POST | `/admin/documents/{document}/versions/{version}/publish` | Проверенная публикация |
| POST | `/admin/documents/{document}/versions/{version}/revoke` | Необратимый отзыв версии |
| POST | `/admin/documents/{document}/revoke` | Необратимый отзыв документа |

GET не вызывает lifecycle и не пишет `kb.preview`. Обе таблицы имеют по 25 строк
на страницу; `offset` 0…10000. Документы сортируются по code/UUID, версии —
по убыванию номера и UUID. Запрашивается одна дополнительная строка для Next.
Идентификаторы UUID должны быть корректными и версия должна принадлежать документу.
Нет generic ModelView/CRUD immutable документов.

В HTML нет document/section title/text, raw file_key, storage paths, произвольного
JSONB или полного `LifecycleReport.technical`. Выводятся фиксированные технические
поля и выбранные UUID/counts. Реквизиты review на GET исторические, не разрешение
публикации; внешний полный экспертный протокол проверяется в защищённом CLI-контуре.

## Статусы и подготовка

Документ: `active` либо терминальный `revoked`. Версия: `draft`, `published`,
`retired`, `revoked`. Review оставляет версию draft. Импорт, AV clean и наличие
reviewed_by сами по себе не означают одобрения. Опубликовать можно draft;
повтор текущей опубликованной версии имеет штатную семантику `NO_OP`.
Retired/revoked не возвращаются в published. Новая редакция требует нового review.

1. Подготовить документ через защищённые `kb-import`, `kb-check` из MVP-043.
2. Получить фактическое экспертное согласование точных bytes/sections и права
   распространения; выполнить `kb-review` с защищённым evidence MVP-023.
3. Войти как active admin через HTTPS. В Documents открыть нужный документ.
4. Выбрать Preview publish для нужной версии. Проверить цель и зависимости,
   подтвердить операцию. Отдельное review или импорт в браузере отсутствуют.
5. Проверить технический результат и свежее чтение списка/файла по нужному ACL.

Publish вызывает `LifecycleService.execute("publish")`: active document,
статус draft/published, успешный audit review именно этой версии, совпадение
content/file/sections fingerprints, валидные экспертные реквизиты, настоящий
`FilePipeline.prepare()`/scan/parser и финальная проверка файла. Public-документ
дополнительно требует `full_file_public_allowed` в действительном evidence.
UI не изменяет visibility и не создаёт согласование. Подготовленные CLI-версии
используются непосредственно, без повторного импорта.

Прежняя published версия атомарно становится retired, зависимые разрешения
инвалидируются; затем новая публикуется. Отказ, ошибка finalizer/audit/commit
откатывает весь переход, включая автоматический retire.

## Preview и подтверждение отзыва

Preview — CSRF POST: существующий сервис создаёт `kb.preview` audit.
Параметры — `operation=publish|revoke` и необязательный `version_id`
(обязателен для publish). Используются прежние EquipmentManualDependencies,
ApplicabilityDependencies и DiagnosticTreeDependencies без изменения порядка
блокировок. Preview сервиса охватывает **все версии документа**; UI явно
предупреждает, что это консервативный список, включая исторические связи.
Показаны полные counts и первые 50 UUID каждого известного provider.
При выполнении инвалидируются только затронутые сервисом версии.

Для отзыва выбрать Preview revoke нужной версии либо whole-document revocation,
прочитать последствия и явно отметить подтверждение. Оно подписано отдельным
purpose `kb-preview`, связано с cookie сессии, document/version/operation и SHA-256
полного текущего preview. В HTML находится только digest и временный токен,
без raw cookie/payload. Срок подтверждения — 10 минут.

На финальном POST preview вычисляется заново под теми же транзакционными
блокировками. Изменившееся состояние требует нового подтверждения (`409
PREVIEW_CHANGED`, HTTP-конфликт формы, не новый lifecycle outcome). Подпись
другой операции/цели/сессии или отсутствие подтверждения — 422 INVALID_REQUEST.
Повтор уже выполненного перехода допускается до истечения подтверждения;
сам сервис определяет `NO_OP` либо отказ по текущему состоянию. Preview никогда
не отменяет повторную проверку review/ACL/file/зависимостей/роли в execute.

Revoke терминален. Applicability permissions очищаются с revision/audit в том же
UoW; позднейшая публикация не восстанавливает их. Historical equipment manual FK
остаётся. Деревья проверяют доступность источников при чтении без записи tree rows
и изменения lock order. После commit новые public и staff чтения больше не
разрешают отозванную версию/документ, включая старые exact retired links.
Уже начавшееся чтение может иметь прежний разрешённый SQL snapshot; уже скачанные
байты отозвать невозможно. Отзыв не требует scan/review и не ждёт активные диалоги.

## Транзакции, аудит и отказы

HTTP передаёт проверенную `StaffIdentity` и `request.state.staff_db` общему
`run_lifecycle_as_actor`; CLI аутентифицирует login/password и вызывает тот же
адаптер. Пароль HTTP не подставляется, не хранится и не запрашивается формами.
Общий savepoint откатывает неуспешный lifecycle до записи безопасного отказа.
Сохраняются прежние actor/entity/actions/results и допустимые audit metadata.
HTTP пишет дополнительные `kb.preview` при показе и перепроверке подтверждения;
они не являются событием успешной публикации. Логи не сериализуют payload/errors.

Бизнес-отказы сохраняют фактические коды: `REVIEW_REQUIRED`, `REVIEW_STALE`,
`PUBLIC_APPROVAL_REQUIRED`, `FILE_METADATA_CONFLICT`, `DOCUMENT_REVOKED` и другие
исходные коды lifecycle/file pipeline; 409, а `NOT_FOUND` — 404.
Успех показывает `PUBLISHED`, `REVOKED` или `NO_OP`. Ошибка SQL/файловой
инфраструктуры безопасна: SERVICE_UNAVAILABLE/503; boundary/commit failure может
вернуть исходный AUTH_UNAVAILABLE/503. Содержимое исключения не выводится.
Нельзя трактовать такой ответ как успешный переход; проверить свежий статус.

Общий staff lock удерживается до outer commit; порядок document → versions →
catalog_items → applicability сохранён. Сервис не commit-ит, новая HTTP-транзакция
с другим actor не открывается. Boundary завершает commit до выдачи ответа.
DB statement=5 s, lock=3 s, idle-in-transaction=15 s и существующие HTTP/AV/parser
лимиты не изменены. CLI по-прежнему имеет собственный локальный idle=180 s.
Scan, не уложившийся в HTTP/DB лимиты, fail closed; повторить штатный CLI-путь
после диагностики. Большой файл/production CVD могут не укладываться в HTTP;
синтетический быстрый scan не доказывает совместимость любого реального файла.

## Проверки и границы допуска

Unit-контракты проверяют подпись/истечение/смену цели и сессии, строгие поля,
пагинацию, проекцию preview, POST-only маршруты и повторы.
`tests/integration/test_admin_content.py` автоматически входит в **существующий
db:core**, без нового CI job: настоящий PostgreSQL 16, staff login/session,
ClamAV, review, публикация/retire/revoke, ошибки scan/review/commit, dependencies,
public/staff file ACL, audit CLI/UI. Synthetic operator sequence MVP-043 выполняет
CLI import/check/review и HTTP publish/revoke через настоящие сервисы.
Прежние lifecycle/visibility/tree/CLI suites сохраняются; golden MVP-047,
expert evidence и ACL fixtures не менялись.

Локально запускаются только unit, Ruff, mypy и diff checks. PostgreSQL/Docker,
браузер, operator/release/backup/load выполняются CI; результаты девяти jobs
проверяются на точном PR HEAD и приводятся в PR, без преждевременного PASS.
Реальные документы, production scan, экспертная/правовая и независимая приёмка
этой работой не подтверждаются. Нет browser upload/редактора sections/деревьев,
нового storage, analytics tables, публичного admin API или нового release процесса.
MVP-052 интегрируется отдельно: перед merge сохранить все объединённые views
в строгом тесте регистрации. Merge/deploy в этой задаче запрещены.

```text
FULL_MVP_READINESS=BLOCKED
REDUCED_PILOT_READINESS=BLOCKED
PUBLIC_LAUNCH_RECOMMENDATION=NO-GO
HUMAN_GO_NO_GO_DECISION=PENDING
PRODUCTION_ADMISSION=BLOCKED
```
