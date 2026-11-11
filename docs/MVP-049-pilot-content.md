# MVP-049 — реестр пилотного наполнения и закрытая техническая проверка

**Реальная контентная приёмка BLOCKED.** Финальный ассортимент, разрешённые
документы/изображения, независимые решения эксперта и целевой content walkthrough
не предоставлены. Эта реализация готовит реестр, проверяет предоставленные
доказательства офлайн и воспроизводит синтетические workflows. Она не разрешает
импорт реальных материалов, пилот, production, merge или deploy. MVP-050 не начат.

Нормативные процедуры: [оператор](MVP-043-operator-runbook.md),
[передача людям](MVP-043-handover-checklist.md), [ACL](MVP-045-acl-publication-regression.md),
[FTS](MVP-047-kb-fts-acceptance.md), [эксплуатация](MVP-048-load-operational-smoke.md),
[KB](MVP-021.md), [review/publication](MVP-023.md), [применимость](MVP-024.md),
[изображения](MVP-025.md), [формы](MVP-026.md), [деревья](MVP-028.md).

## Граница доказательства

`check` проверяет **согласованность выбранного закрытого пакета**. Он не подключается
к БД, не сканирует изображения, не импортирует YAML, не выполняет SQL/HTTP,
не создаёт approvals и ничего не публикует. Проверенные заранее результаты сервисов
передаёт оператор. Новый DB adapter не добавлен; публикационные predicates, ACL,
UoW и аудит остаются у существующих сервисов.

Офлайн-снимок даже с правильными checksum не доказывает свежесть состояния целевой
БД или подлинность решения человека. В v1 нет доверенного независимого verifier;
**все реальные gates остаются BLOCKED даже при технически полном REAL UNVERIFIED
пакете**. Предметное решение должно пройти отдельную независимую проверку вне
этого инструмента. Переименование fixture и JSON со словами approved/PASS этого
не меняют. Ни checksum, ни receipt не являются цифровой подписью.

`technical_checks=PASS` означает проверку предоставленных связей внутри пакета,
а не право распространения, экспертную безопасность текста или проверку live БД.
`MVP049_REHEARSAL/v1` доказывает в пределах журнала runner прохождение синтетических
workflow regressions на указанном HEAD; он не относится к реальному контенту и
не утверждает совпадение ephemeral DB entities со schema fixture.

## CLI и защищённое хранение

Из чистого checkout, Python 3.12, без БД и credentials:

```sh
python3 scripts/mvp049_content_gate.py check \
  --input-root /protected/pilot-input \
  --manifest manifest.json \
  --expected-sha256 MANIFEST_SHA256 \
  --expected-head REVIEWED_FULL_GIT_SHA \
  --expected-base REVIEWED_FULL_BASE_SHA

python3 scripts/mvp049_content_gate.py report \
  --input-root /protected/pilot-input \
  --expected-sha256 MANIFEST_SHA256 \
  --expected-head REVIEWED_FULL_GIT_SHA \
  --expected-base REVIEWED_FULL_BASE_SHA \
  --output-dir /protected/pilot-report-NEW
```

Здесь значения SHA — placeholders; pins получают из независимого review реестра,
а не слепо копируют из проверяемого файла. `--repo` меняет только явно выбранный
локальный checkout для Git-проверок; по умолчанию это репозиторий инструмента.
Нужны точный HEAD, чистое дерево и ancestor relation base → HEAD. Git вызывается
без optional writes, replace objects и fsmonitor; fetch/network инструмент не
выполняет. До подготовки пакета оператор отдельно делает `git fetch origin main`
и сверяет текущий base с согласованным.

`check`: никаких файлов результата. `report`: повторно выполняет всю проверку
исходного пакета; не принимает произвольный JSON с заявленным результатом.
Только после финальной проверки всех открытых файлов создаёт новый каталог и
`result.json`. Каталог 0700, файл 0600, exclusive create, fsync, без перезаписи.
Оба режима печатают одинаковый allowlisted `MVP049_RESULT`. Exit 0 означает
успешную проверку структуры/evidence; **он не означает admission**. Exit 1 —
неверный ввод, checksum/revision/lineage mismatch, ошибка либо небезопасный output;
в stdout только PREFLIGHT_REJECTED и закрытые gates, без traceback/путей/текста.

Input root должен быть явно выбранным абсолютным каталогом вне checkout,
принадлежать оператору и иметь 0700. Все файлы — этого владельца, ровно 0600,
regular, nlink=1. Имена только basename: ASCII буква/цифра, далее `_.-`, до 120
знаков; traversal, symlink на любом компоненте пути, hardlink и FIFO запрещены.
FD/имя/inode/права/размер/mtime/ctime/checksum перепроверяются до результата.
JSON ≤1 MiB, каждый материал ≤25 MiB, суммарно ≤64 MiB, глубина ≤20, ≤30000 JSON
значений. Количества сущностей ограничены схемой; paths ≤4096, path ≤30 переходов.
Duplicate JSON keys, NaN/Infinity, неизвестные поля и неверные типы отвергаются.

Выбрать новый namespace при каждом прогоне. Не помещать реальные inputs, оригиналы
approval records, private snapshots, child logs или reports в Git/CI artifacts.
Имена экспертов, контакты, timestamps, ссылки и содержимое разрешений остаются в
утверждённом защищённом хранилище владельца с отдельной политикой доступа/retention.
Этот PR не назначает новые сроки хранения.

## Точный manifest

Закрытая схема: [manifest.schema.json](../content/pilot/manifest.schema.json).
Все её properties обязательны; отсутствие необязательного свидетельства записывают
явным `null`, а не выдуманной записью. Полный безопасный пример:
[synthetic/manifest.template.json](../content/pilot/synthetic/manifest.template.json).
Он содержит **SYNTHETIC ONLY** данные и не запускается непосредственно из Git:
runner копирует fixture в свой private namespace и закрепляет фактический HEAD.

| Раздел | Контракт |
| --- | --- |
| Корень | `format=MVP049_CONTENT_MANIFEST/v1`, classification `SYNTHETIC ONLY` или `REAL UNVERIFIED`, стабильный release_id, Git base/head и previous_manifest_sha256 |
| artifacts | Уникальные id/filename, SHA-256 точных bytes, kind. Все перечисленные файлы проверяются, даже неиспользованные |
| approvals | Opaque id, scope, subject_sha256, record_artifact и classification. Это ссылки на уже существующие private originals, а не новые экспертные решения |
| catalog.models | Стабильный code, status, revision SHA-256 согласованного model snapshot и business_approval |
| catalog.items | SKU, product/service, revision, status, fixed/on_request, nullable цена строкой с двумя десятичными, RUB, unit, tax_note, shipping, delivery_tariff=null, business_approval |
| images | Логический id, item/revision, original/output artifacts и SHA-256, pipeline observation и rights_approval; отсутствие — ABSENT и все file/rights поля null |
| documents | Code/version, текущие D/V statuses/visibility, original/import artifacts+SHA, sections с точным id/key/bytes/assistant_allowed, service sections_sha256, отдельный section_inventory_sha256, review и full-file permission/approval |
| applicability | Точная item/model/section, review_approval отдельно от disclosure_allowed/disclosure_approval, ожидаемый confirmed/unknown и разрешённые public_sources |
| trees | Key/version/model/status, исходный YAML artifact+SHA, service binding_sha256 отдельно от inventory_sha256, нормализованные nodes/start, paths artifact и review_approval |
| search | Конкретная version, corpus artifact+SHA, предварительное expectations_approval, nullable measurement_artifact |
| minimum | Точные model codes, согласованное число сценариев, отдельное agreement_approval; наличие одной fixture не согласует минимум |
| state/rehearsal | Nullable ссылки на закрытый state snapshot и синтетический workflow receipt |

Деньги не округляются preflight. Fixed с null, on_request с ценой, доставка услуги
и любой придуманный тариф отвергаются. Archived/draft сохраняются в инвентаризации,
но не дают подтверждённой публичной применимости/рекомендаций.

`canonical(v)` — UTF-8 JSON, sort_keys, ensure_ascii=false, separators comma/colon,
без NaN. Inventory digests — SHA-256 canonical JSON. `section_inventory_sha256`
считает список sections; `tree.inventory_sha256` считает ровно key/version/model/
start/nodes. Они **не подменяют** service `sections_sha256` и `binding_sha256` из
защищённого preview. Service digests сверяют с исходными approvals и свежим preview;
preflight связывает их с inventory, paths и закрытым state, но не вычисляет второй
вариант серверного publication binding.

`subject_sha256` — digest scope-specific inventory binding, функция `subject()`
в инструменте. Document review связывает code/version, original/import SHA,
service sections SHA, section inventory SHA и sections; full-file дополнительно
связывает full_file_public_allowed. Tree review связывает key/version/model,
YAML SHA, service binding, inventory SHA, start/nodes. Applicability review
связывает item/model/section; disclosure дополнительно review ID и отдельный flag.
Search expectations связывает version/corpus SHA, независимо от последующего
измерения. Другие scopes связывают все поля сущности, кроме approval references.
Эти inventory bindings не изменяют исходный expert record и не удостоверяют его.

Версия реестра ссылается на точный SHA предыдущего manifest через отдельный
`previous-manifest` artifact kind=previous. Предыдущий manifest должен иметь ту же
release identity/base и Git ancestor HEAD. Историю не перезаписывают. Вне Git
сохраняют оригиналы каждой версии и независимые references к ним.

## Связанные evidence

Все JSON envelopes закрыты; произвольные поля/пропущенные строки не принимаются.
Оригинальные approval records — opaque bytes: их не печатают и не интерпретируют
как доказательство истинности или независимости. Оператор отдельно сверяет
фактического автора, полномочия, содержание, дату, exact edition и правообладателя.
Для реального пакета classification записей только UNVERIFIED EXTERNAL;
SYNTHETIC ONLY там запрещено, но смена строки не создаёт реальное согласование.

- `MVP049_STATE/v1`: format/head/classification/content. Content имеет ту же схему
  и должен побайтно совпасть после canonical serialization с планом. Это уже
  проверенный защищёнными интерфейсами операторский снимок, а не auto-export БД.
  Отсутствие снимка = NOT RUN, совпадение не закрывает TARGET_CONTENT_VERIFICATION.
- `MVP049_IMAGE_OBSERVATION/v1`: format/classification/item/revision/original_sha256/
  output_sha256 и original_scan/output_scan/decoder=PASS. Это перенесённый результат
  существующего pipeline; проверяются обе фактические копии bytes. Сам observation
  не сканирует файл и не даёт права использования.
- `MVP049_PATHS/v1`: format/yaml_sha256/binding_sha256/inventory_sha256/paths.
  Paths — полный детерминированный DFS список node IDs от start до каждого terminal.
  Cycles, missing/unreachable nodes, неверные переходы, references/recommendations
  и неполный список отвергаются. Нормализованную inventory оператор получает
  после существующего tree-check и сверяет с **тем же** YAML; новый YAML parser
  и альтернативный publication validator не добавлены.
- `MVP049_CORPUS/v1`: format/version/classification/queries. Каждая строка содержит
  id/text/group/expected_sections/behavior/safe_sections. Group relevant/negative;
  behavior results/no_match/safe_quote/handoff. Все ожидаемые IDs — допустимые
  public published assistant sections. Вопросы и word bags уникальны. Minimum 20+20.
- `MVP049_MEASUREMENT/v1`: format/head/corpus_sha256/rows. Каждая строка содержит
  id/returned_sections/total/resolution/execution/acl_incidents. Ровно все query IDs,
  без дублей, ошибок или пропусков; не более пяти уникальных разрешённых sections,
  ноль ACL incidents. HitRate пересчитывается: hits*5 >= relevant*4. Negative
  сверяется с заранее закреплённым no_match, safe sections или handoff. Это сверка
  переданных outcomes, а не новое измерение FTS. Реальное измерение выполняет
  существующий `kb_acceptance.external` по процедуре MVP-047; его private outcomes
  переносят без изменения предварительно утверждённых ожиданий.

## Операторская последовательность и матрица

1. Заказчик подтверждает ассортимент, model codes, бизнес-параметры, минимум и
   состав релиза. Владелец прав передаёт исходные файлы и отдельные разрешения
   изображений/полных public-файлов. Эксперт заранее утверждает вопросы/expected
   sections и область технического review. До получения — BLOCKED, без mock вместо
   подтверждения. Admin фиксирует protected references, не создаёт решение сам.
2. Admin создаёт/редактирует каталог и модели через действующие защищённые формы
   и CatalogWriteService. До publish сверяет SKU/kind/unit/налоги/доставку/цену
   и expected revision; после — перечитывает публичную карточку и current revision.
   Archived должен исчезнуть; устаревшая revision требует reload, не force.
3. Admin загружает/replace/remove JPEG/PNG через Manage image (MVP-026). До:
   права на точные исходные bytes и revision. После: current binding/revision,
   private verified bytes, нормальный public URL; старый URL после replace/remove
   даёт 404, отсутствие — placeholder. AV, декодирование и оба scans не обходят.
4. KB: `kb-import` → `kb-check` → `kb-preview`. Только разрешённые материалы,
   ручные sections и assistant_allowed. Эксперт рассматривает полный файл/фрагменты
   и реальную редакцию; admin передаёт фактический evidence в `kb-review` и затем
   `kb-publish`. Отдельное full-file разрешение обязательно для public файла.
   После: fresh search/exact/file; internal не раскрывается в anonymous total,
   snippets, file или публичном ассистенте даже с staff cookie.
5. Applicability: candidate/preview → реальный expert review → applicability-review
   → отдельное разрешение раскрытия факта → applicability-publish → preview.
   Проверить точную пару и основание. Internal evidence может дать confirmed с
   sources=[]; отсутствие любого predicate даёт unknown. Никакой совместимости
   по названию модели/наличию связи не выводят.
6. Tree: tree-check(file) → tree-import(file, exact Git commit) → tree-check(tree)
   → tree-preview → эксперт проверяет каждый путь, sources и recommendations
   → tree-publish с точными YAML/binding evidence → fresh preview/start/history/
   replay. Internal source запрещён независимо от разрешённого факта применимости.
7. Зафиксировать private snapshot/manifest/checksum и завершить minimum check.
   Провести целевой read/search/ACL walkthrough и FTS на финальных **реальных**
   checksum по предварительным экспертным ожиданиям; synthetic 90% его не заменяет.
   Запустить check, затем report с независимо сверенными pins. Другой человек
   сверяет оригиналы approvals и состояние target с aggregate и private manifest.

| Изменение/контроль | Ожидаемое действие |
| --- | --- |
| Missing rights/expert/minimum | BLOCKED, не подставлять synthetic reference |
| Fixed/on_request/shipping/placeholder/archive | Проверить через реальный catalog/image workflow и свежий public read |
| Internal basis | Отдельный fact disclosure; никогда не копировать internal SourceRef в public_sources |
| Retire/revoke основания | Новая выдача исключает его; применимость unknown; история/replay проверяет текущую допустимость, permission не восстанавливается автоматически |
| Retired exact pin | Старый разрешённый источник может работать в закреплённой диагностике по MVP-028; это не разрешение нового release/start или confirmed |
| Изменённые YAML/sections/edition/checksum | Старое binding/evidence непригодно; новая версия, штатные проверки/review и новый manifest |
| Смена visibility | Перепроверить public file/search/tree/history; import manifest сохраняет историческую visibility и не служит текущим разрешением |
| Смена цены/изображения/применимости | Получить current revision, повторить затронутые проверки, новый snapshot/manifest/report |
| Неполный/ошибочный FTS | FAIL/NOT RUN, полный знаменатель и ACL критерии MVP-047 сохраняются |
| Изменение после проверки | Старый report не считать текущим; повторить затронутые workflows, независимую сверку и весь preflight |

Никогда не исправлять immutable/expert/approval/published/applicability SQL-поля
напрямую. Все credentials вводятся скрыто штатным CLI. Scope и роли остаются
прежними; manager не публикует и не заменяет эксперта или правообладателя.

## Синтетическая проверка и результат

```sh
make mvp049-guards
make backend-test backend-lint backend-types
make acceptance-guards
# Чистый checkout; свежие маркированные PG16/files, настоящий AV/HTTP/UoW:
make mvp049-rehearsal
```

Последняя команда расширяет существующий operator rehearsal четырьмя узкими
этапами catalog/applicability/all-paths/locked-FTS и выполняет encrypted restore
один раз. Каждый этап имеет отдельный свежий PG16 stack; volumes сохраняются.
Skip/xfail/partial/error не дают receipt. FTS использует прежний строгий runner
MVP-047 с проверкой всех 40 outcomes, actual PG16, schema и exact clean HEAD.
Receipt содержит только source checksum, закрытые stage codes/counts и FTS counts.

В CI лёгкие guards добавлены в Backend checks, расширенный rehearsal заменяет
один прежний operator запуск в том же job. Все девять обязательных jobs,
включая MVP-048 load-smoke, сохранены; второй полный CI вручную не запускается.
Exact final HEAD, реальные команды, run/job URLs и conclusions фиксируются в PR.
Незавершённые, отменённые и более старые SHA не объявляются PASS.

`MVP049_RESULT/v1` содержит только allowlisted gates, technical_checks, counts,
checksums и фиксированные codes. Статусы PASS/FAIL/BLOCKED/NOT RUN/INCONCLUSIVE
не смешивают: ошибка = FAIL; отсутствие права/человека = BLOCKED; незапущенная
операция = NOT RUN; недостаточное доказательство не является PASS.
Синтетический rehearsal может повысить только SYNTHETIC_CONTENT_REHEARSAL.
MANIFEST_VALIDATION=PASS относится к конкретному переданному manifest; runner
явно помечает SCHEMA_FIXTURE_ONLY и WORKFLOW_REGRESSION_ONLY.

| Gate | Текущее предметное состояние |
| --- | --- |
| MANIFEST_VALIDATION | Результат конкретного check; schema fixture не реальный release manifest |
| SYNTHETIC_CONTENT_REHEARSAL | PASS только после полного фактического runner; иначе NOT RUN/FAIL |
| CATALOG_FINAL / IMAGE_RIGHTS / DOCUMENT_PERMISSIONS | BLOCKED: внешние материалы/решения отсутствуют |
| APPLICABILITY_EXPERT_REVIEW / TREE_EXPERT_REVIEW | BLOCKED: реальные независимые решения отсутствуют |
| REAL_TOP5_ACCEPTANCE / TARGET_CONTENT_VERIFICATION | BLOCKED: нет финального корпуса, ожиданий и целевого walkthrough |
| PILOT_CONTENT_ADMISSION | BLOCKED: нет полной независимой предметной приёмки |
| PRODUCTION_ADMISSION | BLOCKED: отдельное решение MVP-050 |
| MVP043_EXTERNAL / MVP047_EXTERNAL | BLOCKED: этот PR не снимает внешние блокировки предыдущих этапов |

## Недостающие внешние inputs

| Input | Ответственный | Что получить и проверить |
| --- | --- | --- |
| Финальный ассортимент, модели, цена/on_request, unit/налоги/доставка | Заказчик + менеджер | Утверждённый точный набор и источник бизнес-значений, без demo-цен |
| JPEG/PNG либо согласованное отсутствие | Владелец прав + заказчик | Исходные bytes/checksum и право использования конкретного изображения |
| Документы, версии, ручные fragments, visibility | Владелец знаний/прав | Разрешённые originals/import manifests; отдельные full-file public permissions |
| Экспертные решения применимости и деревьев | Технический эксперт | Фактические decisions, exact edition/checksum/section/pair, все paths, независимые references |
| Disclosure фактов применимости | Уполномоченный владелец + admin | Отдельный факт permission после expert review; не публикация внутреннего основания |
| Модели и 3–5 сценариев либо иной согласованный минимум | Заказчик + эксперт | Явное решение о точном составе/минимуме, не автоматическое принятие одной fixture |
| Реальные 20+20 FTS judgements | Эксперт/владелец знаний | Заранее согласованные вопросы, section IDs, negative behavior, corpus version/checksum |
| Целевая сверка и независимый walkthrough | Назначенный operator + другой reviewer | Actual target state/HEAD, private approvals, повторный read/search/ACL, фактический протокол |
| MVP-043/047 и финальный go/no-go | Назначенные владельцы, затем MVP-050 | Внешние решения предыдущих этапов и отдельный допуск; CI их не подписывает |

`content/docs.yaml` — технический индекс; `content/trees/valid/` — структурные
SYNTHETIC ONLY примеры с placeholder UUID. Наличие этих файлов, schema fixture,
успешного теста или зелёного CI не предоставляет права публикации. Полный предметный
DoD MVP-049 остаётся BLOCKED до получения и независимой проверки реального контента.
