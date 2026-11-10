# MVP-047 — шаблон экспертной приёмки

**PENDING. Шаблон не является свидетельством и не подписан от лица эксперта.**
Заполненный документ, реальные имена, тексты и evidence хранятся в защищённом
операторском реестре вне Git/CI. Не использовать production-переписки.

| Поле | Решение эксперта / ссылка на защищённую запись |
| --- | --- |
| ID / версия корпуса | Не предоставлено |
| SHA-256 canonical manifest | Не предоставлено |
| SHA-256 questions + expected answers | Не предоставлено |
| SHA-256 sections / каждого исходного материала | Не предоставлено |
| SHA-256 logical keys → document/version/section UUID mapping | Не предоставлено |
| Relevant question IDs (не менее 20, заранее locked) | Не предоставлено |
| Для каждого relevant ID: ожидаемые разрешённые logical keys и section UUID | Не предоставлено |
| Информационная потребность / тема / критерий релевантности | Не предоставлено |
| Negative question IDs (не менее 20), irrelevant/unsafe | Не предоставлено |
| Для каждого negative ID: no_match / точная безопасная цитата / handoff | Не предоставлено |
| Отсутствие непроверенных технических/ремонтных инструкций | Не подтверждено |
| Точность полного текста и границ каждого фрагмента | Не подтверждено |
| Полезность первых 600 символов, в том числе совпадений позже границы | Не подтверждено |
| assistant_allowed для каждого фрагмента | Не подтверждено |
| Право распространения полного public-файла каждой доступной редакции | Не подтверждено |
| Основание прав / отдельный защищённый rights_reference | Не предоставлено |
| Эксперт / reference принятого решения | Не предоставлено |
| Дата реально принятого решения | Не предоставлено |
| Решение APPROVED / REJECTED / PENDING | PENDING |
| Измеренный HitRate@5, negative safety и приватный отчёт | NOT_MEASURED |
| Первоначальный locked digest / версия исправления / дельта | Не предоставлено |

Для external adapter запись `decision` должна содержать `APPROVED`, точные
`corpus_sha256`, `mapping_sha256`, полный набор `relevant_query_ids`,
`negative_query_ids`, словарь `expected_sections`, реальные `reference`,
`rights_reference`, `reviewer`, `approved_on` и подтверждения
`quote_boundaries_checked`, `no_unverified_instructions`,
`full_file_public_allowed`. Manifest содержит те же реквизиты в
`expert_approval`, каждый вопрос — `expert_status: approved`.

Подпись эксперта и проверка прав — внешние действия. Структурная валидация JSON,
успешный импорт и synthetic fixture review их не заменяют. Решение привязано
к checksum фактически проверенной версии; после изменений нужен новый review.
Production admission остаётся отдельным BLOCKED gate MVP-050.
