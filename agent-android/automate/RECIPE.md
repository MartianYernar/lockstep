# Phone siren — Automate (LlamaLab)

This is the phone agent that actually works, as of 11 Sep 2026, on a HONOR X9b
(Android 15, MagicOS). It replaced MacroDroid, which asks you to watch an ad
every three days, impossible to do when the app is locked behind hidden apps.

Automate is free, has no ads, and the free tier's 30-block limit is plenty for this.

Three flows. Only the first one is essential.

| Flow | Job | Essential? |
|---|---|---|
| **Lockstep** | asks the server how urgent things are, makes the noise | yes |
| **Lockstep watchdog** | restarts Lockstep every 2 min in case it stopped | backup |
| **Lockstep wake** | restarts Lockstep when a Telegram message arrives | backup, never confirmed firing |

---

## What you need first

- **The token.** It's `api.token` in `config.yaml`. Never put it in this repo.
- **The server address:** `https://<your-app>.up.railway.app`
- **Test it in the phone's browser before building anything:**

  ```
  https://<your-app>.up.railway.app/agent/gap?token=YOUR_TOKEN
  ```

  You should see a single number: `0` if nothing is owed, otherwise the seconds
  until the next blast. If the page doesn't load, nothing below will work either.

---

## Flow 1 — Lockstep

Block numbers match the ones Automate shows, so you can check a screenshot
against this list.

```
1   Flow beginning
      ↓
10  Failure catch ──── FAIL ──────────────┐
      ↓ OK                                │
2   HTTP request                          │
      ↓ OK                                │
3   Expression true?                      │
      │ YES             │ NO              │
      ↓                 ↓                 │
4   Audio volume set    7  Delay 1m ──┐   │
      ↓  ◄──────────────────────────────── ┘
5   Vibrate                           │
      ↓                               │
6   Tone play                         │
      ↓                               │
8   Delay max(resp,10) ──┐            │
                         ↓            ↓
                    back to 2 (HTTP request)
```

### Block settings

**10 · Failure catch**
- Retry limit: **`999999`** ← if this is blank the flow dies after the first network drop
- Output variables: leave blank
- `OK` → block 2. `FAIL` → **block 4** (so losing the network makes a noise instead of going silent)

**2 · HTTP request**
- Request URL: `https://<your-app>.up.railway.app/agent/gap?token=YOUR_TOKEN`
- Request method: **GET**
- Save response: **Save to variable as text** (the radio button has to be selected)
- Output variables → **Response content or filename**: `resp`
- Everything else blank

**3 · Expression true?**
- Formula: `resp != "0"`
- `YES` → block 4. `NO` → block 7

**4 · Audio volume set**
- Stream: **Alarm** ← this is what makes it ring through silent mode and Do Not Disturb
- Volume: 90

**5 · Vibrate**
- Pattern: long

**6 · Tone play**
- Tone: **CDMA network busy** (long and harsh; the default *CDMA abbr alert* is a polite single beep)
- Audio stream: **Alarm**
- Volume: 100 (block 4 already sets the Alarm slider, and the two multiply)
- Duration: 3000
- Proceed: the option that waits until the tone finishes, not *Immediately*

**8 · Delay** (the siren path)
- Duration, via the **fx** button: `max(resp, 10)`
- Tick **awake**
- `OK` → back to block 2

**7 · Delay** (the quiet path)
- Duration: 1 minute
- Tick **awake**
- `OK` → back to block 2

### Why it's built this way

Every choice below is here because leaving it out broke the flow at least once:

- **`/agent/gap`, not `/agent/state`.** The JSON endpoint needed a *JSON Parse*
  block, which returns the text `"true"` while the If block compares against a
  real true/false, so it never matched. `/agent/gap` returns one bare number, so
  there's nothing to parse.
- **Failure catch with a huge retry limit.** Every `EAI_NODATA` error in the logs
  was the network dropping: wifi handover, a dead spot, the phone dozing. With no
  catch, a single failure logged *Stopped by failure* and killed the flow. With the
  default limit of 3, a minute without signal used up every retry.
- **`max(resp, 10)`, not plain `resp`.** When a request fails, `resp` still holds
  the *previous* answer. If that was `"0"`, the delay became zero and the loop
  spun: fail, siren, fail, siren, using up the retries in seconds. This was the
  last bug, and it was the one that looked like "HONOR is killing the app".
- **Two delays.** Behind → the server's gap (6s, 5s, 4s as you fall further
  behind). Caught up → a quiet poll once a minute.
- **awake on both delays.** Without it, Android can hold back the timer while the
  screen is off.

---

## Flow 2 — Lockstep watchdog

Automate has no *Flow running?* block, so the watchdog forces a known state
instead of checking one. Stopping a flow that isn't running does nothing, so
this always leaves exactly one copy alive.

```
1  Flow beginning
      ↓
2  Delay 2 minutes   (awake)
      ↓
3  Flow stop  → Lockstep → <untitled>
      ↓
4  Flow start → Lockstep → <untitled>
      ↓
   back to 2
```

- In the Flow URI picker, expand **Lockstep** and select **`<untitled>`**. That's its Flow beginning block.
- **Flow start → "Stop with parent": leave unchecked.** If it's ticked, Lockstep dies whenever the watchdog does.

Mostly redundant now that Failure catch keeps Lockstep alive. It's harmless to keep.

## Flow 3 — Lockstep wake

While you're behind, the server sends a Telegram message every minute
(`escalation.telegram_poke`), deleting the previous one. Android has to deliver
those even when it's holding back app timers. This flow uses them as a wake-up.

```
1  Flow beginning
      ↓
2  Notification posted
     Proceed: When transition
     Package: Telegram
     (everything else blank)
      ↓ YES
3  Flow stop  → Lockstep → <untitled>
      ↓
4  Flow start → Lockstep → <untitled>
      ↓
   back to 2
```

It needs Android's **Notification access** permission for Automate, which is
separate from *Send notifications*. **This flow has never been confirmed to fire.**
If you ever depend on it, check its Log first.

---

## Phone settings (HONOR / MagicOS)

- Automate is in **hidden apps behind a password your father set.** Keep it that
  way. It's the only thing in the whole system you can't switch off on your own.
- **Settings → Battery → App launch → Automate:** *Manage automatically* off, then
  **Auto-launch, Secondary launch and Run in background** all on.
- In Automate's Privileges: **ignore battery optimizations** and **ignore app
  hibernation** ticked.
- Leave **Developer options → Background process limit** on *Standard limit*.

---

## Testing

1. Send `/testsiren` to the bot. The phone should sound within about 10 seconds.
2. **The test that matters:** turn wifi *and* mobile data off for five minutes.
   You should get a siren about every 10 seconds the whole time. When you turn the
   network back on, the flow should carry on by itself and its Log should **not**
   say *Stopped by failure*.
3. Silent mode on, media volume at zero, `/testsiren` again. It should still ring,
   because it's on the Alarm stream.

## When it goes quiet

| Symptom | Cause |
|---|---|
| Log ends in *Stopped by failure* | Retry limit blank or small, or block 8 isn't `max(resp, 10)` |
| Sirens a few times, then stops | Same: the retries ran out |
| Bot says *"Your phone hasn't checked in for N min"* | The flow isn't running. Open Automate and press Start on all three |
| Browser URL doesn't load | Server or token problem, not the phone |
| Siren fires constantly even when caught up | Expression still says `"QUIET"`, but `/agent/gap` answers `0` |

You can see the phone's traffic from the server side:

```
railway logs --deployment | Select-String Dalvik
```

A healthy phone shows a request about every 10 seconds when you're behind, and
once a minute when you're not.
