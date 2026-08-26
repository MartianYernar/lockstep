# Proving it works before you trust it with a year

You asked to test everything and be sure it won't break after a few days. Good
instinct — most of what's gone wrong so far was found by running it, not by
reading it.

Work down this list. Each item says what to do and what should happen.

---

## Stage 1 — Smoke test (30 min, do it in one sitting)

| # | Do this | Should happen |
|---|---|---|
| 1 | `/status` | Shows window, guardian password set, phone agent state |
| 2 | `/plan` then a plan with a **gap in it** | Refused, names the exact gap |
| 3 | Send a proper plan | Locked in, tells you the real first-photo time |
| 4 | `/today` | Your plan, with the score so far |
| 5 | Wait for a checkpoint | Telegram asks for a photo |
| 6 | Send a photo of **the wrong thing** | Rejected, with a reason |
| 7 | Send a photo **with the timestamp off** | Rejected: no visible clock |
| 8 | Send a **correct** photo | Counted |
| 9 | `/offset` with a real reason + photo | Day shifts an hour, father told |
| 10 | `/offset` again same day | Refused — one a day |

If any of these misbehave, stop and tell me before going further.

## Stage 2 — The punishment actually lands (20 min)

**This is the part most worth testing, because it's the part you'll want to
fail quietly.**

| # | Do this | Should happen |
|---|---|---|
| 11 | Ignore a checkpoint for 6 minutes | Phone sirens on the alarm stream |
| 12 | Put the phone on **silent**, ignore another | **Still sirens** — alarm stream ignores silent |
| 13 | Turn the media volume to 0 | Siren unaffected |
| 14 | Check your ringtone still works normally | Untouched |
| 15 | Ignore for 25 minutes | Siren goes off **twice** a minute |
| 16 | Send a valid photo | Everything stops within a minute |

If 12 fails, MacroDroid is using the wrong audio stream. That's the single most
important setting in the whole phone setup.

## Stage 3 — Try to break it on purpose (20 min)

Do these deliberately. Better you find the holes than discover them at 11pm.

| # | Do this | Should happen |
|---|---|---|
| 17 | **Block the bot in Telegram** | Father is told within a minute. Siren keeps running. |
| 18 | Unblock it | Father told it was lifted |
| 19 | **Force-stop MacroDroid** | Within ~5 min Telegram says the phone agent is dead |
| 20 | Leave it dead an hour | Father is told too |
| 21 | Restart MacroDroid | `/status` shows 🟢 alive again |
| 22 | `/disable` and type a reason | Father gets your reason **before** the password prompt |
| 23 | Enter a wrong password 3× | Locked out 30 min, father told each time |
| 24 | Photograph a photo on another screen | Rejected as a re-photograph |
| 25 | Send a photo taken an hour ago | Rejected as stale |

## Stage 4 — Soak test (3 days, no lock)

Settings for this stage:

```yaml
schedule:
  spot_check_avg_min: 90     # ~4 a day, gentle
commitment:
  months: 0                  # no lock — you can stop any time
```

Then just live with it for three days and watch for:

- **Does it survive the night?** Check `/status` each morning — the phone agent
  should say 🟢, not "silent for 9h". If it dies overnight, battery optimisation
  is still on somewhere.
- **Does the nightly report arrive** at 22:15, to both of you?
- **Any photo wrongly rejected?** Note which and tell me — the prompt can be tuned.
- **Any photo wrongly accepted?** More important. Tell me immediately.
- **Does the offset still work on day 3?** (Catches the once-a-day counter not
  resetting at midnight.)
- **Is 4 a day already too much?** Be honest now. Eight will be double.

## Stage 5 — Before the lock goes on

Only after Stage 4 has run clean for three days:

- [ ] Move the server to the VPS, so it doesn't die with a laptop
- [ ] Point MacroDroid at the VPS address, re-run tests 11 and 17
- [ ] Confirm the nightly report still arrives from the new host
- [ ] Drop `spot_check_avg_min` to 45
- [ ] Set `commitment.months: 3`
- [ ] Have your father confirm he still has the password written down

Then it's live.

---

## Things that will break later, and what they look like

Worth knowing in advance so you diagnose instead of assuming it's broken:

| Symptom | Almost certainly |
|---|---|
| Everything accepted, "flagged in the log" | API credit ran out |
| Phone silent, Telegram fine | MacroDroid killed by battery optimisation |
| Telegram silent, phone screaming | You blocked the bot, or the bot's token changed |
| Nothing at all, both quiet | Server is down — the VPS rebooted and didn't restart it |
| Every photo rejected as stale | Phone clock drifted; set it to network time |
| Checkpoints stop after midnight | Timezone mismatch between server and phone |

The nightly report is the thing that catches all of these. If it doesn't arrive,
something is wrong — that's the point of it.
