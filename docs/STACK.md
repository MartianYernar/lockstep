# The whole stack, and how to rebuild it

Everything that makes the discipline hold: the server, the phone, the laptop,
and the two people involved. Written so that if the laptop dies, the phone is
lost, or a year passes, this one file is enough to put it all back.

Nothing secret is in here. Tokens, chat ids and API keys live only in
`config.yaml` on the server's volume (and the local copy, which is gitignored).
`config.example.yaml` in this repo is the shape of that file with the values
removed.

---

## 1 · The idea in one paragraph

A plan written the night before is enforced the next day by photos. Every block
of the day has a checkpoint; if the photo doesn't arrive, a siren goes off on
the phone and keeps going until it does. Nothing can cancel it except a valid
photo — not the bot, not an excuse, not a reply. Around that sit the blockers
that remove the alternatives: Cold Turkey and a hosts file on the laptop,
Switchly and RethinkDNS on the phone. The siren creates the cost of not
working; the blockers remove the easy ways to not work. Neither is a wall on
its own. Together, with a password held by someone else, the easy path becomes
the planned one.

```
  phone (Android)                     server (Railway)              laptop (Windows)
  ───────────────                     ────────────────              ────────────────
  Automate "Lockstep"  ──poll 60s──▶  /agent/gap  ──┐               Cold Turkey Blocker
     └ siren if debt > 0              aiohttp API   │               hosts file blocks
  Automate "keeper"                                 │                 youtube/tiktok/ig
     └ restarts Lockstep              Telegram bot ◀┼──photos──▶     standard user acct
  Switchly  (apps blocked)              "Friday"    │                 (dad holds admin)
  RethinkDNS (DNS filter)             Gemini vision ┘
  Telegram (proof + chat)             SQLite on /data volume
```

---

## 2 · The server

| thing | value |
|---|---|
| host | Railway |
| account | `<second-account-email>` (the first one, `<first-account-email>`, had its trial expire on 8 Oct 2026 and still holds the old volume) |
| project | `lockstep`, environment `production`, workspace `My Projects` |
| url | `https://<your-app>.up.railway.app` |
| builder | the service variable `RAILWAY_DOCKERFILE_PATH=deploy/Dockerfile` — without it Railway auto-detects Python and fails with "no start command" |
| image | `deploy/Dockerfile`, `python:3.12-slim` |
| entrypoint | `python -m server.main` |
| port | 8765 in the Dockerfile; the running container logs the live one |
| volume | `lockstep-volume`, mounted at `/data` |
| on the volume | `config.yaml`, `lockstep.db` (+ `-wal`, `-shm`), `why.md` |
| env in the image | `LOCKSTEP_DB=/data/lockstep.db`, `LOCKSTEP_CONFIG=/data/config.yaml` |
| healthcheck | `GET /health` every 60s |

**First boot on a fresh host.** A volume can only be written to while a
deployment is running, and the app will not start without `/data/config.yaml` —
a deadlock that cost an evening on 9 Oct 2026. The way out is the service
variable `LOCKSTEP_CONFIG_B64`: base64 of the whole config file. `main._seed_config`
writes it to the volume on the first boot only, and never touches the file
again, so later edits on the volume stand.

```bash
railway init -n lockstep && railway add -s lockstep
railway volume add -m /data
railway variables --set "LOCKSTEP_CONFIG_B64=$(base64 -w0 config.yaml)"
railway variables --set "RAILWAY_DOCKERFILE_PATH=deploy/Dockerfile"
railway up --ci
railway domain                     # then put the URL in the phone's block 2
```

`LOCKSTEP_BOT_TOKEN`, `LOCKSTEP_GEMINI_KEY` and `LOCKSTEP_API_TOKEN` also
override their config values if you would rather keep those three out of the
file.

**A fresh database starts unarmed:** no guardian, no commitment, lock not
started. Dad messages the bot, you send `/setguardian <his id>`, he sends
`/commit <months>`. Until then nothing is enforced.

**Deploying:** from the repo root, `railway up --ci`. It builds the image, pushes
it and restarts the container — a few seconds of downtime, no data loss, because
everything stateful is on the volume.

**Reading or changing the live config** (never edit it blind):

```bash
export MSYS_NO_PATHCONV=1          # Git Bash mangles /data paths otherwise
railway volume files --volume lockstep-volume download --overwrite config.yaml ./live.yaml
# edit, then:
railway volume files --volume lockstep-volume upload --overwrite ./live.yaml config.yaml
railway up --ci                    # the config is read at startup only
```

**Reading the live database** the same way: download `lockstep.db`, `-wal` and
`-shm` together, copy all three to a scratch directory, then open the copy.
Without the `-wal` file you are reading yesterday's data.

**Outside services it talks to:**
- **Telegram Bot API** — the only interface. Proof photos in, nags and Friday
  out. Every command checks the sender's chat id against `telegram.user_chat_id`
  (or the guardian's); a stranger who finds the bot gets one "not registered"
  line and nothing else. Pinned by `tests/test_strangers.py`.
- **Google Gemini** (`google-genai`) — two jobs: checking photos, and Friday's
  words. Free tier, shared quota, so the photo check always wins: the chat
  stops at `coach.max_calls_per_day` and runs on the weaker models.
- Nothing else. No database service, no object storage, no analytics.

**What's in the database:** days, blocks, checkpoints, events (the ledger),
tasks, reminders, standups, coach_log, memories, proposals, foundations, terms.

**Photos** are checked in memory, then kept only as a 320px thumbnail
(`privacy.store_photos: thumbnail`, `thumbnail_dimension: 320`), deleted after
`privacy.retain_days` (7). They are written inside the container at
`/app/photos`, **not** on the volume, so every deploy wipes them. A photo that
fails the privacy check (face, login, identifying background) is never written
at all. No API endpoint serves them: `/health`, `/agent/state`, `/agent/siren`,
`/agent/gap` and `/agent/ack` return state and take heartbeats, nothing else.
Set `privacy.store_photos: none` to keep nothing.

---

## 3 · The phone (HONOR X9b, Android)

### 3.1 Automate — the siren

Two flows. Full block-by-block settings in
[`agent-android/automate/RECIPE.md`](../agent-android/automate/RECIPE.md), the
setup order in [`RUNBOOK.md`](../agent-android/automate/RUNBOOK.md).

- **Lockstep** — polls `/agent/gap?token=…` every 60s. The server answers with
  the seconds of debt: `0` means nothing owed, `-1` means "he told me he's out,
  stay quiet", anything else means sound the alarm and poll again sooner. If
  the network is unreachable it falls to a window check done on the phone's own
  clock, so being offline is only suspicious during enforced hours.
- **keeper** — a 1-minute loop that calls *Flow start* on Lockstep. Because
  "Allow parallel launches" is off on Lockstep's beginning block, this does
  nothing while it is alive and restarts it within a minute if it stops.
- **Run on system startup** is on in Automate's settings, so both resume after a
  reboot. Confirmed working, 9 Oct 2026.
- **"Send notification" is unticked on the flow**, which removes the persistent
  notification *and its "stop all flows" button* from the shade. This was the
  last one-tap way out. Android still exposes a stop route through Settings →
  Active apps, which is why Settings is blocked (below).

### 3.2 Switchly — blocked apps

Dad set the password; it is not known here. Blocked during work hours:

- **Automate** — so the flows can't be opened and stopped
- **Settings** — so Automate can't be force-stopped from Active apps, and so
  none of this can be undone during a block
- **Telegram** — note the trade-off: with Telegram blocked on the phone, proof
  photos and `/outside` have to come from the laptop. If that ever leaves you
  stuck away from home, the fix is either to allow Telegram or to build the
  Automate "send proof" flow (an HTTP POST of a camera photo), not to unblock
  everything.

### 3.3 RethinkDNS — adult content and trackers

RethinkDNS runs as a local VPN on the phone and filters DNS, so it covers every
app, not just a browser.

1. Install RethinkDNS, choose a DNS-over-HTTPS resolver.
2. In **Blocklists / On-device blocklists**, enable the adult-content
   categories (and trackers/malware if wanted), then download them.
3. Android → Settings → Network → VPN → RethinkDNS → turn on **Always-on VPN**
   *and* **Block connections without VPN**. This is what makes it survive a
   reboot and what stops it being bypassed by simply turning the VPN off.
4. With Settings blocked by Switchly during work hours, these toggles can't be
   reversed in the moment.

### 3.4 Why the phone matters most

It is the only device that can make a noise you can't ignore and can't walk away
from. Everything on it is therefore set up so that turning it off takes several
deliberate steps, at least one of which needs dad's password.

---

## 4 · The laptop (Windows 11)

- **Cold Turkey Blocker** — blocks sites in Chrome/Edge/Firefox through its
  browser extensions, scheduled over work hours. It is a blocker only: since
  10 Oct 2026 the bot no longer asks for a screenshot of it. Every block is
  proved by one photo of you doing the work, clock in shot.
- **hosts file** — `C:\Windows\System32\drivers\etc\hosts` maps
  `youtube.com`, `www.youtube.com`, `m.youtube.com`, `music.youtube.com`,
  `youtu.be`, `tiktok.com`, `www.tiktok.com`, `vm.tiktok.com`,
  `instagram.com`, `www.instagram.com` to `0.0.0.0`. This catches what Cold
  Turkey misses: apps with their own browser, including the Claude desktop
  app's built-in one. Editing it needs administrator rights.
- **Standard user account** — the wall only exists if the admin password is
  dad's. Added 25 Sep 2026 after finding that the Claude desktop browser was a
  clean way around Cold Turkey.

---

## 5 · The people

- **Yernar** — writes the plan, sends the photos, owns all of it.
- **Dad (guardian)** — holds the Switchly password and the Windows admin
  password, and is a Telegram chat id in the config. He gets the nightly report
  and the alerts that matter. Known limitation, in Yernar's own words: he
  doesn't read most of them. So the alerts are treated as a *record*, not a
  deterrent — the deterrent is the password.

---

## 6 · If everything is lost, rebuild in this order

1. **Clone** `github.com/MartianYernar/lockstep` (private).
2. **Config** — copy `config.example.yaml` to `config.yaml`, fill in: Telegram
   bot token (from @BotFather), your chat id and dad's chat id
   (`scripts/setup-ids.py`), a Gemini API key, and a fresh random API token for
   the phone.
3. **Server** — `railway init` (or link the existing project), attach a volume
   at `/data`, upload `config.yaml` to it, `railway up --ci`. Check the logs say
   `lockstep up — window …`.
4. **Phone** — install Automate, rebuild the two flows from `RECIPE.md` and
   `RUNBOOK.md`, paste the new token into the HTTP block, untick "send
   notification", turn on "Run on system startup".
5. **Blockers** — Switchly (dad sets the password), RethinkDNS with always-on
   VPN, Cold Turkey and the hosts lines on the laptop.
6. **Test** — the four tests in `RUNBOOK.md`: siren, offline, silence, keeper.

The database is the only thing a rebuild can't recreate. Back it up by
downloading `lockstep.db` + `-wal` + `-shm` from the volume now and then.

---

## 7 · The honest part

Read [`THREAT-MODEL.md`](THREAT-MODEL.md) before trusting any of this. Every
layer above is defeatable by the person it constrains, because he is the owner
of all of it. What the stack actually does is:

- make the lazy path the planned path,
- make every way out cost several deliberate steps instead of one tap,
- put the few real locks behind a password someone else holds,
- and make sure that whatever happens, the record of it is honest.

It cannot make him work. It makes not working visible, expensive, and a
decision — rather than a drift.
