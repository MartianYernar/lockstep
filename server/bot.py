"""Telegram interface."""
from __future__ import annotations

import html
import logging
import os
from datetime import date, datetime, timedelta

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from . import planner
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
        self.last_level: int = 0
        self.app: Application | None = None

    # ======================================================================
    # plumbing
    # ======================================================================
    def _is_user(self, update: Update) -> bool:
        return update.effective_chat.id == self.cfg.user_chat_id

    def _is_guardian(self, update: Update) -> bool:
        return update.effective_chat.id == self.cfg.guardian_chat_id

    async def send_user(self, text: str, **kw) -> None:
        await self.app.bot.send_message(self.cfg.user_chat_id, text,
                                        parse_mode=ParseMode.HTML, **kw)

    async def send_guardian(self, text: str, **kw) -> None:
        if self.cfg.guardian_chat_id:
            try:
                await self.app.bot.send_message(self.cfg.guardian_chat_id, text,
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
        if self._is_guardian(update) and not self.lock.is_configured:
            await update.message.reply_text(
                "You are set as the guardian for this system.\n\n"
                "Ansar cannot turn it off without a password that only you know. "
                "To set it, send:\n\n"
                "<code>/guardian CODE your-password-here</code>\n\n"
                "where CODE is the setup code printed in the server log. "
                "Choose something he will not guess, and keep it in your notes app.",
                parse_mode=ParseMode.HTML)
            return
        await self.cmd_status(update, _ctx)

    async def cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self._is_guardian(update):
            await update.message.reply_text(
                "<b>Guardian commands</b>\n"
                "/guardian CODE PASSWORD — set the password (first time only)\n"
                "/guardian_change OLD NEW — change it\n"
                "/pause HOURS reason — grant a pause (illness, travel)\n"
                "/resume — end a pause early\n"
                "/extend MONTHS — extend the commitment period\n"
                "/report — today's numbers on demand\n"
                "/status — system state",
                parse_mode=ParseMode.HTML)
            return
        await update.message.reply_text(
            "<b>Commands</b>\n"
            "/plan — write the plan for the next day\n"
            "/today — today's plan and where you stand\n"
            "/status — system state and days remaining\n"
            "/offset reason — push the day back one hour (once a day, needs a photo)\n"
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
            f"Guardian password: {'set' if self.lock.is_configured else '⚠️ NOT SET'}",
            (f"Commitment: {self.lock.days_remaining(today)} days left "
             f"(until {row['commitment_end']})") if row["commitment_end"]
            else "Commitment: none — trial mode, you can stop any time",
            f"Log: {'intact' if ok else f'⚠️ BROKEN at event {bad}'}",
        ]
        if self.engine.has_plan(today):
            sc = self.engine.score(today, now)
            lines.append(f"Today: {sc['hit']}/{sc['due']} checkpoints ({sc['pct']}%)")
        else:
            lines.append("Today: <b>no plan submitted</b>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

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
        if not self._is_user(update):
            return
        now = self.cfg.now()
        if not self.lock.enabled:
            await update.message.reply_text("It's already off.")
            return
        if not self.lock.is_configured:
            await update.message.reply_text(
                "No guardian password is set yet, so there's nothing holding this shut. "
                "Get that set up first — otherwise this whole thing is decoration.")
            return

        left = self.lock.days_remaining(now.date())
        self.pending[update.effective_chat.id] = {"action": "disable_reason"}
        await update.message.reply_text(
            f"You're {left} days from the end of the commitment.\n\n"
            "Write the reason you want this off. It goes to your father's Telegram "
            "word for word, before you're even asked for the password.\n\n"
            "Send /cancel to drop it.")

    async def _handle_disable_reason(self, update: Update, reason: str) -> None:
        if len(reason) < 15:
            await update.message.reply_text(
                "Longer than that. Write the actual reason — he's the one reading it.")
            return
        self.pending[update.effective_chat.id] = {"action": "disable_password", "reason": reason}
        self.ledger.append("disable_requested", {"reason": reason})
        await self.send_guardian(
            "⚠️ <b>Ansar is asking to turn the system off.</b>\n\n"
            f"His reason:\n<i>{html.escape(reason)}</i>\n\n"
            "He is being asked for your password now. If you did not give it to him, "
            "he cannot proceed. You do not have to do anything.")
        await update.message.reply_text(
            "Sent to your father. Now send the password.\n\n"
            "Three wrong tries locks this for 30 minutes.")

    async def _handle_disable_password(self, update: Update, password: str, reason: str) -> None:
        now = self.cfg.now()
        try:
            ok = self.lock.verify(password, now)
        except LockError as exc:
            await update.message.reply_text(f"❌ {exc}")
            return

        try:
            await update.message.delete()          # don't leave it in the chat log
        except Exception:                                     # noqa: BLE001
            pass

        if not ok:
            attempts = self.db.lock()["failed_attempts"]
            await update.message.reply_text(f"❌ Wrong. Attempt {attempts} of 3.")
            await self.send_guardian(
                f"❌ Wrong password entered ({attempts}/3) for the shutdown request.")
            return

        self.pending.pop(update.effective_chat.id, None)
        self.lock.disable(reason, now)
        await update.message.reply_text(
            "🔴 System off.\n\nSend /enable when you want it back. "
            "No password needed to turn it back on — that direction was never locked.")
        await self.send_guardian(
            "🔴 <b>System turned OFF.</b> The password was correct.\n\n"
            f"Reason he gave:\n<i>{html.escape(reason)}</i>")

    async def cmd_enable(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        now = self.cfg.now()
        if self.lock.enabled:
            await update.message.reply_text("Already running.")
            return
        self.lock.enable(now)
        await update.message.reply_text("🟢 Back on.")
        await self.send_guardian("🟢 Ansar switched the system back on.")

    async def cmd_cancel(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.pending.pop(update.effective_chat.id, None):
            await update.message.reply_text("Dropped.")
        else:
            await update.message.reply_text("Nothing pending.")

    # ======================================================================
    # guardian commands
    # ======================================================================
    async def cmd_guardian(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        parts = update.message.text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("Use: /guardian CODE password")
            return
        try:
            self.lock.complete_setup(parts[1], parts[2], update.effective_chat.id)
        except LockError as exc:
            await update.message.reply_text(f"❌ {exc}")
            return
        try:
            await update.message.delete()
        except Exception:                                     # noqa: BLE001
            pass
        await update.message.reply_text(
            "✅ Password set. Write it in your notes app now — it cannot be recovered.\n\n"
            f"Ansar cannot switch this off before "
            f"<b>{self.db.lock()['commitment_end']}</b> without it. "
            "You'll get a short report every night, and a message the moment he asks to stop.",
            parse_mode=ParseMode.HTML)
        await self.send_user("✅ Guardian password is set. The lock is live.")

    async def cmd_guardian_change(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        parts = update.message.text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("Use: /guardian_change OLD NEW")
            return
        try:
            self.lock.change_password(parts[1], parts[2], update.effective_chat.id)
        except LockError as exc:
            await update.message.reply_text(f"❌ {exc}")
            return
        try:
            await update.message.delete()
        except Exception:                                     # noqa: BLE001
            pass
        await update.message.reply_text("✅ Password changed.")

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
        blob = bytes(await tg_file.download_as_bytearray())

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
        require_blocker = (
            bool(self.cfg.vision.get("require_blocker_for_work"))
            and category == "work"
            and self.engine.is_first_of_block(today, cp["id"])
        )
        await update.message.chat.send_action("typing")
        verdict = await self.validator.check(blob, label, category, now,
                                             require_blocker=require_blocker)

        if not verdict.ok:
            self.db.run("UPDATE checkpoints SET attempts = attempts + 1, verdict = ? WHERE id = ?",
                        (verdict.to_json(), cp["id"]))
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

    def _save_photo(self, blob: bytes, day: date, tag: str) -> str:
        folder = os.path.join(PHOTO_DIR, day.isoformat())
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{tag}_{datetime.now():%H%M%S}.jpg")
        with open(path, "wb") as fh:
            fh.write(blob)
        return path

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat.id
        text = update.message.text.strip()
        pend = self.pending.get(chat, {})
        action = pend.get("action")

        if action == "plan":
            await self._accept_plan(update, pend["date"], text)
            return
        if action == "disable_reason":
            await self._handle_disable_reason(update, text)
            return
        if action == "disable_password":
            await self._handle_disable_password(update, text, pend["reason"])
            return

        # A bare plan pasted with no command is still obviously a plan.
        if self._is_user(update) and "\n" in text and planner.LINE_RE.match(text.splitlines()[0]):
            await self._accept_plan(update, self.target_plan_date(self.cfg.now()), text)
            return

        if self._is_user(update):
            await update.message.reply_text("Send a photo, or /help.")

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

    async def _dispatch(self, act, now: datetime) -> None:
        p = act.payload
        if act.kind == "ask":
            due = datetime.fromisoformat(p["due_at"])
            if p.get("require_blocker"):
                blocker = self.cfg.vision.get("blocker_name", "Cold Turkey")
                body = (f"🔒 <b>{due:%H:%M}</b> — starting: {html.escape(p['label'])}\n\n"
                        f"This one needs a <b>full-screen screenshot</b> showing:\n"
                        f"• {blocker} with the block <b>running</b>\n"
                        f"• the allowed/blocked site list readable\n"
                        f"• the taskbar clock visible\n\n"
                        f"You have {self.cfg.grace_min} min.")
            else:
                body = (f"📸 <b>{due:%H:%M}</b> — {html.escape(p['label'])}\n\n"
                        f"Photo with the timestamp visible, within {self.cfg.grace_min} min.")
                if p.get("also_due"):
                    body += (f"\n\n({p['also_due']} earlier checkpoint came due too — "
                             "one photo covers it.)")
            await self.send_user(body)
            self.ledger.append("checkpoint_asked", {"checkpoint": p["checkpoint_id"],
                                                    "blocker": bool(p.get("require_blocker"))})

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

        elif act.kind == "plan_missing":
            await self.send_user(
                "⚠️ <b>The window is open and there's no plan for today.</b>\n\n"
                "Escalation runs until you send one. " + PLAN_HELP)

        elif act.kind == "plan_nag":
            await self.send_user(
                f"🗓 Plan for <b>{p['date']}</b> isn't written yet. "
                f"Due by {self.cfg.plan_due:%H:%M}. Send /plan.")

        elif act.kind == "daily_report":
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
            ("cancel", self.cmd_cancel), ("guardian", self.cmd_guardian),
            ("guardian_change", self.cmd_guardian_change), ("pause", self.cmd_pause),
            ("resume", self.cmd_resume), ("extend", self.cmd_extend), ("report", self.cmd_report),
        ]:
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.job_queue.run_repeating(self.tick, interval=30, first=10, name="tick")
        return app
