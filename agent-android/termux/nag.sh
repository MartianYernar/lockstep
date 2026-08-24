#!/data/data/com.termux/files/usr/bin/bash
# ---------------------------------------------------------------------------
# Lockstep phone agent (Termux).
#
# Polls the server. While you are behind, it raises the ALARM stream to full
# and speaks/vibrates on it.
#
# Why the alarm stream specifically: on Android the alarm stream ignores silent
# mode and Do Not Disturb, and it has its own volume slider -- separate from
# ring, media and call. So this can be impossible to mute while your ringtone,
# your music and your actual phone calls keep behaving completely normally.
# That was the requirement: hard to silence, without breaking the phone.
#
# It does NOT slam the volume to maximum -- the server tells it what percentage
# to use (55% by default). Loud enough to be impossible to ignore, not loud
# enough to hurt. And it remembers your alarm volume before it touches it and
# puts it back the moment you're clear, so your real morning alarm stays how you
# set it.
# ---------------------------------------------------------------------------
set -uo pipefail

CONF="$HOME/.lockstep-agent.json"
STATE="$HOME/.lockstep-phone-state"
LOG="$HOME/lockstep-agent.log"

[ -f "$CONF" ] || { echo "Missing $CONF — see install.md"; exit 1; }
SERVER=$(jq -r '.server' "$CONF")
TOKEN=$(jq -r '.token' "$CONF")
POLL=$(jq -r '.poll_sec // 60' "$CONF")

log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" >> "$LOG"; }

max_alarm_volume() { termux-volume | jq -r '.[] | select(.stream=="alarm") | .max_volume'; }
cur_alarm_volume() { termux-volume | jq -r '.[] | select(.stream=="alarm") | .volume'; }

# percentage of max -> an actual step on this device's alarm slider, min 1
target_alarm_volume() {
  local pct="$1" max
  max=$(max_alarm_volume)
  awk -v m="$max" -v p="$pct" 'BEGIN{v=int(m*p/100+0.5); if(v<1)v=1; if(v>m)v=m; print v}'
}

# Keep the CPU awake so Doze can't quietly kill the loop overnight.
termux-wake-lock
trap 'termux-wake-unlock; exit 0' INT TERM

log "phone agent up · server=$SERVER · poll=${POLL}s"
WAS_ALARMING=0

while true; do
  RESP=$(curl -fsS --max-time 15 -H "X-Lockstep-Token: $TOKEN" \
         "$SERVER/agent/state" 2>/dev/null)

  if [ -z "$RESP" ]; then
    log "server unreachable — staying quiet"
    sleep "$POLL"; continue
  fi

  ACTIVE=$(echo "$RESP"  | jq -r '.active')
  ALARM=$(echo "$RESP"   | jq -r '.phone_alarm')
  MINUTES=$(echo "$RESP" | jq -r '.debt_minutes')
  HEADLINE=$(echo "$RESP"| jq -r '.headline')
  VOLPCT=$(echo "$RESP"  | jq -r '.alarm_volume_pct')
  REPEATS=$(echo "$RESP" | jq -r '.alarm_repeats')
  POLL=$(echo "$RESP"    | jq -r '.poll_sec')

  if [ "$ACTIVE" = "true" ] && [ "$ALARM" = "true" ]; then
    if [ "$WAS_ALARMING" = "0" ]; then
      cur_alarm_volume > "$STATE"                 # remember it before we stomp on it
      WAS_ALARMING=1
      log "escalating — saved alarm volume $(cat "$STATE"), will use ${VOLPCT}%"
    fi

    termux-volume alarm "$(target_alarm_volume "$VOLPCT")"
    termux-notification \
      --id lockstep --title "Lockstep — $MINUTES min behind" \
      --content "$HEADLINE" --priority max --alert-once false \
      --ongoing --action "termux-open-url https://t.me/$(jq -r '.bot_username // "" ' "$CONF")"

    # Escalation is repetition, not volume: the further behind, the more times
    # it goes off in the same minute. The level you set is never exceeded.
    for _ in $(seq 1 "${REPEATS:-1}"); do
      termux-vibrate -d 1500 -f
      termux-tts-speak -s ALARM \
        "Lockstep. You are $MINUTES minutes behind. $HEADLINE"
      sleep 2
    done

  else
    if [ "$WAS_ALARMING" = "1" ]; then
      if [ -s "$STATE" ]; then
        termux-volume alarm "$(cat "$STATE")"     # give the slider back
        log "cleared — restored alarm volume $(cat "$STATE")"
      fi
      termux-notification-remove lockstep 2>/dev/null
      WAS_ALARMING=0
    fi
  fi

  sleep "$POLL"
done
