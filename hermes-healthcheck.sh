#!/usr/bin/env bash
# Ежедневная проверка гермес-гейтвеев -> статус в Telegram владельцу.
# Запускается через launchd: com.local.hermes-healthcheck
set -uo pipefail

CFG="$HOME/.config/tgbridge/config.env"
val() { grep -E "^$1=" "$CFG" 2>/dev/null | head -1 | sed -E "s/^$1=//; s/^[\"']//; s/[\"']\$//"; }
TOKEN="$(val TG_BOT_TOKEN)"; CHAT="$(val TG_ALLOWED_ID)"
[ -n "$TOKEN" ] && [ -n "$CHAT" ] || { echo "нет TG_BOT_TOKEN/TG_ALLOWED_ID в $CFG" >&2; exit 1; }

# профиль:метка launchd
PROFILES=(
  ".hermes:ai.hermes.gateway:основной"
  ".hermes-mom:ai.hermes-mom.gateway:мама"
  ".hermes-wife:ai.hermes-wife.gateway:жена"
  ".hermes-rishat:ai.hermes-rishat.gateway:ришат"
)

problems=0
lines=""

for entry in "${PROFILES[@]}"; do
  home="${entry%%:*}"; rest="${entry#*:}"
  label="${rest%%:*}"; name="${rest#*:}"
  logf="$HOME/$home/logs/gateway.log"

  pid=$(launchctl list 2>/dev/null | awk -v l="$label" '$3==l {print $1}')

  if [ -z "$pid" ] || [ "$pid" = "-" ]; then
    lines="${lines}❌ <b>${name}</b> — сервис не запущен"$'\n'
    problems=$((problems+1)); continue
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    lines="${lines}❌ <b>${name}</b> — процесс ${pid} мёртв"$'\n'
    problems=$((problems+1)); continue
  fi

  conns=$(lsof -p "$pid" -i -a 2>/dev/null | grep -c ESTABLISHED)

  # последнее событие подключения/отключения в логе
  last=$(grep -E "reconnected successfully|Connected to Telegram|Disconnected from Telegram|could not reconnect" "$logf" 2>/dev/null | tail -1)
  if echo "$last" | grep -qE "reconnected successfully|Connected to Telegram"; then
    state="on"
  elif [ -z "$last" ]; then
    state="unknown"
  else
    state="off"
  fi

  # ошибки за последние сутки
  yday=$(date -v-1d +%Y-%m-%d 2>/dev/null || date +%Y-%m-%d)
  today=$(date +%Y-%m-%d)
  errs=$(grep -cE "^($today|$yday).*(ERROR|could not reconnect)" "$logf" 2>/dev/null)
  [ -z "$errs" ] && errs=0

  # аптайм процесса
  up=$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')

  if [ "$state" = "on" ] && [ "$conns" -gt 0 ]; then
    icon="✅"
  elif [ "$state" = "on" ]; then
    icon="⚠️"; problems=$((problems+1))
  else
    icon="❌"; problems=$((problems+1))
  fi

  extra=""
  [ "${errs:-0}" -gt 0 ] && extra=" · ошибок за сутки: ${errs}"
  lines="${lines}${icon} <b>${name}</b> — pid ${pid}, соединений ${conns}, аптайм ${up}${extra}"$'\n'
done

# доступность Telegram API
api=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 https://api.telegram.org 2>/dev/null)
[ "$api" = "302" ] || [ "$api" = "200" ] && apitxt="доступен" || { apitxt="НЕДОСТУПЕН (код $api)"; problems=$((problems+1)); }

if [ "$problems" -eq 0 ]; then
  head="🟢 <b>Гермесы: всё в норме</b>"
else
  head="🔴 <b>Гермесы: проблем — ${problems}</b>"
fi

msg="${head}
$(date '+%d.%m.%Y %H:%M %Z')

${lines}
Telegram API: ${apitxt}"

curl -sS -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --form-string "chat_id=${CHAT}" \
  --form-string "text=${msg}" \
  --form-string "parse_mode=HTML" \
  -o /dev/null
