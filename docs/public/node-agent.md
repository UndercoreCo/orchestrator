# Исходящий API и подключение узлов

Оркестратор вызывает HTTPS API собственного Undercore Amnezia-agent. SSH не входит
в обработку клиентского запроса. У каждого узла свой служебный токен, HTTPS origin
и стабильный `server_id`. Идентичность проверяется в ответе `connection` либо
через health перед прежними операциями; успешного TCP-подключения недостаточно.
Переадресации не выполняются.

| Запрос к агенту | Действие |
|---|---|
| `GET /v1/health` | Проверить `status`, `server_id`, `protocol` |
| `GET /v1/clients` | Получить занятость и сверить назначения |
| `POST /v1/clients` | Создать peer идемпотентно по `external_id` |
| `GET /v1/clients/{remote_id}` | Проверить peer и срок |
| `POST /v1/clients/{remote_id}/renew` | Продлить срок с `idempotency_key` |
| `POST /v1/clients/{remote_id}/disable` | Подтверждённо отозвать peer |
| `POST /v1/clients/{remote_id}/enable` | Включить peer в пределах срока |
| `POST /v1/clients/{remote_id}/replace` | Повторяемая замена по ожидаемой идентичности |
| `GET /v1/clients/{remote_id}/connection` | Атомарно сверить доступ и получить параметры за один reconcile |
| `GET /v1/clients/{remote_id}/configuration` | Получить документ AWG |
| `GET /v1/clients/{remote_id}/amnezia` | Получить legacy `vpn://` представление |

Создание передаёт `external_id` (ключ конкретного назначения), `device_id: account-v1`
(совместимость агента), `name`, `expires_at` (UTC с миллисекундами). Этот `device_id`
агента не равен каноническому идентификатору устройства во входящем API.

Драйвер проверяет идентификаторы ответа. Реальный `remote_id` хранится отдельно
от стабильного `connection_id` оркестратора. Конфигурации возвращаются с `no-store`,
не сохраняются в журнале оркестратора. Тела ошибок стороннего HTTP не передаются
клиенту и не записываются в диагностические ответы.

## Новый протокол

Реализовать `NodeDriver`, зарегистрировать его в `DriverRegistry`, добавить тесты
повторов, истечения срока и отзыва. Обязательны идемпотентное создание, исполнение
срока на узле и подтверждённый отзыв. Затем включить capability в runtime и клиенте.

TrustTunnel должен возвращать собственный `trusttunnel-toml`, не имитировать AWG.
Его экспериментальный драйвер включается отдельным флагом; [статус и проект API](trusttunnel.md). Типизированный контейнер для параметров уже предусмотрен,
но это не делает произвольный сторонний агент совместимым с сервисом.

## Адреса

`api_url` — адрес управляющего API, не VPN endpoint и не адрес выхода в интернет.
Входной адрес/порт туннеля отдаёт агент; выходной адрес задаёт маршрутизация/NAT
самого VPN-узла. Смена DNS или `api_url` оркестратора сама по себе не меняет egress IP.
Сохранение `server_id` при замене адреса допустимо только для того же логического
узла с сохранёнными peer/journal. Новая пустая машина — новый узел.


## Атомарная выдача

`NodeDriver.connection(node, remote_id, binding_key)` возвращает `NodeConnection`:
состояние клиента и типизированный `TransportConfiguration`. Протокольный драйвер
проверяет принадлежность до выдачи параметров; application дополнительно сверяет
назначение, срок, capability, локальный запрет, lease и незавершённое переключение.

Amnezia-agent отвечает на `GET /v1/clients/{id}/connection` JSON-объектом:
`schema_version: 1`, `status: ok`, `protocol: amneziawg`, `server_id`, `client`
(прежний ClientView), `configuration` (секретный AWG-документ). HTTP/RPC выполняют
одну сериализованную сверку с runtime. Неактивный/истёкший доступ возвращает 410,
просроченный control lease — 503. При отсутствии настроенного server_id выдача
запрещена. Ответ требует `Cache-Control: no-store`; весь JSON ограничен 4 MiB,
документ внутри — 64 KiB. Ответ и ключи нигде не журналируются.

Только 404 либо 405 с `Allow: POST` переводит драйвер на прежнюю
последовательность health/get/health/export. Второй вариант возникает у старого
агента из-за общего POST-маршрута операций; прочие 405 не допускают fallback.
Это позволяет поочерёдно обновлять узлы; неизвестный клиент остаётся 404 после
проверки старым маршрутом. Ошибки авторизации, 409/410/503, неверная идентичность,
схема или отсутствие no-store не вызывают downgrade. Проверка идёт на каждом
запросе: отрицательный кэш возможностей узла и кэш секретных документов отсутствуют.
Публичный контракт v2, ревизии и SQLite-схема не изменены.

## Адаптеры экспорта

`NodeDriver.export(node, remote_id, format, qr_content_format)` возвращает
`ExportDocument`; `DriverCapabilities.export_formats` перечисляет возможности.
Адаптер выбирает исходный документ и валидирует его. Общий renderer на Segno
строит PNG из уже выбранных байтов, не знает о серверах, аккаунтах и протоколах.
`AmneziaExports` допускает conf/amnezia-vpn/qr; `WireGuardExports` — conf/qr.
HTTP-слой не содержит ветвления по этим протоколам. Прежние v1-маршруты остаются
для совместимости; новый backend использует v2 export. Агент и VPN runtime
для этого обновления не меняются.

Версия 0.3.5 собирает `.vpn` в адаптере из выданного агентом `.conf`, включая
параметры AWG 3.1. Старый `/amnezia` на узле больше не требуется. Парсер допускает
один Interface/Peer и известные параметры, отклоняет неоднозначные/неизвестные
поля, сохраняет native config и отдельные строковые поля `last_config`.
Ключ клиента вычисляется стандартным X25519; исходные ключи не меняются.
Формат сверён с [AmneziaVPN 5.0.3.0](https://github.com/amnezia-vpn/amnezia-client/tree/de93650a90739b87bb47a632872ea9d0adc9412f/client/core/models/protocols).
Неизвестные параметры нельзя молча отбрасывать для создания файла старой версии.

## Короткие разрешения и отказ контроллера

Узлы с lease_enabled должны подтверждать поддержку коротких разрешений и прекращать
доступ после их истечения. Оркестратор периодически продлевает их отдельным worker.
Длительная остановка контроллера поэтому может остановить VPN даже на исправном узле.
При недоступности старого узла перенос ждёт безопасное истечение разрешения;
обычный HTTP-таймаут не заменяет это ожидание. После включения leases откат допустим
только на код, который продолжает исполнять этот контракт.

Для разработки другого протокола: [руководство по драйверам](drivers.md).


## DNS names in AmneziaWG exports

Configure a stable node-specific DNS name in the node agent's public `endpoint`
(for example `vpn-node-1.example.net:8443`). This is separate from the node's
management `api_url`, listener bind address and egress/NAT IP. Do not resolve the
name into a numeric IP when generating exports. The Amnezia adapter preserves it
in native configurations, guest `.vpn` documents, QR payloads and native manifests.

Changing only this endpoint on the same server does not require creating a new
assignment or changing keys. Existing downloaded/imported files are static:
re-export and import the same assignment once to replace their old literal IP.
For later DNS changes, clients may need to reconnect to resolve the new address;
TTL alone does not guarantee live tunnel migration. A DNS name must keep pointing
to the node that owns the matching peer and server keys.

The current AWG executor and state agent cache endpoint metadata at startup.
Follow the node's controlled restart procedure after editing configuration, with
a protected backup and rollback. This can briefly interrupt its tunnels; editing
a file alone is not evidence that exports now contain the new endpoint.
This guidance concerns AmneziaWG; other drivers define their own address contracts.
