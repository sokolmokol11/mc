#!/usr/bin/env bash
# =====================================================================
#  Установка симулятора на сервер как службы (работает 24/7, сам
#  перезапускается при сбое и после перезагрузки сервера).
#  Запуск:   bash install_server.sh
#  Повторный запуск безопасен — пароль и данные ботов сохраняются.
# =====================================================================
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
SERVICE=trading-sim

echo "== 1. Ищу Python 3.8+ =="
PY=""
for p in python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    PY="$(command -v "$p")"; break
  fi
done
[ -z "$PY" ] && { echo "Не найден Python 3.8 или новее"; exit 1; }
echo "   $PY ($("$PY" --version))"

echo "== 2. Ставлю библиотеку requests =="
"$PY" -m pip install --user -q --disable-pip-version-check "requests>=2.28" "PySocks>=1.7" \
  || "$PY" -m pip install -q --disable-pip-version-check "requests>=2.28" "PySocks>=1.7"

echo "== 3. Настройки (.env) =="
if [ ! -f .env ]; then
  PASS="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(9))')"
  cat > .env <<EOF
SIM_SERVER=1
SIM_HOST=0.0.0.0
SIM_PORT=8000
SIM_USER=maxim
SIM_PASSWORD=$PASS
# Если Bybit с сервера не открывается — впиши прокси, например:
# SIM_PROXY=socks5h://user:pass@1.2.3.4:1080
SIM_PROXY=
EOF
  chmod 600 .env
  echo "   создан .env с новым паролем"
else
  echo "   .env уже есть — оставляю как есть"
fi
set -a; . ./.env; set +a

echo "== 4. Проверяю связь с Bybit =="
if "$PY" - <<'EOF'
import os, sys
sys.path.insert(0, ".")
import config
from core.bybit import BybitClient
try:
    n = len(BybitClient(config.BASE_URLS, config.BYBIT_PROXY).tickers())
    print(f"   OK: Bybit отвечает, фьючерсов: {n}")
except Exception as e:
    print(f"   ОШИБКА: {e}")
    sys.exit(1)
EOF
then :; else
  echo
  echo "   Bybit с этого сервера не отвечает. Впиши SOCKS5-прокси в $DIR/.env (строка SIM_PROXY=...)"
  echo "   — можно тот же, что используют твои Telegram-боты, — и запусти скрипт ещё раз."
  exit 1
fi

echo "== 5. Создаю службу $SERVICE =="
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
$SUDO tee /etc/systemd/system/$SERVICE.service >/dev/null <<EOF
[Unit]
Description=Crypto trading simulator (paper trading, Bybit data)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$DIR
EnvironmentFile=$DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY $DIR/main.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now $SERVICE
$SUDO systemctl restart $SERVICE

echo "== 6. Открываю порт $SIM_PORT =="
if command -v firewall-cmd >/dev/null 2>&1 && $SUDO firewall-cmd --state >/dev/null 2>&1; then
  $SUDO firewall-cmd --permanent --add-port=${SIM_PORT}/tcp >/dev/null && $SUDO firewall-cmd --reload >/dev/null
  echo "   firewalld: порт открыт"
else
  echo "   firewalld не запущен — пропускаю"
fi

sleep 4
IP="$(curl -s -4 --max-time 5 ifconfig.me || hostname -I | awk '{print $1}')"
echo
echo "================================================================"
if $SUDO systemctl is-active --quiet $SERVICE; then
  echo "  ✅ Симулятор работает 24/7 как служба '$SERVICE'"
else
  echo "  ⚠ Служба не запустилась. Смотри: journalctl -u $SERVICE -n 50"
fi
echo
echo "  Страница:  http://$IP:$SIM_PORT"
echo "  Логин:     $SIM_USER"
echo "  Пароль:    $SIM_PASSWORD      (хранится в $DIR/.env)"
echo
echo "  Журнал в реальном времени:  journalctl -u $SERVICE -f"
echo "  Обновить после git pull:    bash update_server.sh"
echo "  Остановить:                 systemctl stop $SERVICE"
echo "================================================================"
