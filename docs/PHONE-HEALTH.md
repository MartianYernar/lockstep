# Fixing the phone before it becomes the system

A phone that randomly reboots is worse than no enforcement at all, because the
failure is **silent**. The siren doesn't go off, nothing looks broken, and it
reads exactly like a day you did everything right. That's why this comes first.

(The server now watches for it — see *The watchdog* at the bottom — but a
watchdog that fires every day is a nuisance, not a solution.)

---

## What Kidslox almost certainly left behind

Parental-control apps don't work like normal apps. To block Chrome and YouTube
they install themselves at a much deeper level, and uninstalling the app does
**not** always remove those hooks. What's left is usually a half-configured
system service fighting the OS — which produces exactly what you're describing:
lag, then spontaneous reboots.

Check all five, in this order. Any leftover you find is a likely cause.

### 1. Device admin
`Settings → Security & privacy → More security settings → Device admin apps`

Anything you don't recognise, or any greyed-out entry from a deleted app —
deactivate it. A device-admin app can't be fully uninstalled until you do, so a
"deleted" app may still be running.

### 2. A leftover VPN — this is the most likely culprit
`Settings → Connections → More connection settings → VPN`

Kidslox filters content by routing your traffic through a **local VPN**. If that
profile survived the uninstall, your phone is trying to push every packet
through a service that no longer exists. That will lag a phone badly and can
reboot it.

Delete every VPN profile you didn't create yourself. (Keep RethinkDNS —
that one's yours and it's fine.)

**Careful:** RethinkDNS also uses the VPN slot. Android only allows one VPN at a
time, so a dead Kidslox profile fighting RethinkDNS for it is a very plausible
explanation for both the lag and the reboots.

### 3. Accessibility services
`Settings → Accessibility → Installed apps / Downloaded apps`

Blocker apps use accessibility to watch what you open. A leftover service here
runs constantly against every screen you touch. Turn off anything unfamiliar.

### 4. Special app access
`Settings → Apps → ⋮ → Special access`

Check **Usage access**, **Display over other apps**, and **Device admin apps**.
Revoke anything belonging to an app you removed.

### 5. A work profile
`Settings → Accounts and backup → Manage accounts` — look for a **Work** profile
or briefcase icon.

Some blockers provision one. If you find a work profile you didn't set up
deliberately, remove it. This one is worth checking even though it's rarer,
because it's the hardest to spot and the most disruptive.

---

## Then prove it's fixed

### Safe Mode tells you software vs hardware
Hold power → long-press **Power off** → **Safe mode**. Only system apps run.

Leave it in safe mode for a few hours of normal use.

- **Reboots stop** → it's a third-party app. The list above is where it lives.
- **Reboots continue** → it's not an app. Look at storage and battery next.

### Storage
`Settings → Device care → Storage`

Under ~10% free will make any Android phone lag and can cause reboots. Get to
20% free or better.

### Battery and heat
`Settings → Device care → Battery`

A swollen or degraded battery causes reboots under load — the phone browns out
when it draws current. If the back has any bulge at all, stop and get it
replaced; that's a fire risk, not a performance issue.

### Last resort
Back up, then factory reset. It genuinely fixes leftover-blocker damage, because
it removes the layer the uninstall didn't. Do it before setting up MacroDroid,
not after — otherwise you'll be reinstalling everything twice.

---

## The irony worth noticing

You installed Kidslox to constrain yourself, it broke your phone, and now the
wreckage is blocking a better system you actually chose.

That's the argument for this design in one sentence: **nothing here takes
control of your phone.** MacroDroid is an ordinary app that reads a number from
a server and plays a sound. No device admin, no VPN, no accessibility service,
nothing that can survive being uninstalled. If you ever want it gone, uninstall
it and your phone is exactly as it was.

The thing stopping you from quitting is your father having the password. That is
deliberately a *social* obstacle rather than a technical one — technical
obstacles on your own hardware either get bypassed in ten minutes, or they do
what Kidslox did to your phone.

---

## The watchdog

The server now notices when your phone goes quiet:

- Phone hasn't polled for **~5 minutes** during the window → you get a Telegram
  message saying the agent is dead
- Silent for **60 minutes** → your father is told too, with the honest wording
  that it could be a flat battery, a crash, or you switching it off

Telegram checkpoints carry on regardless — a dead phone agent only kills the
siren, not the accountability. But you'll know, which is the point.
