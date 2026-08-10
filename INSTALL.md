# Установка на Ubuntu

Мониторинг лотов goszakup: находит лоты по списку ЕНСТРУ с нулём заявок и близким
дедлайном, шлёт в Telegram. Разворачивается как systemd-сервис.

## Требования

- Ubuntu Server **22.04 или 24.04 LTS** (в 24.04 нужный Python 3.11+ уже стоит).
- Исходящий доступ в интернет: `goszakup.gov.kz`, `api.telegram.org`,
  `docs.google.com` (загрузка таблицы ЕНСТРУ).
- Права `sudo`.
- Ресурсы: **1 vCPU / 2 ГБ RAM / 20 ГБ диска** — с запасом.

## Установка (3 шага)

### 1. Забрать код

```bash
git clone https://github.com/<ВАШ_АККАУНТ>/goszakup-monitor.git
cd goszakup-monitor
```

> Приватный репозиторий попросит логин и пароль — вместо пароля вставьте
> **Personal Access Token** (GitHub → Settings → Developer settings →
> Fine-grained tokens, доступ `Contents: Read` на этот репозиторий).

### 2. Установить

```bash
sudo bash deploy/install.sh
```

Скрипт сам:
- поставит `python3-venv`, `python3-pip`;
- заведёт системного пользователя `goszakup` (без shell, без home);
- разложит код в `/opt/goszakup-monitor`, соберёт venv из `requirements.txt`;
- закроет права на секреты и базу (доступны только сервису);
- поставит и запустит systemd-юнит с автозапуском при перезагрузке.

В конце напечатает статус. Если сервис не поднялся:
`journalctl -u goszakup-monitor -n 50`.

### 3. Вписать токены (headless, без веб-админки)

Токены живут в `config/settings.local.json` (в git не попадают). На сервере без
графики проще всего создать этот файл напрямую:

```bash
sudo -u goszakup tee /opt/goszakup-monitor/config/settings.local.json >/dev/null <<'JSON'
{
  "goszakup_token": "ВАШ_ТОКЕН_ПЛОЩАДКИ",
  "telegram_bot_token": "ВАШ_ТОКЕН_БОТА",
  "telegram_chat_id": "-100XXXXXXXXXX"
}
JSON
sudo chmod 600 /opt/goszakup-monitor/config/settings.local.json
sudo systemctl restart goszakup-monitor
```

При старте сервис **сам запускает расписание**, если токены на месте (и сам
возобновляет его после перезагрузки). Проверить, что пошли проходы:

```bash
journalctl -u goszakup-monitor -f
```

Должно появиться `расписание запущено автоматически при старте сервиса`, а затем
строки о проходах. Остальные параметры (`sheet_url`, `window_hours_min`,
`max_applications`, `min_amount` и т.д.) можно не указывать — берутся из значений
по умолчанию; при желании добавьте их в тот же JSON.

> **Альтернатива — веб-админка.** Если есть входящий доступ к серверу, её можно
> открыть через SSH-туннель (`ssh -N -L 8765:127.0.0.1:8765 user@server`, затем
> <http://127.0.0.1:8765>): там ввод токенов, кнопка «Проверить сейчас (без
> отправки)» и управление расписанием. Админка слушает только `127.0.0.1` и
> авторизации не имеет — наружу не выставлять.

## Эксплуатация

```bash
systemctl status goszakup-monitor      # состояние
journalctl -u goszakup-monitor -f      # живые логи
systemctl restart goszakup-monitor     # перезапуск
```

## Обновление

```bash
cd goszakup-monitor && git pull
sudo bash deploy/install.sh            # переrazложит код и перезапустит сервис
```

Токены и база (`config/settings.local.json`, `monitor.db`) при обновлении не
трогаются — они вне git и лежат в `/opt/goszakup-monitor`.

## Список ЕНСТРУ

Берётся из Google-таблицы (ссылка настраивается в админке; по умолчанию — таблица
классификаторов заказчика). Таблице нужен доступ «Просматривать могут все, у кого
есть ссылка». Пополнять список можно прямо в таблице, код трогать не нужно.

Подробности об архитектуре и особенностях площадки — в [README.md](README.md).
