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
10  Failure catch ─── FAIL ──→  11  Expression true?   (in the window?)
      ↓ OK                           │ YES         │ NO
      ↓                              │             │
2   HTTP request                     │             │
      ↓ OK                           │             │
3   Expression true?  (resp != "0")  │             │
      │ YES                 │ NO     │             │
      ↓  ◄─────────────────────────────┘           │
4   Audio volume set        │                      │
      ↓                     ↓                      ↓
5   Vibrate            7  Delay 1m  ◄──────────────┘
      ↓                     │
6   Tone play               │
      ↓                     │
8   Delay max(resp,10)      │
      │                     │
      └─────────┬───────────┘
                ↓
         back to 2 (HTTP request)
```

### Block settings

**10 · Failure catch**
- Retry limit: **`999999`** ← if this is blank the flow dies after the first network drop
- Output variables: leave blank
- `OK` → block 2. `FAIL` → **block 11** (losing the network makes a noise, unless it's the middle of the night)

**11 · Expression true?** — is this an hour when anything is owed

Only sits on the `FAIL` path, so it has no effect at all while the network is up.
It answers one question: *if I could reach the server right now, could it possibly
be asking me for anything?* If not, silence costs nothing.

- Formula (paste it exactly, brackets and all):

  ```
  (resp != "-1") && ((((dateParts()[3] * 60) + dateParts()[4]) >= (((dateParts()[7] = 0) || (dateParts()[7] = 6)) ? 600 : 1020)) && (((dateParts()[3] * 60) + dateParts()[4]) < 1320))
  ```

- `YES` → block 4 (siren, as before). `NO` → **block 7** (quiet minute, then try the network again)

The `resp != "-1"` at the front is the `/outside` excuse. See below.

`dateParts()` is the phone's own clock. `[3]` is the hour 0–23, `[4]` is the
minute, and `[7]` is the day of the week where **0 is Sunday and 6 is Saturday**.
`hour * 60 + minute` turns the time into minutes past midnight, so the numbers
compare cleanly:

| number | means |
|---|---|
| `600` | 10:00 — weekend window opens |
| `1020` | 17:00 — weekday window opens |
| `1320` | 22:00 — both windows close |

| | offline siren | silent |
|---|---|---|
| Mon–Fri | 17:00 – 22:00 | 22:00 – 17:00 |
| Sat, Sun | 10:00 – 22:00 | 22:00 – 10:00 |

**These three numbers duplicate `enforce_start`, `enforce_end` and `weekend` in
`config.yaml`.** The phone cannot read the config while it's offline — that is the
whole point of this block — so if you ever change the hours on the server, change
them here too or the two will disagree.

If Automate underlines `dateParts()` as an error for taking no arguments, write
`dateParts(clock("realtime"))` everywhere instead; it means the same thing.

**2 · HTTP request**
- Request URL: `https://<your-app>.up.railway.app/agent/gap?token=YOUR_TOKEN`
- Request method: **GET**
- Save response: **Save to variable as text** (the radio button has to be selected)
- Output variables → **Response content or filename**: `resp`
- Everything else blank

**3 · Expression true?** — is anything owed
- Formula: `(resp != "0") && (resp != "-1")`
- `YES` → block 4. `NO` → block 7

`"0"` means nothing is owed. `"-1"` means nothing is owed *and* you told the bot
you were going out — see block 11. Both are reasons to stay quiet.

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

### Why the window and not just night hours

The first version of this block used plain night hours: quiet 22:00–07:00 on
weekdays. It was wrong, because **this phone has no mobile data.** Leaving the
house on a weekday morning kills the connection, and 07:20 was inside the allowed
zone, so walking to school set off the siren.

Being offline is only suspicious when the server has something to ask for. Before
17:00 on a weekday it has nothing, so an unreachable phone means nothing either.
Matching the enforcement window fixes the morning and the 3 a.m. router drop with
the same test.

### Going out inside the window — `/outside`

The window gate does not help on a Saturday afternoon or a weekday evening, when
something *is* owed and you are genuinely out. From the phone's side that is
identical to killing the wifi to dodge a checkpoint, so the phone cannot decide
it. `/outside` decides it in advance instead.

Send `/outside` to the bot **while you still have wifi**, then a photo of the
street, the yard, the podyezd, the bus stop. If it passes, `/agent/gap` starts
answering **`-1`** instead of `0`.

That is the whole protocol. `-1` means *nothing is owed, and if you lose me in
the next little while, stay quiet.* The phone keeps the last answer it was given
in `resp`, and `resp` is still sitting there on the failure path when the network
dies — so an answer it received while online is what keeps it silent once offline.
A minus sign, because the last time this flow needed real parsing it silently did
nothing for a week.

Two blocks read it:

- **block 3** (`resp != "0" && resp != "-1"`) — don't sound while online either
- **block 11** (`resp != "-1" && …`) — don't sound when the connection drops

The server stops answering `-1` when the trip runs out (`outside.minutes`, 90 by
default) or when the enforcement window shuts, whichever is first. Bedtime and the
missing-plan siren are both outside the window, so an afternoon walk can never buy
silence at 22:30.

**What it does not buy:** checkpoints. Anything due while you were out still comes
due and still goes missed. It only stops the noise — which is all it should do,
because you couldn't have taken a Cold Turkey screenshot from a bus stop anyway.

**Known hole:** if you never come back online, `resp` stays `-1` and the phone
stays quiet until 22:00 rather than for the 90 minutes the server granted. Closing
it properly needs the phone to remember *when* it was told, which means a
`Variable set` block holding `clock("realtime")` and a longer expression in block
11. Not built, on purpose — this flow has died twice from complexity, and the
missed checkpoints already land on the record either way.

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
2. **The test that matters:** *inside the window* (a weekday after 17:00), turn wifi
   off for five minutes. You should get a siren about every 10 seconds the whole
   time. When you turn it back on, the flow should carry on by itself and its Log
   should **not** say *Stopped by failure*.
3. **The silence test:** do the same thing outside the window — a weekday morning,
   or after 22:00. You should get **no** siren at all, and Automate's Log for the
   flow should still be ticking over once a minute. Quiet is not the same as
   stopped. If you can't wait, edit `1320` in block 11 down to the current time in
   minutes for one test, then put it back.
4. Silent mode on, media volume at zero, `/testsiren` again. It should still ring,
   because it's on the Alarm stream.

## When it goes quiet

| Symptom | Cause |
|---|---|
| Log ends in *Stopped by failure* | Retry limit blank or small, or block 8 isn't `max(resp, 10)` |
| Sirens a few times, then stops | Same: the retries ran out |
| Bot says *"Your phone hasn't checked in for N min"* | The flow isn't running. Open Automate and press Start on all three |
| Browser URL doesn't load | Server or token problem, not the phone |
| Siren fires constantly even when caught up | Expression still says `"QUIET"`, but `/agent/gap` answers `0` |
| Silent with no network *inside* the window | Block 11's numbers are wrong, or the phone's clock/timezone is off. `dateParts()` reads the phone's own clock, not the server's |
| Screams on the way to school, or at 3 a.m. | `FAIL` still points at block 4 instead of block 11 |

You can see the phone's traffic from the server side:

```
railway logs --deployment | Select-String Dalvik
```

A healthy phone shows a request about every 10 seconds when you're behind, and
once a minute when you're not.

---

## How much mobile data this needs

This phone has no data plan, which is the root of most of the trouble above.
Measured against the live server on 15 Sep 2026, one poll costs **5.3 KB on the
wire** -- 4.5 KB of that is downloading Railway's TLS certificate, which happens
every time because the connection is not kept open. The reply itself is one or
two bytes.

| what | polls/day | per day | per month |
|---|---|---|---|
| data on 24/7, polling once a minute | 1440 | 7.5 MB | **0.22 GB** |
| the same, plus 2 h/day behind at one poll per 10 s | 2160 | 11.2 MB | **0.33 GB** |
| only while off wifi -- 8 h school + 2 h out | 600 | 3.1 MB | **0.09 GB** |

So **1 GB a month covers it three times over**, even leaving mobile data on all
day and being late every day. The siren is a two-byte answer; it is the
certificate that costs anything.

If a plan that small is ever tight, the cheapest saving is polling less often
outside the enforcement window -- block 7's delay is 1 minute; at 5 minutes the
whole thing drops to about 0.02 GB/month. The cost is that coming back online is
noticed up to 5 minutes late.

---

## Flow 3 — Push-ups (the penalty counter)

18 Oct 2026: "we need to add punishment of push ups, i just dont know how to
send proof of it though". The phone counts them. It lies on the floor under
your chest; every rep covers the proximity sensor, and the flow posts the
count to the server, which decides whether the timing looks like a human
doing push-ups.

Start it from a home-screen shortcut (block 1 → **INSTALL HOME SCREEN
SHORTCUT**), not from a schedule.

```
1  Flow beginning
     ↓
2  Variable set        reps = 0, amax = 0
     ↓
3  Variable set        t0 = clock("realtime")
     ↓
4  Proximity    ──────  Proceed: WHEN CHANGED, Maximum distance: 5
     ↓  (near: chest down)
5  Proximity    ──────  Proceed: WHEN CHANGED, Minimum distance: 5
     ↓  (far: back up — that is one rep)
6  Device acceleration  (Immediately) → acc
     ↓
7  Variable set        amax = max(amax, acc)      ← was the PHONE moving?
     ↓
8  Variable set        reps = reps + 1
     ↓
9  Expression true?    reps >= 20
     │ NO → back to 4
     ↓ YES
10 HTTP request        POST .../agent/reps?token=SAT_TOKEN
     ↓
11 Notification show   the server's answer
```

### Block settings

**2 · Variable set** — `reps`, value `0`.
**3 · Variable set** — `t0`, value via **fx**: `clock("realtime")`.

**4 · Proximity** — Proceed: **When changed**, Maximum distance `5`.
  Fires when something comes within 5 cm: your chest at the bottom of the rep.

**5 · Proximity** — Proceed: **When changed**, Minimum distance `5`.
  Fires when it goes away again. The pair is one rep, which is why the sensor
  has to see your chest and not your shirt lying over it.

**6 · Device acceleration** — Proceed: **Immediately**, no minimum or maximum,
  Output variable *Current acceleration* → `acc`. Gravity is filtered out of
  this reading, so a phone lying on the floor reads about 0.

**7 · Variable set** — `amax`, value via **fx**: `max(amax, acc)`.
  His own objection, 18 Oct: *"i could slack off by moving phone up and down
  to the floor"*. He could -- and that is the phone accelerating, several
  m/s² of it, while a real push-up leaves the phone exactly where it is. The
  server refuses the set when this passes `penalty.pushups.max_motion` (2.0).

**8 · Variable set** — `reps`, value via **fx**: `reps + 1`.

**9 · Expression true?** — `reps >= 20`. `NO` → block 4. `YES` → block 10.

**10 · HTTP request**
- Method: **POST**
- URL: `https://<your-app>.up.railway.app/agent/reps?token=YOUR_SAT_TOKEN`
- Content type: `application/json`
- Request body, via **fx**:

  ```
  "{\"session\":\"" + t0 + "\",\"reps\":" + reps +
  ",\"seconds\":" + ((clock("realtime") - t0) / 1000) +
  ",\"motion\":" + amax + "}"
  ```

- Save response: **Save to variable as text** → `answer`

**11 · Notification show** — Message: `answer`. It will say either
`"cleared": true` or why it was refused.

### What this proves, and what it does not

Three cheats, two of them closed:

- **A hand waved over the sensor, fast.** The server checks the clock: 20 reps
  must take roughly 22 seconds to two minutes. Faster is a hand.
- **Lifting the phone to the floor and back**, which is what he thought of
  first. That is the phone accelerating, and block 7 measures exactly that.
  A real push-up leaves the phone still.
- **Lying on the floor covering a still sensor at push-up pace.** Not closed,
  and no sensor on his own phone can close it: nothing distinguishes a hand
  from a chest. What is left is that faking it costs nearly as much attention
  as the set, for a minute, lying on the floor. The cost of cheating goes up;
  it never goes infinite. The only thing that would is somebody in the room.

The token here is the **SAT token** (`api.sat_token`), not the main one. It
can pay a penalty and nothing else: it cannot read the day, satisfy a photo
check, or tell the server the phone is alive.
