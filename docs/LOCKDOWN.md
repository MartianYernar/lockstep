# The two things that actually hold

Everything else in this project is friction. These two are boundaries.

---

## 1. His Telegram, unreachable even with his phone in your hand

The gap you spotted is real: 2FA protects his account from being logged in
elsewhere, but does nothing if you're holding his unlocked phone. You'd just
open the app.

**The fix is Telegram's own Passcode Lock.** A separate PIN for Telegram
specifically, on top of the phone's lock.

**He does this, on his phone, once:**

1. Telegram → Settings → **Privacy and Security** → **Passcode Lock**
2. Turn on. Set a PIN you have never seen and he never types near you.
3. **Auto-Lock → 1 minute** — this is the important setting. Without it the
   app stays open after he uses it.
4. Turn on **Unlock with fingerprint** so it isn't annoying for him.

Now: his unlocked phone shows you a locked Telegram. Grabbing it gets you
nothing.

**Also, same session — Two-Step Verification:**

- Telegram → Settings → Privacy and Security → **Two-Step Verification**
- Password + a recovery email **that is his, not yours, and not one you can
  reset**

That closes the other route: logging his account in on your own device.

**What this does not close:** asking him to unlock it for you. Nothing
technical closes that. It is the same hole as asking him for the password, and
it is the hole this whole project is built around rather than through.

---

## 2. The server, in his account, with no shell for you

The strongest available boundary, and the only one that turns "I choose not to"
into "I cannot."

**He does this:**

1. **He** creates the Oracle Cloud account — his email, his password, his phone
   number for verification. You are not present for this.
2. **He** creates the instance. During setup Oracle asks for an SSH public key —
   he pastes **yours**, so you can deploy.
3. That account, and only that account, can reach the console. The console is
   the only place an instance gets deleted.

**Then, on the server, remove your ability to escalate:**

```bash
# he runs this once, or you run it and he verifies
sudo deluser ubuntu sudo          # or whatever your user is called
sudo passwd -l ubuntu             # no password login either
```

You keep SSH. You lose root. You can look, you cannot remove.

**Optional and stronger:** you get no SSH at all. He deploys once with
`deploy/bootstrap.sh` and you never touch the box.

### Why you can afford to give up shell access

Because the two things you actually need day to day are now Telegram commands:

- **`/logs`** — recent server activity, read-only
- **`/restart`** — bounces the process; the supervisor brings it back in seconds

That covers essentially every real reason you'd have SSH'd in. Without those,
needing to debug something at midnight forces the account back into your hands,
and the boundary quietly dissolves. With them, it holds.

`/restart` cannot be used to switch anything off — Docker's `restart: always`
brings it straight back, and if it somehow didn't, the external heartbeat tells
your father within fifteen minutes.

---

## What is left open, stated plainly

**You can ask him.** For the Telegram PIN, for the cloud password, to send
`/stopnow`. He may say yes.

There is no technical answer to that and there never will be. It is the one
door this system deliberately leaves unlocked, because a system with no door at
all is one you would have destroyed in week two.

What stands between you and that door: you have to ask a person, out loud, for
a specific thing, with a reason. Then wait 24 hours. Then still want it.

That is the whole design. Everything else on this page just stops you from
walking around it without having that conversation.
