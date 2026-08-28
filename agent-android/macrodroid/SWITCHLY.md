# Controlling the app blocker

## Read this before following the Gemini instructions

Gemini told you to install MacroDroid's "Simple Telegram Bot" template, paste
your BotFather token into it, and let MacroDroid **register itself as the
webhook** for that bot.

**That would break Lockstep completely.**

Telegram allows exactly one delivery method per bot token. Your bot currently
uses *long polling* — it asks Telegram "anything new?" every few seconds. The
moment anything registers a *webhook* on that token, Telegram stops answering
polls and starts pushing updates to the webhook URL instead.

So MacroDroid would begin receiving your messages, and Lockstep would go
permanently silent. No checkpoints, no photo validation, no nightly report —
and no error message either. It would simply stop, and look like it had died.

Gemini wasn't wrong about the mechanism. It just didn't know you already have a
bot on that token.

## What to do instead

You don't need a webhook, a second bot, or any new plumbing, because the piece
that would do the work is **already running**: MacroDroid polls the Lockstep
server every minute for the siren. It can read one more field from the same
response.

```
   father ──/unblock 10──▶ Lockstep server ──apps_blocked: false──▶ MacroDroid
                                  │                                     │
                            closes itself                         toggles Switchly
                             after 10 min
```

The server already sends it:

```json
{ "apps_blocked": true, "phone_alarm": false, ... }
```

`true` almost always. It flips to `false` only when your father sends
`/unblock`, and flips back on its own when the window expires.

## The macro

A second macro, next to the siren one. Same trigger, same URL.

**Trigger:** Regular Interval, 1 minute, *use alarm for triggering*

**Actions:**
1. **HTTP Request** → the same `/agent/state` URL → save to string `resp`
2. **JSON Parse** → `resp` → dictionary `data`
3. **If** `data[apps_blocked]` = true
   - **Quick Settings Tile Toggle** → Switchly → **on**
4. **Else**
   - **Quick Settings Tile Toggle** → Switchly → **off**

Add Switchly's tile to your quick settings drawer first, or MacroDroid won't
find it. Gemini's Option A was the right call here — the tile is far more
reliable than its Option B, which drives Switchly's UI by tapping text on
screen and breaks the moment the app's layout changes.

## Why there is no password anywhere in this

Your earlier idea was to auto-generate a new Switchly password each time and
text it to your father, so he'd never have to remember one. That solves the
forgetting, but it keeps the thing causing the problem.

There is no password here at all. The control is *which Telegram account sent
the command*. That can't be forgotten, can't be reset, can't be found written
down, and can't be talked out of him at 11pm by a tired version of you.

## The limit you asked for

`/unblock` opens it for **10 minutes**, and not more than once every **7 days**.
Both are in `config.yaml`.

You flagged the risk yourself: ten minutes is enough to install TikTok. Nothing
in the code prevents that — it can't. What it does is make the window short,
rare, and dependent on someone else agreeing. The rest is you.

## Honest limitation

This is a speed bump, not a wall. You can disable the macro in thirty seconds.

But the watchdog already reports to your father when MacroDroid stops polling,
so turning it off is visible — the same bargain as everything else here.
Blocking the bot, killing the agent, disabling the macro: all possible, all
reported. That is the only kind of lock that works on hardware you own, and
trying to build a stronger one is what broke your phone three times.
