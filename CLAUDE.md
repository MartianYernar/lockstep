# Working on Lockstep

This is a personal accountability system Yernar built to hold himself to a daily
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

- **Proof** happens at the top of each block, not continuously — one Cold Turkey
  screenshot showing the block running, plus an activity photo for non-`work:`
  blocks. Constant interruption was raising his stress, which defeats the point.
  It was three screenshots on 1 Sep and cost an evening: the validation had been
  written against a description of Cold Turkey's UI rather than the real thing,
  exactly as SETUP.md warned. If you are tempted to add a shot back, read that
  warning first. A proof he cannot reliably produce is worth less than no proof
  — it teaches him to ignore the bot, and then nothing works.
- **The allowlist check exists but is unwired.** `vision.compare_policy` and
  `vision.blocks` in config pin what each block may permit, so loosening one
  fails instead of passing; the nightly report carries a fingerprint of that
  policy. Nothing generates the `blocker_allow` checkpoint it hangs off. Wire it
  back in `engine._block_start_times` when a scope shot can be taken without a
  fight.
- **The coach can talk but cannot act.** `server/coach.py` breaks a block into
  steps and answers free text in Telegram. It is handed the day's real numbers
  from the database before it sees a word he wrote, so it cannot be told the day
  went well. It can satisfy no checkpoint, move no deadline and stop no siren --
  a coach that could let him off would be the easiest off-switch in the system.
  It also shares one Gemini free-tier quota with the photo checks, which are the
  only thing that enforces anything, so it runs on the TAIL of the model chain
  and stops at `coach.max_calls_per_day`. If you touch this, keep both of those:
  no write access to the record, and the camera eats first.
  It CAN write two things, both his own notes: the big-task list
  (`server/tasks.py`) and events (the `reminders` table). `Coach.apply` is the
  only door, and `tests/test_friend.py` pins that nothing else moves.
- **Rewrites that cut work need a reason.** `/replan`, or re-sending an already
  locked plan, that removes `friend.rewrite_min_lost` minutes of important work
  (a `work:` block or one matching a big task) is held until he explains it in
  text or a photo and the friend accepts. Work-for-work swaps pass freely. If
  the model is unreachable, a full-sentence reason is taken on trust and logged.
  Every attempt, accepted or not, goes in the ledger as `rewrite_reason`.
- **The 20-minute check.** `server/standup.py` asks three questions on the
  20-minute marks inside a block: the precise thing he is on, whether he is
  moving or drifting, what is in the way. Text answers, stored per block, fed
  back into Friday's context so it can see three checks in a row saying the
  same thing. Skipped while a photo is owed -- the siren is enough -- and in
  `standup.skip_categories`. It replaced the older mid-block "how's it going"
  check-in (`friend.checkins_per_day` is 0).
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
