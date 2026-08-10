#!/usr/bin/env bash
# Установка мониторинга на Ubuntu Server 24.04 LTS.
# Запускать от root на чистой машине:  sudo bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/goszakup-monitor
APP_USER=goszakup

echo "==> проверяю систему"
command -v python3 >/dev/null || { echo "нет python3"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "нужен Python 3.11+; в Ubuntu 24.04 он уже есть"; exit 1; }

echo "==> ставлю зависимости системы"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip

echo "==> создаю пользователя $APP_USER (без shell, без home)"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> раскладываю приложение в $APP_DIR"
mkdir -p "$APP_DIR"
# Копируем всё, кроме венва с Windows и локальных секретов.
rsync -a --exclude '.venv' --exclude 'config/settings.local.json' \
      --exclude '__pycache__' --exclude '*.db' --exclude '*.db.backup-*' \
      ./ "$APP_DIR/"

echo "==> собираю venv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> права: секреты и база доступны только сервису"
mkdir -p "$APP_DIR/config"
touch "$APP_DIR/monitor.db"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 750 "$APP_DIR"
chmod 700 "$APP_DIR/config"
chmod 600 "$APP_DIR/monitor.db"

echo "==> ставлю systemd-юнит"
cp "$APP_DIR/deploy/goszakup-monitor.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable goszakup-monitor
systemctl restart goszakup-monitor

sleep 3
if systemctl is-active --quiet goszakup-monitor; then
  echo
  echo "ГОТОВО. Сервис поднят и включён в автозапуск."
else
  echo
  echo "СЕРВИС НЕ ПОДНЯЛСЯ. Смотрите: journalctl -u goszakup-monitor -n 50"
  exit 1
fi

cat <<'EOF'

Админка слушает только 127.0.0.1 — авторизации в ней нет, наружу не выставлять.
Открыть её со своего ноутбука через SSH-туннель:

    ssh -N -L 8765:127.0.0.1:8765 <пользователь>@<адрес-сервера>

и открыть http://127.0.0.1:8765 в браузере. Пока туннель висит — админка доступна.

Дальше:
  1. вставить токен goszakup и токен Telegram-бота в админке;
  2. нажать «Проверить сейчас (без отправки)» — убедиться, что лоты находятся;
  3. нажать «Запустить» в разделе «Расписание».

Полезное:
    systemctl status goszakup-monitor     состояние
    journalctl -u goszakup-monitor -f     живые логи
    systemctl restart goszakup-monitor    перезапуск
EOF
