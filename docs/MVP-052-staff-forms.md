# MVP-052 — Защищённые формы сотрудников

Основание: ARCHITECTURE.md §5.6, §8, §10.1; [MVP-004](MVP-004.md),
[операторский runbook](MVP-043-operator-runbook.md), раздел 3.

## Доступ и маршруты

Все страницы — SQLAdmin `StaffBaseView`, `required_role="admin"`. В меню активного
admin появляется Staff. Manager получает 403 при прямом чтении или POST; без
действующей staff-сессии (включая disabled/revoked/expired) — 401.
Все изменяющие запросы проходят прежний middleware Origin/CSRF. Ответы no-store.

| Метод | Маршрут | Назначение |
|---|---|---|
| GET | `/admin/staff` | Первые 100 сотрудников по login, затем UUID; форма создания |
| POST | `/admin/staff/create-account` | Создание manager/admin |
| GET | `/admin/staff/{user_id}` | UUID, login, role, is_active; подтверждение disable |
| POST | `/admin/staff/{user_id}/disable` | Отключение и отзыв всех сессий |

Имя create-account избегает встроенного маршрута SQLAdmin `/{identity}/create`;
регрессионный unit-тест проверяет фактический выбор handler в полном router.

GET не меняет бизнес-состояние. Список читает максимум 101 техническую запись,
показывает 100 и предупреждение об ограничении. Пагинации и полного экспорта нет;
карточку за пределами списка можно открыть по известному UUID. В template context
передаются только id/login/role/is_active, а не ORM-объекты с password_hash.

## Создание

1. Войти существующим активным admin через штатный HTTPS login.
2. Открыть Staff, указать login, manager/admin, новый индивидуальный пароль и его
   подтверждение. Пароль действующего admin в этой форме не запрашивается.
3. Отправить форму с CSRF. После успешного commit браузер получает 303 на карточку
   нового сотрудника. Пароль не возвращается и не переносится в URL/flash.
4. Проверить вход нового сотрудника в отдельной сессии и соответствующие права.

Login: 3–64 ASCII буквы, цифры, `_`, `.`, `-`, уникальный с учётом регистра.
Пароль: минимум 12 символов, максимум 1024 UTF-8 байта; пробелы не обрезаются.
Используются прежние valid_login/valid_password и Argon2id с независимой солью.

POST принимает только urlencoded-форму, без query parameters, максимум 16384 байта;
каждое декодированное поле — максимум 1024 UTF-8 байта. Повторные, неизвестные и
файловые параметры отклоняются. Create принимает login/role/password/password_confirm
и csrf_token; disable — confirm и csrf_token. Поля actor_id, password_hash,
is_active и любые произвольные изменения запрещены. CSRF можно также передать
штатным заголовком middleware, но повторные поля всё равно отклоняются.

## Отключение и подтверждение

Открыть карточку, проверить UUID/login и отметить отдельный checkbox подтверждения.
Сервер требует точное `confirm=disable`, а не только браузерный required.
Интерфейс предупреждает о завершении всех сессий, самоотключении и последнем
активном admin. Перед такими действиями создать и проверить другого admin.

Политика MVP-004 сохранена: самоотключение, включая последнего admin, разрешено
после явного подтверждения. Успех атомарно устанавливает is_active=false,
заполняет revoked_at всех ещё не отозванных staff-сессий и записывает audit;
предыдущие revoked_at сохраняются. Старые cookie на следующем запросе получают
401, повторный login запрещён. Существующая открытая страница не означает доступа.
Повторное отключение уже disabled account через прямой POST сохраняет прежнюю
идемпотентную бизнес-семантику CLI; форма на disabled-карточке скрыта.

При самоотключении ответ после commit удаляет staff-cookie и сообщает, что доступ
завершён. Он не содержит авторизованного layout, нового CSRF или обещания
продолжающегося доступа. Отключение всех admin не открывает bootstrap. Штатного
восстановления/реактивации через этот UI нет; потеря всех admin требует отдельно
согласованной операторской процедуры.

## Общие команды, UoW и аудит

CLI сохраняет прежние create_staff/disable_staff и скрытую парольную аутентификацию
admin. Они передают проверенный AuthenticatedStaff общим create_staff_as_actor /
disable_staff_as_actor. HTTP передаёт StaffIdentity из request.state.staff_identity
и тот же request.state.staff_db, который открыл middleware; фиктивных credentials
или повторной парольной проверки admin нет.

Общая команда берёт lock_staff, требует identity из той же ORM Session, обновляет
роль/активность из PostgreSQL и для StaffIdentity проверяет принадлежность,
revoked_at/expiry серверной сессии. Staff-блокировка удерживается до внешнего commit:
победивший disable запрещает ожидающую административную операцию; уже выполняющаяся
операция заканчивает свою транзакцию перед disable. HTTP-адаптер использует savepoint,
чтобы откатить ошибку команды/аудита до безопасного ответа 503, без преждевременного
error response SQLAdmin. Доменный сервис не вызывает commit/rollback; внешним UoW
владеет middleware. Ошибка команды/аудита/commit откатывает пользователя, отзыв и audit.

Сохраняются staff.create/staff.disable и их outcomes. Отказы форм фиксируются как
invalid_input. Role/session отказы и CSRF/Origin фиксируются прежними staff.access /
staff.csrf. Audit содержит actor UUID, target UUID, result и разрешённые технические
role/is_active; нет login, паролей, cookie, session tokens, hashes или тел запросов.

Ошибки: 401 — недействующая identity; 403 — роль/CSRF/Origin; 404 — отсутствующий
сотрудник; 409 — занятый login; 422 — неверные/повторные/неизвестные поля,
подтверждение или UUID; 413 — превышение размера. Ошибки синтаксиса формы могут
возвращать 400. Ошибки БД дают безопасный 503 без SQL/параметров. При неопределённом
исходе проверить карточку и аудит перед повтором.

## Проверки и границы

Быстрые unit-тесты проверяют строгий ввод, UTF-8/URL-границы и регистрацию методов
SQLAdmin. PostgreSQL 16 acceptance — tests/integration/test_staff_forms.py:
реальный HTTPS TestClient, browser CSRF, роли/сессии, создание обеих ролей,
совместимость с CLI, ревокация нескольких сессий, старые cookie/повторный login,
самоотключение, bootstrap, audit rollback, отсутствие секретов, ограничение списка,
устаревшая identity и гонка disable/create. Точный набор admin.views дополнен Staff.

Новый integration-файл автоматически входит в существующую core selection
scripts/db_integration.py. Новых CI jobs, миграций, изменений auth/CSRF middleware,
cookie-контракта, bootstrap, Alembic или release thresholds нет. Локально выполняются
только unit, Ruff, mypy и git diff --check; PostgreSQL/Compose/browser E2E,
operator/release/backup/load остаются CI. Результаты точного HEAD публикуются в PR.
Независимая MVP-053 не включена. Перед merge проверить свежий main и пересечения
регистрации view, точного набора admin.views и соответствующих пунктов плана.

UI не добавляет регистрацию, SSO, роли, смену ролей, парольное восстановление,
generic CRUD, реактивацию или повторный bootstrap. До независимого ревью и
успешных девяти CI jobs приёмка не завершена. Merge/deploy не выполняются.

```text
FULL_MVP_READINESS=BLOCKED
REDUCED_PILOT_READINESS=BLOCKED
PUBLIC_LAUNCH_RECOMMENDATION=NO-GO
HUMAN_GO_NO_GO_DECISION=PENDING
PRODUCTION_ADMISSION=BLOCKED
```
