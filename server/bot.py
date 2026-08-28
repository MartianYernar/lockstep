"""Telegram interface."""
from __future__ import annotations

import html
import logging
import os
from datetime import date, datetime, timedelta

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from . import images, planner, reminders
from .config import _hhmm
from .lock import LockError

log = logging.getLogger("bot")

PHOTO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "photos")

PLAN_HELP = (
    "Send the plan as one block per line:\n\n"
    "<code>16:00-16:40 commute to MNU library\n"
    "16:40-19:00 work: linear algebra pset ch3\n"
    "19:00-19:30 gym\n"
    "19:30-20:00 dinner\n"
    "20:00-21:30 work: essay draft\n"
    "21:30-22:00 clean room</code>\n\n"
    "Every minute of the enforced window needs a block. "
    "<code>relax</code> is a valid block — a planned rest is still a plan."
)


class LockstepBot:
    def __init__(self, cfg, db, ledger, engine, lock, validator):
        self.cfg = cfg
        self.db = db
        self.ledger = ledger
        self.engine = engine
        self.lock = lock
        self.validator = validator
        self.pending: dict[int, dict] = {}      # chat_id -> {"action": ..., ...}
        self.seen_strangers: dict[int, str] = {}
        self.last_level: int = 0
        self.app: Application | None = None

    # ======================================================================
    # plumbing
    # ======================================================================
    def _is_user(self, update: Update) -> bool:
        return update.effective_chat.id == self.cfg.user_chat_id

    def _is_guardian(self, update: Update) -> bool:
        return update.effective_chat.id == self.lock.guardian_id()

    async def send_user(self, text: str, **kw) -> None:
        """Send, and treat a block as an event rather than an error.

        Blocking the bot is the cheapest possible way out: no password, no
        reason, two taps. It cannot be prevented -- Telegram gives every user
        that button and should. What it can be is *loud*. Telegram returns 403
        the moment we try to message a user who has blocked us, so the attempt
        to disappear is itself the signal that reports you.

        Note what does NOT stop: the phone siren. MacroDroid polls the server
        directly and never touches Telegram, and debt keeps climbing because a
        blocked bot cannot receive your photos. Blocking makes it worse, not
        quieter.
        """
        try:
            await self.app.bot.send_message(self.cfg.user_chat_id, text,
                                            parse_mode=ParseMode.HTML, **kw)
        except Forbidden:
            await self._on_blocked()
        else:
            if self.db.lock()["blocked_since"]:
                await self._on_unblocked()

    async def _on_blocked(self) -> None:
        if self.db.lock()["blocked_since"]:
            return                                   # already reported
        now = self.cfg.now()
        self.db.set_lock(blocked_since=now.isoformat())
        self.ledger.append("bot_blocked", {"at": now.isoformat()})
        log.warning("user has blocked the bot")
        await self.send_guardian(
            "🚫 <b>Ansar has blocked the bot on Telegram.</b>\n\n"
            "That stops it messaging him, and it needed no password and no reason. "
            "It does not stop the system: his phone alarm runs off the server "
            "directly and will keep going, and his missed checkpoints keep counting.\n\n"
            "You are being told because a quiet exit was the whole thing this was "
            "built to prevent.")

    async def _on_unblocked(self) -> None:
        since = self.db.lock()["blocked_since"]
        self.db.set_lock(blocked_since=None)
        self.ledger.append("bot_unblocked", {"was_blocked_since": since})
        await self.send_guardian(
            f"✅ Ansar has unblocked the bot. It was blocked since "
            f"{datetime.fromisoformat(since):%d %b %H:%M}.")

    async def send_guardian(self, text: str, **kw) -> None:
        gid = self.lock.guardian_id()
        if gid:
            try:
                await self.app.bot.send_message(gid, text,
                                                parse_mode=ParseMode.HTML, **kw)
            except Exception as exc:                          # noqa: BLE001
                log.error("could not reach guardian: %s", exc)

    def target_plan_date(self, now: datetime) -> date:
        """Today if today is still salvageable and unplanned, else tomorrow."""
        today = now.date()
        minute = now.hour * 60 + now.minute
        _, end = self.engine.window(today)
        if not self.engine.has_plan(today) and minute < end:
            return today
        return today + timedelta(days=1)

    # ======================================================================
    # user commands
    # ======================================================================
    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self._is_guardian(update) and not self.lock.commitment_active(self.cfg.today()):
            await update.message.reply_text(
                "You are the guardian for Ansar's accountability system.\n\n"
                "There is no password to set — the system simply does what this "
                "Telegram account tells it and ignores everyone else, including him.\n\n"
                "To start it, send:\n\n<code>/commit 3</code>\n\n"
                "for three months. After that it stops by itself and asks whether "
                "to continue.",
                parse_mode=ParseMode.HTML)
            return
        await self.cmd_status(update, _ctx)

    async def cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self._is_guardian(update):
            await update.message.reply_text(
                "<b>Guardian commands</b>\n"
                "/commit 3 — start the three-month commitment\n"
                "/pause HOURS reason — grant a pause (illness, travel)\n"
                "/resume — end a pause early\n"
                "/extend MONTHS — extend the commitment period\n"
                "/stopbot reason — stop the system (only you can)\n"
                "/startbot — turn it back on\n"
                "/unblock 10 — open the app blocker briefly\n"
                "/block — close it again\n"
                "/report — today's numbers on demand\n"
                "/status — system state",
                parse_mode=ParseMode.HTML)
            return
        await update.message.reply_text(
            "<b>Commands</b>\n"
            "/plan — write the plan for the next day\n"
            "/today — today's plan and where you stand\n"
            "/status — system state and days remaining\n"
            "/more 30 — the task you're on needs longer; takes time from later\n"
            "/replan — rewrite the rest of today; the end time stays fixed\n"
            "/remind 3 sep 14:40 doctor — nags you the night before and that morning\n"
            "/reminders — what's coming up · /forget N — drop one\n"
            "/offset reason — push the day back one hour (once a day, needs a photo)\n"
            "/testsiren — fire the phone alarm now, to check it works\n"
            "/mute 3 — silence the siren for 3 hours (setup only)\n"
            "/disable — start the shutdown request\n\n"
            "During the window, just send the photo. No command needed.",
            parse_mode=ParseMode.HTML)

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        now = self.cfg.now()
        today = now.date()
        row = self.db.lock()
        ok, bad = self.ledger.verify()

        if not row["enabled"]:
            state = "🔴 OFF"
        elif row["paused_until"] and now < datetime.fromisoformat(row["paused_until"]):
            state = f"⏸ paused until {datetime.fromisoformat(row['paused_until']):%d %b %H:%M}"
        elif self.engine.in_window(now):
            state = "🟢 ON — window is open"
        else:
            state = "🟢 ON — outside the window"

        start, end = self.engine.window(today)
        lines = [
            f"<b>{state}</b>",
            f"Window: {start//60:02d}:{start%60:02d}–{end//60:02d}:{end%60:02d} · "
            + (f"random checks, ~1 per {self.cfg.spot_avg_min} min"
               if self.cfg.mode == "spot_check"
               else f"every {self.cfg.interval_min} min")
            + f" · {self.cfg.grace_min} min grace",
            f"Guardian: {'registered' if self.lock.is_configured else '⚠️ NOT SET'}",
            (f"Commitment: {self.lock.days_remaining(today)} days left "
             f"(until {row['commitment_end']})") if row["commitment_end"]
            else "Commitment: none — trial mode, you can stop any time",
            f"Log: {'intact' if ok else f'⚠️ BROKEN at event {bad}'}",
            f"Phone agent: {self._phone_status(now)}",
        ]
        if row["blocked_since"]:
            lines.append("🚫 <b>You have this bot blocked</b> — your father was told.")
        if row["muted_until"] and now < datetime.fromisoformat(row["muted_until"]):
            lines.append(f"🔕 Siren muted until "
                         f"{datetime.fromisoformat(row['muted_until']):%H:%M}")
        if self.engine.has_plan(today):
            sc = self.engine.score(today, now)
            lines.append(f"Today: {sc['hit']}/{sc['due']} checkpoints ({sc['pct']}%)")
        else:
            lines.append("Today: <b>no plan submitted</b>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    def _phone_status(self, now: datetime) -> str:
        last = self.db.lock()["last_agent_poll"]
        if not last:
            return "⚠️ never seen — not set up yet"
        gap = (now - datetime.fromisoformat(last)).total_seconds() / 60
        if gap < 5:
            return "🟢 alive"
        if gap < 60:
            return f"🟡 quiet for {int(gap)} min"
        return f"🔴 silent for {int(gap // 60)}h — the siren is dead"

    async def cmd_plan(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        now = self.cfg.now()
        target = self.target_plan_date(now)
        # Both shapes have to work: "/plan 16:00-17:00 gym" on one line, and
        # "/plan" with the whole plan on the lines below. Splitting the raw text
        # on its first space gets the second case badly wrong.
        lines = update.message.text.splitlines()
        head = lines[0].partition(" ")[2].strip()
        tail = "\n".join(lines[1:]).strip()
        inline = "\n".join(x for x in (head, tail) if x)

        if inline:
            await self._accept_plan(update, target, inline)
            return
        self.pending[update.effective_chat.id] = {"action": "plan", "date": target}
        await update.message.reply_text(
            f"Plan for <b>{target:%A %d %B}</b>.\n\n{PLAN_HELP}", parse_mode=ParseMode.HTML)

    async def _accept_plan(self, update: Update, target: date, text: str) -> None:
        now = self.cfg.now()
        start, end = self.cfg.enforce_start_min, self.cfg.enforce_end_min
        try:
            blocks = planner.parse(text)
            planner.validate_coverage(blocks, start, end)
        except planner.PlanError as exc:
            await update.message.reply_text(f"❌ {html.escape(str(exc))}",
                                            parse_mode=ParseMode.HTML)
            return

        existing = self.db.day(target)
        if existing and existing["plan_locked"] and target <= now.date() \
                and self.engine.in_window(now):
            await update.message.reply_text(
                "Today's plan is already locked and the window is open. "
                "You can't rewrite the plan you're being held to. Use /offset if something broke.")
            return

        self.db.ensure_day(target)
        self.db.replace_blocks(target, [b.as_tuple() for b in blocks])
        self.db.run(
            "UPDATE days SET plan_locked = 1, plan_submitted_at = ?, checkpoints_built = 0"
            " WHERE date = ?", (now.isoformat(), target.isoformat()))
        self.db.run("DELETE FROM checkpoints WHERE date = ? AND state = 'scheduled'",
                    (target.isoformat(),))
        built = self.engine.build_checkpoints(target, now)
        self.ledger.append("plan_submitted",
                           {"date": target.isoformat(), "blocks": len(blocks),
                            "live": built["live"], "voided": built["voided"]})
        self.pending.pop(update.effective_chat.id, None)

        tail = f"{built['live']} checkpoint{'s' if built['live'] != 1 else ''} ahead of you."
        if built["voided"]:
            tail += (f"\n({built['voided']} fell before you sent this and are void — "
                     "you can't photograph the past.)")
        if built["first"]:
            tail += f"\nFirst photo at <b>{datetime.fromisoformat(built['first']):%H:%M}</b>."
        else:
            tail += "\nNothing left today — this plan starts tomorrow."

        await update.message.reply_text(
            f"✅ Locked in for <b>{target:%A %d %B}</b>\n\n{planner.render(blocks)}\n\n{tail}",
            parse_mode=ParseMode.HTML)

    async def cmd_replan(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Rewrite the rest of today. The past and the end time stay fixed."""
        if not self._is_user(update):
            return
        now = self.cfg.now()
        today = now.date()
        if not self.engine.has_plan(today):
            await update.message.reply_text("No plan today yet — use /plan.")
            return
        if not self.engine.in_window(now):
            await update.message.reply_text(
                "The window isn't open. Use /plan for a future day.")
            return

        minute = now.hour * 60 + now.minute
        _, end = self.engine.window(today)
        lines = update.message.text.splitlines()
        head = lines[0].partition(" ")[2].strip()
        body = "\n".join(x for x in ("\n".join(lines[1:]).strip(), head) if x).strip()

        if not body:
            cur = [planner.Block(b["start_min"], b["end_min"], b["label"], b["category"])
                   for b in self.db.blocks(today) if b["end_min"] > minute]
            self.pending[update.effective_chat.id] = {"action": "replan"}
            await update.message.reply_text(
                f"Rewriting <b>{minute//60:02d}:{minute%60:02d}–"
                f"{end//60:02d}:{end%60:02d}</b>. What's already happened stays on "
                f"the record, and the day still ends at {end//60:02d}:{end%60:02d} — "
                f"you're rearranging the time you have left, not adding to it.\n\n"
                f"Currently:\n{planner.render(cur)}\n\n"
                f"Send the replacement, same format.",
                parse_mode=ParseMode.HTML)
            return
        await self._accept_replan(update, body)

    async def _accept_replan(self, update: Update, text: str) -> None:
        now = self.cfg.now()
        today = now.date()
        minute = now.hour * 60 + now.minute
        _, end = self.engine.window(today)
        try:
            blocks = planner.parse(text)
            planner.validate_coverage(blocks, minute, end)
        except planner.PlanError as exc:
            await update.message.reply_text(
                f"❌ {html.escape(str(exc))}", parse_mode=ParseMode.HTML)
            return

        kept = [b for b in self.db.blocks(today) if b["start_min"] < minute]
        rows = [(b["start_min"], min(b["end_min"], minute), b["label"], b["category"])
                for b in kept]
        rows += [(max(b.start_min, minute), b.end_min, b.label, b.category)
                 for b in blocks if b.end_min > minute]
        self.db.replace_blocks(today, rows)
        self.db.run(
            "DELETE FROM checkpoints WHERE date=? AND state='scheduled' AND due_at > ?",
            (today.isoformat(), now.isoformat()))
        built = self.engine.build_checkpoints(today, now)
        self.ledger.append("replanned", {"date": today.isoformat(),
                                         "from": f"{minute//60:02d}:{minute%60:02d}"})
        self.pending.pop(update.effective_chat.id, None)

        await update.message.reply_text(
            f"✅ Rest of today rewritten.\n\n{planner.render(blocks)}\n\n"
            f"{built['live']} checkpoints ahead. Day still ends at "
            f"{end//60:02d}:{end%60:02d}.",
            parse_mode=ParseMode.HTML)
        await self.send_guardian(
            f"📝 Ansar rewrote the rest of today from "
            f"{minute//60:02d}:{minute%60:02d}. The day still ends at the same time.")

    # ======================================================================
    # reminders
    # ======================================================================
    async def cmd_remind(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        raw = update.message.text.partition(" ")[2].strip()
        if not raw:
            await update.message.reply_text(
                "What and when?\n\n"
                "<code>/remind 3 sep 14:40 doctor's appointment</code>\n"
                "<code>/remind tomorrow 09:00 call mum</code>\n\n"
                "I'll nag you the evening before, the morning of, and an hour "
                "before it.", parse_mode=ParseMode.HTML)
            return
        now = self.cfg.now()
        try:
            got = reminders.parse(raw, now)
        except reminders.ParseError as exc:
            await update.message.reply_text(f"❌ {exc}", parse_mode=ParseMode.HTML)
            return
        cur = self.db.run(
            "INSERT INTO reminders (at_ts, text, created_at) VALUES (?, ?, ?)",
            (got.when.isoformat(), got.text, now.isoformat()))
        self.ledger.append("reminder_set", {"at": got.when.isoformat(), "text": got.text})
        await update.message.reply_text(
            f"🔔 <b>{html.escape(got.text)}</b>\n{got.when:%A %d %B, %H:%M}\n\n"
            f"I'll remind you the evening before, that morning, and an hour ahead.\n"
            f"<code>/forget {cur.lastrowid}</code> to drop it.",
            parse_mode=ParseMode.HTML)

    async def cmd_reminders(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        now = self.cfg.now()
        rows = self.db.q("SELECT * FROM reminders WHERE at_ts >= ? ORDER BY at_ts",
                         (now.isoformat(),))
        if not rows:
            await update.message.reply_text("Nothing coming up.")
            return
        lines = []
        for r in rows:
            when = datetime.fromisoformat(r["at_ts"])
            days = (when.date() - now.date()).days
            near = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
            lines.append(f"<code>{r['id']:>3}</code>  {when:%a %d %b %H:%M} "
                         f"({near})\n     {html.escape(r['text'])}")
        await update.message.reply_text("🔔 <b>Coming up</b>\n\n" + "\n".join(lines),
                                        parse_mode=ParseMode.HTML)

    async def cmd_forget(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        parts = update.message.text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await update.message.reply_text("Which one? /reminders shows the numbers.")
            return
        row = self.db.one("SELECT text FROM reminders WHERE id = ?", (int(parts[1]),))
        if not row:
            await update.message.reply_text("No reminder with that number.")
            return
        self.db.run("DELETE FROM reminders WHERE id = ?", (int(parts[1]),))
        await update.message.reply_text(f"Dropped: {html.escape(row['text'])}",
                                        parse_mode=ParseMode.HTML)

    # ======================================================================
    # distraction blocker
    # ======================================================================
    async def cmd_unblock(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_guardian(update):
            await update.message.reply_text(
                "Only your father can open this, from his own Telegram.")
            return
        now = self.cfg.now()
        conf = self.cfg.raw.get("apps", {})
        cooldown = int(conf.get("unlock_cooldown_days", 7))
        last = self.db.lock()["apps_opened_at"]
        if last and (now - datetime.fromisoformat(last)).days < cooldown:
            nxt = datetime.fromisoformat(last) + timedelta(days=cooldown)
            await update.message.reply_text(
                f"Last opened {datetime.fromisoformat(last):%d %b}. Next available "
                f"{nxt:%d %b}. He set that limit himself.")
            return
        parts = update.message.text.split()
        mins = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() \
            else int(conf.get("unlock_minutes", 10))
        mins = max(1, min(mins, 60))
        until = now + timedelta(minutes=mins)
        self.db.set_lock(apps_open_until=until.isoformat(), apps_opened_at=now.isoformat())
        self.ledger.append("apps_unlocked", {"minutes": mins, "until": until.isoformat()})
        await update.message.reply_text(
            f"🔓 Apps open for {mins} min, until {until:%H:%M}. Closes by itself.")
        await self.send_user(
            f"🔓 Your father opened the blocker for <b>{mins} minutes</b>, "
            f"until {until:%H:%M}. It shuts by itself.\n\n"
            "You wrote the warning about this yourself: ten minutes is enough to "
            "install something you'll regret. Do the thing you asked for.")

    async def cmd_block(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_guardian(update):
            await update.message.reply_text("Only your father can do that.")
            return
        self.db.set_lock(apps_open_until=None)
        self.ledger.append("apps_relocked", {})
        await update.message.reply_text("🔒 Closed again.")
        await self.send_user("🔒 Blocker closed again by your father.")

    async def cmd_today(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        now = self.cfg.now()
        today = now.date()
        if not self.engine.has_plan(today):
            await update.message.reply_text("No plan for today. Send /plan.")
            return
        blocks = [planner.Block(b["start_min"], b["end_min"], b["label"], b["category"])
                  for b in self.db.blocks(today)]
        sc = self.engine.score(today, now)
        await update.message.reply_text(
            f"<b>{today:%A %d %B}</b>\n\n{planner.render(blocks)}\n\n"
            f"✅ {sc['hit']}  ❌ {sc['missed']}  ⏭ {sc['excused']}  "
            f"— {sc['hit']}/{sc['due']} so far ({sc['pct']}%)",
            parse_mode=ParseMode.HTML)

    async def cmd_offset(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        now = self.cfg.now()
        today = now.date()
        reason = update.message.text.partition(" ")[2].strip()

        if not self.engine.in_window(now):
            await update.message.reply_text("The window isn't open. Nothing to offset.")
            return
        row = self.db.ensure_day(today)
        if row["offsets_used"] >= int(self.cfg.offset["max_per_day"]):
            await update.message.reply_text(
                "You've already used today's offset. One per day was the deal.")
            return
        if len(reason) < 10:
            await update.message.reply_text(
                "Say what actually happened, in a sentence:\n"
                "<code>/offset stayed back for a department meeting after class</code>",
                parse_mode=ParseMode.HTML)
            return

        if self.cfg.offset["require_photo"]:
            self.pending[update.effective_chat.id] = {"action": "offset_proof", "reason": reason}
            await update.message.reply_text(
                f"Reason noted: <i>{html.escape(reason)}</i>\n\n"
                "Now send one photo showing where you are, with the timestamp visible. "
                "The offset applies once that lands.",
                parse_mode=ParseMode.HTML)
        else:
            await self._grant_offset(update, reason, None)

    async def _grant_offset(self, update: Update, reason: str, photo_path: str | None) -> None:
        now = self.cfg.now()
        today = now.date()
        minutes = int(self.cfg.offset["minutes"])
        res = self.engine.apply_offset(today, now, minutes, reason)
        self.ledger.append("offset_used", {"date": today.isoformat(), "reason": reason,
                                           "minutes": minutes, "photo": photo_path})
        self.pending.pop(update.effective_chat.id, None)
        _, end = self.engine.window(today)
        await update.message.reply_text(
            f"⏭ Day pushed back {minutes} min. {res['excused']} checkpoints excused, "
            f"window now runs to {end//60:02d}:{end%60:02d}.\n"
            "That's the only one today.")
        await self.send_guardian(
            f"⏭ Ansar used today's one-hour offset.\nReason: <i>{html.escape(reason)}</i>")

    # ======================================================================
    # the shutdown request
    # ======================================================================
    async def cmd_disable(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """You can ask. You cannot do it.

        The password is gone deliberately. A password is a secret that can be
        forgotten, lost with a phone, or wheedled out of someone at 11pm — and
        this project has already been bitten once by a forgotten one. A command
        that only ever arrives from your father's own Telegram account cannot be
        forgotten, cannot be reset, and cannot be typed by you.
        """
        if not self._is_user(update):
            return
        if not self.lock.enabled:
            await update.message.reply_text("It's already off.")
            return
        if not self.lock.guardian_id():
            await update.message.reply_text(
                "No guardian is registered, so nothing is holding this shut. "
                "Get that set up first — otherwise the whole thing is decoration.")
            return
        self.pending[update.effective_chat.id] = {"action": "disable_reason"}
        left = self.lock.days_remaining(self.cfg.now().date())
        await update.message.reply_text(
            (f"You're {left} days from the end of the commitment.\n\n" if left else "")
            + "Write why you want this off. It goes to your father word for word, "
            "and only he can actually stop it — there's no password you can enter.\n\n"
            "/cancel to drop it.")

    async def _handle_disable_reason(self, update: Update, reason: str) -> None:
        if len(reason) < 15:
            await update.message.reply_text(
                "Longer than that. Write the actual reason — he's the one reading it.")
            return
        self.pending.pop(update.effective_chat.id, None)
        self.ledger.append("disable_requested", {"reason": reason})
        await self.send_guardian(
            "⚠️ <b>Ansar is asking to turn the system off.</b>\n\n"
            f"His reason:\n<i>{html.escape(reason)}</i>\n\n"
            "If you agree, send:\n<code>/stopbot</code>\n\n"
            "If you don't, do nothing at all. It stays on. You do not owe him a "
            "reply, and he cannot do this without you.")
        await update.message.reply_text(
            "Sent to your father. Only he can stop it now.\n\n"
            "If he says nothing, it stays on. That was the point of setting it up "
            "this way while you were thinking clearly.")

    async def cmd_stopbot(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_guardian(update):
            await update.message.reply_text(
                "Only your father can stop this, from his own Telegram.")
            return
        reason = update.message.text.partition(" ")[2].strip() or "no reason given"
        self.lock.disable(reason, self.cfg.now())
        await update.message.reply_text(
            "🔴 System stopped. Send <code>/startbot</code> to turn it back on "
            "whenever you decide.", parse_mode=ParseMode.HTML)
        await self.send_user(
            "🔴 <b>Your father stopped the system.</b>\n\n"
            "It stays off until he restarts it. You can't turn it back on yourself "
            "either — that direction is his too.")

    async def cmd_startbot(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_guardian(update):
            await update.message.reply_text("Only your father can restart it.")
            return
        self.lock.enable(self.cfg.now())
        await update.message.reply_text("🟢 System running again.")
        await self.send_user("🟢 <b>Your father restarted the system.</b>")

    async def cmd_enable(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self._is_guardian(update):
            await self.cmd_startbot(update, _ctx)
            return
        await update.message.reply_text(
            "Not yours to switch. Your father sends /startbot.")

    async def cmd_setguardian(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        parts = update.message.text.split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            seen = self.seen_strangers
            if not seen:
                await update.message.reply_text(
                    "Nobody unrecognised has messaged the bot yet. Ask your father to "
                    "send it any message, then run /setguardian again.")
                return
            lines = "\n".join(f"  <code>/setguardian {cid}</code> — {html.escape(name)}"
                               for cid, name in seen.items())
            await update.message.reply_text(
                f"People who've messaged the bot and aren't you:\n{lines}",
                parse_mode=ParseMode.HTML)
            return
        gid = int(parts[1])
        if gid == self.cfg.user_chat_id:
            await update.message.reply_text("That's you. The guardian has to be someone else.")
            return
        if self.lock.commitment_active(self.cfg.today()):
            await update.message.reply_text(
                "The commitment is running. You don't get to change who holds the "
                "switch halfway through — that's the point of it.")
            return
        self.lock.register_guardian(gid)
        await update.message.reply_text(f"✅ Guardian registered: <code>{gid}</code>",
                                        parse_mode=ParseMode.HTML)
        await self.send_guardian(
            "You are the guardian for Ansar's accountability system.\n\n"
            "There is no password to set. The system does what this Telegram "
            "account tells it and ignores everyone else, him included.\n\n"
            "To start it, send:\n\n<code>/commit 3</code>\n\nfor three months.")
        await update.message.reply_text("He's been asked to send /commit 3.")

    async def cmd_testsiren(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        parts = update.message.text.split()
        secs = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 180
        secs = max(30, min(secs, 600))
        until = self.cfg.now() + timedelta(seconds=secs)
        self.db.set_lock(siren_test_until=until.isoformat())
        self.ledger.append("siren_test", {"seconds": secs})
        await update.message.reply_text(
            f"🔔 Test siren armed for <b>{secs} seconds</b>.\n\n"
            "Your phone should start within a minute — that's the poll interval.\n\n"
            "<b>While it's going, check all four:</b>\n"
            "1. Put the phone on <b>silent</b> — it must keep sounding\n"
            "2. Turn <b>media volume to zero</b> — it must keep sounding\n"
            "3. Check your <b>ringtone</b> still works normally\n"
            "4. Check it <b>vibrates</b> too\n\n"
            "If it goes quiet on silent mode, Play Sound is on the wrong stream. "
            "Send /stopsiren to end it early.",
            parse_mode=ParseMode.HTML)

    async def cmd_stopsiren(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        self.db.set_lock(siren_test_until=None)
        await update.message.reply_text("🔕 Test siren off. Stops within a minute.")

    async def cmd_more(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Give the task you're on right now more time."""
        if not self._is_user(update):
            return
        now = self.cfg.now()
        today = now.date()
        parts = update.message.text.split()
        minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30

        if not self.engine.in_window(now):
            await update.message.reply_text("The window isn't open — nothing to extend.")
            return
        if not self.engine.has_plan(today):
            await update.message.reply_text("No plan today. Send /plan.")
            return
        block = self.engine.current_block(today, now)
        if block is None:
            await update.message.reply_text("You're not inside a planned block right now.")
            return

        cap = int(self.cfg.raw.get("extend", {}).get("max_minutes_per_day", 90))
        used = (self.db.day(today)["extend_minutes"] or 0)
        minutes = max(5, min(minutes, 120))
        if used + minutes > cap:
            await update.message.reply_text(
                f"That would put you {used + minutes} min over for the day, and the "
                f"limit is {cap}. You've used {used} so far.\n\n"
                "Plans that need more than this much stretching were written wrong. "
                "Write tomorrow's with bigger blocks instead.")
            return

        # You can only extend something you have already proven you're doing.
        # Otherwise this is just a way to push the whole day back by claiming to
        # be busy, which is an offset wearing a disguise.
        if not self.engine.block_was_proven(today, block["id"]):
            await update.message.reply_text(
                f"Send a photo of <b>{html.escape(block['label'])}</b> first.\n\n"
                "You can only extend a task you've shown you're actually doing — "
                "otherwise this is just the offset with extra steps.",
                parse_mode=ParseMode.HTML)
            return

        res = self.engine.extend_block(today, now, block["id"], minutes)
        if res.get("error"):
            await update.message.reply_text(res["error"])
            return
        _, end = self.engine.window(today)
        cost = []
        for label, mins in res["shortened"]:
            cost.append(f"• <b>{html.escape(label)}</b> loses {mins} min")
        for label in res["dropped"]:
            cost.append(f"• <b>{html.escape(label)}</b> is gone")
        await update.message.reply_text(
            f"⏱ <b>{html.escape(res['label'])}</b> now runs to "
            f"{res['new_end']//60:02d}:{res['new_end']%60:02d}.\n\n"
            f"<b>Paid for by:</b>\n" + "\n".join(cost) + "\n\n"
            f"Your day still ends at {end//60:02d}:{end%60:02d} — bedtime doesn't move. "
            f"Used {res['minutes'] + used} of {cap} min today.\n\n"
            f"If that's the wrong trade, use /replan instead and choose yourself.",
            parse_mode=ParseMode.HTML)

    async def cmd_mute(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Silence the phone for a while. Only while there is no commitment.

        This exists for setup and testing. Once the lock is live it refuses,
        because a mute button available on a bad evening is just the exit door
        with a friendlier label.
        """
        if not self._is_user(update):
            return
        now = self.cfg.now()
        if self.lock.commitment_active(now.date()):
            await update.message.reply_text(
                "No. The commitment is live, and a mute button on a bad evening "
                "is just the way out with a nicer name.\n\n"
                "If something real has happened: /offset for an hour, or ask your "
                "father for /pause.")
            return
        parts = update.message.text.split()
        hours = float(parts[1]) if len(parts) > 1 and parts[1].replace(".", "").isdigit() else 3
        hours = max(0.25, min(hours, 24))
        until = now + timedelta(hours=hours)
        self.db.set_lock(muted_until=until.isoformat(), siren_test_until=None)
        self.ledger.append("muted", {"until": until.isoformat()})
        await update.message.reply_text(
            f"🔕 Phone siren muted until <b>{until:%H:%M}</b>.\n\n"
            "Checkpoints still arrive and misses still count — this only silences "
            "the noise. /unmute to end it early.\n\n"
            "<i>This command stops working once the commitment is live.</i>",
            parse_mode=ParseMode.HTML)

    async def cmd_unmute(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        self.db.set_lock(muted_until=None)
        await update.message.reply_text("🔔 Siren back on.")

    async def cmd_cancel(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.pending.pop(update.effective_chat.id, None):
            await update.message.reply_text("Dropped.")
        else:
            await update.message.reply_text("Nothing pending.")

    # ======================================================================
    # guardian commands
    # ======================================================================
    async def cmd_commit(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """The guardian starts the clock. One command, no code, no password."""
        if not self._is_guardian(update):
            await update.message.reply_text(
                "Only the guardian can start the commitment.")
            return
        parts = update.message.text.split()
        months = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 3
        months = max(1, min(months, 24))
        today = self.cfg.now().date()
        if self.lock.commitment_active(today):
            await update.message.reply_text(
                f"Already running until {self.db.lock()['commitment_end']}. "
                f"Use /extend {months} to add more.")
            return
        end = self.lock.start_commitment(months, today)
        await update.message.reply_text(
            f"✅ Commitment started — runs to <b>{end}</b>.\n\n"
            "From now until then:\n"
            "• only you can stop it, with <code>/stopbot</code>\n"
            "• he cannot, by any route, and there is no password to lose\n"
            "• you'll get a short report each night\n"
            "• <code>/pause 24 reason</code> if he's genuinely ill or travelling\n\n"
            "If he asks you to stop it, you don't have to reply. Doing nothing "
            "is a valid answer and he knows that.",
            parse_mode=ParseMode.HTML)
        await self.send_user(
            f"🔒 <b>Your father started the commitment. It runs to {end}.</b>\n\n"
            "You can't stop it. Only he can, and only he can restart it. "
            "There's no password anywhere — nothing to find, forget or talk him out of.\n\n"
            "This is the version of you that wanted this, holding the door for the "
            "version that won't.")

    async def cmd_pause(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_guardian(update):
            await update.message.reply_text("Only the guardian can pause this.")
            return
        parts = update.message.text.split(maxsplit=2)
        if len(parts) < 2 or not parts[1].isdigit():
            await update.message.reply_text("Use: /pause HOURS reason")
            return
        hours = int(parts[1])
        reason = parts[2] if len(parts) > 2 else "no reason given"
        until = self.cfg.now() + timedelta(hours=hours)
        self.lock.pause(until, reason)
        await update.message.reply_text(f"⏸ Paused until {until:%d %b %H:%M}.")
        await self.send_user(
            f"⏸ Your father paused the system until <b>{until:%d %b %H:%M}</b>.\n"
            f"Reason: <i>{html.escape(reason)}</i>", )

    async def cmd_resume(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_guardian(update):
            return
        self.lock.enable(self.cfg.now())
        await update.message.reply_text("▶️ Resumed.")
        await self.send_user("▶️ Pause lifted. Back on.")

    async def cmd_extend(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_guardian(update):
            return
        parts = update.message.text.split()
        months = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 3
        end = self.lock.extend_commitment(months, self.cfg.now().date())
        await update.message.reply_text(f"Commitment now runs to {end}.")
        await self.send_user(f"Commitment extended by {months} months — to <b>{end}</b>.")

    async def cmd_report(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(self._report_text(self.cfg.now().date()),
                                        parse_mode=ParseMode.HTML)

    # ======================================================================
    # photos and free text
    # ======================================================================
    async def on_photo(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        now = self.cfg.now()
        today = now.date()
        chat = update.effective_chat.id
        pend = self.pending.get(chat, {})

        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        raw = bytes(await tg_file.download_as_bytearray())

        priv = self.cfg.raw.get("privacy", {})
        if priv.get("warn_on_gps", True) and images.has_gps(raw):
            await update.message.reply_text(
                "⚠️ That photo carried GPS coordinates. I stripped them before doing "
                "anything with it, but turn location tagging off in your camera app — "
                "every other app you send photos to will keep them.")
        # Downscaled and metadata-stripped before it goes to Google or to disk.
        blob, mime = images.prepare(raw, int(priv.get("max_dimension", 1024)))

        if pend.get("action") == "offset_proof":
            path = self._save_photo(blob, today, "offset")
            await self._grant_offset(update, pend["reason"], path)
            return

        if not self.lock.enabled:
            await update.message.reply_text("System is off. Nothing to prove.")
            return
        if not self.engine.in_window(now):
            await update.message.reply_text("Outside the window — nothing is being asked.")
            return
        if not self.engine.has_plan(today):
            await update.message.reply_text("There's no plan for today yet. Send /plan first.")
            return

        cp = self.engine.current_checkpoint(today, now)
        if cp is None:
            await update.message.reply_text("Nothing outstanding right now. 👍")
            return

        label = cp["label"] or "your planned task"
        category = cp["category"] or "other"
        await update.message.chat.send_action("typing")
        verdict = await self.validator.check(blob, label, category, now, mime=mime,
                                             kind=cp["kind"] or "activity")

        if not verdict.ok:
            self.db.run("UPDATE checkpoints SET attempts = attempts + 1, verdict = ? WHERE id = ?",
                        (verdict.to_json(), cp["id"]))
            # A frame rejected for privacy is never written to disk, not even as
            # a thumbnail. Storing the thing we just told you to retake would be
            # a strange way to respect the rule.
            self.ledger.append("photo_rejected",
                               {"checkpoint": cp["id"], "code": verdict.failure_code})
            await update.message.reply_text(
                f"❌ Not counted.\n\n{html.escape(verdict.reason)}\n\n"
                "Send another one — the checkpoint is still open.",
                parse_mode=ParseMode.HTML)
            return

        path = self._save_photo(blob, today, f"cp{cp['id']}")
        res = self.engine.satisfy(today, now, cp["id"], verdict.to_json(), path)
        self.ledger.append("checkpoint_satisfied",
                           {"checkpoint": cp["id"], "unverified": verdict.unverified})
        self.last_level = 0

        sc = self.engine.score(today, now)
        note = ""
        if res["swept"]:
            note = f"\n({res['swept']} earlier checkpoint{'s' if res['swept'] > 1 else ''} " \
                   "stay missed — one photo can't cover a past moment. Debt is cleared.)"
        if verdict.unverified:
            note += "\n⚠️ The vision model was unreachable; this one is flagged in the log."
            await self.send_guardian("⚠️ Vision model unreachable — a photo was accepted unchecked.")
        await update.message.reply_text(
            f"✅ Counted. {sc['hit']}/{sc['due']} today.{note}", parse_mode=ParseMode.HTML)

    def _save_photo(self, blob: bytes, day: date, tag: str) -> str | None:
        """Keep what the policy allows and no more.

        The default is a thumbnail: enough to remember what you sent, too small
        to be worth much to anyone who gets hold of the server.
        """
        priv = self.cfg.raw.get("privacy", {})
        mode = priv.get("store_photos", "thumbnail")
        if mode == "none":
            return None
        data = (blob if mode == "full"
                else images.thumbnail(blob, int(priv.get("thumbnail_dimension", 320))))
        folder = os.path.join(PHOTO_DIR, day.isoformat())
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{tag}_{datetime.now():%H%M%S}.jpg")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat.id
        text = update.message.text.strip()
        pend = self.pending.get(chat, {})
        action = pend.get("action")

        if action == "plan":
            await self._accept_plan(update, pend["date"], text)
            return
        if action == "replan":
            await self._accept_replan(update, text)
            return
        if action == "disable_reason":
            await self._handle_disable_reason(update, text)
            return

        # A bare plan pasted with no command is still obviously a plan.
        if self._is_user(update) and "\n" in text and planner.LINE_RE.match(text.splitlines()[0]):
            await self._accept_plan(update, self.target_plan_date(self.cfg.now()), text)
            return

        if self._is_user(update):
            await update.message.reply_text("Send a photo, or /help.")
            return

        # A stranger. Almost always the guardian saying hello before anyone has
        # told the bot who they are — previously this was silently discarded,
        # which is a miserable way to lose the one message that matters.
        if not self._is_guardian(update):
            chat = update.effective_chat
            name = " ".join(filter(None, [getattr(chat, "first_name", ""),
                                          getattr(chat, "last_name", "")])) or "unknown"
            self.seen_strangers[chat.id] = name
            await update.message.reply_text(
                "You're not registered with this bot yet. Ansar has been told you "
                "messaged — he'll add you.")
            await self.send_user(
                f"👤 <b>{html.escape(name)}</b> just messaged the bot "
                f"(<code>{chat.id}</code>) and isn't registered.\n\n"
                f"If that's your father:\n<code>/setguardian {chat.id}</code>")

    # ======================================================================
    # the tick
    # ======================================================================
    async def tick(self, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        now = self.cfg.now()
        try:
            actions = self.engine.tick(now)
        except Exception as exc:                              # noqa: BLE001
            log.exception("tick failed: %s", exc)
            return

        for act in self._merge(actions):
            try:
                await self._dispatch(act, now)
            except Exception as exc:                          # noqa: BLE001
                log.exception("action %s failed: %s", act.kind, exc)

        try:
            await self._fire_reminders(now)
        except Exception as exc:                              # noqa: BLE001
            log.exception("reminders failed: %s", exc)

        # Tell them when the pressure steps up, not on every single tick.
        st = self.engine.agent_state(now)
        if st.active and st.level > self.last_level:
            self.last_level = st.level
            await self.send_user(
                f"🔺 <b>{st.debt_minutes} minutes behind.</b> {html.escape(st.headline)}\n"
                f"{html.escape(st.detail)}\n\n"
                f"Your phone is now sounding {st.alarm_repeats}× a minute on the alarm "
                f"stream. It stops the moment a valid photo lands.")
        elif not st.active:
            self.last_level = 0

    @staticmethod
    def _merge(actions: list) -> list:
        """Collapse same-kind actions from one tick into one message each.

        Two checkpoints going overdue in the same tick produced two identical
        "grace period gone" messages, which reads like the bot stuttering. And
        two simultaneous asks are misleading: a single photo settles the newest
        and sweeps the rest, so asking twice implies work that isn't required.
        """
        from .engine import Action
        out, asks, misses = [], [], []
        for a in actions:
            if a.kind == "ask":
                asks.append(a)
            elif a.kind == "mark_missed":
                misses.append(a)
            else:
                out.append(a)
        if asks:
            newest = asks[-1]
            if len(asks) > 1:
                newest = Action("ask", {**newest.payload, "also_due": len(asks) - 1})
            out.append(newest)
        if misses:
            labels = list(dict.fromkeys(m.payload["label"] for m in misses))
            out.append(Action("mark_missed", {
                "labels": labels, "count": len(misses),
                "checkpoint_id": misses[-1].payload["checkpoint_id"],
            }))
        return out

    async def _fire_reminders(self, now: datetime) -> None:
        conf = self.cfg.raw.get("reminders", {})
        eve = _hhmm(conf.get("evening_before_at", "20:00"))
        morn = _hhmm(conf.get("morning_at", "08:00"))
        soon = int(conf.get("soon_minutes", 60))
        wording = {
            "eve": "🔔 <b>Tomorrow</b> — {when:%H:%M}\n{text}\n\nWorth putting in "
                   "tonight's plan.",
            "morn": "🔔 <b>Today</b> at {when:%H:%M}\n{text}",
            "soon": "🔔 <b>In under an hour</b> — {when:%H:%M}\n{text}",
        }
        for r in self.db.q("SELECT * FROM reminders WHERE at_ts >= ?",
                           ((now - timedelta(hours=2)).isoformat(),)):
            when = datetime.fromisoformat(r["at_ts"])
            fired = set(filter(None, (r["fired"] or "").split(",")))
            stage, settled = reminders.due_stages(when, now, fired, eve, morn, soon)
            if settled - fired:
                self.db.run("UPDATE reminders SET fired = ? WHERE id = ?",
                            (",".join(sorted(fired | settled)), r["id"]))
            if not stage:
                continue
            await self.send_user(
                wording[stage].format(when=when, text=html.escape(r["text"])))

    async def _dispatch(self, act, now: datetime) -> None:
        p = act.payload
        if act.kind == "ask":
            due = datetime.fromisoformat(p["due_at"])
            kind = p.get("kind", "activity")
            blocker = self.cfg.vision.get("blocker_name", "Cold Turkey")
            label = html.escape(p["label"])
            asks = {
                "blocker_toggle": (
                    f"🔒 <b>{due:%H:%M}</b> — starting: {label}\n\n"
                    f"<b>Shot 1 of 3.</b> Screenshot {blocker} with the block "
                    f"<b>running</b> — toggle on, timer going, taskbar clock visible."),
                "blocker_allow": (
                    f"🔒 <b>{due:%H:%M}</b> — {label}\n\n"
                    f"<b>Shot 2 of 3.</b> Screenshot the <b>allowed</b> sites list."),
                "blocker_block": (
                    f"🔒 <b>{due:%H:%M}</b> — {label}\n\n"
                    f"<b>Shot 3 of 3.</b> Screenshot the <b>blocked</b> sites list."),
                "activity": (
                    f"📸 <b>{due:%H:%M}</b> — {label}\n\n"
                    f"Photo of you actually doing it, timestamp visible."),
            }
            body = asks.get(kind, asks["activity"])
            if kind == "activity":
                body += ("\n\n🔒 No face, no name or login on screen, nothing "
                         "behind you that shows where you are.")
            body += f"\n\n{self.cfg.grace_min} min."
            await self.send_user(body)
            self.ledger.append("checkpoint_asked",
                               {"checkpoint": p["checkpoint_id"], "kind": kind})

        elif act.kind == "mark_missed":
            labels = p.get("labels") or [p.get("label", "your planned task")]
            what = ", ".join(html.escape(x) for x in labels)
            n = p.get("count", 1)
            prefix = f"⏰ Grace period gone ({n} checkpoints)" if n > 1 else "⏰ Grace period gone"
            await self.send_user(
                f"{prefix} for <b>{what}</b>. "
                "Escalation has started — it stops the moment a valid photo lands.")
            self.ledger.append("checkpoint_overdue",
                               {"checkpoint": p["checkpoint_id"], "count": n})

        elif act.kind == "outage_amnesty":
            await self.send_user(
                f"🔌 The server was down for about {p['minutes']} min. "
                f"{p['excused']} checkpoints in that gap are void — not counted against you.")
            await self.send_guardian(
                f"🔌 System was offline ~{p['minutes']} min; "
                f"{p['excused']} checkpoints voided. Not his doing.")

        elif act.kind == "phone_silent":
            await self.send_user(
                f"📵 <b>Your phone hasn't checked in for {p['minutes']} min.</b>\n\n"
                "The alarm agent is dead, so nothing on your phone will nag you right now. "
                "Check MacroDroid is running and that battery optimisation is off for it.\n\n"
                "Telegram checkpoints carry on regardless — this only affects the siren.")
            if p.get("escalate"):
                await self.send_guardian(
                    f"📵 Ansar's phone agent has been silent for {p['minutes']} min "
                    "during the enforcement window. Could be a flat battery or a crash; "
                    "could also be switched off. Worth asking.")

        elif act.kind == "plan_missing":
            await self.send_user(
                "⚠️ <b>The window is open and there's no plan for today.</b>\n\n"
                "Escalation runs until you send one. " + PLAN_HELP)

        elif act.kind == "plan_nag":
            await self.send_user(
                f"🗓 Plan for <b>{p['date']}</b> isn't written yet. "
                f"Due by {self.cfg.plan_due:%H:%M}. Send /plan.")

        elif act.kind == "daily_report":
            keep = int(self.cfg.raw.get("privacy", {}).get("retain_days", 7))
            gone = self.engine.purge_photos(keep)
            if gone:
                log.info("purged %d photos older than %d days", gone, keep)
            day = date.fromisoformat(p["date"])
            text = self._report_text(day)
            await self.send_user(text)
            await self.send_guardian(text)
            self.ledger.append("daily_report", {"date": p["date"]})

        elif act.kind == "commitment_over":
            await self.send_user(
                "🏁 <b>The commitment period is up.</b>\n\n"
                "It keeps running until you decide. Your father can send /extend 3 "
                "for another three months, or you can /disable — no password needed now.")
            await self.send_guardian(
                "🏁 The commitment period Ansar signed up for has ended.\n\n"
                "Send <code>/extend 3</code> to carry on for another three months, "
                "or let him stop.")

    def _report_text(self, day: date) -> str:
        sc = self.engine.score(day, self.cfg.now())
        row = self.db.day(day)
        ok, bad = self.ledger.verify()
        _, tip = self.ledger.tip()
        bar_len = 20
        filled = round(bar_len * sc["hit"] / sc["due"]) if sc["due"] else 0
        bar = "█" * filled + "░" * (bar_len - filled)
        lines = [
            f"<b>{day:%A %d %B}</b>",
            f"<code>{bar}</code> {sc['pct']}%",
            f"✅ {sc['hit']}   ❌ {sc['missed']}   ⏭ {sc['excused']}   of {sc['due']} due",
        ]
        if row and row["offsets_used"]:
            lines.append(f"Offset used: {html.escape(row['offset_reason'] or '')}")
        if not (row and row["plan_locked"]):
            lines.append("⚠️ No plan was submitted for this day.")
        if self.db.lock()["blocked_since"]:
            lines.append("🚫 Bot is currently blocked on Telegram.")
        lines.append(f"Log: {'intact' if ok else f'⚠️ BROKEN at event {bad}'} · <code>{tip[:12]}</code>")
        return "\n".join(lines)

    # ======================================================================
    def build(self) -> Application:
        app = Application.builder().token(self.cfg.bot_token).build()
        self.app = app
        for name, fn in [
            ("start", self.cmd_start), ("help", self.cmd_help), ("status", self.cmd_status),
            ("plan", self.cmd_plan), ("today", self.cmd_today), ("offset", self.cmd_offset),
            ("disable", self.cmd_disable), ("enable", self.cmd_enable),
            ("cancel", self.cmd_cancel), ("commit", self.cmd_commit),
            ("setguardian", self.cmd_setguardian),
            ("testsiren", self.cmd_testsiren), ("stopsiren", self.cmd_stopsiren),
            ("mute", self.cmd_mute), ("unmute", self.cmd_unmute),
            ("more", self.cmd_more), ("replan", self.cmd_replan),
            ("stopbot", self.cmd_stopbot), ("startbot", self.cmd_startbot),
            ("remind", self.cmd_remind), ("reminders", self.cmd_reminders),
            ("forget", self.cmd_forget),
            ("unblock", self.cmd_unblock), ("block", self.cmd_block),
            ("pause", self.cmd_pause),
            ("resume", self.cmd_resume), ("extend", self.cmd_extend), ("report", self.cmd_report),
        ]:
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.job_queue.run_repeating(self.tick, interval=30, first=10, name="tick")
        return app
