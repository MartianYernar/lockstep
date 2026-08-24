# Phone agent — MacroDroid route (recommended)

More reliable than Termux, because MacroDroid is a normal foreground app that
Android's battery manager already understands. About ten minutes of tapping.

Install MacroDroid from the Play Store. The free tier caps you at five macros;
this needs one.

---

## The macro: "Lockstep"

### Trigger
- **Device Events → Regular Interval** — every **1 minute**
  - Tick *"Use alarm for triggering"* so Doze cannot stretch the interval.

### Actions, in order

**1. HTTP Request**
- Applications → HTTP Request
- Method: `GET`
- URL: `http://YOUR_VPS_IP:8765/agent/state?token=YOUR_API_TOKEN`
- Under *Response*: **Save response to variable** → create a string variable
  called `resp`
- Block next action until complete: **yes**

**2. Set Variable — `alarm_on`** (boolean)
- Value type: **Expression**
- Expression: `getJsonPath(resp, "$.phone_alarm") == "true"`

**3. Set Variable — `minutes`** (integer)
- Expression: `getJsonPath(resp, "$.debt_minutes")`

**4. Set Variable — `headline`** (string)
- Expression: `getJsonPath(resp, "$.headline")`

**4b. Set Variable — `volpct`** (integer)
- Expression: `getJsonPath(resp, "$.alarm_volume_pct")`
- The server decides how loud, so you can change it in `config.yaml` later
  without touching your phone.

**5. IF `alarm_on` = true**  *(Logic → If/Else)*

Inside the IF:

- **Volume → Set Volume**
  - Stream: **Alarm** → set the percentage from the variable `volpct` (55% by default)
  - *This is the whole trick. The alarm stream ignores silent mode and Do Not
    Disturb, and has its own slider, so your ringtone, media and calls all keep
    working exactly as normal. It is set to just over half — loud enough that
    you cannot work through it, not so loud it hurts.*
- **Media → Play Sound** (or Play/Stop Sound)
  - Pick a genuinely irritating tone
  - Stream: **Alarm**
- **Device Actions → Vibrate** → pattern: *Long*
- **Notifications → Display Notification**
  - Title: `Lockstep — [lv=minutes] min behind`
  - Text: `[lv=headline]`
  - Priority: **Max**, Ongoing: **yes**

**6. ELSE**

- **Volume → Set Volume** → Stream: **Alarm** → back to your normal level (7 or so)
- **Notifications → Clear Notification** → the one above

---

## Battery settings

Settings → Apps → MacroDroid → Battery → **Unrestricted**. On Xiaomi also
enable **Autostart**. Without this, Android will quietly stop the macro after a
few hours and you will think the system is broken when it is just asleep.

## Check it

MacroDroid → the macro → **⋮ → Test Actions**. With no debt outstanding you
should get the ELSE branch and silence.
