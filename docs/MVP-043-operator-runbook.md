# MVP-043 — операторский runbook

Проверен по реализации `main` на базе `a60df9e7f4caf89058987bbd0512460b7e621cff`.
Назначение: сопровождение admin и передача работы с заявками manager.
Это инструкция и synthetic rehearsal, **не разрешение production deploy/cleanup**.
Результаты на точном HEAD и CI фиксируются в Draft PR; человеческая приёмка —
в [handover checklist](MVP-043-handover-checklist.md). Инструкция менеджера:
[manager guide](MVP-043-manager-guide.md).

## 1. Перед любой операцией

1. Убедиться в правильности хоста, checkout, Compose project и роли. Работать из
   корня репозитория, если явно не указан `backend`. Получить индивидуальный
   защищённый терминал; отключить shell tracing, запись терминала и screenshots.
2. Сверить согласованное окно работ, точный Git HEAD/manifest, ожидаемую схему,
   наличие пригодного backup и ответственного для эскалации. Пока назначение или
   необходимая инфраструктура не подтверждены, зависимая операция **BLOCKED**.
3. Пароли вводить только в скрытые интерактивные prompts. Не передавать их через
   argv, pipe, env, shell history, ticket, Git или лог. Для интерактивных CLI не
   использовать `exec -T`. Не запускать команды с `--actor-id`, force/skip-флагами.
4. Использовать ниже только IDs своего synthetic стенда. `DOCUMENT_UUID`,
   `VERSION_UUID`, `TREE_UUID`, `FULL_SHA` — placeholders, их заменяют фактическими
   техническими значениями; это не готовые к запуску идентификаторы.
5. После действия проверить commit и бизнес-постусловие в свежем чтении. Exit 0
   отчёта диагностики означает получение отчёта, а не исправность всех источников.
   Не считать прошлую квитанцию текущим успехом. При неопределённом commit не
   повторять операцию с новым ID; сначала установить итог штатным повтором.

Безопасная исходная проверка dev-Compose:

```sh
git rev-parse HEAD
docker compose ps
curl --fail http://127.0.0.1:8080/readyz
docker compose exec api python -m app.core.operations
```

`/readyz` проверяет доступность БД, revision/права и конфигурацию; 503 не раскрывает
SQL. Защищённый отчёт требует active admin и даёт только агрегаты. Отсутствие
runtime journal/backup receipt отображается unavailable/not_configured, не PASS.
Нормативные контракты: [MVP-040](MVP-040-safe-observability.md).

## 2. Release, миграция, restart и rollback

**Раздел относится к сокращённому synthetic профилю MVP-041.** Он разрешает
каталог и staff authentication, но отключает создание/replay заявок, knowledge,
chat, файлы, SMTP и почти все admin mutations. Его нельзя использовать для
полной staging-приёмки остальных разделов. Полная инфраструктурная приёмка
остаётся **BLOCKED**; не включать режимы редактированием `modes.json`.

Нормативная процедура/границы: [MVP-041](MVP-041-release.md). На **чистом checkout**:

```sh
make release-test
make release-rehearsal RELEASE_REHEARSAL_ARGS='--root /tmp/akbriz-release-review'
```

Root должен быть новым, вне Git; controller создаёт private 0700 namespace,
secrets, локальный TLS и immutable manifests. Повторный rehearsal использует новый
root. Не печатать его содержимое. Самоподписанный localhost TLS не доказывает
production TLS. После репетиции сервисы остановлены, volumes сохранены.

Для уже подготовленного root:

```sh
python3 scripts/release.py check --root /tmp/akbriz-release-review --manifest /tmp/akbriz-release-review/first.json
python3 scripts/release.py apply --root /tmp/akbriz-release-review --manifest /tmp/akbriz-release-review/candidate.json
python3 scripts/release.py rollback --root /tmp/akbriz-release-review --manifest /tmp/akbriz-release-review/first.json
```

`check` проверяет preflight/образы, **не выполняет миграцию и не доказывает readiness
живой БД**. Проверить поля manifest: полный commit, schema, backend/frontend
content-addressed image IDs либо registry digests и PostgreSQL digest; mutable
tag не принимается. Сверяются OCI revision, backend schema constant и PG16.
Текущий линейный Alembic head: `0011_mvp039_http_security`.

`apply`: lock → preflight/images → PostgreSQL → остановка Nginx/API/worker →
одноразовый migrator → runtime-role/schema probe → запуск → verified HTTPS smoke →
durable acceptance. Обычный запуск API/worker не мигрирует БД. При ошибке миграции
не запускать новый API вручную: получить фиксированный stage/error code,
сохранить старый manifest и передать владельцу. После quiesce controller оставляет
приложение остановленным. Ошибка до quiesce может оставить прежние сервисы работающими;
не интерпретировать blocked attempt как их успешное обновление.

Ожидаемый результат — `accepted-local-rehearsal`, все проверки пройдены. При
прерывании `in-progress`, старый accepted receipt или `RELEASE_BLOCKED_*` не PASS.
Отсутствие secrets/TLS/digest, чужой namespace, unsupported mode, плохая схема,
недоступная БД, провал migrate/probe/smoke либо неподтверждённая совместимость — BLOCKED.

Dev-Compose имеет отдельный маршрут, не смешивать его с `compose.release.yml`:

```sh
make build
docker compose stop nginx api worker
make migrate
# Только после успешной миграции:
make up
curl --fail http://127.0.0.1:8080/readyz
```

`make migrate` запускает отдельный migrator, допускает повтор upgrade до head.
При ошибке оставить приложение остановленным и эскалировать; не обходить revision
проверку. Stop/start/restart сохраняют выбранный код, сами по себе не откатывают
релиз. Rollback выбирает **ранее принятый manifest** в том же namespace, не мигрирует,
требует совпадения фактической схемы и снова выполняет readiness/HTTPS smoke.
Cross-schema rollback без отдельного доказанного контракта запрещён. Репетиция
меняет packaging одной версии кода; совместимость с произвольной старой версией
из этого не следует. `alembic downgrade`, `stamp`, ручной SQL-reset и удаление
volumes не являются стандартным восстановлением.

## 3. Сотрудники и отзыв сессий

Нормативно: [MVP-004](MVP-004.md), реализация `app.core.cli`.

```sh
docker compose exec api python -m app.core.cli staff-bootstrap
docker compose exec api python -m app.core.cli staff-create --role manager
docker compose exec api python -m app.core.cli staff-create --role admin
docker compose exec api python -m app.core.cli staff-disable --user-id STAFF_UUID
```

Bootstrap — только первоначальная пустая инициализация: отсутствие **всех** admin,
включая disabled, и отсутствие bootstrap audit. Потеря последнего admin не открывает
bootstrap. Создание/отключение требуют скрытого login/password существующего active
admin, проверяемого в БД; передача UUID оператора не даёт полномочий. Новый пароль
индивидуальный, от 12 символов, с подтверждением; его не сохраняют вместе с UUID.

После `success UUID` проверить вход нового сотрудника через HTTPS и его права.
Перед отключением своего/последнего admin создать и проверить второго. После
disable все старые сессии этого сотрудника должны перестать работать; повторный
login также запрещён. Проверять старую сессию свежим запросом, не видимой старой
страницей. Audit сохраняет actor/техническое действие, без login/password.

Неверные credentials/disabled → authentication_denied, manager → forbidden,
повтор bootstrap → bootstrap_closed. Exit 1 — отказ, ошибка ввода/БД; 2 — неверные
аргументы. При недоступном non-echo вводе отменить. При БД/schema error не обходить
проверку; при неизвестном исходе проверить состояние перед повтором. Если active
admin больше нет — эскалация владельцу, штатного bypass восстановления нет.

## 4. Документы: импорт → review → публикация → отзыв

Нормативно: [MVP-021](MVP-021.md), [MVP-023](MVP-023.md), ACL MVP-022.
Нужны runtime DB, active admin, свежий подписанный CVD release, утверждённые права
на контент и private operator inputs вне Git. Файлы JSON: строгий формат, владелец
runtime UID 10001, mode 0600, regular file без symlink/hardlink; контейнерный
каталог монтируется read-only через `compose.kb.yml`. Не писать в files volume.

В подготовленной Python 3.12 среде из `backend` (индивидуальный скрытый ввод):

```sh
uv run --locked python -m app.modules.kb.cli kb-import --file /operator/synthetic.txt --manifest /operator/import.json
uv run --locked python -m app.modules.kb.cli kb-check --manifest /operator/import.json
uv run --locked python -m app.modules.kb.cli kb-preview --document DOCUMENT_UUID
# Получить фактическое экспертное согласование точного содержания, затем:
uv run --locked python -m app.modules.kb.cli kb-review --document DOCUMENT_UUID --version VERSION_UUID --evidence /operator/review.json
uv run --locked python -m app.modules.kb.cli kb-publish --document DOCUMENT_UUID --version VERSION_UUID
uv run --locked python -m app.modules.kb.cli kb-preview --document DOCUMENT_UUID
```

Эквивалент в Compose, пути `/operator` из `KB_OPERATOR_INPUT_DIR`:

```sh
docker compose -f docker-compose.yml -f compose.kb.yml -f compose.clamav.yml run --rm kb kb-preview --document DOCUMENT_UUID
```

Порядок приёмки:

1. Импортировать только разрешённый PDF/TXT/Markdown с полным manifest MVP-021.
   `IMPORTED_DRAFT` не публикует; `PREPARATION_INCOMPLETE` — незавершённая подготовка,
   exit 1. Не продолжать как будто sections готовы.
2. `kb-check`: `READY_FOR_MANUAL_REVIEW`. Preview показывает версии, checksum,
   file_binding/sections digests, зависимости; это техническая проверка, не mutation
   permission. Открыть полный файл и все фрагменты в защищённом контуре.
3. Эксперт реально согласует конкретную редакцию. Перенести фактические реквизиты
   в private evidence по MVP-023, связав точный version/checksum/file/sections.
   Не выдумывать имена, даты или reference ради evidence. Тестовые записи строго
   помечены SYNTHETIC ONLY и непригодны для реальной публикации.
4. Review и publish повторяют проверки. Clean ClamAV и `assistant_allowed` не
   заменяют согласование и **не дают права публично раздавать полный документ**.
   Public требует отдельного full-file разрешения. Изменение байтов/разделов после
   review блокирует publish и требует нового настоящего review.
5. Проверить свежий поиск/точный source и файл: public доступен анонимно в разрешённом
   объёме, internal анонимно скрыт общей 404/исключён из total. Staff-cookie не
   расширяет права публичного ассистента. Не сохранять внутренние тексты в отчёт.

Перед lifecycle изменением выполнить preview и оценить manual links, applicability
и tree dependencies. Срочный revoke не откладывается до завершения сессий:

```sh
uv run --locked python -m app.modules.kb.cli kb-visibility --document DOCUMENT_UUID --visibility internal
# Обратное раскрытие только с отдельным full-file evidence ВСЕХ published/retired:
uv run --locked python -m app.modules.kb.cli kb-visibility --document DOCUMENT_UUID --visibility public --evidence /operator/release.json
uv run --locked python -m app.modules.kb.cli kb-retire --document DOCUMENT_UUID --version VERSION_UUID
uv run --locked python -m app.modules.kb.cli kb-revoke --document DOCUMENT_UUID --version VERSION_UUID
uv run --locked python -m app.modules.kb.cli kb-revoke --document DOCUMENT_UUID
```

Retired исключена из обычного поиска, но точная старая ссылка может оставаться
разрешённой по ACL. Revoke версии/документа терминален: новые чтения старого URL
запрещены, зависимости теряют допустимость, applicability permission не
восстанавливается автоматически. Уже скачанные байты отозвать нельзя. Ошибка
review/scan/ACL/audit/commit — отказ, не SQL-исправление; выяснить код, исправить
вход и повторить штатный сервис. Новая редакция требует нового draft/review.

## 5. YAML-деревья и зависимости

Нормативно: [MVP-027](MVP-027.md), [MVP-028](MVP-028.md).
В той же защищённой среде `backend`, без обхода авторизации:

```sh
uv run --locked python -m app.modules.kb.cli tree-check --file /operator/synthetic.yaml
uv run --locked python -m app.modules.kb.cli tree-import --file /operator/synthetic.yaml --git-commit FULL_SHA
uv run --locked python -m app.modules.kb.cli tree-check --tree TREE_UUID
uv run --locked python -m app.modules.kb.cli tree-preview --tree TREE_UUID
# После реальной экспертной проверки всех путей и evidence:
uv run --locked python -m app.modules.kb.cli tree-publish --tree TREE_UUID --file /operator/synthetic.yaml --evidence /operator/tree-review.json
uv run --locked python -m app.modules.kb.cli tree-retire --tree TREE_UUID
uv run --locked python -m app.modules.kb.cli tree-restore --tree TREE_UUID --file /operator/synthetic.yaml
uv run --locked python -m app.modules.kb.cli tree-revoke --tree TREE_UUID
```

Check YAML проверяет schema, безопасный parser, связи, отсутствие циклов и
недостижимых узлов. Draft/check не согласование. Эксперт проверяет текст, каждый
путь, безопасность, источники и рекомендации; evidence связывает tree UUID,
**точные YAML bytes checksum и binding**, реальный review reference/имя/aware time.
Нужны опубликованные public источники с assistant_allowed, доступная модель и
допустимая confirmed применимость рекомендаций. Internal evidence применимости
не открывает internal source дерева; наружу не переносить внутренние реквизиты.

После publish проверить preview и допустимое чтение/диагностику в полноценном
synthetic стенде. После revoke следующий запрос/история/replay не должны раскрывать
отозванный контент. Revoked restore запрещён. Restore допускает только retired
с исходным YAML, первоначальным publish audit и всеми текущими валидными основаниями;
сохраняет прежнее согласование, не создаёт новое. На ошибке graph/source/evidence
остановиться: не обходить dependency checks через SQL. Срочный tree-revoke не
требует исправлять повреждённый граф или получать новое review.

## 6. Изображения и применимость в admin UI

Нормативно: [MVP-025](MVP-025.md), [MVP-026](MVP-026.md). Нужны active admin,
HTTPS/CSRF, подписанный CVD release и private files. Manager получает 403.

1. В `/admin/catalog/items/ITEM_UUID` открыть **Manage image**. Загрузить synthetic
   JPEG/PNG через форму. Максимум 5 MiB, сторона 4096 px, 16 млн pixels; SVG/GIF/
   анимация/URL-import не разрешены. Сервер проверяет формат, AV исходника,
   декодирует/пересоздаёт без metadata в sandbox и сканирует результат.
2. После успешного redirect перечитать карточку: новая revision и публичное
   изображение. Replace выполняется той же формой; проверить новый URL и 404
   прежнего. Невалидная загрузка/scan/revision/commit error сохраняет старую картинку.
3. Remove снимает связь; проверить placeholder/image_url=null и старый URL 404.
   Физические файлы остаются private, не удалять их вручную.
4. Archived item read-only и не виден публично; его публичный image URL даёт 404.
   Admin preview при действующих полномочиях может показывать сохранённое изображение.
5. Через **Manage applicability** привязать точный model/section, выполнить preview,
   получить отдельное фактическое экспертное review и разрешение факта публикации.
   На public карточке проверить confirmed; internal evidence даёт `sources=[]`.
   Выполнить preview/revoke, проверить unknown и отсутствие внутренних ссылок.

При stale revision обновить форму и заново оценить изменение. Не менять image_key,
approval или public_allowed прямой записью. Generic image CRUD и запись в volume
не поддерживаются. Сбой AV не повод временно ослаблять форму.

## 7. ClamAV signature release

Нормативный источник: [MVP-020, «ClamAV и базы»](MVP-020.md#clamav-и-базы).
Пока нет назначенного оператора и доверенной production доставки — **BLOCKED**.

1. На доверенной операторской машине получить официальные подписанные CVD по TLS
   с проверкой сертификата из согласованного канала. Приложение не выполняет
   freshclam; Git/CI не являются каналом production signatures.
2. В новом каталоге проверить **ровно** main.cvd, daily.cvd, bytecode.cvd:

   ```sh
   sigtool --info /protected/cvd-new/main.cvd
   sigtool --info /protected/cvd-new/daily.cvd
   sigtool --info /protected/cvd-new/bytecode.cvd
   sha256sum /protected/cvd-new/main.cvd /protected/cvd-new/daily.cvd /protected/cvd-new/bytecode.cvd
   ```

   Каждый sigtool: exit 0 и `Verification OK`. Сверить корректность часов и
   подписанное Unix-время заголовка daily: возраст ≤72 ч, будущее ≤5 минут.
   mtime не доказательство свежести; loose `.ndb/.hdb/.cld`, symlinks и лишние файлы
   не принимаются. Без подписи/свежести не переключать release.
3. Закрепить проверенные файлы в новом immutable каталоге; сохранить версии,
   SHA256 и время в защищённом реестре. Активный каталог на месте не обновлять.
   Указать `CLAMAV_RELEASE_DIR` и пересоздать потребителей с read-only mount:

   ```sh
   export CLAMAV_RELEASE_DIR=/protected/cvd-new
   docker compose -f docker-compose.yml -f compose.clamav.yml up -d --force-recreate api worker
   ```

4. На допущенном **полном synthetic стенде** проверить clean и стандартный
   synthetic infected образец через файловый конвейер: clean даёт техническую
   подготовку, infected отклоняется без публикации/повреждения старой связи.
   Локальная тестовая `.ndb` в `make files-smoke` проверяет реальный движок и sandbox,
   но не production подпись. Пока реальная поставка не проверена — BLOCKED.
5. При отсутствующих/просроченных базах заблокировать импорт/выдачу, восстановить
   проверенный свежий release, пересоздать consumers и повторить проверки.
   Нельзя skip-scan или считать отсутствие баз успешным сканированием.

Не применять этот Compose overlay к сокращённому release profile MVP-041 и
самостоятельно не включать там файловый режим.

## 8. Privacy cleanup и deletion journal

Нормативно: [MVP-037](MVP-037.md), [MVP-038](MVP-038.md),
[journal protocol](MVP-038-journal-protocol.md). **Production full delete всё ещё
недоступен:** только APP_ENV demo/test **и owner marker**, существующий synthetic
harness, private 0700 sink, active admin и migrator. Независимый production sink
не утверждён; CLI возвращает `INDEPENDENT_JOURNAL_REQUIRED`. Не снимать этот gate.

1. До удаления ответственный вне приложения проверяет заявителя, полномочия,
   основание, объект, исключения/hold и все последствия для consent/snapshots/
   outbox/backup. Manager не принимает и не исполняет такое техническое решение.
2. Для automatic cleanup применим immutable packet **собственной** consent version,
   не latest/default; неизвестная policy/terminal date/binding → review. Открытые
   заявки и pending/sending требуют review, не придуманного TTL.
3. Manual full delete использует внешний неперсональный decision UUID и явно
   выбранную journal policy; она определяет только journal horizon. Legacy
   collection policy не выдумывается. Из `backend`, **только synthetic harness**:

   ```sh
   uv run --locked python -m app.modules.orders.deletion_cli ORDER_UUID --decision-id DECISION_UUID --journal-policy-version JOURNAL_POLICY_VERSION
   ```

   Пароли и подтверждение `DELETE ORDER_UUID` вводятся скрыто. Migrator secret и
   SYNTHETIC_DELETION_JOURNAL_DIR предоставляет подготовленный harness, не manager.
4. Последовательность обязательна: committed PREPARED → внешний durable export/
   read-back witness → новая авторизованная mutation UoW → отдельное чтение
   applied_at **и postcondition**. При sink outage не выполнять mutation и не
   создавать фиктивный success audit/receipt.

| Результат | Действие оператора |
| --- | --- |
| PREPARED | Команда сохранена; ещё не удаление. Проверить sink и тот же command ID |
| WITNESSED | Обязательство durable, возможна частичная порция; reconciliation тем же UUID/bytes |
| APPLIED + commit_confirmed | Проверены applied_at и постусловие; передать результат ответственному |
| UNCONFIRMED | Commit мог состояться. После восстановления доступа повторить тот же target/decision/command |
| SINK_UNAVAILABLE | Не менять БД, восстановить sink и проверить exact witness |
| CONFLICT | Остановить и эскалировать; не переписывать immutable bytes |
| REVIEW_REQUIRED / LOCAL_MISSING | Разбор policy/authority/completeness; не ручной SQL и не «успешная очистка» |

Worker не исполняет manual full delete. Restore_missing — только существующий
opt-in migrator seam в закрытом synthetic restore, не discovery всех команд.
Отсутствие объекта не разрешает удалить другой. Witness не удаляют по возрасту;
applied_at не внешний terminal ack. Не вводить auto pruning или новые retention
TTL. **ADR-038-A остаётся PROPOSED**; полное удаление, legacy, exceptions, service
binding, production sink и age-based TTL host logs требуют отдельных решений.

## 9. Backup и закрытое восстановление

Единый маршрут — [MVP-042](MVP-042-backup-restore.md), включая P1-01 high-water gate:

```sh
make backup-rehearsal BACKUP_REHEARSAL_ARGS='--report /tmp/mvp042-measurements.json'
```

Требуется чистый checkout. Harness строит exact-source tool image, отдельные
PG16/files/secret volumes и внутреннюю сеть **без API/worker/SMTP/ingress**;
создаёт реальный encrypted age backup и новую чистую restore-среду. Сервисы после
проверки остановлены, volumes сохранены. Файл отчёта содержит только агрегаты;
archive/dump/manifest/identity/runtime logs не публикуются.

Контрольные точки оператора:

1. Backup: один exported PG snapshot для dump и ссылок; публикация/смена файлов
   сериализованы. Encryption identity/ключи отдельно от архива, не потерять старый
   recovery key при ротации. Synthetic local store не offsite backup.
2. Restore: authentication age, exact HEAD/image/schema, manifest и checksum всех
   файлов/sidecars, новая пустая БД и private files. Провал после import оставляет
   quarantined target; для повторной попытки требуется новая чистая среда.
3. Получить **независимый** полный deletion journal/checkpoint после последних
   source deletions; проверить epoch/count/digest/fence, replay v1/v2 и postconditions.
   Неполный/конфликтный/недостоверный checkpoint блокирует готовность.
4. High-water нужен сверх snapshot floor: номер мог быть выдан **после** backup.
   Требуется ранее инициализированная external authority и проверенный живой
   source fence. Нет authority/доверенного maximum, потерян source/epoch/fence —
   BLOCKED. Нельзя вывести исторический максимум из surviving rows или написать
   его вручную в JSON. Счётчик назад не переводят.
5. Проверить FK/CHECK, lifecycle/ACL, сохранность support.comment после удаления
   чата, отсутствие удалённых ПДн и non-reuse номеров. Обязательно:
   `smtp_disabled=true`, `new_order_admission=false`, `offsite=BLOCKED`,
   `target_rpo_rto=BLOCKED`. Synthetic-ready-for-manual-review не открывает пользователей.

При ошибке backup/restore нет PASS: оставить target закрытым, сохранить защищённое
evidence, передать владельцу инфраструктуры/ключей. Не удалять последний пригодный
backup или witnesses. Synthetic timing **не доказывает production RPO≤24 ч/RTO≤4 ч**.
RF offsite storage, независимый journal/authority, custody, ≤24h schedule/watchdog,
retention и измеренный target drill остаются внешними BLOCKED.

## 10. SMTP retry и аудит

Нормативно: [MVP-018](MVP-018.md), [manager guide](MVP-043-manager-guide.md).
В карточке заявки manager/admin использует **Повторить уведомление**:
`POST /admin/orders/{id}/retry-notification` через штатную CSRF/Origin форму.
Не копировать cookie/token в curl и не править outbox SQL.

| Состояние | Значение / действие |
| --- | --- |
| pending | Ожидает worker; ручной retry запрещён |
| sending | Есть lease; recovery принадлежит worker, даже при истечении lease |
| sent | SMTP принял письмо; это не доказательство прочтения, retry запрещён |
| failed | Автоматические попытки исчерпаны; после проверки причины разрешён retry |
| Строка отсутствует | 404; retry не создаёт outbox заново |

После retry перечитать карточку: pending/уже sending/sent допустимы, заявка и её
номер/снимки не меняются, новая заявка не создаётся. Статусная история отдельно от
доставки: retry не переход статуса заявки. Сервис пишет один `notification.retry`
audit на прежний outbox UUID; audit проверяет уполномоченный в защищённом контуре,
UI менеджера не предлагает произвольную выгрузку audit. 409 требует обновить
состояние; 401/403 — переавторизация/проверка полномочий; 503 — технический сбой,
сначала проверить результат, не выполнять серию кликов.

**At-least-once SMTP:** crash после SMTP acceptance до фиксации sent может дать
два экземпляра одного письма. Exactly-once не обещается. Оператор и заказчик должны
отдельно принять это ограничение в checklist; тест механизма не является принятием.

## 11. Инциденты

Источник сигналов — защищённый `python -m app.core.operations`, не public debug.
Дополнительно можно передать `--runtime-journal /protected/runtime.jsonl` и
`--backup-receipt /protected/backup.json`, см. MVP-040/042. Journal — bounded
приложенческие JSON lines без Compose prefix; доступ только оператору. Не включать
SQL echo, DEBUG, SMTP debug/access logs, не выгружать environment/secrets.

| Инцидент / признаки | Безопасные шаги | Проверка / эскалация |
| --- | --- | --- |
| DB unavailable: readiness 503, DB_UNAVAILABLE/DB_ERROR, нет отчёта | Остановить публикацию/миграции; проверить `docker compose ps`, readiness и фиксированные runtime сигналы. Не выдавать SQL/DSN/raw driver errors | После штатного восстановления повторить readyz/report. При утрате/повреждении данных — закрытый backup drill §9; владельцу инфраструктуры, открытие доступа только отдельным решением |
| Low disk/full: DISK_LOW/DISK_UNAVAILABLE, ошибки PG/files/backup/journal | `df -h`; учесть PG, files, backup, журналы могут быть на разных mounts. Report измеряет только APP_FILES_DIR; порог <100 MiB либо <5%. Приостановить новые загрузки/backup attempts, сохранить witnesses | Не удалять active volumes, PG-файлы, последний backup или deletion witnesses; не запускать prune/reset. Владелец инфраструктуры увеличивает ёмкость/согласует безопасное освобождение; затем проверка report/backup/readiness |
| SMTP backlog/worker: OUTBOX_PENDING_OVER_24H, OUTBOX_FAILED/DELIVERY_ERROR, WORKER_TICK_FAILED | Проверить counts/oldest pending и runtime sample worker/SMTP. Отделить сохранённую заявку от недоставленного письма; заявку повторно не создавать. Проверить процесс и согласованную SMTP конфигурацию, затем штатный retry только failed | Отчёт не даёт age каждого failed: не выдумывать такую метрику. Отсутствие наблюдений не healthy. Передать оператору SMTP/владельцу, контролировать counts/age после восстановления; SQL outbox mutation запрещена |
| ClamAV unavailable/stale: FILE_ERROR, SIGNATURES_* / SCANNER_* | Остановить файловые действия; проверить подпись/свежесть/часы/mount по §7, восстановить проверенный release | Clean/infected через pipeline перед возобновлением; без баз/доставки BLOCKED. Не skip-scan и не переключать reduced profile |

При недоступном journal sink запретить зависимое удаление и эскалировать владельцу
sink. Ответственных/канал/время реакции заполняет владелец в checklist. Пока они
не назначены, организационная готовность BLOCKED; фиктивных контактов здесь нет.

## 12. Воспроизведение и передача

```sh
make acceptance-guards
make operator-rehearsal
```

Нужны Docker Compose, достаточный диск и чистый checkout. Новый smoke использует
`db_integration.run`/SyntheticStack: случайный project, test marker, отдельные
volumes/secrets и ownership до mutation. Первое построение tests image, затем
отдельные БД staff/KB/tree/image/SMTP/privacy/diagnostics, затем **тот же** encrypted
restore drill MVP-042. Команды ограничены timeout, volumes автоматически не удаляются.
SMTP только localhost synthetic sink во внутренней сети; production credentials
не принимаются. Test-only ClamAV composition запускает реальный движок/sandbox;
production-класс не принимает synthetic `.ndb`.

Smoke обязан выполнить negative tests: denied/failed/skipped требования не дают
общего PASS. Вывод — закрытые stage codes и `OPERATOR_EVIDENCE` с HEAD и внешними
BLOCKED, child logs не артефакты. CI сохраняет все прежние шесть jobs и добавляет
операторский. Полная матрица/точные результаты и обнаруженные дефекты — в Draft PR.

После automation другой человек проходит §§2–11 на допущенном полном staging,
а менеджер — отдельный guide. Фиксируются роль, дата, exact HEAD/profile, операция,
постусловие, безопасный ID evidence и итог PASS/FAIL/BLOCKED/NOT RUN. Это не запуск
production, не автоматическое закрытие внешних gate и не начало MVP-044.
