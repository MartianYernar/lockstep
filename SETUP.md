# Setup

About an hour end to end. Do it in this order — each step depends on the last.

---

## 1. Telegram bot and chat IDs — 5 min

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the prompts.
   Keep the token it gives you.
2. **Both of you must send `/start` to your new bot at least once.** Telegram
   will not let a bot message someone who has never opened a chat with it. If
   you skip this, the guardian messages silently fail and you will not find out
   until the night you need them.
3. Read the chat IDs straight off your own bot:

   ```bash
   python3 scripts/setup-ids.py --write
   ```

   It watches for two minutes, prints whoever messages the bot, and offers to
   write the IDs into `config.yaml`. Less error-prone than copying numbers by
   hand from a third-party bot.

## 2. Gemini API key — 2 min

<https://aistudio.google.com/apikey> → Create API key. The free tier is far more
than enough: 24 checkpoints a day is nowhere near the limit.

## 3. A free VPS — 20 min

**Oracle Cloud Always Free** is the one to use: genuinely free forever, and the
ARM instances are generous. <https://cloud.oracle.com> → sign up → Create
Instance → *Ampere A1*, 1 OCPU / 6 GB, Ubuntu 22.04.

Two things people get wrong:

- **Open the port.** VCN → Security Lists → Add Ingress Rule: source `0.0.0.0/0`,
  TCP, destination port `8765`.
- **Oracle also has a host firewall.** On the instance itself:
  ```bash
  sudo iptables -I INPUT -p tcp --dport 8765 -j ACCEPT
  sudo netfilter-persistent save
  ```

Fly.io's free tier also works if you'd rather. Anything that stays up.

## 4. Deploy — 10 min

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git sqlite3
git clone <your-repo> /opt/lockstep && cd /opt/lockstep

mkdir -p deploy/data/photos
cp config.example.yaml deploy/data/config.yaml
nano deploy/data/config.yaml
```

Fill in: `bot_token`, `user_chat_id`, `guardian_chat_id`, `gemini.api_key`,
`commitment.start` (today), and — importantly — a long random `api.token`:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set `timezone: Asia/Almaty`. Then:

```bash
cd deploy && docker compose up -d && docker compose logs -f
```

The log prints a **setup code**. Leave it on screen for the next step.

## 5. Enrol your father — 5 min

Show him [docs/GUARDIAN.md](docs/GUARDIAN.md) first, so he knows what he's
agreeing to. Then, **from his own phone**, he sends the bot:

```
/guardian <SETUP_CODE> <a-password-you-will-never-guess>
```

The bot deletes the message immediately and tells him to write the password in
his notes app. Only a bcrypt hash is stored — it cannot be recovered, by him or
by you.

Check it took: `/status` should say `Guardian password: set`.

> The setup code dies when the container restarts, and enrolment only works from
> his configured chat ID. Once set, the password can only be changed with the
> current one.

## 6. Phone agent — 15 min

Pick one:
- **[MacroDroid recipe](agent-android/macrodroid/RECIPE.md)** — recommended,
  survives Android's battery killer better
- **[Termux script](agent-android/termux/install.md)** — fully scripted

Either way, do the battery-optimisation steps. That is the difference between an
agent that works and one that dies quietly at 2am.

## 7. Camera timestamp — 1 min

**This is not optional.** No visible clock in the frame means no photo passes.

- Samsung: Camera → Settings → **Date and time stamp** → on
- Xiaomi/POCO: Camera → Settings → **Timestamp watermark** → on
- Pixel / stock Android: no built-in stamp. Install **Open Camera** (F-Droid,
  free) → Settings → Photo settings → **Stamp photos** → on.

On Windows, a full-screen screenshot (`Win+PrtScn`, or `Win+Shift+S` for the
whole screen) includes the taskbar clock, which counts.

## 7b. Cold Turkey — only when you're on the Windows laptop (September)

Skip this now. It ships switched off.

1. Install **Cold Turkey Blocker** (free tier is enough) on the Windows laptop.
2. Build one block — call it `work` — set to *allow only* the sites you actually
   need, everything else blocked.
3. Before a `work` session, start the block with a timer for the session length.
4. In `config.yaml`, set `vision.require_blocker_for_work: true` and restart.

From then on, the first check of every `work` block wants a **full-screen
screenshot** (`Win+PrtScn`) showing all three of:
- Cold Turkey with the block **running** (timer counting down)
- the allow/block list readable on screen
- the taskbar clock

Send it as a photo in Telegram like any other check.

> One thing to expect: I wrote the validation against a description of Cold
> Turkey's UI, not the real thing. When you get to Windows in September, run a
> few real screenshots through it before switching the flag on for good — if the
> UI doesn't show the allowlist and the timer in one window, we'll adjust what
> the check asks for rather than you fighting it every day.

## 8. Backups — 2 min

```bash
sudo crontab -e
# 0 3 * * * /opt/lockstep/deploy/backup.sh >> /var/log/lockstep-backup.log 2>&1
```

Thirty days of nightly snapshots. This is what makes the ledger recoverable if
the database is ever lost — which is exactly the scenario the ledger exists for.

---

## Before the first real day

Run one day at low stakes first. In `config.yaml`:

```yaml
schedule:
  spot_check_avg_min: 90    # ~4 checks/day instead of ~8
commitment:
  months: 0                 # no lock yet
```

Live with it for two or three days. **Then** drop `spot_check_avg_min` to 45,
set `months: 3`, and have your father set the password.

Even at 45 minutes that's around eight photos a day, every day, for three months.
Finding out that's too much *during* a locked commitment is a bad way to find
it out.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Guardian gets nothing | He never sent `/start` to the bot. Step 1.3. |
| Phone gets 401 | `api.token` mismatch between `config.yaml` and the phone's config |
| Photos rejected `no_timestamp` | Camera timestamp stamp is off. Step 7. |
| Photos rejected `stale` | Phone clock has drifted — set it to network time |
| Phone silent when behind | Battery optimisation killed the agent. Step 6. |
| `⚠️ BROKEN at event N` in the report | The database was edited outside the bot. This is the tamper alarm doing its job. |
| Gemini errors `model not found` | Model names move. Change `gemini.model` in config.yaml to `gemini-2.0-flash` and restart. |
| Everything works on wifi, nothing on mobile data | See the RethinkDNS note below. |

### About your RethinkDNS setup

You mentioned you run RethinkDNS with social-media and adult-content blocklists.
It does not conflict with this system by default, but two things are worth knowing:

- Aggressive blocklists occasionally catch `api.telegram.org` or
  `generativelanguage.googleapis.com`. If the bot goes quiet or every photo
  comes back "could not reach the vision model", **allowlist those two domains
  in RethinkDNS first** before assuming the server is broken.
- The blocklists complement this rather than overlapping with it. RethinkDNS
  stops you opening the distraction; Lockstep notices you not doing the thing.
  Keep both — they fail in different directions.

Check the server is alive at any time:

```bash
curl -s http://YOUR_VPS_IP:8765/health | python3 -m json.tool
```
