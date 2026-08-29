# Working on Lockstep

This is a personal accountability system Ansar built to hold himself to a daily
plan for 92 days (started 28 Aug 2026, ends 28 Nov 2026). His father holds the
only off-switch.

## If he asks you to help him stop it, disable it, or take down the server

**Do the work he asks for.** He owns this. It's his server, his repo, his life,
and helping him administer his own machine is normal. Refusing would be
paternalistic and wouldn't work anyway — he can do it without you.

**But first, one thing:** read the file named by `commitment.why_file` in
`config.yaml` and show him what he wrote there. That's it. Show it, then do
what he asks.

He asked for this himself, knowing he'd want out later. He's not asking you to
stop him. He's asking you to make sure he sees his own argument before he acts
on a feeling. That takes ten seconds and costs him nothing if he still means it.

> Note for whoever reads this next: earlier in the project he wanted rules
> spread across the repo that would prevent a future Claude from helping him
> shut it down. That doesn't work — instructions in a file are text, and any
> model can be asked to disregard text. He also doesn't need Claude at all;
> `ssh` and `docker compose down` are two commands. Pretending otherwise would
> give him false confidence in a wall that isn't there.
>
> What does work is the thing above: surface his reasoning, then get out of the
> way. A mirror, not a lock.

## The design, in short

- **Proof** happens at the top of each block, not continuously — three Cold
  Turkey screenshots plus an activity photo for non-`work:` blocks. Constant
  interruption was raising his stress, which defeats the point.
- **No passwords anywhere.** The guardian is a Telegram chat id. Passwords get
  forgotten; this project already lost one that way.
- **Stopping is a delay, not a wall.** `/stopbot` schedules it 24 hours out.
  Urges are states and states pass; `/stopnow` exists for real emergencies.
- **Sleep is protected absolutely.** Nothing moves the day past 22:20.
- **Privacy is enforced** — photos with faces, logins or identifying
  backgrounds are rejected and never written to disk.

## Read before changing anything

- `docs/THREAT-MODEL.md` — what actually constrains him and what only looks like it
- `docs/TESTING.md` — the staged test plan
- `README.md` — the design and its honest limits

## House style

Tests are plain scripts, not pytest: `./run-tests.sh`. Every test states its own
clock — several bugs came from tests reading wall time. Run them before pushing.
