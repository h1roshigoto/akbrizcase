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

`backend/tests/fixtures/kb_acceptance/` содержит текущий locked corpus **1.1.0**:
20 relevant, 14 irrelevant, 6 unsafe; 35 разделов, 12 редакций 11 документов.
Все материалы написаны для этого теста, не описывают настоящие модели и имеют
маркировку **SYNTHETIC ONLY**. Контрольные права, эксперт и review существуют
исключительно как явно синтетические fixtures; реальные согласования отсутствуют.

Manifest SHA-256: `903071740ae8518e36ae051115dfed99beb1cbd3a49f6a24be4541f1790d797d`.
Questions + ожидаемые ответы SHA-256: `6bacd2ef6c6260aa3c7e61fe781f3395ce6b1d97b641c487a3f2d5e36e57c747`.
Sections SHA-256: `73c588e0ec5ac4544ce7becd119f68d7bc089551196a79d378125e2829e247ae`.

`manifest.sha256` закрепляет точные UTF-8 bytes manifest; manifest закрепляет
точные bytes queries/sections и каждого исходного `.txt`. Проверка отклоняет
неполный набор, повторные JSON keys/query IDs/нормализованные вопросы и перестановки одного word bag, неизвестные
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

Полный локальный прогон текущей версии 1.1.0: **18 hits, 2 misses, 90%, MRR@5 0.875**;
ожидаемых привязок relevant вопросов — **21** (R11 имеет два допустимых раздела).
20/20 negative safe, 0 ACL incidents; 16 интеграционных cases PASS, без skip.
Один case выполняет все 40 вопросов, а не один случай вместо полного корпуса.

| Query ID | Класс / подкласс | Expected sections | Первый релевантный / hit@5 | Safety |
| --- | --- | --- | --- | --- |
| R01 | relevant / direct | indicator | 2 / 1 | PASS |
| R02 | relevant / wordform | filters | 1 / 1 | PASS |
| R03 | relevant / plural | passport | 1 / 1 | PASS |
| R04 | relevant / declension | passport | 1 / 1 | PASS |
| R21 | relevant / word-order | journal | 1 / 1 | PASS |
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
manifest с previous digest, revision_reason и новым внешним review. Заголовки, словарь, rank и отбор не менялись. Единственное исправление
набора вопросов связано с обнаруженным дублированием поисковой потребности,
описано ниже и не основано на успешности нового запроса.

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
| Targeted measurement unit tests | 39 PASS |
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
| `make backend-test backend-lint backend-types` | **1048 PASS**, Ruff/format и strict Mypy PASS |
| `make frontend-typecheck frontend-build`, `npm --prefix frontend test` | PASS, **219 PASS** |
| API contracts export `--check` | PASS |
| `make acceptance-guards` | **51 PASS** |
| Два независимых полных свежих `make kb-acceptance` версии 1.0.0 (до ручного обнаружения дубля) | **16/16 PASS** каждый, 18/20 = 90%, negatives 20/20, ACL 0; 41.37 s / 54.59 s runner |
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


## Исправление валидности корпуса 1.0.0 → 1.1.0

В финальной ручной проверке выявлено: R05 «наблюдений журнал» и R12 «журнал
наблюдений» используют один набор слов и близкую потребность. Автоматический
валидатор 1.0.0 проверял нормализованный порядок слов, но не их перестановку.
Первоначальные 90% **не считаются окончательной приёмкой**: выявлен дефект
измерительной валидности P2, исправленный в этом PR.

Исходный полный корпус/результат сохранены в Git commit
`89cd7e231da5a60825be6130bebe48ecfb228ae6`, manifest
`0ff0a4340c785a16fbb869361e748c8106f2e03382e83c6a169062c765686b7b`.
`git show 89cd7e2:backend/tests/fixtures/kb_acceptance/manifest.json` восстанавливает
его точные bytes. Новый manifest **1.1.0** содержит `previous_corpus_sha256`
и причину изменения; checksums источников/sections не менялись.

R05 выведен из текущего набора. Новый ID **R21**: «индикации описание», отдельная
потребность о поле описания видимой индикации в записи журнала. Он закреплён
в manifest **до своего первого FTS измерения**. Теперь валидатор также отклоняет
одинаковый word bag при перестановке слов; отдельный отрицательный unit test
это проверяет. Семантическую уникальность всё равно обязан проверять эксперт.

Первый полный свежий прогон 1.1.0: **16/16 cases PASS, 18/20 = 90%,
20/20 negatives, 0 ACL incidents**, MRR@5 0.875, runner 52.54 s. R21 — позиция 1,
всего 3 разрешённых совпадения. Промахи R07/R20 сохранены. Дельта HitRate — **0**;
исправление устраняет повторное измерение близкой потребности, не подбирает вопросы
для роста процента. Это по-прежнему только SYNTHETIC ONLY. Второй/CI прогон на
точном commit текущей версии и полный eight-job результат фиксируются в Draft PR.

## Exact-HEAD delivery evidence

[Draft PR #60](https://github.com/h1roshigoto/akbrizcase/pull/60) содержит актуальный
final HEAD, таблицу восьми jobs и ссылки на фактические run/job URLs. Начальный
[workflow 42441607578](https://github.com/h1roshigoto/akbrizcase/actions/runs/42441607578)
создан для implementation commit
`1cf2cf977223b2942f0b8a77112453d15d8801c1`; последующий report-only commit не меняет
код/корпус и получает обязательный отдельный exact-HEAD CI. Его окончательные
conclusions публикуются в PR без дальнейшего изменения проверенного HEAD.
Незавершённый или отменённый workflow не считается PASS.

Clean exact-HEAD локальный повтор implementation commit выше:
`dirty=false`, PostgreSQL 16.13, corpus **1.1.0**, **16/16 PASS**, полный 20+20,
18/20 = 90%, MRR@5 0.875, negatives 20/20, ACL 0; runner **50.61 s**. UUID mapping
разрешён заново на собственном fresh stack. В сочетании с первым 52.54 s прогоном
это два независимых полных измерения версии 1.1.0. Синтетический score не закрывает
внешние экспертный, правовой, staging и production gates.

Первый actual exact-HEAD CI
[42441647350](https://github.com/h1roshigoto/akbrizcase/actions/runs/42441647350)
на `e3885d454b9299c054ad2cab5269341cd3eb3a91` завершился отказом сборки нового
integration package: `backend/.dockerignore` использовал deny-all allowlist и
не включал `kb_acceptance`. DB suite не запускался; operator job отказал на той
же сборке. Это P2 packaging defect, не PASS. Local bind mount раньше обходил
build context и потому не выявил его. В allowlist добавлен ровно пакет с
исключением pycache/pyc. Отдельная настоящая Docker COPY-сборка этого context
прошла, затем полный стандартный CI повторяется на новом exact HEAD.

Уточнение упаковки: API runtime image не содержит acceptance package. Помимо
integration target, существующий одноразовый backup target копирует весь
разрешённый backend context; это не production API и не новая точка доступа.


Отчёт дополнен точными полями §9: `total_relevant`, `hit_at_5_count`,
`miss_at_5_count`, `hit_rate_at_5`, `total_negative`, `negative_safe_count`,
`negative_unsafe_count`, `failed_query_ids`, `http_error_count`, `db_error_count`,
`query_error_count`, `missing_result_count`, `measured_corpus_versions` и
`measured_schema_revisions` (настоящий SELECT из app.alembic_version).
Сервисные/HTTP ошибки учитываются для каждого вопроса и не исключаются из
знаменателя. Инъекция настоящего PostgreSQL division-by-zero отдельно требует
40 DB errors / 20 relevant / FAIL, а не успешные пустые результаты.

После добавления полей полный corpus run: **16 PASS**, 18/20, 20/20 safety,
0 HTTP/DB errors, runner 51.30 s; corpus **1.1.0**, SHA неизменён. Measurement
units: **39 PASS**. Packaging исправлен настоящей сборкой context; следующий
[workflow 42441796251](https://github.com/h1roshigoto/akbrizcase/actions/runs/42441796251)
подтвердил сборку и ряд jobs, но superseded последним изменением отчётности и
не заменяет полный exact-HEAD прогон. Актуальный окончательный run/job URLs —
в таблице [PR #60](https://github.com/h1roshigoto/akbrizcase/pull/60).

Последующий exact-HEAD
[workflow 42442016852](https://github.com/h1roshigoto/akbrizcase/actions/runs/42442016852)
на `a2da814c6c0e0d385b358fe6b7645e20b1d9dd23`: **7 jobs SUCCESS, PG job FAIL**.
Backend 1048, frontend 219, Compose, оба linked browser E2E, backup/restore,
release/operator rehearsals прошли. Каждый browser run подтвердил все 15
checkpoints и тот же HEAD. В PG job прошли clean upgrade, core (955 outcomes,
включая прежние отдельные skips), images 58, lifecycle 43, applicability 75,
trees 136, chat 174, HTTP security 11, commercial 64, ACL/publication 65 и files 119.

Отказ нового suite произошёл **после успешного exit pytest**, в строгом
`record_verbose`: `--capture=tee-sys` выводил JSON посередине строки текущего
verbose test ID, поэтому число разобранных ID не совпало с summary. Этот run
не считается PASS, даже при успешном pytest. Исправлен только capture:
`-rP --show-capture=stdout` сохраняет обычные ID/outcome строки и выводит закрытый
synthetic aggregate после завершения cases; captured runtime logs не печатаются.
Строгая сверка inventory не ослаблена. CI timeout остался 20 минут; отказ не был
превышением лимита.

Локальный повтор с **реальным `CI_INVENTORY_DIR`**: runner **57.81 s, PASS**;
`db-kb-acceptance.json` содержит ровно **16 PASSED identities**, отдельный
`kb047-aggregate.json` — все 40 вопросов, 18 hits, 20 safe negatives, 0 HTTP/DB errors,
правильный manifest digest. Это воспроизводит ранее не проверявшийся CI inventory
path, а не заменяет его отсутствием переменной. Новый полный exact-HEAD workflow
обязан подтвердить все восемь jobs; final HEAD/conclusions/URLs остаются в PR.
