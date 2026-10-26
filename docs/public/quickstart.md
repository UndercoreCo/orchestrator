# Undercore Orchestrator

Самостоятельный сервис управления VPN-узлами. Python 3.12+, SQLite, Linux/systemd.
Оркестратор принимает запросы доверенного backend, управляет назначениями и
возвращает конфигурацию. VPN-трафик идёт напрямую между приложением и VPN-узлом.
Сайт Undercore не нужен для установки или управления.

## Локальный запуск

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps .
.venv/bin/undercore-orchestrator init --directory "$PWD/local"
.venv/bin/undercore-orchestrator --settings "$PWD/local/settings.json" config validate
.venv/bin/undercore-orchestrator --settings "$PWD/local/settings.json" serve
```

`init` создаёт новый приватный каталог, ключ backend и настройки; повторный запуск
не перезаписывает их. Добавьте `local/` в локальное исключение Git или используйте
каталог вне checkout. Пустой реестр допустим: health процесса работает, но выдать
подключение без сервера невозможно. Нельзя путать health процесса с проверкой VPN.

## Добавление сервера

Нужен совместимый Amnezia-agent. Обычный сервер Amnezia без управляющего агента не
подойдёт. [Контракт агента](node-agent.md) описывает обязательные операции и гарантии.
TrustTunnel доступен экспериментально с явным флагом и совместимым managed-agent;
его публичная установка ещё не поставляется. Установка агента на VPS — отдельная операция.

Скопируйте `config/server.example.json` в приватный файл вне Git. Укажите HTTPS URL,
закреплённый `server_id`, регион, ёмкость и путь к отдельному файлу ключа
`api_key_file`. Оба файла должны иметь права 0600. Не вставляйте ключ в аргументы shell.

```sh
undercore-orchestrator --settings /path/to/settings.json add server --file /private/server.json
undercore-orchestrator --settings /path/to/settings.json list servers
undercore-orchestrator --settings /path/to/settings.json check server node-a
```

Для редактирования используйте полный документ сервера с `expected_revision` из
списка. Поле ключа можно опустить, сохранив текущий ключ:

```sh
undercore-orchestrator update server node-a --file /private/change.json
undercore-orchestrator restore server node-a
```

Запускайте команды управления от пользователя службы `vpn-orchestrator`, например
через `sudo -u vpn-orchestrator`. CLI работает локально с теми же сценариями,
транзакциями и блокировками, что API. Сайт и административный HTTP API не нужны.

## Установка службы

На чистом Linux нужны Python 3.12+, venv, pip и systemd. Скачайте проверенный релиз
и SHA256SUMS. Скрипт `scripts/install.py` из доверенной версии репозитория проверяет
архив, создаёт отдельного системного пользователя, каталоги и службы. Существующую
установку он не перезаписывает:

```sh
sudo python3 scripts/install.py /path/to/release.tar.gz --sha256 CHECKED_SHA256
sudo undercore-orchestrator start
undercore-orchestrator status
sudo undercore-orchestrator restart
sudo undercore-orchestrator stop
```

`start/stop/restart` доступны только на Linux/systemd. Остановка включает API,
оба таймера и фоновые обработчики. Длительная остановка прекращает продление
разрешений на узлах, поэтому управляемый VPN-доступ может остановиться.
Установщик не открывает firewall, не выдаёт TLS-сертификат и не добавляет серверы.
API слушает loopback: для удалённого backend настройте HTTPS reverse proxy и
ограничение источников. Все маршруты требуют backend-токен; не передавайте его клиентам.

## Обновление и резервная копия

Существующий контролируемый `scripts/update.py` проверяет архив и миграцию на копии
журнала, сохраняет backup и умеет вернуть код без восстановления устаревшей БД.
Процедура описана в [обслуживании](operations.md).

```sh
sudo -u vpn-orchestrator undercore-orchestrator backup --output /private/new-backup
```

Копия включает консистентный SQLite snapshot, настройки, секреты узлов и backend,
карту исходных путей. Это секретный архив-каталог, не диагностический экспорт.
Не храните его в Git. Восстановление требует остановки всех writers, восстановления
файлов по карте с правами 0600/0700 и сверки с агентами. Старый backup не отменяет
выданный после него доступ на VPN-узлах; слепой откат запрещён.

## Разработка

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_contracts.py
.venv/bin/ruff check orchestrator scripts tests
.venv/bin/ruff format --check orchestrator scripts tests
```

Интеграционные тесты используют закреплённый агент с подменённым VPN backend;
они не подключаются к серверам. [Архитектура](architecture.md),
[настройки](configuration.md), [API](api.md).
