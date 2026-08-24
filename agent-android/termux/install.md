# Phone agent — Termux route

Fully scripted, nothing to click through. Slightly more fragile than MacroDroid
against Android's battery killer, so do the Doze steps at the bottom.

## 1. Install

Get Termux and Termux:API from **F-Droid**, not the Play Store — the Play Store
builds are abandoned and `termux-api` will not work with them.

- <https://f-droid.org/packages/com.termux/>
- <https://f-droid.org/packages/com.termux.api/>

Then, inside Termux:

```
pkg update && pkg install -y termux-api jq curl
termux-setup-storage
```

## 2. Configure

```
mkdir -p ~/lockstep
cat > ~/.lockstep-agent.json <<'JSON'
{
  "server": "http://YOUR_VPS_IP:8765",
  "token": "THE_api.token_FROM_config.yaml",
  "poll_sec": 60,
  "bot_username": "your_bot_username"
}
JSON
chmod 600 ~/.lockstep-agent.json
```

Copy `nag.sh` into `~/lockstep/nag.sh`, then:

```
chmod +x ~/lockstep/nag.sh
~/lockstep/nag.sh          # run it in the foreground once to check it works
```

You should see it write to `~/lockstep-agent.log`. Kill it with Ctrl-C.

## 3. Make it start on boot

```
pkg install -y termux-services
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/lockstep <<'SH'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
~/lockstep/nag.sh &
SH
chmod +x ~/.termux/boot/lockstep
```

Install **Termux:Boot** from F-Droid as well, and open it once — it does nothing
visible, but it will not run at boot until it has been launched at least once.

## 4. Stop Android from killing it

This is the part that actually decides whether the agent survives the night.

- Settings → Apps → Termux → Battery → **Unrestricted**
- Same for Termux:API and Termux:Boot
- On Xiaomi/Redmi/POCO: Security app → Permissions → **Autostart** → enable Termux
- On Samsung: Settings → Battery → Background usage limits → make sure Termux is
  **not** in "Sleeping apps" or "Deep sleeping apps"

## 5. Check it

With debt on the clock, you should get: vibration, a persistent notification,
and a spoken line on the **alarm** stream. Your ringtone and media volume are
untouched — only the alarm slider moves, and it is restored the moment you clear.

```
tail -f ~/lockstep-agent.log
```
