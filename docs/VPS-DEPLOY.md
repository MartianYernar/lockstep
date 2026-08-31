# Moving the server to the VPS

`SETUP.md` §3–4 covers a *first* install. This covers a **move**, which is a
different job: there is already a live commitment, a guardian, and 128 events of
hash-chained history that must arrive intact.

Read `docs/LOCKDOWN.md` §2 first. Who owns the Oracle account is the whole point
of doing this — if it ends up in your account, you have moved the bot to a
nicer machine and changed nothing else.

---

## Before anything: the ordering problem

The bot is **not running right now**. It stopped when the Mac did. Your father
was told, so this is a planned gap rather than a silent one — keep it that way.

Do not run the laptop and the VPS at once. Two processes polling the same bot
token fight over `getUpdates`, and Telegram hands each of them a random half of
your messages. Stop one before starting the other.

---

## Step 1 — Your father creates the account (he does this, not you)

Per `docs/LOCKDOWN.md`: his email, his password, his phone for verification, and
you are not present. During instance creation Oracle asks for an SSH public key
— he pastes **yours**, so you can deploy but cannot delete.

- Ampere A1, 1 OCPU / 6 GB, Ubuntu 22.04
- VCN → Security Lists → Ingress: source `0.0.0.0/0`, TCP, port `8765`

Generate the key you give him, on this laptop:

```
ssh-keygen -t ed25519 -C lockstep-deploy -f "$HOME/.ssh/lockstep"
```

Send him `~/.ssh/lockstep.pub`. Never the file without `.pub`.

## Step 2 — Deploy

```
ssh -i ~/.ssh/lockstep ubuntu@YOUR_VPS_IP
sudo git clone <your-repo> /opt/lockstep
cd /opt/lockstep && sudo bash deploy/bootstrap.sh
```

It stops after creating `deploy/data/config.yaml` and tells you to edit it.
Don't edit it — you are replacing it wholesale in step 3.

## Step 3 — Carry the two irreplaceable files by hand

These are `.gitignore`d for good reason and must never travel through git.

```
scp -i ~/.ssh/lockstep config.yaml   ubuntu@YOUR_VPS_IP:/tmp/
scp -i ~/.ssh/lockstep lockstep.db   ubuntu@YOUR_VPS_IP:/tmp/
ssh -i ~/.ssh/lockstep ubuntu@YOUR_VPS_IP \
  'sudo mv /tmp/config.yaml /tmp/lockstep.db /opt/lockstep/deploy/data/ && \
   sudo chmod 600 /opt/lockstep/deploy/data/config.yaml'
```

Stop the laptop instance **before** copying the database, or you will carry a
snapshot that is already out of date.

## Step 4 — The five things that break on a move

### 1. `why_file` will not exist on the server

It currently points at the Obsidian vault on `E:`. On the VPS that path is
nothing, `_why_text()` swallows the error and returns `None`, and `/why` and
`/disable` quietly show you nothing at all.

That matters more than it sounds. `CLAUDE.md` says showing you your own
reasoning is the one safeguard here that actually does something. Silently
losing it is the worst possible failure, because it fails *open* and looks fine.

```
scp -i ~/.ssh/lockstep "E:/Obsidian_Yernar_StrategyLife/Yernar Strategy Life/Yernars_obs/Yernar's obs/Why Lockstep.md" \
  ubuntu@YOUR_VPS_IP:/tmp/why.md
ssh -i ~/.ssh/lockstep ubuntu@YOUR_VPS_IP 'sudo mv /tmp/why.md /opt/lockstep/deploy/data/why.md'
```

Then in the server's `config.yaml`: `why_file: /data/why.md`

This makes it a **snapshot**. Editing the note in Obsidian no longer changes
what the bot reads — you have to re-copy. Set a reminder to, or you will edit it
in November and be shown the August version.

> As of this writing that file is still the blank template. Every section empty.
> Copying it changes nothing until you write it.

### 2. The phone is still pointed at the laptop

`agent-android/macrodroid/RECIPE.md` line 31 and `termux/install.md` line 27
both hold the server address, and it is currently the laptop's **LAN IP**.

Repoint it to `http://YOUR_VPS_IP:8765`. A phone that cannot reach the server
stops polling, and a phone that stops polling cannot nag you — which from the
outside is indistinguishable from you doing everything right.

### 3. The heartbeat is switched off

`heartbeat.url` is `''`. Read the docstring in `server/api.py`: this is the one
alarm you cannot suppress, because the signal is *absence*. With it empty,
deleting the instance is silent.

Create a free check at healthchecks.io **on your father's account, not yours**,
period 15 minutes. Put the ping URL in `heartbeat.url`.

### 4. The nightly backup path was wrong until now

`backup.sh` defaulted to `/opt/lockstep/data/`, while bootstrap creates
`/opt/lockstep/deploy/data/`. Fixed, and it now exits loudly instead of failing
into a logfile. Verify it once by hand rather than trusting it:

```
sudo /opt/lockstep/deploy/backup.sh && ls -la /opt/lockstep/backups/
```

### 5. Give up root, or none of this counted

`docs/LOCKDOWN.md` §2. Once it is confirmed working:

```
sudo deluser ubuntu sudo
sudo passwd -l ubuntu
```

You keep SSH and lose root. `/logs` and `/restart` in Telegram cover the real
reasons you would have needed it.

---

## Step 5 — Prove the history arrived intact

This is the check that matters. The ledger tip **before** the move:

```
128 events, tip e061f08a0a05efc343015f2d5c0574f9fe75cd4e0812867426db4e95607b52c6
```

On the server, before starting anything:

```
sqlite3 /opt/lockstep/deploy/data/lockstep.db \
  'SELECT COUNT(*), (SELECT hash FROM events ORDER BY id DESC LIMIT 1) FROM events;'
```

Same count, same hash, or the database did not come across and you are about to
start a system that will report itself as tampered with.

Then `curl -s http://YOUR_VPS_IP:8765/health` — `ledger_intact` must be `true`.

And in Telegram, `/status`: guardian registered, commitment to **2026-11-28**.
If the date is wrong, the DB didn't arrive; stop and fix it.

---

## Afterwards

Revoke nothing yet — the laptop instance is your rollback until the VPS has run
a clean day. Once it has, and only then, follow `docs/HANDOFF.md` §"Before
giving the old machine away".
