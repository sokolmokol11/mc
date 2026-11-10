#!/usr/bin/env bash
# Обновить симулятор: забрать новую версию из GitHub и перезапустить службу.
# Данные ботов (папка data) и пароль (.env) не трогаются.
set -e
cd "$(dirname "$0")"
git -C "$(git rev-parse --show-toplevel)" pull
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
$SUDO systemctl restart trading-sim
sleep 3
$SUDO systemctl --no-pager --lines=15 status trading-sim
