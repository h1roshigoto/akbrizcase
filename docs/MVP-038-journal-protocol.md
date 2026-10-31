# MVP-038: synthetic journal acknowledgement и reconciliation

**PARTIAL / RELEASE BLOCKED. ADR-038-A остаётся PROPOSED.** Это реализация
технического протокола для synthetic demo/test, не утверждение production v2,
сроков, оснований или исключений. Продуктовое направление А сохраняется.

## Реализованные границы и доверие

`JournalSink` определяет create-if-absent export по UUID и durable read. Конфликт
bytes одного UUID — ошибка, а не новая версия команды. `DurableAcknowledgement`
содержит UUID и SHA-256 canonical bytes; receipt сам по себе не является witness.
`verify_witness` читает существующий durable объект из выбранного sink и сверяет
точные bytes. Только результат проверки `VerifiedWitness` допускается к mutation;
передача даже правильных canonical bytes напрямую отклоняется.

Адаптер — доверенная зависимость процесса. Типизированная capability не защищает
от вредоносного Python-кода или лгущего адаптера. Новый production provider не
выбран; credentials, независимый failure/backup domain, аутентификация receipts,
полнота discovery и production SLA этим интерфейсом не доказываются. Обе runtime
проверки APP_ENV demo/test и owner marker synthetic database остаются обязательны.
Production-исполнение не разрешается ни receipt, ни capability.

`SyntheticJournal` реализует интерфейс в private directory: temporary file,
file fsync, atomic link, directory fsync. Recovery read проверяет regular file,
owner/mode, размер, O_NOFOLLOW/O_NONBLOCK и повторяет file+directory fsync перед
возвратом witness. Это позволяет безопасно повторить незавершённый export, но не
доказывает независимость от потери хоста/его backup. Temporary file не считается
export; неполный/conflicting final file требует review и не перезаписывается.

Runner `reconcile_command` разделяет:

1. Короткое чтение committed local state в собственном UoW.
2. Sink read/export/verification после закрытия UoW. Export разрешён только для
   committed PREPARED. Никакая транзакция БД не охватывает sink I/O.
3. Новый mutation UoW с прежними advisory/row locks, проверкой policy/eligibility,
   authority и атомарным applied_at. Manual delete повторно авторизуется.
4. После выхода из UoW отдельное чтение applied_at **и postcondition**. Boolean
   «изменена порция» не является подтверждением завершения. Для PII проверяются
   все designated fields и reasons; для full delete — отсутствие parent и consent
   при действующих FK/CASCADE; для chat — отсутствие session после последней порции.

При исключении commit нельзя утверждать rollback: сервер мог уже зафиксировать
операцию. Runner пытается прочитать результат в новом UoW. Если это невозможно,
возвращает UNCONFIRMED. Повтор проверяет тот же UUID/bytes и не создаёт второй эффект.
Worker пишет CLEANUP_APPLIED только после подтверждения postcondition; частичный
chat batch остаётся WITNESSED. CLI повторно сверяет известную команду даже когда
целевая строка уже отсутствует после утраченного commit acknowledgement.

## Состояния одного известного command

| Local / sink | Результат inspection | Разрешённое действие execute |
|---|---|---|
| PREPARED / отсутствует | PREPARED | Export, read-back, затем mutation |
| PREPARED / exact durable witness | WITNESSED | Повтор eligibility/authority и mutation |
| PREPARED / partial mutation committed | WITNESSED | Следующая ограниченная порция; не completion |
| APPLIED / exact witness / postcondition | APPLIED + commit_confirmed | Без повторного эффекта |
| APPLIED / witness отсутствует | UNCONFIRMED | Не создавать witness задним числом |
| Любое / sink недоступен | SINK_UNAVAILABLE | Без мутации и без completion claim |
| Несовпадение UUID/bytes или APPLIED без postcondition | CONFLICT | Review; не переписывать immutable evidence |
| Local отсутствует / внешний witness существует | LOCAL_MISSING | По умолчанию только сигнал; opt-in restore seam |
| Не удалось заново прочитать результат commit | UNCONFIRMED | Повторная reconciliation после восстановления доступа |
| Неизвестная policy/authority/production runtime | REVIEW_REQUIRED | Fail closed |

`commit_confirmed` — подтверждение локального результата отдельным чтением,
**не долговечный внешний terminal acknowledgement**. Никакое из этих состояний
не разрешает pruning.

## Ограниченный restore seam

Для известной внешней команды `restore_missing=True` вместе с execute разрешает
в synthetic окружении и только migrator восстановить PREPARED с исходными UUID,
timestamps и canonical fields. Требуются существующий exact witness, immutable
journal policy и прежние CHECK/FK/triggers. PREPARED коммитится до mutation. Для
order_delete дополнительно требуется повторная admin authorization. Конфликт
operation/object с иным UUID не заменяется. Worker не включает этот режим и не
исполняет manual order_delete.

Это не discovery всех команд и не способ открыть восстановленную БД. Полнота
внешнего журнала, policy evidence и закрытый доступ ко всему restore окружению
остаются обязательными требованиями MVP-042. Неизвестные hold/legacy условия не
получают новую автоматическую eligibility; новые основания automation не добавлены.
Существующий failed outbox при retry и новой failure ждёт нового deadline, даже
если старый prepare-time minimum уже истёк.

## Совместимость и retention

Wire serialization v1/v2 не изменена. v1 UUID/bytes сохраняются, v2 остаётся
предложением. Миграции не изменены и не добавлены: head —
`0010_mvp038_journal_basis`. Сохраняются constraints, ACL, immutable triggers,
AQ-01 erasure и общая 404; token-verification material не добавлен. Полный DELETE
удаляет весь разрешённый граф варианта А, сохраняя независимые chat/evidence.
Автоматический full DELETE не включён.

Минимальные hold v1/v2 — нижние границы, не expiry. Неразрешённая команда не
истекает. Sink не имеет prune/delete API; автоматического pruning нет. Даже
подтверждённое local applied_at не заменяет независимый долговечный terminal ack
и контроль всех допустимых backup/PITR горизонтов. Существующий calculator
earliest_prune_at лишь вычисляет границу при предоставленном completion evidence.

## Проверяемое evidence и его пределы

Unit-тесты проверяют receipt без durable файла, неполные temporary/final файлы,
exact retry, конфликт UUID/bytes, file/directory fsync failures, symlink/FIFO/mode
и запрет production adapter. PG16-тесты проверяют committed PREPARED, crash до/после
export, receipt без файла, sink partition, rollback всей mutation, отказ commit,
утрату acknowledgement после успешного commit, недоступность повторного чтения,
повтор после успешного commit, local conflicts и частичный chat batch.

Изолированная симуляция старого состояния сохраняет новый journal в отдельной
synthetic schema, возвращает его более раннюю копию с CHECK/FK/triggers и точные
строки удалённого графа, затем повторяет v1/v2 full delete для support/product/
mixed/service, с PREPARED и без local записи. Новая история не сбрасывается;
reset/downgrade/stamp/volume deletion не применяются. Это table-level simulation,
не pg_dump/PITR, не проверка независимого production sink и не полный restore drill.
После возврата исходной таблицы временная schema с копией удаляется; это teardown
тестового артефакта, чтобы последующие Alembic/model checks не видели чужих таблиц.

Фактические количества/результаты локальных прогонов и четырёх Actions jobs на
точном SHA фиксируются в PR #51; добавление теста не означает его успешный запуск.
Первый локальный стандартный integration запуск ограничен ENOSPC Docker VFS;
повтор PG16 использует отдельный маркированный стенд с 16.13-alpine и native pytest.
Неисполненные Compose/restore проверки не объявляются пройденными.

## Оставшиеся production-блокеры и решения владельца

- Утвердить основания/срок полного графа, применимость к collection/legacy версиям,
  исключения/hold и гонку hold с уже irrevocable export; согласовать промежуточный
  связуемый граф и последствия для отчётности. Сроки не назначены этой итерацией.
- Отдельно принять или изменить ADR-038-A, policy schema и journal v2/basis.
- Выбрать независимый sink/ACL/юрисдикцию и доказать фактическую durability, discovery,
  доступность при restore, внешний terminal acknowledgement и backup horizon.
- Выполнить полный закрытый restore/reapply MVP-042 с файлами, manifest, v1/v2,
  commit-ack loss/partitions и отсутствием resurrected данных до открытия доступа.
- Закрыть юридические и инфраструктурные gates MVP-037/038: service policy binding,
  legacy terminal evidence, privileged maintenance runner и age-based host log TTL.

PR #51 остаётся Draft / RELEASE BLOCKED. Полный DoD MVP-038 не заявляется выполненным.
Merge, deploy, production cleanup и MVP-039 не входят в эту итерацию.
