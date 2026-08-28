# Lockstep

Beeminder without the money. Instead of charging you when you fail, this makes
failing **loud and inconvenient**, and puts the off-switch in someone else's hands.

Built from `Beeminder(but without money).canvas`.

---

## How it works

**The night before**, you write tomorrow's plan in Telegram — one line per block,
covering every minute of the enforced window:

```
16:00-16:40 commute to MNU library
16:40-19:00 work: linear algebra pset ch3
19:00-19:30 gym
19:30-20:00 dinner
20:00-21:30 work: essay draft
21:30-22:00 clean room
```

The bot refuses plans with gaps. `relax` is a legitimate block — a planned rest
is still a plan, which is how holidays work here.

**At the top of each block**, and nowhere else, you prove the session:

1. Cold Turkey with the block **running** — toggle on, timer going, clock visible
2. The **allowed** sites list
3. The **blocked** sites list

A minute apart. For anything that isn't a `work:` block, a fourth photo of the
activity itself — the bar mid-pull-up, the bed made, the sink you're at.

Then it leaves you alone until the next block.

That last part is the design, not a concession. Being interrupted every few
minutes to prove you're concentrating interferes with the concentrating. A
system that raises your cortisol to make you work has traded one problem for
another. And a running blocker with a visible allowlist is *better* evidence
than a spot check anyway: a photo proves a moment, a locked block proves the
hour.

**Gemini checks each photo** against what that particular shot was asked for,
plus a visible burned-in clock reading within 12 minutes of now.

It also enforces your own privacy rules and **rejects** anything showing a face,
a name or login on screen, or a background that could place you. Rejected frames
are never written to disk — not even as a thumbnail.

**If you ignore it**, escalation starts on your phone and does not stop until a
valid photo lands:

| Behind by | What the phone does |
|---|---|
| 0–5 min | nothing |
| 5+ min | siren + vibration, once a minute |
| 20+ min | twice a minute |
| 40+ min | three times a minute |
| 60+ min | four times a minute (the ceiling) |

**Sleep is protected absolutely.** Nothing pushes the day past 22:20 — not an
overrun, not an emergency offset. Past that point the answer is to sacrifice
another task, never the night. A productivity system that borrows from sleep is
just a sleep-debt machine with extra steps.

Escalation is **repetition, not volume**. It never gets louder than the level you
set — falling further behind just makes it harder to sit through. There is no
laptop component: the phone is the entire enforcement surface.

The siren uses Android's **alarm stream** specifically — it ignores silent mode
and Do Not Disturb, and has its own volume slider. So it is very hard to mute
while your ringtone, media volume and actual phone calls all keep behaving
completely normally. It plays at **55% of the alarm slider**, not 100%: loud
enough that you can't work through it, not loud enough to be painful. That
number lives in `config.yaml`, server-side, so you can change it without
touching the phone.

**Laptop work** can optionally require a Cold Turkey screenshot — see below.

**Once a day** you can `/offset <reason>` to push everything back an hour, with a
photo of wherever you actually are. Once. One hour.

**Turning it off** is not something you can do. You can `/disable` to *ask*, and
your reason reaches your father word for word — but only his `/stopbot`, from his
own Telegram, actually stops it. Only his `/startbot` turns it back on.

There is deliberately **no password anywhere in the project.** A password is a
secret, and secrets fail in the ways this project has already been bitten by:
forgotten, lost with a phone, or talked out of someone at 11pm. The App Lock on
the phone is still shut for exactly that reason.

What replaces it is an *address*. The system does what one Telegram account
tells it and ignores everyone else. An account cannot be forgotten, cannot be
reset by guessing a childhood pet, and cannot be typed by someone who isn't
holding that phone.

---

## The Cold Turkey rule (off by default)

You spotted the real hole yourself: open the right tab, take the photo, switch
straight back to YouTube. The photo is honest and the fifteen minutes after it
are not.

The fix, when `vision.require_blocker_for_work` is on: the **first check of every
`work` block** must be a full-screen screenshot showing Cold Turkey with a block
actually running, the allow/block list readable, and the taskbar clock visible.
Later checks in that block are normal photos. Asking for the blocker screenshot
every time would just get you the same screenshot six times; asking once per
session proves the session was started under a block.

It ships **switched off**, because you're on the Windows laptop from September.
Flip it in `config.yaml` then — nothing to install, it's purely a rule about what
counts as valid proof.

It closes the laptop hole. It does not close every hole: nothing stops you
photographing a book and then picking up your phone. Spot-check randomness is
what makes that expensive, not impossible.

---

## Privacy

Photos are stripped of metadata, downscaled, stored as thumbnails, and deleted
after a week. Your father sees counts, never images.

But: every photo goes to Google's vision API to be checked, and on the free tier
Google may retain and human-review it. Downscaling does **not** hide a skyline —
I tested that, and the model described "high-rise buildings" just as readily at
512px as at 3000px. If you photograph yourself outdoors, frame tight. Full
accounting in [docs/PRIVACY.md](docs/PRIVACY.md).

---

## The honest part

You have root on the server. You can `rm` the database. Any claim that this is
unbreakable would be a lie, so it doesn't make one.

What it does instead is make breaking it **visible**:

- Every event is chained by SHA-256 to the one before it. Rewriting history
  changes the tip hash, and the tip hash goes to your father every night.
- Deleting the database doesn't produce a clean slate, it produces a *missing
  nightly report*, which is louder than a bad one.
- Every disable attempt — successful, failed, or merely started — is pushed to
  him before anything happens.

The enforcement is social, not cryptographic. That's not a shortcoming of the
implementation; it's the only thing that can work when the person being
constrained owns the hardware. A lock you hold the key to isn't a lock. A room
with windows is still worth building.

**Two things follow from that, and they matter:**

1. **It only works if he'll actually use it.** If he'd hand over the password
   whenever you ask, you've built an elaborate way to ask yourself for
   permission. The mechanism is him, not the code.
2. **If you find yourself wanting to tear it out at 3am, that's information.**
   Either the settings are wrong — 24 photos a day is genuinely a lot, and
   nothing stops you from starting at 30-minute intervals — or the commitment
   was wrong. Both are better handled by changing the config than by breaking
   the system, and `/disable` exists so you have an honest exit that doesn't
   require lying to anyone.

---

## Layout

```
server/          the bot, on a VPS, 24/7
  config.py      config + timezone handling
  db.py          SQLite schema
  ledger.py      hash-chained append-only event log
  planner.py     plan parsing and gap validation
  vision.py      Gemini photo validation
  engine.py      checkpoints, spot-checks, debt, escalation   <- the rules live here
  lock.py        the two-party lock
  api.py         what the agents poll
  bot.py         Telegram handlers
  main.py        entry point

agent-android/   Termux script + MacroDroid recipe
deploy/          Dockerfile, compose, systemd unit, backup script
tests/           run these before you trust it with a year of your life
docs/GUARDIAN.md the note to show your father
```

## Running the tests

```bash
.venv/bin/python tests/test_engine.py && .venv/bin/python tests/test_lock_api.py
```

## Setting it up

See [SETUP.md](SETUP.md). Budget about an hour.
