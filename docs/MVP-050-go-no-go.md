# MVP-050 — финальная приёмка и go/no-go

**PUBLIC_LAUNCH_RECOMMENDATION=NO-GO. HUMAN_GO_NO_GO_DECISION=PENDING.
PRODUCTION_ADMISSION=BLOCKED.** Техническое завершение этого PR не означает
выполнения полного предметного DoD MVP-050. Codex не разрешает публичный запуск.

База: `f3589ebb275bf2033518882a40165725dc4275be`, merge PR #63 (MVP-049).
Exact HEAD этой реализации фиксируется в единственном PR и воспроизводится через
`git rev-parse HEAD`; коммит не содержит собственного SHA. Перед подготовкой
конкретного candidate человек отдельно обновляет `origin/main` и сверяет base.

## Readiness-модель и область доверия

[Инструмент](../scripts/mvp050_readiness.py) читает только локальные Git metadata
и выбранный защищённый пакет. Он не подключается к production DB или сети,
не вызывает Docker/deploy/release apply, не импортирует контент, не создаёт
согласования, не меняет режимы/права и не удаляет данные/containers/volumes.
Он переиспользует `PrivateInput`, bounded JSON parser, closed-schema validator,
canonical SHA-256 и clean Git lineage из MVP-049; MODES/SCHEMA из MVP-041;
полный `MVP048_RESULT/v2` validator из MVP-048. Второго release controller нет.

Результат `MVP050_RESULT/v1` разделяет:

- `TECHNICAL_READINESS_CHECK=PASS`: формат, локальный clean HEAD/base, независимо
  переданные checksum pins и связи прочитанных bytes проверены этим процессом.
  Это единственный автоматический PASS; `technical_scope=PACKET_CONSISTENCY`,
  `subject` содержит exact HEAD/base, binding и canonical checklist checksum.
- `declared_status`: заявленный исход конкретного requirement, не подтверждение
  выполнения. Каждый заявленный PASS должен ссылаться на конкретное evidence;
  при отсутствии ссылки эффективный `status=INCONCLUSIVE`.
- Эффективный `status`: FAIL для обнаруженного несоответствия/сообщённого отказа,
  BLOCKED для отсутствующего внешнего prerequisite, NOT RUN для незапущенной
  проверки, INCONCLUSIVE для недоказанного события/согласования. Импортированный
  успешный CI/target report и opaque approval остаются INCONCLUSIVE: ни JSON,
  ни checksum, ни approval ID/подпись в файле не удостоверяют событие/полномочия.
- `OUT_OF_SCOPE_APPROVED`: допустимое заявленное исключение только при отдельном
  scope decision и полном списке отключений. Офлайн-инструмент не удостоверяет
  решение: эффективный статус остаётся INCONCLUSIVE/SCOPE_DECISION_UNVERIFIED.
  В независимом человеческом протоколе OOS можно поставить только после проверки
  оригинала решения и фактического отключения UI/API; исключение не называется PASS.
- `packet_completeness=COMPLETE_FOR_INDEPENDENT_VERIFICATION`: только технически
  полный набор непротиворечивых ссылок/снимков, требующий независимой проверки.
  Неподтверждённый пакет не даёт `ELIGIBLE_FOR_HUMAN_REVIEW` или GO.

Все реальные release gates в v1 остаются BLOCKED, рекомендация NO-GO, human
решение PENDING, admission BLOCKED даже у безупречного самостоятельно составленного
файла. Финальный GO возможен только после независимой проверки уполномоченными
людьми и предусмотренного релизного процесса **вне этого инструмента**. Отсутствие
доверенного внешнего verifier здесь явно сохранено; trust-флага/production switch нет.

## Закрытый release checklist

Схемы: [checklist.schema.json](../content/readiness/checklist.schema.json),
[ci-snapshot.schema.json](../content/readiness/ci-snapshot.schema.json).
Все поля обязательны; отсутствие значения — явный null. Неизвестные поля, дубли
JSON keys/IDs, неверные типы, NaN/Infinity, traversal/links и частичный список
requirements отвергаются. [Каталог](../content/readiness/requirements.json)
фиксирует **86** требований: все 52 P0/P1 DoD MVP-001–049 и существующих P0
MVP-054–056, плюс 34 финальных gate. MVP-051–053 (P2) в объём не включены.

| Раздел | Точная привязка и назначение |
| --- | --- |
| format/classification/pilot | MVP050_CHECKLIST/v1; SYNTHETIC ONLY или REAL UNVERIFIED; FULL_MVP / REDUCED_PILOT / SYNTHETIC_DEMO |
| binding | Opaque rc- + 32 hex release ID; exact 40-hex head/base; immutable backend/frontend/postgres image config digests либо null; Alembic revision; полный modes object; final content SHA-256 либо null; enabled_features |
| checks | Ровно каждый ID каталога один раз; status и nullable evidence reference. Отсутствие P0/P1 строки — отказ всего preflight |
| evidence | ev- + 32 hex ID; один requirement (или SCOPE_DECISION); scope CI_SYNTHETIC / LOCAL_SYNTHETIC / TARGET / EXTERNAL_DECISION; SHA-256 полного binding; artifact reference |
| artifacts | Уникальные opaque IDs/filenames, SHA-256 точных bytes; kind opaque / ci / load. Opaque bytes могут быть безопасной ссылкой на private original; это не создание решения |
| scope | Отдельный nullable scope decision; для каждого исключённого feature полный фиксированный список HTTP method/routes, UI surfaces, CLI/worker paths и checks, объявленных неприменимыми |
| defects | Nullable reference к независимому A-DEFECTS review; все известные entries с opaque ID, severity P0/P1/P2, OPEN/CLOSED, feature. Пустой список без triage не доказывает отсутствие дефектов |

Canonical binding: UTF-8 JSON, sort_keys, ensure_ascii=false, separators comma/colon,
allow_nan=false (`digest()` из MVP-049). Он включает release ID, HEAD/base, **все**
images, schema, modes, features и content checksum. Перепривязка только HEAD без
остальных полей не работает. Pins поступают из независимого review, их нельзя
слепо скопировать из проверяемого файла. Исторические реестры и оригиналы решений
сохраняются вне Git; новая редакция пакета требует новых независимо сверенных pins.

`images` в этой версии использует Docker `sha256:<config content ID>` как MVP-041;
при доставке через registry оператор дополнительно сверяет registry manifest
RepoDigest и OCI revision/context, сохраняет mapping в защищённом evidence.
Mutable tag и неподтверждённый mapping не заменяют exact image identity.

### CI и load evidence

`A-CI` требует kind=ci, scope=CI_SYNTHETIC. Снимок фиксирует repository/workflow,
head/binding, push в main, run ID/attempt, status/conclusion, ровно девять jobs
по действующим именам, failures_masked и все 13 Backend/PG/files inventories.
Каждый inventory фиксирует SHA-256 ожидаемого и выполненного набора identities,
expected/passed/skipped/failed/xfailed/errors. Hash наборов — canonical sorted
список test identities без outcomes; полные identities остаются в исходных
CI artifacts и сверяются через существующие `ci_inventory`/`compare_ci_tests`.
Новые инструменты тесты не собирают и не запускают повторно.

Другой HEAD/binding, PR/workflow_dispatch вместо post-merge release evidence,
частичный/отменённый run, пропущенный job/inventory не закрывают gate.
Masked failure, несовпадение identities/счётчиков, failed/error/xfail дают отказ;
любой skip требует отдельного review применимости и не считается успешным тестом.
Существующие skips не удаляются: reviewer проверяет отдельный фактический запуск
соответствующей проверки (например, clean upgrade в отдельном suite).
Даже структурно полный снимок — UNVERIFIED_CI_SNAPSHOT: источник и actual outcomes
другой человек проверяет в GitHub, включая шаги, artifacts и отсутствие continue-on-error.

`C-TARGET-LOAD` требует kind=load, scope=TARGET, полный исходный MVP048_RESULT/v2,
exact HEAD/images/schema и environment=authorized_target. Пороговые числа берутся
из существующего validator: fixed pilot200-v1, 200 клиентов, 30 источников/peak,
минимум samples, p95 диагностики ≤300 мс, поиска ≤800 мс, заявки ≤1000 мс,
без критических ошибок, наблюдённые CPU/RAM/DB/disk, backup/runtime evidence.
Подмена порогов/профиля/итогов отвергается. Synthetic environment не закрывает target.
Импортированный даже правильный target PASS остаётся UNVERIFIED_TARGET_SNAPSHOT.
Сам инструмент измерений или инфраструктурных fault injections не выполняет.

### Private inputs и безопасный stdout

Переиспользованы **те же** MVP-049 проверки: absolute root вне checkout, owner 0700;
файлы owner 0600, regular/nlink=1, O_NOFOLLOW по всем компонентам, basename ≤120;
запрещены symlink/hardlink/FIFO. Каждый файл ≤1 MiB, общий предел ≤64 MiB,
JSON depth ≤20/30000 values. FD/name/inode/mode/mtime/ctime/checksum перепроверяются
после анализа. Output-файлов инструмент не создаёт: только allowlisted stdout.

В публичном результате — фиксированные requirement IDs, статусы/коды, роли
ответственных и действия из committed каталога, head/base и binding digest.
Нет input paths/filenames, свободного текста evidence, opaque approval IDs,
контактов, подписей, токенов, private journal или исходных документов.
Errors дают только PREFLIGHT_REJECTED без traceback/exception text. Private originals,
контакты, runtime secrets и private reports остаются вне Git/CI artifacts.

## Воспроизведение без БД

Из чистого checkout Python 3.12 (зависимости не нужны):

```sh
python3 scripts/mvp050_readiness.py baseline \
  --expected-head "$(git rev-parse HEAD)" \
  --expected-base f3589ebb275bf2033518882a40165725dc4275be
```

Это начальный список **неподтверждённых условий FULL_MVP**, не результат повторного
выполнения прежних тестов. Неизвестные images/content остаются null, release ID
с нулями — явно synthetic placeholder. Baseline не читает production/private данные.

[Полный безопасный synthetic пример](../content/readiness/synthetic.checklist.json)
соответствует схеме, но имеет нулевые placeholder head/base и не является release
packet. Оператор копирует его в новый private namespace, заполняет фактические
привязки/ссылки и сохраняет исходные результаты без переименования synthetic в real.
Для подготовленного извне пакета:

```sh
python3 scripts/mvp050_readiness.py check \
  --input-root /protected/pilot-readiness \
  --expected-head REVIEWED_FULL_HEAD \
  --expected-base REVIEWED_FULL_BASE \
  --expected-sha256 INDEPENDENTLY_REVIEWED_CHECKLIST_BYTES_SHA256 \
  --expected-binding-sha256 INDEPENDENTLY_REVIEWED_CANONICAL_BINDING_SHA256
```

Имя основного файла — checklist.json. `--repo` выбирает только локальный checkout;
Git вызывается без lazy fetch, replace objects, optional writes и fsmonitor.
Exit 0 = пакет проверен, **не** admission; exit 1 = preflight отвергнут.
Readiness/рекомендацию всегда читают из результата, не из exit code.

Быстрые проверки этого PR: `make mvp050-guards`, релевантные release guards,
Ruff check/format изменённых Python-файлов, `git diff --check`. Полные integration,
operator/content/browser/Compose/load/release/backup rehearsals локально в MVP-050
не выполняются. В Backend checks добавлен только лёгкий guard step; все девять
существующих jobs и их бюджеты сохранены. После единственного финального push
независимый reviewer проверяет автоматический CI; Codex его вручную не запускает,
не ждёт завершения всех jobs и не опрашивает статусы.

## Фактическая начальная готовность

```text
TECHNICAL_READINESS_CHECK=PASS       # проверка структуры/связей конкретного пакета
RELEASE_READINESS=BLOCKED
FULL_MVP_READINESS=BLOCKED
REDUCED_PILOT_READINESS=BLOCKED
TARGET_LOAD=BLOCKED
PUBLIC_LAUNCH_RECOMMENDATION=NO-GO
HUMAN_GO_NO_GO_DECISION=PENDING
PRODUCTION_ADMISSION=BLOCKED
```

Исторически подтверждённый synthetic baseline:
[post-merge run 42962729215](https://github.com/h1roshigoto/akbrizcase/actions/runs/42962729215),
HEAD `f3589ebb275bf2033518882a40165725dc4275be`, **9/9 SUCCESS**.
В этой работе один раз прочитаны job metadata и безопасный MVP048 aggregate
[load job 130332846379](https://github.com/h1roshigoto/akbrizcase/actions/runs/42962729215/job/130332846379).
Сохранён [безопасный исторический aggregate](../content/readiness/historical-baseline.json).
Это scope HISTORICAL_CI_SYNTHETIC, valid_for_current_candidate=false.

| GitHub runner, pilot200-v1 | p95, мс | Порог, мс | Фактический исход |
| --- | ---: | ---: | --- |
| Диагностическая кнопка | 932.749545 | 300 | FAIL |
| Создание заявки | 1762.679099 | 1000 | FAIL |
| Поиск каталога | 532.598703 | 800 | PASS в synthetic scope |
| FTS знаний | 632.871447 | 800 | PASS в synthetic scope |

9 SUCCESS jobs не означают прохождение всех performance gates: существующий smoke
отдельно публикует latency outcomes. Пороги/лимиты не меняются. Эти числа не являются
измерением выбранного сервера и не закрывают TARGET_LOAD. Synthetic FTS 18/20 из
MVP-047 также не является экспертной приёмкой реального корпуса. Прежние документы
содержат исторические числа jobs; для текущего candidate обязательны все девять.
Актуального независимого triage дефектов нет; отсутствие найденного domain defect
в прежних synthetic tests не доказывает отсутствие открытых P0/P1 сейчас.

**Полная начальная таблица** со всеми 86 условиями, P0/P1 DoD, областью,
текущим статусом/причиной, ответственным, действием, зависимостью и флагом блокировки:
[MVP-050-checklist.md](MVP-050-checklist.md). Поля соответствуют машинному baseline;
роль не является назначенным человеком. Имена/даты/решения не предоставлены.
Все внешние NO-GO сохраняются, в частности:

- E-PD-OPERATOR/POLICY/RETENTION/ADR/PROCESSORS: MVP-037/038, ADR-038-A PROPOSED,
  законные основания/сроки/legacy/holds, service binding, policy/journal v2,
  host log TTL и обязательные правовые/организационные зависимости.
- B-TARGET/TLS/SECRETS/SMTP/AV/MONITORING: реальная RF инфраструктура,
  domain/TLS/firewall, роли, SMTP, signed свежие ClamAV signatures и наблюдаемость.
- B-OFFSITE/KEYS/JOURNAL/RESTORE/RPO-RTO: независимый storage и journal,
  custody/escrow/rotation, clean target restore/replay/high-water, RPO≤24h/RTO≤4h.
- B-OPERATORS/E-WALKTHROUGH/NO-HA/SMTP-RISK: реальные назначения/резерв/эскалация,
  независимый handover MVP-043, письменное принятие no HA и повторного SMTP.
- C-TARGET-LOAD: выбранный сервер и фактические performance/resource measurements.
- D-CATALOG/RIGHTS/EXPERT/TREES/FTS/TARGET: финальные права/экспертные решения,
  цены/модели, minimum/all paths, реальные 20+20 и final target checksum comparison.
- E-SCOPE/CUSTOMER/MODE-SUPPORTED: решение заказчика о полном/сокращённом пилоте
  и поддерживаемый release profile. Текущий MVP-041 reduced профиль отключает
  **новые заявки**, chat/knowledge/files/SMTP и real PII; готовым коммерческим
  пилотом он не является. FULL_MVP/реальный REDUCED_PILOT пока BLOCKED.

## Независимая полная и сокращённая приёмка

1. Release owner фиксирует exact HEAD/base и candidate ID после freeze. Другой
   reviewer проверяет Git, clean tree, OCI revision/context и actual immutable
   image IDs/registry mapping. DBA сопоставляет Alembic chain с единственной
   `0011_mvp039_http_security`, image schema и actual target revision; проверяет
   upgrade/совместимый rollback через предусмотренную процедуру. Нельзя stamp/
   downgrade/reset для сокрытия проблемы. Новый SHA или image требует новых pins.
2. Для FULL_MVP сверяют **все** P0/P1 в полном каталоге и архитектуре §§9–12,
   plan §8, критические regressions MVP-044/045, PG16, linked browser MVP-046,
   privacy/readiness/safe files и фактический CI inventory на exact candidate.
   При scope reduction безопасность, privacy, restore и защита включённой
   коммерции остаются обязательными core gates. Любой отсутствующий допуск — NO-GO.
3. Для REDUCED_PILOT заказчик отдельно утверждает исключённые features, полномочия
   согласовавшего, обязательные оставшиеся проверки и неприменимые tests.
   Отдельный человек проверяет действительное отключение каждого UI/API/worker
   пути (включая прямой вызов/replay/history/import/retry) и сохраняет evidence.
   Каталог ENDPOINTS фиксирует routes/методы и UI/CLI/worker surfaces; фактические
   результаты их проверки остаются в private scope decision. Перечень строк JSON сам по
   себе не доказывает отключения. Core requirement нельзя поместить в exclusion;
   включённый feature тоже исключить нельзя; соответствующие mode flags должны
   быть отключены и согласованы с exclusion. Если profile не поддерживается
   существующим controller — BLOCKED; расширения production modes вне MVP-050.
4. Контент принимают существующими процедурами MVP-047/049: exact source/version,
   изображение/полный public-файл, real expert/fact disclosure, published versions,
   минимум и все paths; независимое сравнение final content SHA с target,
   предварительно согласованные реальные 20+20, ≥80% top-5, safe negatives/0 ACL.
   Целевой backend/frontend/schema/modes/content должны совпадать со всем пакетом.
5. Оператор/ИБ принимают реальные инфраструктуру/AV/SMTP/monitoring/offsite/keys,
   независимый journal, clean restore и RPO/RTO. Нагрузка выполняется по MVP-048
   на выбранной машине после подготовки и отдельного разрешения владельца;
   synthetic restore или GitHub runner не заменяют target acceptance.
6. Оператор ПДн/юрист закрывают правовые зависимости; другой назначенный человек
   проходит operator walkthrough MVP-043. Заказчик/оператор сверяют originals,
   фактические события/полномочия/даты и triage. Выполняют offline check с pins;
   INCONCLUSIVE разрешают только независимой проверкой в отдельном протоколе.
7. Уполномоченные люди принимают фактическое решение вне CLI. Даже подписанный
   протокол не выполняет deploy: открытие допуска требует предусмотренного
   релизного процесса и отдельно авторизованного доступа к среде.

После изменений SHA/images/schema/modes/scope/content, policy/approvals/roles,
инфраструктуры, AV/SMTP/backup/journal или performance profile прежний отчёт не
считается текущим. Получить новые pins/evidence, повторить затронутые проверки,
нагрузку при изменении производительного профиля, независимую сверку и **весь**
offline preflight. Старый CI не является постоянным разрешением на выпуск.

## Шаблон протокола заказчика/оператора (не заполнен)

Хранить оригинал в защищённой системе заказчика; в Git допустима только согласованная
неперсональная metadata. Ниже нет вымышленных подписей или существующего approval ID.

| Поле | Заполняет уполномоченный участник |
| --- | --- |
| Решение/полномочия | Фактические участники и роли, основание полномочий; независимость проверяющего |
| Событие | Фактическая дата/время/часовой пояс; защищённое место хранения originals |
| Candidate | Release ID, exact HEAD/base, backend/frontend/PG digests, schema, modes, final content checksum, checklist/binding SHA |
| Объём | FULL_MVP / REDUCED_PILOT / закрытый SYNTHETIC_DEMO; точные исключения и отдельный scope decision |
| Проверки | Все requirement IDs, статус в действительном scope, original evidence, actual CI run/attempt/jobs/inventories, defects/triage |
| Эксплуатация/контент/privacy | Target load/resources, offsite/keys, clean restore/RPO/RTO/journal, права/experts/FTS/checksum, юридические документы и handover |
| Исключения | OOS только с проверенным решением и реально отключёнными путями; сохранённые security/privacy/restore/commerce gates |
| Риски | Письменное принятие no HA и at-least-once SMTP; сроки/операторы/эскалация |
| Итог | GO / NO-GO / PENDING, фактические основания; блокеры, владелец/действие/зависимость, условия следующего review |
| Удостоверение | Реальный способ подтверждения решений и независимой проверки по правилам заказчика; не автоматически заполненная JSON-подпись |

При NO-GO остаётся только закрытое synthetic demo без реальных пользователей/ПДн,
устранение блокеров и новый review. Календарная дата, merge этого PR, зелёный CI
и демонстрация не открывают production admission.
