# TECH-001 — ускорение CI

PR: https://github.com/h1roshigoto/akbrizcase/pull/40. База:
`d5c6a81a1806c0cfdac57851967ccf080219823d`. Merge запрещён до независимого GO/NO-GO.

## Диагностика до изменений

Успешный push baseline: [run 40566678772](https://github.com/h1roshigoto/akbrizcase/actions/runs/40566678772),
GitHub-hosted `ubuntu-24.04`, runner image `20261025.353.1`.

| Обязательный job | До изменений |
|---|---:|
| Backend checks | 46 s |
| Frontend checks | 20 s |
| PostgreSQL 16 integration | 782 s (13:02) |
| Compose build and smoke | 635 s (10:35) |

Время job считается от `started_at` до `completed_at`, без очереди.
По шагам и журналам PostgreSQL: integration 616 s, сборка runtime 15 s,
ClamAV/files 138 s. Внутри integration: core pytest 326.46 s, images
385.50 s (уже параллельно core), lifecycle 136.48 s, applicability
60.26 s, trees 26.71 s. Lifecycle/applicability/trees и последующий
ClamAV удлиняли критический путь при свободном втором слоте.

В Compose: build 26 s, smoke 87 s, worker/SMTP smoke 137 s;
два независимых guarded проекта запускались последовательно. Worker-smoke
намеренно повторяет полный smoke перед собственными assertions — оба
запуска сохраняются. Браузерные проверки используют общую NSS trust database,
поэтому их параллельный запуск не внедряется.

Backend dependency sync — 1 s, frontend sync — 2 s. Внутри Docker уже
используется обычный content-addressed build cache, повторная сборка
integration target для file acceptance использует те же исходники и слои.
Оснований усложнять CI новым межзапусковым кэшем не найдено. Кэш БД,
пользовательских файлов, секретов или сессий не добавляется.

Исходные параллельные stdout перемешивали pytest node IDs; file acceptance
использовал `-q`, frontend reporter показывал только итоговые числа. Поэтому
commit `45cba9c43ed26ef95678d8cb263d0ce81f88c93b` меняет только отчётность,
сохраняя расписание, и служит контрольным запуском для сравнения test IDs:
[run 40576199792](https://github.com/h1roshigoto/akbrizcase/actions/runs/40576199792).

## Изменения

- Вместо пяти boolean-переключателей — явные именованные PostgreSQL suites.
  `core` сохраняет прежний набор исключений; все остальные модули выполняются
  отдельно. Fresh clean-upgrade по-прежнему выполняется первым и строит образ
  текущего checkout ровно один раз.
- Общая очередь максимум из двух окружений для core, images, lifecycle,
  applicability, trees и реального ClamAV/files. File acceptance занимает один
  слот вместе со сборкой runtime. Каждый запуск получает собственные random
  project name, secrets, PostgreSQL, сети и volumes. Guard/marker/ownership
  проверки и сохранение volumes не изменены.
- Обычный Compose smoke и полный worker/SMTP smoke выполняются параллельно
  в двух отдельных проектах. Все Chromium/HTTPS suites остаются последовательными.
- PR concurrency отменяет только устаревшие запуски того же PR.
  Push на main использует уникальный `github.run_id` в группе и
  `cancel-in-progress: false`; обязательные push CI не отменяются и не
  вытесняют друг друга из очереди concurrency.
- `CI_SUITE_START/END` и GitHub Step Summary показывают длительность и результат
  каждого integration/smoke suite, включая ошибки. Ошибка любого suite делает
  job неуспешным; остальные уже выбранные независимые suites также выполняются.
- Полные pytest/Vitest IDs в журналах. Backend выполняет исходную команду без
  новых pytest options; после её успеха отдельная collection записывает IDs.
  File acceptance также сохраняет исходную команду и после её успеха записывает
  collection в том же sandbox/image. Сравнение требует совпадения количества всех passed с полным inventory и
  отсутствия skipped/failed, иначе отказывается приписывать результат IDs.
  `scripts/compare_ci_tests.py` сравнивает
  мультимножества `(node ID, outcome)`, включая параметры, дубли и existing skip,
  и статические PASS checkpoints smoke. HEAD в checkpoint нормализуется.
  Только у трёх известных backend-параметров со случайным UUIDv4 нормализуется
  случайная часть; список зафиксирован в `RANDOM_UUID_CASES`. Все остальные
  параметры и outcomes сравниваются буквально.
- Новые unit tests проверяют лимит параллелизма, передачу ошибок, отчёт ошибок
  и обнаружение подмены теста/результата при неизменном числе тестов.

Приложение, API, DB-модели, migrations, исходные test assertions и security
checks не меняются. Таймауты четырёх jobs и вложенных команд не увеличиваются.
Существующий skip clean-upgrade внутри core сохранён: тот же test ID реально
выполняется на отдельной пустой PostgreSQL перед core; новый skip не добавлен.

## Проверки и воспроизведение

Полная локальная collection: 1003 уникальных PostgreSQL IDs. Объединение
именованных suites совпадает с полной collection, плюс прежний повтор
clean-upgrade: clean 1, core 754 (753 pass + existing skip), images 58,
lifecycle 43, applicability 75, trees 73. Окончательное доказательство
исполнения — сравнение результатов GitHub CI, а не collection сама по себе.

Локально: 622 backend unit/contract tests, backend Ruff check/format,
strict Mypy (138 файлов), 29 script tests. Новые scripts также проверены Ruff.
Реальные PostgreSQL/ClamAV/Chromium проверки подтверждаются GitHub CI.

Для рабочего цикла достаточно проверок затронутого набора, например:

```sh
make integration INTEGRATION_ARGS='--suite images'
make integration INTEGRATION_ARGS='--suite lifecycle'
make integration INTEGRATION_ARGS='--suite applicability'
make integration INTEGRATION_ARGS='--suite trees'
make integration INTEGRATION_ARGS='--suite clean-upgrade'
uv --directory backend run --locked pytest tests/test_diagnostic_yaml.py
```

`--images` и `--trees` остаются совместимыми aliases. Обычный `make integration`
выполняет все PostgreSQL suites; `make integration-ci` добавляет все исходные
file acceptance tests в тот же бюджет двух окружений. `make files-smoke`
и все прежние индивидуальные Make targets доступны. Полный CI обязателен
для PR и push main; path filters, пропуски jobs и ослабление проверок не добавлены.

Для сравнения скачайте полные job logs двух запусков в отдельные директории
с именами `backend.log`, `frontend.log`, `database.log`, `compose.log`:

```sh
python3 scripts/compare_ci_tests.py /tmp/before /tmp/after
```

Exit code 0 требует полного совпадения идентификаторов и результатов, включая
повторные проверки. Для review необходимы также четыре успешных job conclusions.

## Измеренный результат

Финальный HEAD, ссылки на контрольный и оптимизированный запуски, длительности
четырёх jobs, suite timings и результаты сравнения test IDs публикуются
в [описании PR](https://github.com/h1roshigoto/akbrizcase/pull/40).
Время ≤10 минут можно считать достигнутым только по фактическому результату CI.

## Ограничения

Измерения — отдельные запуски shared GitHub runners, не статистический benchmark.
Длительности зависят от CPU, registry/network и очередей; suite timings отделены
от очереди runner. Измеренный выигрыш и test-ID parity являются основанием
оставить оптимизацию. При нестабильности конкретную оптимизацию нужно откатить.
Постоянные данные и volumes не удаляются; проверку housekeeping/ретенции эта
задача не добавляет. Следующие MVP и архитектура не входят в TECH-001.
