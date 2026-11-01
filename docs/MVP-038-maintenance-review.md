# MVP-038: protected audit maintenance и historical retention review

**PARTIAL IMPLEMENTATION / RELEASE BLOCKED. Только synthetic demo/test.**
ADR-038-A и journal v2 остаются PROPOSED. Направление А сохраняется. Этот документ
не назначает сроки, юридические основания, исключения или production cleanup.

## Фактическая граница полномочий

`akbrizcase_runtime` — NOSUPERUSER, не member/owner схемы; migration 0002 отозвала
UPDATE/DELETE audit. API/worker получают только runtime secret. Migrator — owner
`app`, имеет DELETE/DDL; его credentials уже используются только protected tools.
Python authority не ограничивает администратора, имеющего прямой SQL/DDL доступ.
Новая SQL-роль/SECURITY DEFINER/procedure не добавлены; ACL не расширены.

`maintenance_cli` — отдельный one-shot процесс, не worker, API endpoint или daemon.
Login/password поступают через существующие SafeParser/hidden getpass; non-echo
fallback запрещён. Authenticated active admin, role=migrator, APP_ENV demo/test и
точный owner-applied synthetic DB marker обязательны. Каждая проверка пишет
`privacy.audit_maintenance`/`privacy.retention_review` с фиксированными result,
actor UUID и пустым metadata. Эти audit actions не входят в удаляемый allowlist.
Authority привязана к текущей транзакции Session, не boolean снаружи; после commit
нужна новая авторизация. Отказ фиксируется outer UoW до возврата результата.

Audit scan ≤100, существующие advisory/keyset/time budgets и durable journal
сохранены. Pending audit имеет отдельный cursor; обычный pending_commands исключает
его даже при ошибочном запуске cleanup_tick с migrator. PREPARED commit → sink I/O
без DB connection → durable read-back exact bytes → новый UoW с повторной authority,
row lock/eligibility → mutation+applied_at → fresh reconciliation. SIGTERM/SIGINT
останавливают новые команды между UoW; текущая короткая транзакция завершается
или откатывается. Crash оставляет тот же UUID/bytes для повтора, не новый decision.
Sink failure, partial export, mismatch и неизвестный commit не считаются APPLIED.

Только routine `staff.login`/`staff.logout`, `result=success`,
`entity_type=staff_user` — технические кандидаты **при доказанной policy**.
CSRF/access/failed authentication, expert review, publication/applicability,
privacy decisions и неизвестные combinations сохраняются независимо от общего TTL.
Этот фильтр не утверждает юридическую классификацию successful authentication logs.

**Текущая безопасная граница:** staff_sessions/audit_events не имеют versioned
service-policy binding. Поэтому shared_policy всегда отказывает в их удалении,
включая случай единственного валидного publication. Runner реализует protected
execution mechanics, но его реальный unbound scan даёт manual_review, без новых
witnesses и DELETE. Старые PREPARED/witness тоже не создают applicability.
Положительные PG16 mutation-тесты используют явную fixture-подстановку applicability
только в test process; этот seam не доступен через CLI/env/config приложения.

## Защищённый запуск на synthetic стенде

Из backend, в operator terminal с уже защищённым migration secret mount:

```sh
uv run --locked python -m app.modules.orders.maintenance_cli review --limit 100 --max-batches 10
uv run --locked python -m app.modules.orders.maintenance_cli audit --limit 100
```

MIGRATION_DB_PASSWORD_FILE не передавать API/worker. APP_ENV и DB marker должны
совпадать с demo/test. Audit требует private SYNTHETIC_DELETION_JOURNAL_DIR по
существующему SyntheticJournal контракту. Нет флага override-policy, latest/default,
force, restore_missing, decision-id или production adapter. Не подключать cron.
Production scheduling требует отдельной least-privilege роли/credential injection,
неинтерактивной делегированной operator authority/revocation, аудита назначения,
supervisor/shutdown и утверждённого sink. Это конкретный инфраструктурный blocker;
интерактивный admin password не сохраняется ради unattended запуска.

## Агрегированный отчёт

Review использует один REPEATABLE READ snapshot, до 10 keyset pages по ≤100 строк
каждой категории. UUID cursor остаётся только в памяти. Для каждой категории:
inspected, manual_review, reasons→counts и truncated. Причины могут пересекаться;
сумма reasons не обязана равняться manual_review. truncated=true означает неполный
охват и не позволяет объявлять остальные записи проверенными. Значения policy,
IDs, action/result строки из БД, даты, контакты, ФИО, comments, hashes, tokens,
бизнес-суммы и transport errors в output не попадают. Отчёт не создаёт PREPARED,
не меняет cursors/evidence и ничего не удаляет; собственный authorization audit
является единственной записью CLI review.

| Причина | Значение / действие |
|---|---|
| POLICY_UNBOUND | Нет закреплённой версии chat; не backfill |
| POLICY_MISSING | Нет consent/publication; не выбрать latest |
| POLICY_INVALID | Повреждённый packet/checksum/consent или approval; review |
| POLICY_AMBIGUOUS | Несколько service publications без binding; не выбрать один |
| POLICY_CHRONOLOGY | Publication позже grant/chat creation либо future chronology |
| SERVICE_BINDING_UNKNOWN | Staff/audit без доказанной versioned applicability, даже с одним packet |
| ORDER_TERMINAL_EVIDENCE | terminal_at отсутствует, раньше создания или в будущем |
| OUTBOX_TERMINAL_EVIDENCE | sent/failed date отсутствует, раньше создания или в будущем |
| OUTBOX_PENDING_REVIEW | Pending старше policy review window или истёкший sending lease |
| OPEN_ORDER_REVIEW | Открытая старая заявка; не synthetic terminal transition |
| LINKED_SNAPSHOTS_LEGAL_DECISION | Истёк linked deadline; erasure не доказывает anonymity; нужен graph decision |
| AUDIT_ACTION_PRESERVED | Evidence/security/неизвестное событие вне узкого allowlist |

Orders используют immutable consent version/grant chronology, chat — собственный FK,
outbox — policy своего order и реальную completion date. `updated_at`,
`next_attempt_at`, сегодняшняя публикация или единственность packet не подменяют
historical evidence. Migration 0009 уже имеет узкий backfill terminal_at из ровно
одного подтверждённого history transition; эта итерация его не расширяет. failed_at
и historical chat binding автоматически не заполняются. Manual full graph deletion
по отдельному реальному решению сохраняет существующий контракт варианта А.

## Service binding: минимальный PROPOSED-контракт и migration plan

Ничего из этого раздела не реализовано и не утверждено для production.

1. Отдельная immutable `service_retention_policies`: version PK, scope/category,
   точный approved policy reference/checksum, approved/effective timestamps,
   evidence reference и отдельно утверждённая applicability semantics. Никаких
   default TTL; существующий ConsentPublication не переписывается и не объявляется
   автоматически service policy.
2. Nullable immutable binding FK у новых staff_session/audit records, назначаемый
   при создании только из явно активной policy нужного service scope. Создание
   login/security audit не должно зависеть от доступности policy: unknown → NULL
   и диагностика, не потеря аудита. Нужна версия code/action classification.
3. Исторические NULL сохраняются. Если владелец допустит retrospective binding,
   нужен отдельный versioned immutable applicability decision по точной области,
   с evidence/scope/approver и разрешением противоречий. Не массовый backfill latest.
4. После owner/legal/security approval — additive migration после актуального
   единственного head, FK/immutable trigger/ACL; runtime не получает DELETE audit
   или право изменять binding. Dual-read unknown→review, новых writes→binding,
   upgrade tests с прежними NULL и неизменными consent/publications/v1/v2 bytes.
5. Только затем planner/apply/restore проверяют scope/version/chronology/holds
   перед export и под mutation locks. Replay старой команды не закрепляет policy
   постфактум; conflicts остаются REVIEW_REQUIRED. Нужен отдельный approved контракт
   для того, как binding evidence переживает backup и доступно независимому sink.

## Exceptions / holds (PROPOSED)

Таблицы holds сейчас нет. Нельзя заявлять, что приложение уже исполняет legal holds.
Минимальный будущий контракт: неизменяемое решение с настоящим reference, версией,
scope/targets, основанием, ответственным, датами review/release и порядком
разрешения конфликтов. Отсутствие/неполнота состояния не трактуются как «hold нет».
Нет фиктивного UUID, автоматического продления всех ПДн или отмены witness.

| Граница | Точная гонка | Обязательное решение / механизм до production |
|---|---|---|
| Eligibility → PREPARED commit | Hold появляется после SELECT, до commit | Единый lock order/serialization для policy+hold+target; exact hold generation в authorization evidence |
| PREPARED → начало export | Hold меняется после prepare | Повторная проверка перед export; PREPARED без witness не deletion permit; stale generation→REVIEW_REQUIRED |
| Проверка → durable irrevocable export | DB lock освобождён ради sink I/O, hold принимается параллельно | Нужен утверждённый linearization/fencing протокол DB↔sink. Удержание DB transaction во время сетевого I/O не решает распределённую атомарность |
| Durable export → apply | Hold появляется после принятого irrevocable obligation | Владелец/юрист определяют приоритет и escalation. Нельзя просто отменить witness, молча применить DELETE или обещать одновременно оба обязательства |
| Apply row lock → commit | Hold изменяется в другой транзакции | Hold writer обязан участвовать в том же lock/fencing; свежий SELECT без сериализации недостаточен |
| Commit acknowledgement потерян | Повтор не знает, исполнено ли удаление; hold уже изменился | Fresh reconciliation postcondition; неизвестность→UNCONFIRMED, decision escalation; не выдавать новый UUID |
| Restore старой БД | Backup не содержит нового hold/binding, sink содержит witness | Закрытый restore gate с полным authoritative hold/binding/decision log и sink discovery. Отсутствие local hold не доказательство разрешения; REVIEW_REQUIRED до разрешения конфликта |
| Partial apply / снятие hold | Часть chat уже удалена или меняется scope | Утвердить неделимость scope и переходы; не обещать восстановить удалённое или продлевать retention автоматически |

До принятия этих решений production execution/scheduling/restore gate остаются
закрытыми. Retention TTL не превращается в legal hold, manual decision не заменяет
подтверждённый scope. Полный restore/reapply остаётся MVP-042.

## P2 restore isolation и проверка

finally охватывает CREATE SCHEMA, подготовку copy, первое удаление, swap и восстановление
графа. OID исходной journal проверяется до/после; swap/restoration DDL транзакционны.
Удаляются только собственные copy и UUID-schema, без CASCADE. При ошибке cleanup
транзакция откатывается, исходный journal не удаляется; оператор получает failure,
а не ложный PASS. Fault injection покрывает committed schema/copy, rollback swap,
ошибку внутри/после graph restore; v1/v2 × local present/missing × 4 графа сохранены.
Это table-level synthetic simulation, не pg_dump/PITR или production restore drill.

Фактические counts, точный checkout CI и P0/P1/P2 фиксируются в PR #51.
Стандартный локальный make integration ограничен ENOSPC Docker VFS до PG tests;
меньший PG16-alpine native harness и GitHub Actions учитываются отдельно.
