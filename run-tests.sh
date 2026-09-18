#!/usr/bin/env bash
# Run everything. Do this before trusting the system with a locked commitment.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
PY="${PY:-.venv/bin/python}"
FAIL=0
for t in tests/test_engine.py tests/test_spotcheck.py tests/test_proof.py tests/test_outside.py tests/test_coach.py tests/test_friend.py tests/test_model_chain.py tests/test_recheck.py tests/test_foundations.py tests/test_swap.py tests/test_bedtime_chat.py tests/test_standup.py tests/test_siren_audit.py tests/test_voice.py tests/test_nodrop.py tests/test_reminders.py tests/test_bot.py tests/test_lock_api.py tests/test_e2e.py; do
  echo ""
  echo "═══ $t ═══"
  if ! "$PY" -W ignore "$t" 2>&1 | grep -vE "FutureWarning|warnings\.warn"; then
    FAIL=1
  fi
done
echo ""
[[ $FAIL -eq 0 ]] && echo "✅ everything passed" || echo "❌ something failed"
exit $FAIL
