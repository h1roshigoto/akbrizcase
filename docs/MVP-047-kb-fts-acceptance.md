# MVP-047 — приёмочный корпус знаний и измерение PostgreSQL FTS

Base: `05e5bfca847d7149eee0c2f742cfaf488fb76a50` (`origin/main`, проверен
`git fetch origin`). Ветка: `mvp-047-kb-fts-acceptance`. Exact PR HEAD и ссылки
на Actions фиксируются в Draft PR: коммит не содержит собственного SHA.

## Первичная инвентаризация (до написания кода)

| Требование MVP-047 | Существующее поведение | Существующие тесты | Пробел | Новая проверка |
| --- | --- | --- | --- | --- |
| Русские формы/терминология | stored russian vector, websearch_to_tsquery | test_kb: russian FTS; test_kb_reads: russian_acl_http | Несколько форм одного слова не измеряют релевантность | locked 20 relevant, разные потребности, hit по section UUID |
| Заголовок и текст | vector из title + text без отдельных весов | test_kb: пересчёт vector | Нет размеченных предметных вопросов | title-only/body-only, неоднозначные соседние разделы |
| Excerpt | первые 600 Unicode-символов, не окно вокруг совпадения | test_kb_reads: xss_is_exact_bounded_json | Совпадение после границы может отсутствовать в цитате | поздний термин; точное сравнение с prefix, ограничение полезности отдельно |
| Похожие источники/top-5 | ts_rank_cd DESC, section_id ASC | test_kb_reads: acl_before_page_total_and_equal_ranks | Нет locked relevance judgement | конкуренты, несколько допустимых источников, повтор/offset/равный rank |
| Anonymous ACL | MATERIALIZED fence до rank/count/page | test_kb_reads; test_acl_publication_regression: matrix | Нет общего корпуса и агрегата | полный прогон до/после скрытых конкурентов, total/IDs без изменений |
| No-match/negative | пустая page; chat no_match + specialist | test_chat_modes: no_match; test_kb_reads: empty | Нет 20 заранее описанных негативных вопросов | irrelevant/unsafe + ожидаемый no_match либо точная безопасная цитата |
| Revoke/visibility | новый read проверяет D/V; retired exact разрешён | test_kb_lifecycle; test_acl_publication_regression: linked_revoke | Нет связи с измеренным корпусом | успешный corpus hit → lifecycle → search/file/history/replay |
| Direct API vs chat | staff internal только live session; chat PUBLIC_VIEWER | test_chat_modes; test_chat_reply_projection; MVP-045 | Нет измерительного HTTP сопоставления | каждый query service/HTTP/public chat, staff-cookie не расширяет chat |
| Staff authority loss | SQL EXISTS проверяет role/active/expiry/revoked | test_kb_reads: staff_loses_access; MVP-045 | Новый corpus ещё не связан | manager/admin + logout/disable/expiry на corpus |
| Достоверность метрики | измерителя/manifest пока нет | нет | Пропуски/ошибки/дубли могут дать ложный PASS | строгий format/checksums, полный знаменатель, 16/20 vs 15/20, ACL override |
| Реальный экспертный корпус | защищённые import/review/publish CLI и content/docs.yaml | test_kb_import, test_kb_lifecycle | Нет материалов/прав/решений эксперта | отдельный protected external corpus adapter, fail closed, шаблон PENDING |

Расхождения/уточнения: ранний docstring draft_service говорит «Publication and
ACL arrive later», но lifecycle/access уже реализованы. `get_source` — внутренний
сервис, HTTP endpoint для него нет. Retired допустим по точной утверждённой ссылке,
но исключён из нового поиска. Empty knowledge API разрешён; TextInput чата
требует непустой ввод. FTS не является классификатором опасности; безопасная
дословная цитата может совпасть с unsafe query. Full-file public permission
не выводится из assistant_allowed. Ни один из этих контрактов не меняется.

## Внешние gates

```text
SYNTHETIC_CORPUS_VALIDATION: PASS
SYNTHETIC_HIT_RATE_AT_5: 18/20 = 0.90 (synthetic only)
SYNTHETIC_NEGATIVE_SAFETY: PASS
REAL_EXPERT_CORPUS: BLOCKED
REAL_TOP5_ACCEPTANCE: BLOCKED
REAL_HIT_RATE_AT_5: NOT_MEASURED
REAL_NEGATIVE_SAFETY: BLOCKED
EXPERT_REVIEW: PENDING
TARGET_STAGING: BLOCKED
PRODUCTION_ADMISSION: BLOCKED
SUBJECT_DOD: BLOCKED_BY_EXTERNAL_CONTENT_AND_EXPERT
```

Не предоставлены реальные разрешённые документы, права на полный публичный файл,
20+ предварительно согласованных relevant questions/точные разделы, 20+ negative
judgements, экспертные решения и защищённые references/checksums. Production и
target staging не используются; merge/deploy и MVP-048/049/050 не выполняются.

## Формат, блокировка и воспроизведение

`backend/tests/fixtures/kb_acceptance/` содержит original locked corpus **1.0.0**:
20 relevant, 14 irrelevant, 6 unsafe; 35 разделов, 12 редакций 11 документов.
Все материалы написаны для этого теста, не описывают настоящие модели и имеют
маркировку **SYNTHETIC ONLY**. Контрольные права, эксперт и review существуют
исключительно как явно синтетические fixtures; реальные согласования отсутствуют.

Manifest SHA-256: `0ff0a4340c785a16fbb869361e748c8106f2e03382e83c6a169062c765686b7b`.
Questions + ожидаемые ответы SHA-256: `8f86f13f9d5b8e68388ed73d635f4a19a9314e413b9dc130aeb69e394596c46a`.
Sections SHA-256: `73c588e0ec5ac4544ce7becd119f68d7bc089551196a79d378125e2829e247ae`.

`manifest.sha256` закрепляет точные UTF-8 bytes manifest; manifest закрепляет
точные bytes queries/sections и каждого исходного `.txt`. Проверка отклоняет
неполный набор, повторные JSON keys/query IDs/нормализованные вопросы, неизвестные
или запрещённые expected keys, неправильный document/version mapping, несвежие
checksums и отсутствие synthetic labels. `content/docs.yaml` остаётся техническим
индексом, не разрешением публикации; закрытый корпус туда не добавляется.

`make kb-acceptance` вызывает штатный `scripts/db_integration.py --suite
kb-acceptance`: собственные random secrets, свежие маркированные PG16/files,
owner marker, настоящие Alembic upgrade, runtime/migrator roles и обычные UoW.
Доменный DraftEditService создаёт разделы из проверенных настоящим ClamAV файлов;
LifecycleService проверяет synthetic review и публикует/retire/revoke. Только
rollback-only hidden-competitor fixture устанавливает publication state напрямую
в уже guarded synthetic DB; это не операторский обход review. Runtime-приложение
и его schema/FTS/публичные DTO не меняются. Acceptance package входит только
в integration image, не в production runtime image.

Для каждого вопроса выполняются настоящий KnowledgeReadService, HTTP knowledge
API и HTTP chat через COMMIT + projector. Две сессии по 20 сообщений соблюдают
существующие лимиты. Empty/whitespace отдельно дают empty knowledge page, но
422 в chat TextInput. No-match внутреннего ModeRouter содержит «или выберите
другой режим»; публичный projector использует более короткое предложение.
Измеритель проверяет обе существующие точные формы, safe handoff и whitelist
действий; свободный технический текст, чужие sources/actions/buttons дают FAIL.

Private `measurement.json` (0600) в собственном synthetic files volume содержит
полный logical-key → section/document/version UUID mapping, реальные top-5 и
окружение. Общий `KB_ACCEPTANCE_RESULT` и CI `kb047-aggregate.json` содержат
только allowlisted aggregate и IDs/логические ключи **synthetic** вопросов.
Exact HEAD/dirty записываются runner; CI требует clean exact expected SHA.
Приватный input внешнего корпуса никогда не передаётся в pytest/stdout.

## Правила метрики и фактический исход

Hit@5 равен 1, только если первые пять **разрешённых section UUID** содержат
хотя бы один заранее размеченный expected section UUID. Совпадение document UUID
само по себе недостаточно. Знаменатель — все locked relevant вопросы, даже при
ошибке/пропуске. Порог вычисляется целыми числами: `hits * 5 >= count * 4`,
minimum hits = ceil(4n/5); 16/20 PASS, 15/20 FAIL, 16/21 FAIL. MRR@5 — только
дополнительная диагностическая метрика. Ни rank, ни HitRate не являются
вероятностью/уверенностью ответа и не включаются в DTO.

Первый полный валидный локальный прогон: **18 hits, 2 misses, 90%, MRR@5 0.875**;
ожидаемых привязок relevant вопросов — **21** (R11 имеет два допустимых раздела).
20/20 negative safe, 0 ACL incidents; 16 интеграционных cases PASS, без skip.
Один case выполняет все 40 вопросов, а не один случай вместо полного корпуса.

| Query ID | Класс / подкласс | Expected sections | Первый релевантный / hit@5 | Safety |
| --- | --- | --- | --- | --- |
| R01 | relevant / direct | indicator | 2 / 1 | PASS |
| R02 | relevant / wordform | filters | 1 / 1 | PASS |
| R03 | relevant / plural | passport | 1 / 1 | PASS |
| R04 | relevant / declension | passport | 1 / 1 | PASS |
| R05 | relevant / word-order | journal | 1 / 1 | PASS |
| R06 | relevant / short | humidity | 1 / 1 | PASS |
| R07 | relevant / natural | passport | — / 0 | PASS |
| R08 | relevant / title | receipt | 1 / 1 | PASS |
| R09 | relevant / body | units | 1 / 1 | PASS |
| R10 | relevant / similar-titles | pressure | 1 / 1 | PASS |
| R11 | relevant / multiple-sources | packing, packing_alt | 1 / 1 | PASS |
| R12 | relevant / competition | journal | 1 / 1 | PASS |
| R13 | relevant / abbreviation | dictionary | 1 / 1 | PASS |
| R14 | relevant / bounded-term | safety | 1 / 1 | PASS |
| R15 | relevant / current-edition | current | 1 / 1 | PASS |
| R16 | relevant / manual-judgement | transport | 1 / 1 | PASS |
| R17 | relevant / two-terms | storage | 1 / 1 | PASS |
| R18 | relevant / noise-resilience | dispatch | 1 / 1 | PASS |
| R19 | relevant / exact-source | late | 1 / 1 | PASS |
| R20 | relevant / vocabulary-gap | label | — / 0 | PASS |
| N01 | irrelevant / unknown | no_match | — / — | PASS |
| N02 | irrelevant / unknown | no_match | — / — | PASS |
| N03 | irrelevant / off-topic | no_match | — / — | PASS |
| N04 | irrelevant / off-topic | no_match | — / — | PASS |
| N05 | irrelevant / nonsense | no_match | — / — | PASS |
| N06 | irrelevant / nonsense | no_match | — / — | PASS |
| N07 | irrelevant / similar-no-answer | no_match | — / — | PASS |
| N08 | irrelevant / unconfirmed | no_match | — / — | PASS |
| N09 | irrelevant / internal-only | no_match | — / — | PASS |
| N10 | irrelevant / revoked-version | no_match | — / — | PASS |
| N11 | irrelevant / revoked-document | no_match | — / — | PASS |
| N12 | irrelevant / draft-only | no_match | — / — | PASS |
| N13 | irrelevant / retired-only | no_match | — / — | PASS |
| N14 | irrelevant / assistant-excluded | no_match | — / — | PASS |
| N15 | unsafe / acl-injection | no_match | — / — | PASS |
| N16 | unsafe / unauthorized | no_match | — / — | PASS |
| N17 | unsafe / repair | no_match | — / — | PASS |
| N18 | unsafe / unsupported-safety | no_match | — / — | PASS |
| N19 | unsafe / unconfirmed-compatibility | compatibility | — / — | PASS |
| N20 | unsafe / safe-quotation | safety | — / — | PASS |

R07 — естественный вопрос с дополнительными лексемами: websearch AND не находит
все слова. R20 — «шильдик» не введён в словарь, размеченный «бирка» не находится.
Эти два промаха сохраняются в знаменателе. Возможные будущие улучшения: отдельно
экспертно утвердить предметный словарь/границы и заголовок, затем новая версия
manifest с previous digest, revision_reason и новым внешним review. В этой работе
не менялись вопросы, заголовки, словарь, rank или отбор после измерения; дельта
качества корпуса не заявляется.

R19 находит правильный раздел по полному тексту, но термин находится после
600 символов. Hit@5 засчитывается по точному определению; это **не доказательство
полезности excerpt**. Реальный эксперт обязан проверить фактическую цитату и
границы, прежде чем признать её пригодной. N19/N20 честно находят безопасные
разрешённые цитаты; safety не сводится к total=0 для любого unsafe вопроса.

## ACL, ошибки и первоначальные неудачи

Проверяются hidden high-density конкуренты для всех relevant запросов: до/после
одинаковые public top-5/total, repeated read и offset; отдельные настоящие
равные ранги упорядочены section UUID, включая шестой результат и пустую страницу.
Manager/admin internal читается только живой staff-сессией; public chat даже
с этой cookie его не получает. После revoke session/expiry/disable права исчезают.
Corpus содержит draft, retired, revoked version/document и assistant_allowed=false.
Действительные старые file URLs и exact source повторно проверяют текущий ACL.
После реального lifecycle visibility/version revoke/document revoke search/file,
сохранённая history и replay скрывают исходную цитату. Исключения PostgreSQL
проверяются настоящим division-by-zero: сервис бросает DBAPIError, HTTP даёт 503,
успешной пустой страницы нет. Старые подробные матрицы MVP-022/023/034/045 сохранены.

Все первоначальные неудачи отделены от PASS:

1. Стандартный integration image построен, но распаковка PG16 bookworm закончилась
   Docker VFS ENOSPC до тестов. Local infrastructure BLOCKED — не PASS.
2. Локальная flattened composition первоначально не имела integration target/CMD;
   исправлено только в `/tmp`. Production/CI Dockerfile guards не ослаблялись.
3. Первый исполнившийся suite: 2 FAIL / 12 PASS, corpus не завершён. Создание 40
   сессий достигло штатного quota; lifecycle test повторно использовал уже скрытый
   документ. Исправлены только harness sessions/независимые документы, без изменения
   лимитов или locked вопросов.
4. Следующий полный run: 18/20, но safety FAIL (2/20), overall FAIL. Измеритель
   ошибочно требовал внутреннюю длинную no-match формулировку от HTTP projector.
   Учтены обе существующие точные формулировки; отдельный unit доказывает этот
   контракт. Исходные вопросы/checksums и результаты FTS не менялись.
5. Следующий полный run: 16/16 cases PASS, все 40 проверены, 18/20 и 20/20 safety.
6. Начальный unit run имел 3 FAIL (две поверхности ACL-инцидента считались одной;
   некорректный SourceRef отвергался уже DTO; external checksum не канонизирован).
   Исправлены test inputs/ожидания. Initial full unit collection также обнаружил
   одинаковые basenames unit/integration; unit переименован, исключений не добавлено.
7. Первый локальный MVP-045 regression не собрался из-за 0700 permissions bind-mounted
   content/trees. Добавлено только чтение несекретных локальных fixtures, повтор
   запускается с прежними assertions/timeouts.

Local PG16.13 Alpine + flattened **настоящий** integration image используются
из-за 8 GiB VFS. Read-only mounts дают текущий checkout tests/package; сохраняются
штатные роли, миграции, synthetic markers, AV/parser и DB/HTTP deadlines. Это
локальный результат, а не доказательство bookworm/exact-HEAD CI. Volumes сохранены;
удалялся только созданный этой задачей image/build cache. Нет destructive reset,
down -v, downgrade/stamp или force-push.

## Реальный корпус и внешнее решение

[Шаблон](templates/MVP-047-expert-acceptance.md) — PENDING, не свидетельство.
`backend/kb_acceptance/external.py` — отдельный read-only операторский adapter:

```sh
cd backend
uv run --locked python -m kb_acceptance.external
# Без входов: BLOCKED, NOT_MEASURED, exit 2; БД не открывается.
uv run --locked python -m kb_acceptance.external \
  --packet /protected/corpus/packet.json --report /protected/reports/result.json
```

Это процедура для разрешённого изолированного операторского контура после
проверки прав/внешних решений, не разрешение использовать production-данные сейчас.
Production image этот пакет не содержит. Protected packet — строгий JSON ≤1 MiB,
owner-only 0600, regular file, без symlinks/hardlinks, с checked_record finalizer.
Он содержит `manifest`, `queries`, `sections`, `mapping`, `decision` по строгим
моделям adapter. Mapping включает logical section key → document_code,
document_id, document_version_id, section_id. UUID сверяются с настоящими FK,
версией, checksum, visibility/status, полным текстом/страницами/assistant_allowed;
FilePipeline.prepare заново проверяет фактические bytes и production AV.
Исходные материалы импортируются заранее только обычным защищённым CLI, с реальными
внешними review и full-file rights; adapter ничего не публикует и не seed-ит.

External JSON использует canonical UTF-8 JSON (`sort_keys=True`, без нормализации,
`ensure_ascii=False`, separators comma/colon) для questions/sections/manifest и
mapping digests. Decision должен заранее привязывать exact manifest и UUID mapping,
полные relevant/negative ID sets и expected keys. Один status `expert-approved`
или шаблон без decision не даёт PASS. Shipped synthetic packet после переименования
status также отвергается: software fixture не становится реальным согласованием.
Программа проверяет форму и привязку, **не истинность прав/квалификации эксперта**;
оператор должен сверить защищённые references с реально принятыми решениями.

Измерение использует один PostgreSQL 16 REPEATABLE READ / READ ONLY snapshot,
PUBLIC_VIEWER и тот же FTS/ModeRouter. Ошибка сохраняет BLOCKED/NOT_MEASURED с
закрытым error code, без traceback/SQL/input в stdout. Private report создаётся
эксклюзивно (0600) в owner-only каталоге вне checkout, только после finalizer/UoW.
В stdout лишь отдельные real gates; реальные question IDs/имена/цитаты/материалы
не экспортируются. HTTP-контракт external adapter не объявляет измеренным:
его отдельно проверяет обязательный synthetic HTTP suite. Успех предметной
приёмки не открывает PRODUCTION_ADMISSION или TARGET_STAGING.

Нужны от заказчика: реальные разрешённые файлы с checksum; права на полный файл
и доступные старые редакции; ручные проверенные sections/assistant_allowed;
20+ заранее согласованных уникальных вопросов и точные ожидаемые section UUID;
20+ отрицательных judgements; review цитат/границ/безопасности, эксперт/дата и
защищённые references, привязанные к фактическим digest; разрешённый target staging
и отдельный последующий допуск MVP-050. Их отсутствие не компенсируется 90% synthetic.

## Проверки и передача

Новый suite включён ровно один раз в `integration-ci`, исключён из core для
устранения дублирования; сохраняются все восемь CI jobs и их checkout exact HEAD.
Timeouts CI/DB/HTTP не увеличены. CI inventory сохраняет все 40 outcomes внутри
корпусного теста плюс собственные cases; aggregate проверяется runner отдельно.
Окончательные SHA, PR и ссылки/результаты восьми jobs обновляются в Draft PR без
самоссылочного коммита. Merge/deploy не выполняются.

| Фактически выполнено локально | Результат |
| --- | --- |
| Frontend typecheck/build, 219 UI tests | PASS |
| Export API contracts / TypeScript DTO --check | PASS |
| acceptance-guards | 51 PASS |
| Targeted measurement unit tests | 36 PASS |
| Полный backend test/lint/types | Финальный прогон ниже / в Draft PR |
| Первый валидный PG16 corpus suite | 16 PASS, 90%, negatives 20/20 |
| Commercial / ACL / full integration / Compose | Итоги ниже / в Draft PR; незавершённые не считаются PASS |

Логи полного нового corpus run проверяются на точные наблюдавшиеся session tokens,
пароль/login synthetic operator, все query texts и полные sections. Проверяется
также allowlisted aggregate; assertion предшествует PASS emission. В git только
вымышленные материалы, без заказчиков/экспертных ПДн, runtime secrets и сторонних
лицензируемых руководств. Это ограниченная проверка конкретных каналов/значений,
не доказательство отсутствия любой возможной утечки.

Финальные локальные проверки implementation checkout (перед commit):

| Проверка | Фактический результат |
| --- | --- |
| `make backend-test backend-lint backend-types` | **1045 PASS**, Ruff/format и strict Mypy PASS |
| `make frontend-typecheck frontend-build`, `npm --prefix frontend test` | PASS, **219 PASS** |
| API contracts export `--check` | PASS |
| `make acceptance-guards` | **51 PASS** |
| Два независимых полных свежих `make kb-acceptance` | **16/16 PASS** каждый, 18/20 = 90%, negatives 20/20, ACL 0; 41.37 s / 54.59 s runner |
| Последний corpus run с private-log/aggregate scan и DB UUID mapping guards | PASS; initial implementation HEAD + dirty=true явно отражены, exact PR HEAD проверяется отдельно CI |
| `make commercial-regression` | **64 PASS**, 99.35 s runner |
| `make acl-publication-regression` повтор | **BLOCKED ENOSPC**, 10 PASS / 55 ERROR; не PASS |
| `make config` | PASS |
| `make build` / `make smoke` | **BLOCKED ENOSPC** при слоях образов / распаковке PG16 bookworm; до этого были local buildx-cache/CA-overlay preflight отказы |
| `make integration-ci` | Clean upgrade **1 PASS**; полный dispatcher **BLOCKED ENOSPC**, остальные группы не объявляются PASS |

Полный локальный dispatcher штатно завершился ненулевым кодом с перечислением
неуспешных групп; после завершения их containers/networks остановлены штатным
SyntheticStack cleanup. Сохранились все synthetic volumes. Cache reclaim относится
только к созданным этой задачей build layers, не к данным. Все полные регрессии
остаются обязательными в стандартных GitHub Actions; локальные ограничения не
служат основанием пропускать их или повышать timeout.
