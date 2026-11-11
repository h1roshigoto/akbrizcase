# MVP-051 — административные агрегаты

`GET /admin/stats?from=YYYY-MM-DD&to=YYYY-MM-DD` — страница SQLAdmin для
действующих manager/admin через существующую PostgreSQL staff-сессию. Новый API,
экспорт и разрешения не добавлены. Все ответы имеют `Cache-Control: no-store`.

Без параметров выбираются последние 30 календарных дней UTC, включая текущий.
Обе даты обязательны при явном выборе; максимум 90 календарных дней включительно.
Допускается только строгий формат `YYYY-MM-DD`; неизвестные, повторные параметры,
одна граница, невозможная дата и обратный диапазон дают 422. `9999-12-31`
отклоняется: следующий день непредставим. SQL использует `>= 00:00 UTC from`
и `< 00:00 UTC следующего дня после to`.

Все счётчики получаются одним SQL statement в согласованном PostgreSQL MVCC
snapshot; доменные read-методы возвращают только ограниченные агрегаты COUNT/FILTER.
Не добавлены блокировки строк, commit внутри сервисов, таблицы или индексы.
Staff middleware сохраняет свои UoW, блокировку авторизации и проверку отзыва.
Ошибка PostgreSQL даёт безопасный неуспешный ответ, а не нулевые значения.

| Группа / счётчики | Значение и временная ось |
|---|---|
| orders.total | Сохранившиеся заявки с `orders.created_at` в периоде |
| orders.commercial / support | Эти же заявки по kind |
| orders.new / in_progress / closed / cancelled | Эти же заявки по текущему status; смена статуса не создаёт новую заявку |
| orders.site / chat | Эти же заявки по source |
| orders.chat_linked | Эти же заявки с сохранившимся `chat_session_id`; после удаления сессии FK становится NULL, source=chat сохраняется |
| sessions.total | Сохранившиеся сессии с `chat_sessions.created_at` в периоде |
| sessions.diagnose / knowledge / commerce | Эти же сессии по текущему mode |
| sessions.resolved / handoff / unresolved / no_outcome | Эти же сессии по текущему outcome, включая NULL; handoff — отдельный счётчик направления к специалисту |
| feedback.positive / negative | Принятые feedback-обмены с `chat_messages.created_at` в периоде, независимо от даты создания сессии |

Feedback берётся из минимизированного `ChatMessage.input` (`type=feedback`,
JSON boolean `useful`). Принятие проверяется по `matched_by=button` (обработчик
записывает его только для нетекстового ввода с `code=ok`) и response format v1:
режим diagnose в reply/state_after; для positive — `completed/resolved`, для
negative — `handoff/handoff`. Отклонённый feedback может сохранять прошлый outcome,
поэтому одного input или outcome недостаточно. Строки, пропущенные/текстовые
значения boolean и непринятый feedback не учитываются. Достижение result и
просмотр ответа не подтверждают полезность. Идемпотентный replay возвращает
существующую строку, защищённую UNIQUE(session_id, request_id), без новой записи.
Считаются обмены, а не уникальные посетители или сессии.

Mode/outcome — изменяемое состояние на момент запроса, а не неизменяемая история.
Resolved подтверждает принятую положительную оценку; negative может привести к
handoff, но handoff бывает и по другим причинам. Созданная заявка учитывается
отдельно. Эти значения не означают конверсию, продажу, оплату или выручку.

Страница содержит только счётчики и календарные даты: без контактов, текстов,
ID, исходного JSONB, CSV/Excel и персональных выгрузок. Статистика описывает только
сохранившиеся данные. Заявка, строка которой остаётся после стирания ПДн,
продолжает учитываться; полное удаление графа заявки уменьшает её счётчики.
Действующие retention/erasure и deletion journal не
меняются; удалённые записи не восстанавливаются и аналитическая копия не хранится.

Локально выполняются только целевые unit/guard, Ruff и mypy. PostgreSQL 16
приёмка `tests/integration/test_admin_stats.py` автоматически собирается suite
`core` в `scripts/db_integration.py` (все integration, кроме явно исключённых
других suites) существующим database job; десятый job не добавляется.

Внешние условия допуска MVP-050 остаются открытыми:

```text
FULL_MVP_READINESS=BLOCKED
REDUCED_PILOT_READINESS=BLOCKED
PUBLIC_LAUNCH_RECOMMENDATION=NO-GO
HUMAN_GO_NO_GO_DECISION=PENDING
PRODUCTION_ADMISSION=BLOCKED
```
