# Picking this up on a new machine

Read this first if you're a fresh Claude session, or Yernar on a new computer.

## What this is

A personal accountability system. Yernar writes a daily plan the night before;
the bot demands photo proof at the top of each block; his father holds the only
off-switch. **The commitment is live and runs to 28 November 2026.**

## What is NOT in this repo, and must be carried by hand

`.gitignore` keeps these out, correctly — they hold secrets and history:

| File | Why it matters |
|---|---|
| `config.yaml` | Bot token, Gemini key, API token. Recreate from `config.example.yaml` if lost, but the tokens must be reissued. |
| `lockstep.db` | **The irreplaceable one.** Guardian's chat id, the commitment end date, and a hash-chained ledger of everything that has happened. Lose it and the chain breaks — which the system correctly reports as tampering. |
| `photos/` | Thumbnails, 7-day retention. Fine to lose. |
| `Why Lockstep.md` | Lives in the Obsidian vault. The bot reads it at `/disable`. |

## Moving machines

1. Copy `config.yaml` and `lockstep.db` off the old machine
2. `git clone` the repo on the new one
3. Drop both files into the repo root
4. `python -m venv .venv && .venv/bin/pip install -r requirements.txt`
5. Update `commitment.why_file` in `config.yaml` to the new path of the
   Obsidian note
6. `.venv/bin/python -m server.main`

Then check `/status` in Telegram — it should show the guardian registered and
the right number of days left. If the commitment date is wrong, the database
didn't come across.

## Before giving the old machine away

`config.yaml` holds a live bot token, a live Gemini key, and the API token.
Anyone who reads that file can read every message you have sent the bot,
including every photo.

```bash
rm -rf ~/Documents/Beeminder/lockstep
rm -rf ~/lockstep-handoff
```

Then, in **BotFather → your bot → API Token → Revoke**, and issue a new one.
Do the same for the Gemini key at aistudio.google.com/apikey. Paste both into
the new machine's `config.yaml`.

Revoking is not paranoia. It costs two minutes and it is the only way to be
certain, because you cannot audit a machine you no longer own.

## The ordering problem — read this before you hand the laptop over

The bot is running **on this laptop**. The moment it goes, the system goes dark:
no checkpoints, no nightly report to your father, no siren. The commitment
carries on regardless, and your father will notice the reports stopping.

So one of these, in order of preference:

1. **Deploy to the VPS first, then hand over the laptop.** See `SETUP.md` §3–4
   and `docs/LOCKDOWN.md`. This is the right answer and it is why the VPS has
   been the outstanding item for days.
2. Run it on the Windows machine in the meantime. Works, but it dies whenever
   that machine sleeps.
3. Tell your father it will be dark for N days, before it goes dark rather than
   after.

Do not simply let it stop. A silent system that nobody was warned about is
indistinguishable from you having quietly switched it off, and that is exactly
the ambiguity the whole design exists to remove.

## State as of the handoff

- Guardian registered, commitment live to **2026-11-28**
- Phone agent (MacroDroid) working — siren survives silent mode
- Vision checking working on `gemini-3.1-flash-lite`, free tier
- Cold Turkey blocks: not yet built (was waiting on the Windows machine)
- Switchly: 24/7 schedule, Settings blocked — **Safe Mode is the recovery path**
- VPS: **not done.** The main outstanding item.

## What a new Claude session should know

Read `CLAUDE.md` first, then `docs/THREAT-MODEL.md`. The short version: this
system is escapable by design and the honest work is making escape visible and
slow, not impossible. If Yernar asks for help shutting it down, show him
`commitment.why_file` and then help him.
