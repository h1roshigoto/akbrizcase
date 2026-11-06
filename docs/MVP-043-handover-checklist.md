# MVP-043 — checklist передачи

**Передача людям и production acceptance: BLOCKED.** Назначения, полный целевой
staging, production providers и внешние согласования не предоставлены. Не считать
CI, localhost TLS или synthetic evidence человеческой/юридической приёмкой.
Этот файл не содержит реквизитов доступа.

Материалы: [operator runbook](MVP-043-operator-runbook.md),
[manager guide](MVP-043-manager-guide.md), [ограничения backup](MVP-042-backup-restore.md),
[privacy gates](MVP-037.md), [journal](MVP-038-journal-protocol.md).

## Правило статусов и evidence

| Статус | Когда ставить |
| --- | --- |
| PASS | Выполнена именно эта проверка, есть фактический результат и evidence на точном HEAD |
| FAIL | Проверка выполнена, получен отказ критерия/ошибка; сохранить безопасный код |
| BLOCKED | Нужен внешний участник, решение или отсутствующая инфраструктура |
| NOT RUN | Проверка ещё не запускалась; её существование не означает успех |

Результаты автоматических прогонов на окончательном HEAD прикладываются к Draft
PR вместе с URL run/job и `OPERATOR_EVIDENCE`/`BACKUP_EVIDENCE`. Итоговый отчёт PR
содержит отдельно локальные ограничения и все семь CI jobs. Для каждой новой
ревизии evidence проверяется заново; прежний зелёный SHA не закрывает новый.

## Автоматическая часть

Ниже исходные статусы формы передачи; фактические статусы exact-HEAD прогона
берутся из итоговой матрицы Draft PR, а при человеческой приёмке переносятся в
защищённый протокол с HEAD/run. Не подменять их историческими числами MVP-004–042.

| Проверка | Команда / evidence | Исходный статус |
| --- | --- | --- |
| Staff hidden CLI, manager/wrong/disabled denial, старые сессии | operator stage staff | NOT RUN |
| KB import/check/preview/review/publish/revoke, ACL и отсутствие evidence | stage knowledge | NOT RUN |
| YAML import/check, exact review, публикация/revoke/restore и роли | stage trees | NOT RUN |
| JPEG/PNG, replace/remove/старый URL, invalid upload, manager denial | stage images | NOT RUN |
| Настоящий SMTP rejection→failed, manager retry, business rows, negative states | stage smtp | NOT RUN |
| Policy/journal/authority denials, witness, commit-ack и reconciliation | stage privacy | NOT RUN |
| Protected aggregates, disabled/manager denial, disk/DB/backlog, missing/stale receipt | stage diagnostics | NOT RUN |
| Age backup/clean PG16 restore, high-water/journal negatives, admission/SMTP закрыты | stage backup / BACKUP_EVIDENCE | NOT RUN |
| Backend tests/Ruff/strict mypy, frontend tests/types/build | Backend/Frontend CI jobs | NOT RUN |
| PG16/full files, Compose/browser/HTTPS, release, backup regressions | Пять остальных CI jobs; URL/HEAD в PR | NOT RUN |

Операторский job дополняет прежние шесть: Backend, Frontend, PG16 integration,
Compose/browser/HTTPS, Synthetic staging release, Encrypted PG16 backup/restore.
Synthetic PASS всегда сопровождается `production=BLOCKED`, `human_handover=BLOCKED`.

## Назначения и внешняя готовность

Заполняется владельцем в защищённом реестре; в Git допустим только согласованный
безопасный ID подтверждения. Не помещать сюда пароль, секрет, ссылку с access token,
реальные контакты клиента, документ личности или внутреннее содержание материалов.

| Необходимое подтверждение | Сейчас | Ответственный за решение / требуемое evidence |
| --- | --- | --- |
| Назначенный оператор admin, индивидуальный доступ и резервный admin | BLOCKED | Владелец: назначение и проверка доступа; имена не предоставлены |
| Назначенный manager и индивидуальный доступ | BLOCKED | Владелец: назначение; не предоставлено |
| Другой человек для независимого staging walkthrough | BLOCKED | Владелец: участник и дата, не автор автоматизации |
| Полный целевой staging в РФ, отдельные данные/secrets, domain/TLS/firewall | BLOCKED | Инфраструктурный владелец: проверенный стенд; reduced MVP-041 недостаточен |
| Канал эскалации, дежурный/замещение и сроки реакции по типам инцидентов | BLOCKED | Владелец: регламент и проверенный канал; сроков по умолчанию нет |
| Backup/key custody/escrow/rotation, RF offsite provider, timer/watchdog | BLOCKED | Владелец backup: назначение и проверяемые независимые копии |
| Target timed restore, согласованные RPO/RTO и объём | BLOCKED | Владелец инфраструктуры: фактические замеры полного восстановления |
| Независимый production journal/sink, completeness/source fencing, high-water authority | BLOCKED | Владелец/ИБ: durability/discovery/terminal ack/backup horizons и восстановление после потери source |
| Доверенный канал official signed CVD, оператор свежести и clean/infected приёмка | BLOCKED | Владелец AV: поставка и мониторинг; synthetic ndb не evidence подписи |
| SMTP credentials/политики, утверждённые sender/recipient, эксплуатационный контроль | BLOCKED | Владелец SMTP: защищённая настройка и приёмка, без секретов в Git |
| Реальные экспертные согласования, права на полный public файл и факты применимости | BLOCKED | Владелец контента/эксперт: конкретные материалы/версии и решения |
| Политика ПДн/retention/legacy/holds, graph deletion, service binding, host log TTL | BLOCKED | Оператор ПДн/юрист: решения MVP-037/038; новые TTL здесь не назначены |
| Решение по ADR-038-A/journal v2 | BLOCKED | Владелец/юрист: ADR остаётся PROPOSED |
| Ограничение одного сервера и отсутствие HA | BLOCKED | Отдельное явное принятие владельцем/заказчиком |
| At-least-once SMTP, возможный дубликат письма | BLOCKED | Отдельное явное принятие оператором и владельцем/заказчиком; не exactly-once |

## Наблюдаемая передача людям

При наличии назначений и допущенного полного synthetic staging другой участник
работает **только по инструкции**, фиксируя пропущенные шаги для исправления.
Форма записи: дата/часовой пояс, участник/роль, exact HEAD/profile, шаг, фактическое
постусловие, безопасный evidence ID, PASS/FAIL/BLOCKED/NOT RUN, решение/исправление.
Имена/доступы и чувствительное evidence хранятся по правилам владельца вне Git.

| Шаг приёмки | Что должен воспроизвести человек | Статус |
| --- | --- | --- |
| Подготовка | Найти стенд, версию, роли, backup и канал эскалации без подсказок автора | BLOCKED |
| Release | Check/apply/migration failure/совместимый rollback в отдельном reduced synthetic профиле; объяснить ограничения | BLOCKED |
| Staff | Создать manager/admin, проверить права, отключить synthetic сотрудника и старую сессию | BLOCKED |
| KB/tree | Импорт/check/preview, настоящее review конкретного материала либо честный BLOCKED, publication/revoke/dependencies/restore denial | BLOCKED |
| Images/applicability | JPEG/PNG replace/remove, invalid file, archived item, preview/revoke и публичная карточка | BLOCKED |
| AV | Проверить official signatures/время/read-only release и fail closed | BLOCKED |
| Privacy | Объяснить policy/review/witness/postcondition/commit-ack; synthetic reconciliation, production gate сохраняется | BLOCKED |
| Backup | Зашифрованная копия → новая закрытая среда → journal/high-water → проверка, без открытия admission/SMTP | BLOCKED |
| Инциденты | По сигналам разобрать DB/disk/backlog/stale AV, запреты и адресата эскалации | BLOCKED |
| Manager | Login/logout, commercial/support, snapshots, status/history, stale version, failed retry и границы роли | BLOCKED |
| Принятие ограничений | Один узел/no HA, at-least-once SMTP, незакрытые внешние gates | BLOCKED |

Автоматизация не подписывает эту форму за человека. Draft PR готовится для
независимого ревью; merge/deploy, production cleanup, включение release modes,
открытие восстановленного окружения и MVP-044 не разрешены этим checklist.
