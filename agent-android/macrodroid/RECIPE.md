# Phone agent — MacroDroid

Install **MacroDroid** by **ArloSoft** (Jamie Arlott). The free tier allows five
macros; this needs one.

> **How to follow this:** don't hunt through the menu tree. When you tap **+** to
> add a trigger or action, there's a **search box at the top** — type the name in
> bold below and pick it. Menu categories move between versions; names don't.

---

## Create the macro

MacroDroid → **Macros** → **+** → name it `Lockstep`.

### Trigger

Tap **+** under Triggers, search **Regular Interval** *(it lives under Date/Time)*.

- Interval: **1 minute**
- Tick **Use alarm for triggering** — without this Android's Doze will stretch
  your one-minute interval into fifteen while you sleep, and the siren arrives
  far too late to matter.

### Actions

Add these in order. Tap **+** under Actions and search each name.

**1. Search: HTTP Request**
- Method: `GET`
- URL: `http://YOUR_SERVER:8765/agent/state?token=YOUR_API_TOKEN`
- Turn on **Save response to string variable** → create a **String** variable
  named `resp`
- Turn on **Block next action until complete** — otherwise the next action reads
  an empty variable and the macro does nothing at all, silently, with no error

  > It must be a **String**, not a Dictionary. HTTP Request only ever writes
  > text; the parsing is a separate step.

**2. Search: JSON Parse** *(under "MacroDroid Specific")*
- Input: the string variable `resp`
- Output: create a **Dictionary** variable named `data`

  > This turns the text into fields you can read directly. After running the
  > macro once you can open Local Variables and see `data` expanded, which is
  > the easiest way to confirm it's working.

**3. Search: If** — condition: `data[phone_alarm]` **is true**

Inside the IF:

- **Set Volume** → Stream: **Alarm** → value `data[alarm_volume_pct]` (percent)

  *This is the whole trick. Android's alarm stream ignores silent mode and Do
  Not Disturb and has its own slider — so this is very hard to mute while your
  ringtone, media and calls all behave completely normally. At 55% it's loud
  enough that you can't work through it and not loud enough to hurt.*

- **Loop** → repeat `data[alarm_repeats]` times, containing:
  - **Play Sound** — something genuinely irritating, Stream: **Alarm**
  - **Vibrate** → pattern: *Long*
  - **Wait Before Next Action** → 2 seconds

  *Escalation is repetition, not volume: twice a minute at 20 minutes behind,
  three times at 40, four at an hour. It never gets louder than you set.*

- **Display Notification**
  - Title: `Lockstep — {v=data[debt_minutes]} min behind`
  - Text: `{v=data[headline]}`
  - Priority **Max**, **Ongoing** on

**4. Else**

- **Set Volume** → Stream: **Alarm** → back to your normal level (7 or so)
- **Clear Notification** → the one above

---

## Battery settings — do not skip

`Settings → Apps → MacroDroid → Battery → Unrestricted`. On Honor also check
`Settings → Battery → App launch` and set MacroDroid to **Manage manually** with
auto-launch and run-in-background both on.

Without this Android kills the macro after a few hours and you'll think the
system is broken when it's just asleep. The server will notice and tell you —
but it's better not to need telling.

## Check it works

MacroDroid → the macro → **⋮ → Test Actions**. With nothing outstanding you
should get the ELSE branch and silence.

Then do **Stage 2** of [the test plan](../../docs/TESTING.md): put the phone on
silent, ignore a checkpoint for six minutes, and confirm it still sirens. If it
doesn't, the Play Sound action is on the wrong stream — that single setting is
the difference between a real system and a suggestion.

## Server address

While the bot runs on a laptop, `YOUR_SERVER` is that laptop's **local IP**
(`ipconfig getifaddr en0` on a Mac) and the phone must be on the same wifi. Once
it's on the VPS it becomes the public IP and works from anywhere — which is a
large part of why the VPS matters.
