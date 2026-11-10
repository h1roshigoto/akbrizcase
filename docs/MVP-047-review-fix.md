# FIX после MVP-047: целостность evidence и внешние public-конкуренты

База: `0d2a77a1fe5aae9d6c4b805ad772e135faf4913c`, получена через
`git fetch origin main`; совпадает с исходной базой задания. Отдельный исправляющий
PR; MVP-048 не начинается. Exact HEAD и результаты восьми Actions jobs фиксируются
в PR и итоговом отчёте, чтобы документ не ссылался на собственный commit SHA.

## P1

Экспортер принимает только полный закрытый формат и все 40 outcomes locked
synthetic corpus 1.1.0. Manifest закреплён SHA-256, проверяются байты questions,
sections и исходных материалов. ID, group, subclass, expected count и возвращённые
логические section keys сверяются с этим корпусом; section должен быть разрешён.
Дубли JSON keys, неизвестные/отсутствующие поля, bool вместо int, float вместо
счётчика, NaN/Infinity и несогласованные derived values отклоняются.

Hit, первый rank и MRR пересчитываются по полному порядку top-5; denominator —
все 20 relevant, включая misses. Negative safety пересчитывается по закреплённому
negative_behavior и safe_sections. Failed IDs, все дублирующие агрегаты и totals
должны совпадать. Успешный экспорт требует complete, пустых faults, корректных
статусов, правильного corpus provenance и текущих Alembic heads.

Минимальное расширение outcomes: reply_valid/reply_resolution, флаги HTTP/DB/query
errors и отсутствующего результата, отдельные page/reply ACL counters. Это позволяет
проверять error aggregates и negative outcomes без текстов и SourceRef. Приватные
запросы, материалы, source UUID и секреты в public evidence не добавлены.

Полная валидация выполняется до печати KB_ACCEPTANCE_RESULT, записи aggregate
и сохранения pytest inventory в scripts/db_integration.py.

## P2

measure получает только внутреннюю SQLAlchemy Session внешнего адаптера;
входного множества разрешённых UUID нет. run_external передаёт ту же read-only
REPEATABLE READ session, в которой получены mapping и ответы сервисов.
Независимый SELECT соединяет section → version → document по фактическим FK,
проверяет public/active/published/assistant_allowed, точный SourceRef и excerpt.
Corpus-mapped source дополнительно обязан совпасть с исходным Binding.

Разрешённый посторонний public section остаётся конкурентом на своей реальной
позиции. В returned_sections ему соответствует null, а не удалённая позиция;
неожиданные UUID и тексты не экспортируются. Такой section может дать relevance
miss, но сам по себе не делает execution invalid. Для no_match/safe_quote он
не входит в разрешённые negative sources. Согласованность page/reply сохранена.

Подтверждённый запрещённый источник учитывается отдельно на page и reply.
Отсутствующие строки и неправильные binding/SourceRef/excerpt делают измерение
невалидным; отсутствие доказательства не объявляется подтверждённым ACL disclosure.
Обычные search/publication API, миграции и production image не изменены.

## Новые проверки и воспроизведение старых дефектов

- scripts/test_kb_acceptance_result.py: полный валидный результат и inventory
  permissions, включая создание inventory directory; группы негативных
  проверок со subtests: агрегаты, unsafe
  negative, rank, identity/schema/types, provenance/statuses, strict JSON;
  каждый отказ проверяет отсутствие stdout и файлов inventory.
- backend/tests/test_kb_acceptance_measurement.py: public competitor до/после/
  вместо expected, negative no_match/safe_quote, отсутствие независимых данных.
- PostgreSQL suite: отдельный публичный документ создаётся, проверяется и
  публикуется штатными domain services, вне locked corpus. Проверяются настоящий
  FTS и rank в read-only snapshot, позиции конкурента, negative safety, internal,
  draft, retired, revoked document/version, assistant-excluded, подмены metadata,
  UUID и excerpt, отдельные page/reply incidents. Запрещённые состояния также
  создаются domain services; constraints и immutable triggers не обходятся.

Сравнение с исходными файлами из `git show origin/main:<path>` выполнялось в
изолированных Python modules, без изменения рабочей реализации. Старый emit
**принял все 8 некорректных вариантов**: hits=20 при 15 row hits, inflated HitRate/MRR,
ненулевой DB error, неправильный corpus hash, неверные failed IDs, unsafe negative
при 20 passed, неправильный rank, подменённый query ID. Для него использовался
прежний outcome schema без новых полей, чтобы отказ из-за расширения формата
не скрывал исходный дефект. Новые негативные проверки отвергают эти расхождения.

При public competitor перед expected старый measure дал ACL=2, rank=null,
execution_valid=false; исправленный — ACL=0, rank=2, execution_valid=true.
При competitor вместо expected: старый ACL=2/invalid; новый ACL=0/valid relevance
miss. Это unit-проверка алгоритма с замоканной независимой проверкой, а не
доказательство FTS/экспертной приёмки; PostgreSQL cases проверяют настоящий SQL.

## Локальная верификация

- `UV_CACHE_DIR=/tmp/akbrizcase-uv-cache make backend-test backend-lint backend-types`:
  1054 unit tests PASS, Ruff check/format PASS, strict mypy PASS (225 files).
- `make acceptance-guards`: 57 PASS, включая расширенные exporter subtests.
- `make kb-acceptance`: попытка штатного fresh PG16 suite; Docker остановился
  до pytest с `no space left on device` при распаковке PostgreSQL image.
- `make integration-ci`: попытка полного штатного suite; Docker остановился до
  pytest с ENOSPC при сборке integration image.

В облачной локальной среде `/` имеет 7.9 GiB, Docker storage driver — vfs.
Для сборки использовались только временные CA-secret override и BUILDX_CONFIG
в /tmp; версии образов, assertions, timeouts и resource gates не менялись.
Очистка воспроизводимого build cache не устранила ограничение. Volumes не удалялись.
Полные результаты PostgreSQL и остальные regression jobs должны быть проверены
GitHub Actions на exact PR HEAD; локальные заблокированные команды не считаются PASS.

Реальные expert corpus, target staging и production admission остаются BLOCKED,
экспертное review — PENDING. Merge/deploy не выполняются; PR требует независимого
ревью до merge и post-merge CI, затем можно рассматривать MVP-048.
