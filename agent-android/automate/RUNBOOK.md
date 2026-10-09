# The phone, set up once, in order

What to do on the phone so the siren actually runs, and so stopping it is not a
one-tap decision made at 21:00 by someone who will regret it at 23:00.

His own words, 9 Oct 2026: *"my dad to be honest doesn't read those
notifications, the real ceiling is the block and that he holds password... I am
smart enough to give him phone to set up password but am prone to short term
temptations, although am disciplined and aware in long term."*

That is the design brief. Alerts to the guardian are a **record**, not a wall.
The wall is: during the hours he is tempted, the stop button is behind a
password he does not have. Everything below is ordered by how much it actually
holds.

---

## 0 · First, work out which flow is the real one

Automate numbers flows internally, and there are old copies on this phone.
Every "Stop flow" and "Start flow" block points at a URI with a number in it,
and pointing at the wrong number is a watchdog that guards nothing.

1. Open the **Lockstep** flow (the one with the HTTP request and the siren).
2. Tap block **1 · Flow beginning**.
3. Under **Current values**, read **Flow URI**. It looks like:

   ```
   content://com.llamalab.automate.provider/flows/10/statements/1
   ```

4. Write that number down. Call it **N** (it was 10 on 9 Oct 2026).

While that dialog is open, check two options:

- **Allow parallel launches from this block** — leave it **UNCHECKED**. This is
  what makes the keeper safe: starting an already-running flow does nothing
  instead of running a second copy.
- **Don't show in list of starting points** — leave it unchecked.

---

## 1 · The Lockstep flow itself

Block settings are in `RECIPE.md`; this is only the short check.

| block | must say |
|---|---|
| 10 · Catch failure | retry limit `999999`, `OK` → 2, `FAIL` → 11 |
| 2 · HTTP request | the `/agent/gap?token=...` URL, response saved to `resp` |
| 3 · Expression | `(resp != "0") && (resp != "-1")`, YES → 4, NO → 7 |
| 11 · Expression | the window formula (below), YES → 4, NO → 7 |
| 4, 5, 6 | alarm volume 90, vibrate, tone — `CDMA network busy` is the harsh one |
| 8 · Delay | `max(resp, 10)`, awake, → 2 |
| 7 · Delay | 1 minute, awake, → 2 |

Block 11's formula, as of the 17:00 weekday start:

```
(resp != "-1") && ((((dateParts(clock("realtime"))[3] * 60) + dateParts(clock("realtime"))[4]) >= (((dateParts(clock("realtime"))[7] = 0) || (dateParts(clock("realtime"))[7] = 6)) ? 600 : 1020)) && (((dateParts(clock("realtime"))[3] * 60) + dateParts(clock("realtime"))[4]) < 1320))
```

`1020` = 17:00 weekdays, `600` = 10:00 weekends, `1320` = 22:00 both. **These
three numbers are a copy of the server's `enforce_start`/`enforce_end`. Change
the server, change these.**

---

## 2 · The keeper (new flow)

One flow, four blocks. It does nothing while Lockstep is running, and restarts
it within a minute if it stops.

```
1  Flow beginning
     ↓
5  Catch failure ── FAIL ──┐   (retry 999999)
     ↓ OK                  │
2  Delay 1m awake          │
     ↓                     │
3  Flow start  (flow N)    │
     ↓ OK                  │
     └───────── back to 2 ─┘
```

- **5 · Catch failure** — retry limit `999999`. Without it, one error (starting
  a flow that is already running may count as one) kills the keeper silently.
- **2 · Delay** — 1 minute, **awake** ticked.
- **3 · Flow start**
  - Flow URI: `content://com.llamalab.automate.provider/flows/N/statements/1`
    — the exact string from step 0, with the real N.
  - **Stop with parent: OFF.** If this is on, stopping the keeper also kills the
    siren, which is the opposite of the point.
  - `OK` → back to block 2.

Because parallel launches are off on Lockstep's beginning, block 3 is a no-op
while Lockstep is alive. No duplicates, no double polling.

**Do not add a "Stop flow" block to the keeper.** The two older flows
(*Lockstep wake*, *Lockstep wat...*) stop and restart the flow every 2 minutes,
which interrupts a sounding siren and re-polls constantly for no gain. Once the
keeper runs, delete both — or at minimum stop them, so three flows are not
fighting over one.

## 3 · Start both, and survive a reboot

1. Start **Lockstep**, then start **keeper**. Both now show in Automate's
   persistent notification.
2. Automate → **Settings** → turn on **Run on system startup**. Per Automate's
   docs this resumes the fibers that were running at shutdown, so as long as
   both are running when the phone restarts, both come back. There is no
   per-flow boot checkbox.
3. Reboot the phone and check the notification lists both again. If it doesn't,
   Android killed them: give Automate "no battery restrictions" / "allow
   background activity" in the phone's battery settings.

---

## 4 · The part that actually holds: the block and the password

Without this, everything above is a ten-second undo.

1. **Hand the phone to dad.** He sets a password in Switchly that you do not
   know, and in his own Switchly block list he adds, for the work hours:
   - **Automate** — so you cannot open the app to stop the keeper
   - **Settings** — so you cannot force-stop Automate from Android's app list
     or "Active apps"
2. Test, with the block active: try to open Automate. It should refuse, and the
   siren should keep polling. Verify from the laptop that polls are still
   arriving (ask Claude; it can read the server's poll log).
3. **What stays possible, honestly:** Android guarantees a way to stop a
   foreground service. The Stop button in Automate's own notification is still
   there, and stopping both flows within the keeper's one-minute window still
   works. The block makes it awkward and deliberate, not impossible.
4. The laptop has the same shape: dad holds the Windows admin password, you are
   a Standard user, and the hosts file blocks youtube/tiktok/instagram for every
   app including Claude's built-in browser.

---

## 5 · The three tests

Run these once after setup, then whenever anything changes.

| test | do this | expected |
|---|---|---|
| siren | weekday after 17:00, with a block due, ignore the photo | alarm within ~3 min |
| offline | same hours, turn wifi off | alarm within ~2 min |
| silence | weekday morning, or after 22:00, turn wifi off | nothing, ever |
| keeper | hit **Stop** on the Lockstep notification | it is back within 1 min |

If the offline test is silent, block 11's numbers or the phone's clock are
wrong. If the morning test sirens, block 11 is wired to the wrong path.

---

## 6 · When the siren is dead anyway

The server notices a phone that stops checking in and says so in Telegram
within a minute or two. Nothing on the phone can enforce anything once Automate
is stopped — the record is the enforcement at that point: the checkpoints still
go unanswered, the misses still land in the nightly report, and the Sunday
review still counts them.

Which is the honest summary of the whole system: it cannot make him do the
work. It can make sure that not doing it is never invisible, and that the
easiest path at 21:00 is the one he chose at 15:00.
