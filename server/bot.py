"""Telegram interface."""
from __future__ import annotations

import html
import logging
import os
import pathlib
import re
from datetime import date, datetime, timedelta

from telegram import (BotCommand, BotCommandScopeChat, InlineKeyboardButton,
                      InlineKeyboardMarkup, ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from . import images, planner, reminders, vision
from .dailydoc import DailyDoc
from .microsteps import DONE_WORDS, Steps
from .config import _hhmm
from .lock import LockError

log = logging.getLogger("bot")

PHOTO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "photos")

PLAN_HELP = (
    "send the plan, one block per line:\n\n"
    "<code>16:00-16:40 commute to MNU library\n"
    "16:40-19:00 work: linear algebra pset ch3\n"
    "19:00-19:30 gym\n"
    "19:30-20:00 dinner\n"
    "20:00-21:30 work: essay draft\n"
    "21:30-22:00 clean room</code>\n\n"
    "cover the whole window. <code>relax</code> counts as a block."
)


class LockstepBot:
    def __init__(self, cfg, db, ledger, engine, lock, validator):
        self.cfg = cfg
        self.db = db
        self.ledger = ledger
        self.engine = engine
        self.lock = lock
        self.validator = validator
        from .coach import Coach
        self.coach = Coach(cfg, db, engine)
        self.tasks = self.coach.tasks
        self.foundations = self.coach.foundations
        self.standups = self.coach.standups
        from .agenda import Agenda
        from .dayplan import DayPlanner
        self.agenda = Agenda(db, cfg, self.tasks)
        self.coach.agenda = self.agenda
        self.doc = DailyDoc(cfg)
        self.steps = Steps(db, cfg)
        self.dayplanner = DayPlanner(self.coach, cfg, db, self.agenda, self.doc)
        from .dayplan import Proposals
        self.proposals = Proposals(db)
        try:
            self.coach.terms.seed(cfg.raw.get("terms"), cfg.now())
            self.coach.routines.seed(cfg.raw.get("routines"), cfg.now())
        except Exception as exc:                              # noqa: BLE001
            log.warning("term seed failed: %s", exc)
        # The last photo the checker turned down, held in memory only, so he can
        # explain it instead of re-shooting. Never written to disk.
        self.last_reject: dict | None = None
        try:
            self.tasks.seed((cfg.raw.get("tasks") or {}).get("seed"), cfg.now())
        except Exception as exc:                              # noqa: BLE001
            log.warning("task seed failed: %s", exc)
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
            f"🚫 <b>{self.cfg.user_name} blocked the bot on Telegram.</b> No password, "
            "no reason needed.\n\n"
            "His alarm and his misses keep going — only my messages stop.\n\n"
            "Telling you because a quiet exit is the one thing this is for.")

    async def _on_unblocked(self) -> None:
        since = self.db.lock()["blocked_since"]
        self.db.set_lock(blocked_since=None)
        self.ledger.append("bot_unblocked", {"was_blocked_since": since})
        await self.send_guardian(
            f"✅ {self.cfg.user_name} has unblocked the bot. It was blocked since "
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
    # ======================================================================
    # buttons
    #
    # Typing /goals and /plan on a phone every evening is friction in exactly
    # the wrong place -- the cost should sit on skipping the work, not on
    # reporting it. These are plain reply keyboards: tapping one sends the same
    # text you would have typed, so every existing handler works unchanged.
    # ======================================================================
    USER_KEYS = [["/today", "/goals"], ["/plan", "/steps"],
                 ["/tasks", "/events"], ["/done", "/outside", "/help"]]
    GUARDIAN_KEYS = [["/report", "/status"], ["/pause 24 ", "/resume"], ["/help"]]

    def _keyboard(self, update: Update) -> ReplyKeyboardMarkup:
        keys = self.GUARDIAN_KEYS if self._is_guardian(update) else self.USER_KEYS
        return ReplyKeyboardMarkup(keys, resize_keyboard=True, is_persistent=True)

    async def publish_command_menu(self, app: Application) -> None:
        """Fill in Telegram's own '/' menu, separately for each side.

        Without a scope the guardian is offered /plan and /offset, which do
        nothing for him, and never sees /stopbot -- the one command that is
        actually his.
        """
        try:
            await app.bot.set_my_commands([
                BotCommand("today", "today's goals, plan and score"),
                BotCommand("goals", "the 3-5 things tomorrow is for"),
                BotCommand("done", "tick off a goal"),
                BotCommand("steps", "break this task into small steps"),
                BotCommand("tasks", "your big tasks and deadlines"),
                BotCommand("events", "meetings and events coming up"),
                BotCommand("night", "tonight's basics: socks, clothes, teeth"),
                BotCommand("standup", "what am I doing exactly, right now"),
                BotCommand("paper", "this block is on paper, not the computer"),
                BotCommand("plan2", "build the next 2 days for me"),
                BotCommand("forecast", "will I make my deadlines?"),
                BotCommand("plan", "write tomorrow's timetable"),
                BotCommand("status", "system state and days left"),
                BotCommand("more", "the task needs longer"),
                BotCommand("replan", "rewrite the rest of today"),
                BotCommand("offset", "push the day back an hour"),
                BotCommand("outside", "going out — mute the offline siren"),
                BotCommand("why", "read back your own reasoning"),
                BotCommand("testsiren", "check the phone alarm works"),
                BotCommand("help", "all commands"),
            ])
            gid = self.lock.guardian_id()
            if gid:
                await app.bot.set_my_commands([
                    BotCommand("report", "today's numbers"),
                    BotCommand("status", "is it running, days left"),
                    BotCommand("pause", "pause it — illness, travel"),
                    BotCommand("resume", "end a pause early"),
                    BotCommand("stopbot", "agree to stop, 24h from now"),
                    BotCommand("cancelstop", "call off a scheduled stop"),
                    BotCommand("extend", "carry on another 3 months"),
                    BotCommand("help", "all commands"),
                ], scope=BotCommandScopeChat(chat_id=gid))
        except Exception as exc:                              # noqa: BLE001
            log.warning("could not publish the command menu: %s", exc)

    async def on_button(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Inline buttons: ticking off a goal, or a step of a block."""
        q = update.callback_query
        await q.answer()
        chat = q.message.chat.id if getattr(q, "message", None) else None
        if chat is not None and chat != self.cfg.user_chat_id:
            return
        if q.data.startswith("step:"):
            await self._on_step_button(q)
            return
        if q.data.startswith("task:"):
            await self._on_task_button(q)
            return
        if q.data.startswith("basic:"):
            await self._on_basic_button(q)
            return
        if q.data.startswith("plan:"):
            await self._on_plan_button(q)
            return
        if not q.data.startswith("done:"):
            return
        today = self.cfg.now().date()
        idx = int(q.data.split(":", 1)[1])
        if self.db.mark_goal(today, idx, True):
            self.ledger.append("goal_done", {"date": today.isoformat(), "idx": idx})
        rows = self.db.goals(today)
        hit = sum(1 for r in rows if r["done"])
        await q.edit_message_text(
            f"{self._goals_text(today)}\n\n<b>{hit}/{len(rows)}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=self._goal_buttons(today))

    async def _on_step_button(self, q) -> None:
        day = self.cfg.now().date()
        _, bi, idx = q.data.split(":", 2)
        blocks = self.db.blocks(day)
        try:
            label = blocks[int(bi)]["label"]
        except (IndexError, ValueError):
            return
        self.coach.tick_step(day, label, int(idx), True)
        await q.edit_message_text(
            self._render_steps(day, label), parse_mode=ParseMode.HTML,
            reply_markup=self._step_buttons(day, int(bi), label))

    @staticmethod
    def _nice(seed: int) -> str:
        return ["✅ got it.", "✅ nice.", "✅ good stuff.", "✅ that counts 👊",
                "✅ bet.", "✅ ok ok, counted."][int(seed or 0) % 6]

    def _goal_buttons(self, day: date) -> InlineKeyboardMarkup | None:
        left = [g for g in self.db.goals(day) if not g["done"]]
        if not left:
            return None
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"✔ {g['text'][:40]}", callback_data=f"done:{g['idx']}")]
             for g in left])

    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self._is_guardian(update) and not self.lock.commitment_active(self.cfg.today()):
            await update.message.reply_text(
                f"You're the guardian for {self.cfg.user_name}.\n\n"
                "No password — it listens to this account and nobody else, "
                "him included.\n\n"
                "To start three months:\n\n<code>/commit 3</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=self._keyboard(update))
            return
        await update.message.reply_text(
            "yo. buttons are under the text box now, no need to type commands.",
            reply_markup=self._keyboard(update))
        await self.cmd_status(update, _ctx)

    async def cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self._is_guardian(update):
            await update.message.reply_text(
                "<b>Guardian</b>\n"
                "/commit 3 — start three months\n"
                "/pause HOURS reason · /resume\n"
                "/extend MONTHS — carry on longer\n\n"
                "/stopbot reason — stop it, 24h from now\n"
                "/stopnow reason — real emergencies only\n"
                "/cancelstop · /startbot\n\n"
                "/unblock 10 · /block — the app blocker\n"
                "/report · /status",
                parse_mode=ParseMode.HTML,
                reply_markup=self._keyboard(update))
            return
        await update.message.reply_text(
            "during the window just send me a photo of you doing it, clock in shot. "
            "a photo of your laptop with the work on it counts.\n\n"
            "<b>The day</b>\n"
            "/goals — the 3-5 things tomorrow is for\n"
            "/plan — tomorrow's timetable · /done 2 — tick a goal off\n"
            "/doc — what I read in your google doc for today\n"
            "/steps — break this task into steps · /today · /status\n\n"
            "<b>Food</b>\n"
            "/eat 15 — or just say \"eating 15 min\". no pic needed for food\n\n"
            "<b>Big stuff</b>\n"
            "/tasks — your big tasks · /task add Physics IA due 15 oct\n"
            "/events — meetings coming up · /remind 3 sep 14:40 dentist\n"
            "(or just tell me — \"meeting thursday 3pm\" and I'll save it)\n\n"
            "<b>When it goes wrong</b>\n"
            "/more 30 — this task needs longer\n"
            "/replan — rewrite the rest of today\n"
            "/offset reason — push the day back an hour (once a day, photo)\n"
            "/outside — going out (photo) · /latenight reason — 2h, once a week\n\n"
            "<b>Everything else</b>\n"
            "/remind 3 sep 14:40 doctor · /reminders · /forget N\n"
            "/why · /testsiren · /mute 3 · /logs · /restart · /disable\n\n"
            "or just text me like normal, I'm here.",
            parse_mode=ParseMode.HTML,
            reply_markup=self._keyboard(update))

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
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
        pend = self.lock.pending_stop()
        if pend:
            lines.append(f"⏳ Stopping at {pend:%H:%M on %d %b} — /nevermind to call it off")
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

    # ======================================================================
    # the day's few things that matter
    # ======================================================================
    def _goal_conf(self) -> tuple[int, int]:
        g = self.cfg.raw.get("goals", {}) or {}
        return int(g.get("min", 3)), int(g.get("max", 5))

    def _goals_text(self, day: date) -> str | None:
        rows = self.db.goals(day)
        if not rows:
            return None
        return "\n".join(
            f"{'✅' if r['done'] else '▫️'} <b>{r['idx']}.</b> {html.escape(r['text'])}"
            for r in rows)

    async def cmd_goals(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """The 3-4 things the day is actually for, written before the timetable.

        A plan says where the hours go. It does not say what they were for, and
        without that a day can be fully compliant and still worth nothing.
        """
        if not self._is_user(update):
            return
        now = self.cfg.now()
        target = self.target_plan_date(now)
        lo, hi = self._goal_conf()

        lines = update.message.text.splitlines()
        head = lines[0].partition(" ")[2].strip()
        tail = "\n".join(lines[1:]).strip()
        body = "\n".join(x for x in (head, tail) if x)

        if not body:
            # No argument: show today's if we have them, otherwise ask.
            existing = self._goals_text(now.date())
            if existing:
                await update.message.reply_text(
                    f"<b>Goals for {now.date():%A %d %B}</b>\n\n{existing}\n\n"
                    "<code>/done 2</code> when one's done.",
                    parse_mode=ParseMode.HTML)
                return
            self.pending[update.effective_chat.id] = {"action": "goals", "date": target}
            await update.message.reply_text(
                f"what are the <b>{lo}-{hi} things</b> that matter for {target:%A}?\n\n"
                "outcomes, not times. one per line, like:\n\n"
                "<code>finish linear algebra pset ch3\n"
                "first draft of the essay, any quality\n"
                "gym, legs</code>",
                parse_mode=ParseMode.HTML)
            return
        await self._accept_goals(update, target, body)

    async def _accept_goals(self, update: Update, target: date, text: str) -> None:
        lo, hi = self._goal_conf()
        items = [l.strip(" -•\t").strip() for l in text.splitlines()]
        items = [i for i in items if i]
        if len(items) < lo:
            await update.message.reply_text(
                f"only {len(items)}? give me at least <b>{lo}</b>.",
                parse_mode=ParseMode.HTML)
            return
        if len(items) > hi:
            await update.message.reply_text(
                f"{len(items)} is too many bro. cut it to <b>{hi}</b> — the rest is a wish list.",
                parse_mode=ParseMode.HTML)
            return
        self.db.ensure_day(target)
        self.db.set_goals(target, items)
        self.ledger.append("goals_set", {"date": target.isoformat(), "count": len(items)})
        held = self.pending.pop(update.effective_chat.id, None) or {}
        then = (held.get("then_plan") if held.get("action") == "goals"
                and held.get("date") == target else None)
        await update.message.reply_text(
            f"<b>{target:%A %d %B} is for:</b>\n\n{self._goals_text(target)}\n\n"
            + ("locking the plan now 👇" if then else "Now the timetable — /plan."),
            parse_mode=ParseMode.HTML)
        if then:
            await self._accept_plan(update, target, then)

    async def cmd_done(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        today = self.cfg.now().date()
        parts = update.message.text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            # Bare /done: don't make him go and look up a number. Show the
            # goals still open as buttons and let him tap one.
            goals = self._goals_text(today)
            if not goals:
                await update.message.reply_text("no goals for today yet. /goals")
                return
            await update.message.reply_text(
                goals, parse_mode=ParseMode.HTML,
                reply_markup=self._goal_buttons(today))
            return
        if not self.db.mark_goal(today, int(parts[1]), True):
            await update.message.reply_text("don't have a goal with that number. /goals to check")
            return
        self.ledger.append("goal_done", {"date": today.isoformat(), "idx": int(parts[1])})
        rows = self.db.goals(today)
        hit = sum(1 for r in rows if r["done"])
        await update.message.reply_text(
            f"{self._goals_text(today)}\n\n<b>{hit}/{len(rows)}</b>",
            parse_mode=ParseMode.HTML)

    # ======================================================================
    # the once-a-week late night
    # ======================================================================
    async def cmd_latenight(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE,
                            reason: str | None = None) -> None:
        """Two hours past bedtime, once a week, and it has to be earned.

        This breaks a rule the README states in bold -- nothing moves the day
        past the hard end. It is broken deliberately and narrowly: a real
        hackathon deadline and a night out with friends are real, and a system
        that cannot accommodate them gets abandoned rather than obeyed. The
        narrowness is the whole safeguard: once every seven days, with a photo
        that has to show it, and his father is told each time.
        """
        if not self._is_user(update):
            return
        conf = self.cfg.raw.get("sleep", {}) or {}
        every = int(conf.get("latenight_every_days", 7))
        hours = int(conf.get("latenight_hours", 2))
        now = self.cfg.now()
        night = self.engine.bed_night(now) or now.date()

        row = self.db.day(night)
        if row and int(row["bed_offset_minutes"] or 0):
            await update.message.reply_text("you already used it tonight 😅")
            return

        last = self.db.one(
            "SELECT date FROM days WHERE bed_offset_minutes > 0 AND date < ?"
            " ORDER BY date DESC LIMIT 1", (night.isoformat(),))
        if last:
            gap = (night - date.fromisoformat(last["date"])).days
            if gap < every:
                await update.message.reply_text(
                    f"Used it on <b>{last['date']}</b>, {gap} day"
                    f"{'s' if gap != 1 else ''} ago. It's once every {every} days — "
                    f"next one on <b>{date.fromisoformat(last['date']) + timedelta(days=every)}</b>.\n\n"
                    "go to bed man.", parse_mode=ParseMode.HTML)
                return

        if reason is None:
            reason = update.message.text.partition(" ")[2].strip()
        reason = reason.strip()
        if len(reason) < 12:
            await update.message.reply_text(
                "what for? tell me properly — <code>/latenight hackathon "
                "submission due at midnight</code>.\n\nthen send a pic that shows it.",
                parse_mode=ParseMode.HTML)
            return

        self.pending[update.effective_chat.id] = {
            "action": "latenight_photo", "date": night, "reason": reason,
            "hours": hours}
        await update.message.reply_text(
            f"ok, <b>{reason}</b>. send the pic —\n\n"
            "your friends and where you are, or the work on screen. not your ceiling lol",
            parse_mode=ParseMode.HTML)

    async def cmd_outside(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Leave the wifi without the phone screaming about it.

        The phone has no mobile data. Walking to the shop looks exactly like
        killing the wifi to dodge a checkpoint, and the agent -- offline by
        then -- has no way to tell them apart. So it is settled here, before
        he goes, with a photo of where he is.

        It buys quiet, not credit. Whatever came due while he was out is still
        due and still missed; the only thing bought is not being shouted at in
        the street, where he could not have produced the proof anyway.
        """
        if not self._is_user(update):
            return
        conf = self.engine.outside_conf()
        minutes = int(conf.get("minutes", 90))
        cap = int(conf.get("max_per_day", 2))
        now = self.cfg.now()
        today = now.date()

        if not self.engine.in_window(now):
            await update.message.reply_text(
                "nothing due rn, just go 👍")
            return

        until = self.engine.outside_until(today)
        if until is not None and now < until:
            left = int((until - now).total_seconds() // 60)
            await update.message.reply_text(
                f"you're already covered — <b>{left} min</b> left, till "
                f"<b>{until:%H:%M}</b>.", parse_mode=ParseMode.HTML)
            return

        used = self.engine.outside_used(today)
        if used >= cap:
            await update.message.reply_text(
                f"that's {used}/{cap} today already. can't use it all day bro.")
            return

        self.pending[update.effective_chat.id] = {
            "action": "outside_photo", "minutes": minutes, "left": cap - used - 1}
        await update.message.reply_text(
            "send me a pic of where you are — street, yard, podyezd, whatever.\n\n"
            f"then no siren for <b>{minutes} min</b> if you lose signal. stuff that's "
            "due while you're out still counts as missed tho.", parse_mode=ParseMode.HTML)

    def _block_for_steps(self, arg: str, now: datetime):
        """The block he means: the one running now, or the nth of the day."""
        blocks = self.db.blocks(now.date())
        if not blocks:
            return None
        if arg.strip().isdigit():
            i = int(arg.strip())
            return blocks[i - 1] if 1 <= i <= len(blocks) else None
        minute = now.hour * 60 + now.minute
        here = next((b for b in blocks
                     if b["start_min"] <= minute < b["end_min"]), None)
        if here is not None:
            return here
        return next((b for b in blocks if b["start_min"] >= minute), None)

    async def cmd_steps(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Turn one block into steps small enough that the first one is obvious.

        The failure this is for is not laziness, it is a block called "work:
        analysing" at 16:10 with nothing to actually put your hands on. Sitting
        in front of that for twenty minutes reads as defiance on the record and
        is really just not knowing where to start.
        """
        if not self._is_user(update):
            return
        if not self.coach.enabled:
            await update.message.reply_text("The coach is switched off in config.")
            return
        now = self.cfg.now()
        arg = update.message.text.partition(" ")[2]
        block = self._block_for_steps(arg, now)
        if block is None:
            await update.message.reply_text(
                "nothing to break down yet — send /plan first. or /steps 2 for "
                "your second block.")
            return

        cached = bool(self.coach.steps_for(now.date(), block["label"]))
        if not cached and self.coach.budget_left(now.date()) <= 0:
            await update.message.reply_text(
                "I'm out of juice for today, gotta save it for checking your photos. tomorrow 🙏")
            return

        await update.message.chat.send_action("typing")
        try:
            steps = await self.coach.breakdown(block, now)
        except Exception as exc:                              # noqa: BLE001
            log.warning("coach breakdown failed: %s", exc)
            await update.message.reply_text(
                "my brain's lagging rn. just start with the smallest bit you can think of.")
            return
        if not steps:
            await update.message.reply_text("hm, got nothing. try again?")
            return
        blocks = self.db.blocks(now.date())
        bi = next((i for i, b in enumerate(blocks) if b["id"] == block["id"]), 0)
        await update.message.reply_text(
            self._render_steps(now.date(), block["label"]),
            parse_mode=ParseMode.HTML,
            reply_markup=self._step_buttons(now.date(), bi, block["label"]))

    def _render_steps(self, day: date, label: str) -> str:
        rows = self.coach.steps_for(day, label)
        done = sum(1 for r in rows if r["done"])
        head = f"<b>{html.escape(label)}</b> — {done}/{len(rows)} done\n"
        body = "\n".join(
            ("✅ <s>" + html.escape(r["text"]) + "</s>") if r["done"]
            else f"<b>{r['idx']}.</b> {html.escape(r['text'])}"
            for r in rows)
        return head + body

    def _step_buttons(self, day: date, bi: int, label: str) -> InlineKeyboardMarkup | None:
        """Tick-off buttons. The callback carries the block's POSITION in the
        day, not its label -- labels are long and Telegram allows 64 bytes, and
        a position still resolves after the bot restarts."""
        open_ = [r for r in self.coach.steps_for(day, label) if not r["done"]]
        if not open_:
            return None
        keys, line = [], []
        for r in open_:
            line.append(InlineKeyboardButton(f"✓ {r['idx']}",
                                             callback_data=f"step:{bi}:{r['idx']}"))
            if len(line) == 4:
                keys.append(line); line = []
        if line:
            keys.append(line)
        return InlineKeyboardMarkup(keys)

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
            f"Plan for <b>{target:%A %d %B}</b> — "
            f"cover <b>{self.cfg.window_label(target)}</b>"
            + (" (weekend hours)" if self.cfg.is_weekend(target) else "")
            + f".\n\n{PLAN_HELP}", parse_mode=ParseMode.HTML)

    async def _accept_plan(self, update: Update, target: date, text: str,
                           justified: str | None = None) -> None:
        now = self.cfg.now()
        # Goals before timetable, deliberately. Writing the hours first turns
        # the day into a shape to be filled; writing the outcomes first makes
        # the timetable answer to something.
        if not self.db.goals(target):
            # 21 Sep: he tapped lock, was asked for goals, sent them -- and the
            # plan he had locked was gone. It waits here and goes in with them.
            lo, hi = self._goal_conf()
            self.pending[update.effective_chat.id] = {"action": "goals", "date": target,
                                                      "then_plan": text}
            await update.message.reply_text(
                f"goals first bro. what are the <b>{lo}-{hi} things</b> "
                f"{target:%A %d %B} is for?\n\n"
                "one per line — the plan locks in right after.",
                parse_mode=ParseMode.HTML)
            return
        # The TARGET day's window, not today's. A Saturday plan written on Friday
        # has to cover Saturday's hours; validating it against the weekday window
        # would accept a plan that leaves the whole weekend morning unblocked,
        # inside an enforced window, and therefore never checked.
        # The window for THAT day, except that today starts now: a plan
        # written at 13:21 cannot be asked to cover the morning.
        start = self.cfg.plan_start_for(target, now)
        end = self.cfg.enforce_end_for(target)
        try:
            blocks = planner.parse(text)
            planner.validate_coverage(blocks, start, end)
        except planner.PlanError as exc:
            # Not an error -- an instruction. "From 13:50 till 14:20 lunch...
            # Rest plan do yourself" is a plan; it just isn't in the shape the
            # parser wants. Hand it to the one that can read it.
            if await self._offer_built_plan(update, target, text, now, str(exc)):
                return
            await update.message.reply_text(f"❌ {html.escape(str(exc))}",
                                            parse_mode=ParseMode.HTML)
            return

        existing = self.db.day(target)
        if existing and existing["plan_locked"] and not justified:
            old = [(b["start_min"], b["end_min"], b["label"]) for b in self.db.blocks(target)]
            new = [(b.start_min, b.end_min, b.label) for b in blocks]
            since = (now.hour * 60 + now.minute) if target == now.date() else 0
            if await self._ask_why(update, "plan", target, text, old, new, since):
                return
        if existing and existing["plan_locked"] and target <= now.date() \
                and self.engine.in_window(now):
            # 10 Oct 14:41: he asked to change the day, Friday built a new
            # proposal, he tapped lock -- and a locked day in its window is a
            # wall on this path, so it went nowhere. A change to today, mid-
            # window, is what /replan is for; send it there, guard and all.
            await self._accept_replan(update, text, justified=justified)
            return

        self.db.ensure_day(target)
        before = self._open_checks(target)
        self.db.replace_blocks(target, [b.as_tuple() for b in blocks])
        self._relink_checks(target)
        self.db.run(
            "UPDATE days SET plan_locked = 1, plan_submitted_at = ?, checkpoints_built = 0"
            " WHERE date = ?", (now.isoformat(), target.isoformat()))
        self.db.run("DELETE FROM checkpoints WHERE date = ? AND state = 'scheduled'",
                    (target.isoformat(),))
        # A checkpoint whose block is no longer in the plan keeps demanding a
        # photo of work the day does not contain. 10 Oct 14:43: the plan said
        # CAS and the siren was still asking for the IELTS pic, because that
        # check had already been asked and survived the rewrite.
        #
        # Closed as MISSED, never deleted: the miss was real, and replanning
        # must not become a way to wipe one. What goes is the impossible
        # demand, not the record of it. The bed check has no block, so it is
        # left alone.
        stale = self._close_orphaned(target, before, now)
        built = self.engine.build_checkpoints(target, now)
        self.ledger.append("plan_submitted",
                           {"date": target.isoformat(), "blocks": len(blocks),
                            "live": built["live"], "voided": built["voided"]})
        self.pending.pop(update.effective_chat.id, None)

        tail = ""
        if built["first"]:
            tail = (f"first pic at {datetime.fromisoformat(built['first']):%H:%M}, "
                    f"{built['live']} check{'s' if built['live'] != 1 else ''} total.")
        if built["voided"]:
            tail += (f" ({built['voided']} were already in the past so those "
                     "don't count)")
        if stale:
            tail += (f"\n({stale} unanswered check{'s' if stale != 1 else ''} for a block "
                     "that's not in the plan any more counts as missed — a pic "
                     "can't be replanned away)")
        if built["first"]:
            pass
        elif target > now.date():
            tail = f"{built['live']} checks on {target:%A}. see you then 👊"
        else:
            # A plan for today whose blocks have all already started produces
            # nothing checkable, and the old wording ("this plan starts
            # tomorrow") said the opposite of the truth: it is today's plan and
            # tomorrow still needs its own. Accepting it silently let him think
            # the evening was covered when nothing at all would be asked.
            tail += ("\nbut every block already started, so nothing can be checked "
                     "today. /replan with blocks from now if you want today covered.")

        tail += self._plan_task_note(target, blocks)
        when = "today" if target == now.date() else (
            "tomorrow" if target == now.date() + timedelta(days=1) else f"{target:%A %d %b}")
        await update.message.reply_text(
            f"locked in for {when} 🤝\n\n{planner.render(blocks)}\n\n{tail.strip()}",
            parse_mode=ParseMode.HTML)

    async def cmd_replan(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Rewrite the rest of today. The past and the end time stay fixed."""
        if not self._is_user(update):
            return
        now = self.cfg.now()
        today = now.date()
        if not self.engine.has_plan(today):
            await update.message.reply_text("no plan for today yet — /plan")
            return
        if not self.engine.in_window(now):
            await update.message.reply_text(
                "we're not in the window rn. /plan for another day.")
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
                f"ok, rewriting <b>{minute//60:02d}:{minute%60:02d}–"
                f"{end//60:02d}:{end%60:02d}</b>. What's done stays done, and the "
                f"day still ends at {end//60:02d}:{end%60:02d}.\n\n"
                f"right now it's:\n{planner.render(cur)}\n\n"
                f"send me the new version, same format.",
                parse_mode=ParseMode.HTML)
            return
        await self._accept_replan(update, body)

    # Written from how he actually types it -- "i am filling out info still
    # bro", "need 15 more mins", "ещё 10 мин" -- rather than from a list of
    # verbs, which is what missed "stretched" on 11 Oct.
    MORE_WORDS = re.compile(
        r"\bstill\b|\bnot (?:done|finished|ready)\b|\bhaven'?t finished\b|"
        r"\b(?:need|gimme|give me|want)\s+(?:\d+\s*\w*\s*)?(?:more|another)\b|"
        r"\bmore (?:time|mins?|minutes)\b|\b(?:a bit|a little|abit)\s+longer\b|"
        r"\blonger on\b|\blemme finish\b|\blet me finish\b|\balmost done\b|"
        r"\bnearly done\b|\bfinishing (?:it|this|up)\b|"
        r"(?:ещ[её]|еще)\s*\d*\s*мин|не закончил|не доделал|дай ещ[её]", re.I)
    MORE_MINUTES = re.compile(
        r"(\d{1,3})\s*(?:more\s+|another\s+)?(?:m\b|min|mins|minute|minutes|мин)", re.I)

    def _more_request(self, text: str) -> int | None:
        """Minutes, if this says "I'm not finished with what I'm on"."""
        t = (text or "").strip()
        if not t or "?" in t or len(t) > 120 or not self.MORE_WORDS.search(t):
            return None
        if self.MEAL_WORDS.search(t):          # "still eating" is a meal, handled there
            return None
        m = self.MORE_MINUTES.search(t)
        return int(m.group(1)) if m else 20

    async def _quick_more(self, update: Update, minutes: int) -> bool:
        """Stretch the block he is in, paying for it out of what comes next.

        The same arithmetic and the same limits as /more -- including the rule
        that you can only stretch a block you have proved you are doing -- but
        reached by saying it in his own words. Returns False when there is
        nothing to stretch, so the message falls through to Friday.
        """
        now = self.cfg.now()
        today = now.date()
        if not self.engine.in_window(now) or not self.engine.has_plan(today):
            return False
        block = self.engine.current_block(today, now)
        if block is None:
            return False
        cap = int(self.cfg.raw.get("extend", {}).get("max_minutes_per_day", 90))
        used = (self.db.day(today)["extend_minutes"] or 0)
        minutes = max(5, min(int(minutes), 120))
        if used + minutes > cap:
            await update.message.reply_text(
                f"that'd be {used + minutes} min of stretching today and the cap is "
                f"{cap} ({used} used). /replan if the day's shape is wrong.")
            return True
        if not self.engine.block_was_proven(today, block["id"]):
            # Not a refusal he can't act on: the photo he owes IS the one for
            # this block, so say that instead of a rule.
            await update.message.reply_text(
                f"send the pic for <b>{html.escape(block['label'])}</b> first and I'll "
                f"stretch it {minutes} min — can't extend something I haven't seen "
                "you doing.", parse_mode=ParseMode.HTML)
            return True
        res = self.engine.extend_block(today, now, block["id"], minutes)
        if res.get("error"):
            await update.message.reply_text(res["error"])
            return True
        self.db.run("UPDATE days SET extend_minutes = COALESCE(extend_minutes,0) + ?"
                    " WHERE date = ?", (res["minutes"], today.isoformat()))
        self._relink_checks(today)
        self.ledger.append("block_extended", {"date": today.isoformat(),
                                              "label": res["label"],
                                              "minutes": res["minutes"]})
        cost = ", ".join([f"{lbl} -{m}m" for lbl, m in res["shortened"]]
                         + [f"{lbl} gone" for lbl in res["dropped"]]) or "the rest of the day"
        self._app_note(now, f"his block \"{res['label']}\" was stretched {res['minutes']} "
                            f"min to {res['new_end']//60:02d}:{res['new_end']%60:02d}; "
                            f"paid for by {cost}")
        await update.message.reply_text(
            f"⏱ <b>{html.escape(res['label'])}</b> till "
            f"{res['new_end']//60:02d}:{res['new_end']%60:02d}. paid for by {html.escape(cost)}. "
            f"bedtime doesn't move.", parse_mode=ParseMode.HTML)
        return True

    MEAL_WORDS = re.compile(r"\b(eat|eating|food|lunch|dinner|breakfast|snack|"
                            r"поесть|поем|кушать|покушаю|обед|ужин)\b", re.I)
    NOW_WORDS = re.compile(r"\b(now|rn|right now|lemme|let me|imma|i'?m gonna|gonna|"
                           r"going to|сейчас|щас)\b", re.I)
    MINUTES = re.compile(r"(\d{1,3})\s*(?:m\b|min|mins|minute|minutes|мин)", re.I)

    def _meal_request(self, text: str) -> int | None:
        """Minutes, if this is "I'm going to eat now" in any of his phrasings.

        Not a question ("what should i eat?"), not the past ("i ate"), and only
        with a time or a now-word, so "food for thought" stays chat.
        """
        t = (text or "").strip()
        if not t or "?" in t or len(t) > 80 or not self.MEAL_WORDS.search(t):
            return None
        m = self.MINUTES.search(t)
        if m:
            return int(m.group(1))
        return 20 if self.NOW_WORDS.search(t) else None

    async def _quick_meal(self, update: Update, minutes: int) -> bool:
        """Carve a meal out of now, without asking anybody to approve food.

        The current block is paused for the meal and resumes after it; nothing
        later moves. It goes through the ordinary replan door -- the guard sees
        a meal inside its allowance and lets it through without a question.
        Returns False when there is no locked day running to change, so the
        message falls through to Friday as before.
        """
        now = self.cfg.now()
        today, minute = now.date(), now.hour * 60 + now.minute
        if not self.engine.has_plan(today) or not self.engine.in_window(now):
            return False
        _, end = self.engine.window(today)
        cap = int((self.cfg.raw.get("friend", {}) or {}).get("meal_grace_min", 45))
        minutes = max(5, min(int(minutes), cap, end - minute))
        if minutes < 5:
            return False
        stop = minute + minutes
        rows = [(minute, stop, "eat")]
        resume = None
        for b in self.db.blocks(today):
            if b["end_min"] <= minute:
                continue
            s, e = max(b["start_min"], minute), b["end_min"]
            if e <= stop:
                continue                     # swallowed by the meal
            if s < stop:
                s = stop
                resume = resume or b["label"]
            rows.append((s, e, b["label"]))
        text = "\n".join(f"{s//60:02d}:{s%60:02d}-{e//60:02d}:{e%60:02d} {lbl}"
                         for s, e, lbl in rows)
        mark = self._ledger_mark()
        await self._accept_replan(update, text)
        if not self._plan_changed_since(mark):
            self._app_note(now, f"he asked to eat for {minutes} min; the change was NOT "
                                "applied")
            return True
        back = f"{stop//60:02d}:{stop%60:02d}"
        self._app_note(now, f"he is eating {minutes} min, until {back}; no photo is "
                            "needed for the meal" + (f", then back to {resume}" if resume
                                                     else ""))
        await update.message.reply_text(
            f"enjoy 🍽 {minutes} min, no pic needed for food. "
            + (f"back to {resume} at {back}." if resume else f"see you at {back}."))
        return True

    async def cmd_eat(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/eat [minutes] -- the same as saying it."""
        if not self._is_user(update):
            return
        arg = update.message.text.partition(" ")[2].strip()
        mins = int(arg) if arg.isdigit() else 20
        if not await self._quick_meal(update, mins):
            await update.message.reply_text(
                "nothing's running right now, so just go eat 🍽")

    # 11 Oct: "stretched the rec letter block till 1" went uncorrected because
    # "stretched" was not on this list. A list of verbs will always be missing
    # one, so this one is deliberately greedy -- the cost of a false positive
    # is one extra line telling him the truth about his own plan.
    CLAIM_VERB = re.compile(
        r"\b(switch(?:ed)?|swap(?:ped)?|add(?:ed)?|mov(?:ed|ing)|shift(?:ed)?|"
        r"push(?:ed)?|grab(?:bed)?|slot(?:ted)?|carv(?:ed)?|trim(?:med)?|"
        r"squeez(?:ed)?|stretch(?:ed)?|extend(?:ed)?|lengthen(?:ed)?|bump(?:ed)?|"
        r"nudg(?:ed)?|freed|f(?:ou|i)nd you|bought you|gave you|giv(?:en|ing) you|"
        r"resched(?:uled)?|replann?ed|rewrote|rearrang(?:ed)?|"
        r"knock(?:ed)? (?:it )?back|made (?:it |you )?room|made room|"
        r"put (?:it|you|that) (?:in|back|down)|chang(?:ed)?|updat(?:ed)?|"
        r"set (?:it|that) to|cut|dropp?ed|remov(?:ed)?|split|merg(?:ed)?)\b", re.I)
    TIME_WORD = re.compile(
        r"\b(min|mins|minute|minutes|hour|hours|hr|hrs|block|plan|schedule|"
        r"till|until|to)\b|\d{1,2}:\d{2}|\b\d{1,2}\s*(?:am|pm)\b", re.I)

    # These say it on their own -- there is no innocent reading of "rescheduled
    # the rest" or "made room for it" that leaves the plan untouched.
    STRONG_CLAIM = re.compile(
        r"\b(resched(?:uled)?|replann?ed|rewrote|rearrang(?:ed)?|made (?:you )?room|"
        r"freed up|sorted the (?:rest|day)|fixed the (?:plan|day))\b", re.I)

    def _claims_change(self, reply: str) -> bool:
        """Does this sentence say the day was rearranged?"""
        if "📌" in reply:                 # it saved a task or an event; not a schedule
            return False
        if self.STRONG_CLAIM.search(reply):
            return True
        for sentence in re.split(r"[.!\n]", reply):
            if self.CLAIM_VERB.search(sentence) and self.TIME_WORD.search(sentence):
                return True
        return False

    def _ledger_mark(self) -> int:
        row = self.db.one("SELECT MAX(id) m FROM events")
        return int(row["m"] or 0) if row else 0

    def _plan_changed_since(self, mark: int) -> bool:
        """Did a plan actually get written after `mark`? The ledger knows."""
        return self.db.one(
            "SELECT COUNT(*) c FROM events WHERE id > ?"
            " AND kind IN ('plan_submitted', 'replanned')", (mark,))["c"] > 0

    def _app_note(self, now: datetime, text: str) -> None:
        """Put what the app actually did into Friday's memory.

        Friday only ever saw its own replies, never the app's: not the
        "why are you cutting work?" question, not a refusal, not a held lock.
        So on 10 Oct it read its own "[tapped lock]" line and told him "plan
        #4 is already locked with CAS until 15:11" -- a plan the guard had
        held and that never went in. What happened goes on the record now,
        in the app's words, so the next reply starts from the truth.
        """
        try:
            self.coach.remember(now.date(), now, "coach", f"[app: {text}]")
        except Exception as exc:                              # noqa: BLE001
            log.warning("could not note outcome: %s", exc)

    def _relink_checks(self, day: date) -> None:
        """Point checkpoints at the blocks that now cover them.

        replace_blocks deletes and re-inserts every row, so after a rewrite
        every checkpoint holds a block_id that no longer exists. Anything
        keyed on it then silently does nothing: /paper reported "already
        wants a photo" while the open check went on demanding a Cold Turkey
        screenshot, 10 Oct 16:24.
        """
        blocks = self.db.blocks(day)
        for cp in self.db.q("SELECT id, due_at FROM checkpoints WHERE date = ?"
                            " AND kind != 'bed'", (day.isoformat(),)):
            due = datetime.fromisoformat(cp["due_at"])
            minute = due.hour * 60 + due.minute
            blk = next((x for x in blocks
                        if x["start_min"] <= minute < x["end_min"]), None)
            self.db.run("UPDATE checkpoints SET block_id = ? WHERE id = ?",
                        (blk["id"] if blk else None, cp["id"]))

    def _open_checks(self, day: date) -> list[dict]:
        """Open checkpoints with the label they were asked about."""
        return [dict(r) for r in self.db.q(
            "SELECT c.id, c.due_at, b.label FROM checkpoints c"
            " JOIN blocks b ON b.id = c.block_id"
            " WHERE c.date = ? AND c.state IN ('asked','overdue')", (day.isoformat(),))]

    def _close_orphaned(self, day: date, before: list[dict], now: datetime) -> int:
        """Close checks the new plan has made impossible to answer.

        A photo can only ever prove the block it was asked for. Once that block
        is gone -- replaced, truncated, renamed -- the demand cannot be met by
        anything honest, and the siren runs on an obligation with no exit.
        10 Oct 14:43: the plan said CAS and the siren was still asking for the
        IELTS pic.

        Matching is by label over the minute it was due, because replace_blocks
        deletes and re-inserts every row: the old block ids do not survive, so
        the id-based version of this check quietly matched nothing.

        MISSED, never deleted. The miss was real; replanning cannot wipe one.
        """
        blocks = self.db.blocks(day)
        nowmin = now.hour * 60 + now.minute if day == now.date() else 0
        closed = 0
        for row in before:
            due = datetime.fromisoformat(row["due_at"])
            minute = due.hour * 60 + due.minute
            # Answerable means: a block with that label still covers the minute
            # it was asked for, AND that block has not already ended -- you
            # cannot photograph yourself doing something you stopped doing.
            if any(b["start_min"] <= minute < b["end_min"] and b["label"] == row["label"]
                   and b["end_min"] > nowmin for b in blocks):
                continue
            closed += self.db.run(
                "UPDATE checkpoints SET state='missed', settled_at=? WHERE id=?"
                " AND state IN ('asked','overdue')",
                (now.isoformat(), row["id"])).rowcount
        return closed

    async def _accept_replan(self, update: Update, text: str,
                             justified: str | None = None) -> None:
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

        if justified is None:
            old = [(b["start_min"], b["end_min"], b["label"]) for b in self.db.blocks(today)]
            new = [(b.start_min, b.end_min, b.label) for b in blocks]
            if await self._ask_why(update, "replan", today, text, old, new, minute):
                return

        before = self._open_checks(today)
        kept = [b for b in self.db.blocks(today) if b["start_min"] < minute]
        rows = [(b["start_min"], min(b["end_min"], minute), b["label"], b["category"])
                for b in kept]
        rows += [(max(b.start_min, minute), b.end_min, b.label, b.category)
                 for b in blocks if b.end_min > minute]
        self.db.replace_blocks(today, rows)
        self._relink_checks(today)
        self.db.run(
            "DELETE FROM checkpoints WHERE date=? AND state='scheduled' AND due_at > ?",
            (today.isoformat(), now.isoformat()))
        # A replan truncates the block you were in at the moment you replan, so
        # an unanswered checkpoint inside it is left demanding proof of a task
        # the plan no longer says you are doing -- and the block is over, so it
        # cannot be satisfied at all. The siren then runs on an obligation whose
        # only exit is a faked photo.
        #
        # Those are closed as MISSED, not deleted. The miss was real and stays
        # on the record; what goes is the impossible demand. Replanning still
        # cannot erase a miss, which is the property that stops it being a way
        # to wipe debt.
        closed = self._close_orphaned(today, before, now)
        built = self.engine.build_checkpoints(today, now)
        self.ledger.append("replanned", {"date": today.isoformat(),
                                         "from": f"{minute//60:02d}:{minute%60:02d}"})
        self.pending.pop(update.effective_chat.id, None)

        tail = (f"\n({closed} unanswered checkpoint{'s' if closed != 1 else ''} from the "
                "block you just replanned away counts as missed — that block is over.)"
                if closed else "")
        await update.message.reply_text(
            f"done, here's the rest of today:\n\n{planner.render(blocks)}\n\n"
            f"{built['live']} check{'s' if built['live'] != 1 else ''} left, still ending at "
            f"{end//60:02d}:{end%60:02d}.{tail}",
            parse_mode=ParseMode.HTML)
        why = (f"\nReason: <i>{html.escape(justified)}</i>"
               if justified and justified != "-" else "")
        await self.send_guardian(
            f"📝 {self.cfg.user_name} rewrote the rest of today from "
            f"{minute//60:02d}:{minute%60:02d}. The day still ends at the same time.{why}")

    # ======================================================================
    # building the day for him
    # ======================================================================
    async def _offer_built_plan(self, update: Update, day: date, text: str,
                                now: datetime, why: str = "", current: str | None = None,
                                change: str | None = None) -> bool:
        """Build a plan from what he wrote and offer it. True if we handled it.

        With `current` and `change` it edits the plan he was just shown.
        """
        if not self.coach.enabled or self.coach.budget_left(now.date()) <= 0:
            return False
        await update.message.chat.send_action("typing")
        try:
            blocks, note = await self.dayplanner.build(day, text, now,
                                                       current=current, change=change)
        except planner.PlanError as exc:
            log.info("could not build a plan: %s", exc)
            return False
        except Exception as exc:                              # noqa: BLE001
            log.warning("dayplan failed: %s", exc)
            return False
        rendered = "\n".join(f"{b.hhmm()} {b.label}" for b in blocks)
        pid = self.proposals.add(day, rendered, note, now)
        self.pending[update.effective_chat.id] = {
            "action": "plan_proposal", "date": day, "text": rendered, "id": pid,
            "ask": text}
        # On the record, so Friday knows what he was shown when he answers it.
        # No id in the text: it is a database row number, and on 10 Oct Friday
        # read it straight back to him as "plan #4 is already locked", which
        # means nothing to anyone outside this file.
        self.coach.remember(now.date(), now, "coach",
                            f"[proposed a plan for {day:%a %d %b}, not locked yet]\n"
                            f"{rendered}" + (f"\n{note}" if note else ""))
        when = "today" if day == now.date() else (
            "tomorrow" if day == now.date() + timedelta(days=1) else f"{day:%a %d %b}")
        await update.message.reply_text(
            f"here's <b>{when}</b> ({day:%a %d %b}):\n\n{planner.render(blocks)}"
            + (f"\n\n{html.escape(note)}" if note else "")
            + "\n\nlock it in?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ lock it", callback_data=f"plan:ok:{pid}"),
                InlineKeyboardButton("🔁 redo", callback_data=f"plan:redo:{pid}")]]))
        return True

    async def _chat_make_plan(self, update: Update, want: dict, now: datetime) -> None:
        """He asked Friday to build it. One day is offered; several are locked in."""
        pend = self.pending.get(update.effective_chat.id) or {}
        if pend.get("action") == "plan_proposal" and int(want.get("days") or 1) == 1:
            said = str(want.get("day") or "").strip().lower()
            same = (not said or (said.startswith("tod") and pend["date"] == now.date())
                    or (said.startswith("tom")
                        and pend["date"] == now.date() + timedelta(days=1))
                    or said[:10] == pend["date"].isoformat())
            if same:
                change = str(want.get("text") or "").strip() or "make it better"
                if not await self._offer_built_plan(
                        update, pend["date"], pend.get("ask") or "", now,
                        current=pend["text"], change=change):
                    await update.message.reply_text(
                        "couldn't make that change work — the one above still stands, "
                        "or send the blocks yourself.")
                return
        raw_day = str(want.get("day") or "tomorrow").strip().lower()
        if raw_day.startswith("tod"):
            day = now.date()
        elif raw_day.startswith("tom"):
            day = now.date() + timedelta(days=1)
        else:
            try:
                day = date.fromisoformat(raw_day[:10])
            except ValueError:
                day = self.target_plan_date(now)
        days = max(1, min(int(want.get("days") or 1), 3))
        text = str(want.get("text") or "")
        if days == 1:
            if not await self._offer_built_plan(update, day, text, now):
                await update.message.reply_text(
                    "couldn't build that one — send it as blocks and I'll lock it in.")
            return
        for i in range(days):
            d = day + timedelta(days=i)
            if not self.db.goals(d):
                self.pending[update.effective_chat.id] = {"action": "goals", "date": d}
                await update.message.reply_text(
                    f"goals for {d:%a %d %b} first — the 3-5 things it's for?")
                return
            try:
                blocks, _note = await self.dayplanner.build(d, text if i == 0 else "", now)
            except Exception as exc:                          # noqa: BLE001
                log.warning("chat multi-day plan failed on %s: %s", d, exc)
                await update.message.reply_text(f"couldn't build {d:%a %d %b}.")
                return
            await self._accept_plan(update, d,
                                    "\n".join(f"{b.hhmm()} {b.label}" for b in blocks))

    async def _on_plan_button(self, q) -> None:
        """Lock the plan on THIS message, not whichever one came last."""
        chat = q.message.chat.id if getattr(q, "message", None) else self.cfg.user_chat_id
        now = self.cfg.now()
        parts = q.data.split(":")
        pid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        row = self.proposals.get(pid) if pid else None
        if row is None:
            await q.edit_message_text("that plan's gone — send /plan again.")
            return
        if q.data.startswith("plan:redo"):
            self.proposals.use(row["id"], now)
            # Still waiting: his next message is the change, and it edits THIS
            # plan rather than starting from nothing.
            ask = (self.pending.get(chat) or {}).get("ask") or ""
            self.pending[chat] = {"action": "plan_proposal",
                                  "date": date.fromisoformat(row["date"]),
                                  "text": row["text"], "id": None, "ask": ask}
            await q.edit_message_text("ok, what do you want different?")
            return
        if row["used_at"]:
            await q.edit_message_text("that one's already locked in — /today to see it.")
            return
        if not self.proposals.use(row["id"], now):
            return
        day = date.fromisoformat(row["date"])
        if (self.pending.get(chat) or {}).get("id") == row["id"]:
            self.pending.pop(chat, None)
        await q.edit_message_text("on it 👊")
        self.coach.remember(now.date(), now, "user", "[tapped the lock button]")
        mark = self._ledger_mark()
        await self._accept_plan(_Reply(q, chat), day, row["text"])
        waiting = (self.pending.get(chat) or {})
        self._app_note(now, "that plan is now locked" if self._plan_changed_since(mark)
                       else "the plan he tapped is waiting for his goals; it locks the "
                            "moment he sends them" if waiting.get("then_plan")
                       else "the plan he tapped was NOT locked -- it was held for a "
                            "reason or refused; the old plan still stands")

    async def _lock_proposal(self, update: Update, pend: dict) -> None:
        """He said yes in words. Lock exactly what he was shown."""
        now = self.cfg.now()
        pid = pend.get("id")
        self.pending.pop(update.effective_chat.id, None)
        if pid and not self.proposals.use(pid, now):
            await update.message.reply_text("that one's already locked in — /today to see it.")
            return
        await self._accept_plan(update, pend["date"], pend["text"])

    async def cmd_plandays(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/plan2 -- the next two days at once, built around the deadlines."""
        if not self._is_user(update):
            return
        now = self.cfg.now()
        arg = update.message.text.partition(" ")[2].strip()
        days = int(arg) if arg.isdigit() else 2
        days = max(1, min(days, 3))
        first = self.target_plan_date(now)
        made = []
        for i in range(days):
            day = first + timedelta(days=i)
            if not self.db.goals(day):
                await update.message.reply_text(
                    f"goals for {day:%a %d %b} first — what are the 3-5 things it's for?")
                self.pending[update.effective_chat.id] = {"action": "goals", "date": day}
                return
            await update.message.chat.send_action("typing")
            try:
                blocks, note = await self.dayplanner.build(day, "", now)
            except Exception as exc:                          # noqa: BLE001
                log.warning("multi-day plan failed on %s: %s", day, exc)
                await update.message.reply_text(
                    f"couldn't build {day:%a %d %b} — {html.escape(str(exc))}",
                    parse_mode=ParseMode.HTML)
                break
            await self._accept_plan(update, day,
                                    "\n".join(f"{b.hhmm()} {b.label}" for b in blocks))
            made.append(day)
        if len(made) > 1:
            await update.message.reply_text(
                f"that's {len(made)} days sorted. /forecast to see where the deadlines stand.")

    async def cmd_forecast(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Hours left against hours available, per deadline."""
        if not self._is_user(update):
            return
        today = self.cfg.now().date()
        rows = self.agenda.all_status(today)
        if not rows:
            await update.message.reply_text("no big tasks saved yet. /tasks")
            return
        body = "\n".join(self.agenda.line(r) for r in rows[:12])
        risk = [r for r in rows if r["verdict"] in ("OVERDUE", "WILL MISS")]
        tail = ""
        if risk:
            r = risk[0]
            tail = (f"\n\n<b>{html.escape(r['title'])}</b> is the one that doesn't fit: "
                    f"{r['hours_left']:g}h left, about {r['available']:g}h of window "
                    f"before {r['due']}.")
        missing = [r for r in rows if r["verdict"] == "no estimate"]
        if missing:
            tail += ("\n\nno hour estimate on: "
                     + ", ".join(html.escape(r["title"]) for r in missing[:4])
                     + " — say <code>/task hours 3 20</code> or just tell me.")
        await update.message.reply_text(
            "<b>where the deadlines stand</b>\n" + body + tail, parse_mode=ParseMode.HTML)

    # ======================================================================
    # holding the line on a rewritten plan
    #
    # A plan is a promise made while thinking clearly. The rewrite that
    # matters is the one made later, in the moment, that quietly turns
    # "work: physics IA" into "relax" -- nothing is broken, nothing is
    # missed, and the IA just does not happen. So any rewrite that takes work
    # time away has to come with a reason, and the friend decides whether it
    # is one. Swapping one piece of work for another is never questioned.
    # ======================================================================
    def _important(self, label: str) -> bool:
        low = (label or "").lower().lstrip()
        return low.startswith("work:") or bool(self.tasks.match(label))

    def _work_minutes(self, rows, since: int) -> int:
        total = 0
        for start, end, label in rows:
            start = max(start, since)
            if end > start and self._important(label):
                total += end - start
        return total

    @staticmethod
    def _meal_minutes(rows, since: int) -> int:
        total = 0
        for start, end, label in rows:
            start = max(start, since)
            if end > start and planner.categorise(label) == "meal":
                total += end - start
        return total

    def _lost_work(self, old, new, since: int) -> int:
        """Work minutes a rewrite takes away, with a meal's worth forgiven.

        10 Oct 2026, in his words: "the bot isn't letting me eat". Moving work
        aside for dinner reads to the guard exactly like moving it aside for
        nothing, and the judge is told to refuse "I need a break" -- so food
        had to be argued for. A body is not a negotiation. The first
        `friend.meal_grace_min` minutes that a rewrite hands to a meal are not
        counted as lost, so a normal dinner goes through in silence. Beyond
        that it is a two-hour dinner, and that is a real question again.
        """
        lost = self._work_minutes(old, since) - self._work_minutes(new, since)
        if lost <= 0:
            return lost
        grace = int((self.cfg.raw.get("friend", {}) or {}).get("meal_grace_min", 45))
        added = max(0, self._meal_minutes(new, since) - self._meal_minutes(old, since))
        return lost - min(added, grace)

    async def _ask_why(self, update: Update, kind: str, day: date, text: str,
                       old, new, since: int) -> bool:
        """True if the rewrite is on hold until he explains it."""
        conf = self.cfg.raw.get("friend", {}) or {}
        if not conf.get("guard_rewrites", True):
            return False
        lost = self._lost_work(old, new, since)
        if lost < int(conf.get("rewrite_min_lost", 10)):
            return False
        gone = [l for (_, e, l) in old if e > since and self._important(l)
                and l not in {n[2] for n in new}]
        self.pending[update.effective_chat.id] = {
            "action": "rewrite_why", "kind": kind, "date": day, "text": text,
            "lost": lost, "since": since, "tries": 0}
        what = (f" — <b>{html.escape(', '.join(dict.fromkeys(gone)))}</b> is gone"
                if gone else "")
        await update.message.reply_text(
            f"wait. that cuts <b>{lost} min</b> of real work{what}.\n\n"
            "why? tell me what happened, or send a pic that shows it. "
            "\"don't feel like it\" won't do it 😅\n\n"
            "/cancel to keep the plan as it is.",
            parse_mode=ParseMode.HTML)
        return True

    async def _rewrite_reason(self, update: Update, text: str,
                              image: bytes | None = None, mime: str = "image/jpeg") -> None:
        chat = update.effective_chat.id
        pend = self.pending.get(chat) or {}
        now = self.cfg.now()
        day, since = pend["date"], pend["since"]
        cur = [planner.Block(b["start_min"], b["end_min"], b["label"], b["category"])
               for b in self.db.blocks(day) if b["end_min"] > since]
        try:
            want = planner.parse(pend["text"])
        except planner.PlanError:
            self.pending.pop(chat, None)
            await update.message.reply_text("that plan doesn't parse anymore, send it again.")
            return

        await update.message.chat.send_action("typing")
        try:
            if self.coach.budget_left(now.date()) <= 0:
                raise RuntimeError("coach allowance spent")
            ok, say = await self.coach.judge_replan(
                text, image, mime, pend["lost"], planner.render(cur),
                planner.render(want), now)
        except Exception as exc:                              # noqa: BLE001
            # Nobody to ask. A written reason of real length is taken on trust
            # and goes on the record for his father; a bare photo can't be
            # judged by anything, so it isn't enough on its own.
            log.warning("rewrite judge unavailable: %s", exc)
            ok = len((text or "").strip()) >= 40
            say = ("can't properly check that rn, so I'm taking your word for it. "
                   "it's in the log tho." if ok else
                   "can't check that rn — write me the actual reason, a full sentence.")

        if not ok and not image and not getattr(self.coach, "last_judge_was_reason", True):
            # "pic of what?" is a question, not an excuse. Answer it; don't
            # spend one of his three tries on it.
            self.coach.last_judge_was_reason = True
            await update.message.reply_text(
                say or "I need the reason you're changing the plan — what happened?")
            return

        reason = (text or "").strip() or "(photo)"
        self.ledger.append("rewrite_reason", {
            "date": day.isoformat(), "kind": pend["kind"], "lost_min": pend["lost"],
            "reason": reason[:500], "photo": bool(image), "accepted": ok})
        if ok:
            self.pending.pop(chat, None)
            await update.message.reply_text(say or "ok, fair enough.")
            if pend["kind"] == "replan":
                await self._accept_replan(update, pend["text"], justified=reason)
                await self._recheck_after_swap(update, now)
            else:
                await self._accept_plan(update, day, pend["text"], justified=reason)
            return

        pend["tries"] = pend.get("tries", 0) + 1
        if pend["tries"] >= int((self.cfg.raw.get("friend") or {}).get("rewrite_tries", 3)):
            self.pending.pop(chat, None)
            say = (say or "nah.") + "\n\nplan stays as it is. /replan again if something real comes up."
        await update.message.reply_text(say or "nah, that's not a reason bro.")

    # ======================================================================
    # the big tasks
    # ======================================================================
    def _tasks_text(self, day: date) -> str:
        body = self.tasks.describe(day)
        if not body:
            return "no big tasks saved. <code>/task add Physics IA</code>"
        return ("<b>your big stuff</b>\n" + html.escape(body)
                + "\n\n<code>/task done 3</code> · <code>/task due 3 15 oct</code>"
                  " · <code>/task add ...</code>")

    def _task_buttons(self) -> InlineKeyboardMarkup | None:
        rows = self.tasks.open()
        if not rows:
            return None
        keys, line = [], []
        for r in rows:
            line.append(InlineKeyboardButton(f"✓ #{r['id']}", callback_data=f"task:{r['id']}"))
            if len(line) == 5:
                keys.append(line); line = []
        if line:
            keys.append(line)
        return InlineKeyboardMarkup(keys)

    async def _load_doc(self, now: datetime) -> None:
        """Put today's list from his doc in front of Friday before it answers."""
        if not self.doc.enabled:
            return
        try:
            sec = await self.doc.for_day(now.date(), now)
            self.coach.doc_today = self.doc.describe(sec)
        except Exception as exc:                              # noqa: BLE001
            log.warning("doc load failed: %s", exc)

    async def cmd_doc(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """What the bot reads in his doc for today — so a mismatch is visible.

        He writes the day in Google Docs and ticks boxes there. If the bot and
        the doc ever disagree about what today is for, that is a parsing bug or
        a heading he wrote differently, and he should be able to see which in
        one command rather than guessing.
        """
        if not self._is_user(update):
            return
        if not self.doc.enabled:
            await update.message.reply_text(
                "no doc hooked up. put its link in <code>daily_doc.url</code>.",
                parse_mode=ParseMode.HTML)
            return
        now = self.cfg.now()
        arg = update.message.text.partition(" ")[2].strip().lower()
        day = now.date() + timedelta(days=1) if arg.startswith("tom") else now.date()
        await update.message.chat.send_action("typing")
        sec = await self.doc.for_day(day, now, force=True)
        if sec is None:
            days = [f"{s['date']:%a %d %b}" for s in await self.doc.sections(now)]
            await update.message.reply_text(
                f"nothing in the doc for {day:%a %d %b}.\n\n"
                + ("days I can see: " + ", ".join(days[-8:]) if days else
                   "I can't read any day headings in it — is the link shared "
                   "with anyone who has it?"))
            return
        left = len(self.doc.open_items(sec))
        await update.message.reply_text(
            f"<b>{day:%a %d %b}</b> — {left} left of {len(sec['items'])}\n\n"
            f"{html.escape(self.doc.describe(sec))}",
            parse_mode=ParseMode.HTML)

    async def cmd_tasks(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        await update.message.reply_text(
            self._tasks_text(self.cfg.now().date()), parse_mode=ParseMode.HTML,
            reply_markup=self._task_buttons())

    async def cmd_task(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/task add TITLE [due DATE] · /task done N · /task due N DATE · /task drop N"""
        if not self._is_user(update):
            return
        now = self.cfg.now()
        verb, _, rest = update.message.text.partition(" ")[2].strip().partition(" ")
        verb, rest = verb.lower(), rest.strip()

        def parse_due(chunk):
            got, _left = reminders._parse_day(chunk, now)
            return got

        try:
            if verb == "add" and rest:
                title, due = rest, None
                low = rest.lower()
                if " due " in low:
                    i = low.rindex(" due ")
                    title, due = rest[:i].strip(), parse_due(rest[i + 5:])
                if self.tasks.find(title):
                    await update.message.reply_text("you already have that one.")
                    return
                tid = self.tasks.add(title, now, due=due)
                await update.message.reply_text(
                    f"📌 saved #{tid}: {html.escape(title)}"
                    + (f", due {due:%a %d %b}" if due else "")
                    + ("" if due else "\n\nwhen's it due? <code>/task due "
                       f"{tid} 15 oct</code>"), parse_mode=ParseMode.HTML)
                return
            if verb in ("hours", "hrs", "h") and rest:
                num, _, hrs = rest.partition(" ")
                t = self.tasks.find(num.lstrip("#"))
                try:
                    hours = float(hrs.strip().rstrip("h"))
                except ValueError:
                    hours = 0
                if t is None or hours <= 0:
                    await update.message.reply_text(
                        "like this: <code>/task hours 3 20</code> (20 hours of work left)",
                        parse_mode=ParseMode.HTML)
                    return
                self.tasks.set_hours(t["id"], hours)
                st = self.agenda.status(self.tasks.get(t["id"]), now.date())
                await update.message.reply_text(
                    f"📌 {html.escape(t['title'])}: {hours:g}h"
                    + (f", so {st['per_day']:g}h/day to make {st['due']}"
                       if st["per_day"] else ""), parse_mode=ParseMode.HTML)
                return
            if verb in ("pri", "priority") and rest:
                num, _, lvl = rest.partition(" ")
                t = self.tasks.find(num.lstrip("#"))
                if t is None or not lvl.strip().isdigit():
                    await update.message.reply_text(
                        "like this: <code>/task pri 3 1</code> (1 = drop everything)",
                        parse_mode=ParseMode.HTML)
                    return
                self.tasks.set_priority(t["id"], int(lvl))
                await update.message.reply_text(
                    f"📌 {html.escape(t['title'])} is priority {max(1, min(3, int(lvl)))}",
                    parse_mode=ParseMode.HTML)
                return
            if verb in ("done", "drop") and rest:
                t = self.tasks.find(rest.lstrip("#"))
                if t is None:
                    await update.message.reply_text("which one? /tasks shows the numbers")
                    return
                self.tasks.close(t["id"], now, "done" if verb == "done" else "dropped")
                self.ledger.append("task_" + verb, {"id": t["id"], "title": t["title"]})
                await self._after_task_closed(update, t, now)
                await update.message.reply_text(
                    f"✅ {html.escape(t['title'])} — done. that's a big one 🔥"
                    if verb == "done" else f"🗑 dropped {html.escape(t['title'])}.",
                    parse_mode=ParseMode.HTML)
                return
            if verb == "due" and rest:
                num, _, when = rest.partition(" ")
                t = self.tasks.find(num.lstrip("#"))
                due = parse_due(when) if when.strip() else None
                if t is None or due is None:
                    await update.message.reply_text(
                        "like this: <code>/task due 3 15 oct</code>", parse_mode=ParseMode.HTML)
                    return
                self.tasks.set_due(t["id"], due)
                await update.message.reply_text(
                    f"📌 {html.escape(t['title'])} due {due:%a %d %b}. "
                    f"that's {(due - now.date()).days} days.", parse_mode=ParseMode.HTML)
                return
        except reminders.ParseError as exc:
            await update.message.reply_text(f"❌ {exc}", parse_mode=ParseMode.HTML)
            return
        await update.message.reply_text(
            "<code>/task add Physics IA due 15 oct</code>\n"
            "<code>/task done 3</code> · <code>/task due 3 15 oct</code> · "
            "<code>/task drop 3</code>\n\nor just tell me in normal words.",
            parse_mode=ParseMode.HTML)

    async def _on_task_button(self, q) -> None:
        now = self.cfg.now()
        tid = int(q.data.split(":", 1)[1])
        t = self.tasks.get(tid)
        if t is not None and self.tasks.close(tid, now):
            self.ledger.append("task_done", {"id": tid, "title": t["title"]})
        await q.edit_message_text(self._tasks_text(now.date()), parse_mode=ParseMode.HTML,
                                  reply_markup=self._task_buttons())

    async def _after_task_closed(self, update, task, now: datetime) -> None:
        """A finished task leaves holes in the plans he already wrote.

        Those hours are the most valuable thing he owns: they are already
        committed, already defended by the guard. Handing them straight to
        whatever is closest to missing is the whole point of keeping deadlines
        as numbers.
        """
        spare = []
        for d in (now.date(), now.date() + timedelta(days=1), now.date() + timedelta(days=2)):
            mins = sum(b["end_min"] - b["start_min"] for b in self.db.blocks(d)
                       if self.tasks.owns(task, b["label"]))
            if mins >= 30:
                spare.append((d, mins))
        if not spare:
            return
        risk = self.agenda.at_risk(now.date())
        nxt = next((r for r in risk if r["id"] != task["id"]), None)
        hours = sum(m for _, m in spare) / 60.0
        where = ", ".join(f"{d:%a}" for d, _ in spare)
        msg = (f"that frees {hours:.1f}h already in the plan ({where}).")
        if nxt:
            msg += (f" want me to put it on <b>{html.escape(nxt['title'])}</b>? "
                    f"({nxt['verdict']}"
                    + (f", {nxt['hours_left']:g}h left" if nxt["hours_left"] else "") + ")")
        else:
            msg += " want me to rebuild those days?"
        try:
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        except Exception:                                     # noqa: BLE001
            await self.send_user(msg)

    def _plan_task_note(self, target: date, blocks) -> str:
        """After a plan: the big task that should have been in it and isn't."""
        conf = self.cfg.raw.get("friend", {}) or {}
        covered = set()
        for b in blocks:
            covered |= self.tasks.match(b.label)
        urgent = [t for t in self.tasks.due_within(target, int(conf.get("due_soon_days", 7)))
                  if t["id"] not in covered]
        stale = [t for t in self.tasks.stale(target, int(conf.get("stale_days", 4)))
                 if t["id"] not in covered and t not in urgent]
        if urgent:
            t = urgent[0]
            left = (date.fromisoformat(t["due"]) - target).days
            return (f"\n\n👀 btw nothing for <b>{html.escape(t['title'])}</b> in there and "
                    f"it's due in {left} day{'s' if left != 1 else ''}.")
        if stale:
            t = stale[0]
            return (f"\n\n👀 <b>{html.escape(t['title'])}</b> hasn't had any real work "
                    "in a while. just saying.")
        return ""

    # ======================================================================
    # speaking first
    # ======================================================================
    # ======================================================================
    # the 20-minute check
    # ======================================================================
    async def _standup_tick(self, now: datetime) -> None:
        su = self.standups
        if not su.enabled or not self.engine.in_window(now):
            return
        day, minute = now.date(), now.hour * 60 + now.minute
        # Behind already means the siren is going and he owes a photo. Piling
        # three questions on top of that is noise, not accountability.
        if self.engine.debt_seconds(day, now) > 0:
            return
        blk = next((b for b in self.db.blocks(day)
                    if b["start_min"] <= minute < b["end_min"]), None)
        if blk is None or (blk["category"] or "").lower() in su.skip_categories():
            return
        # 21 Sep: the photo ask and the three questions landed in the same
        # second, every 20 minutes. When a photo is due now, the questions
        # ride along in that message instead (see _standup_rider).
        if self._photo_due_near(day, blk["id"], now):
            return
        every = su.every()
        since = minute - blk["start_min"]
        if since < every or since % every:
            return
        if su.count_asked(day) >= int(su.conf.get("max_per_day", 24)):
            return
        if not self.db.claim(f"standup:{day}:{minute}"):
            return
        n = su.ask(now, blk["label"])
        await self.send_user(su.question(n))

    def _photo_due_near(self, day: date, block_id: int, now: datetime,
                        minutes: int = 3) -> bool:
        lo = (now - timedelta(minutes=minutes)).isoformat()
        hi = (now + timedelta(minutes=minutes)).isoformat()
        return self.db.one(
            "SELECT 1 FROM checkpoints WHERE date=? AND block_id=? AND due_at BETWEEN ? AND ?"
            " AND state IN ('scheduled','asked','overdue') LIMIT 1",
            (day.isoformat(), block_id, lo, hi)) is not None

    def _standup_rider(self, due: datetime, now: datetime) -> str:
        """The 20-minute questions, to go inside the photo ask. '' if not due."""
        su = self.standups
        if not su.enabled:
            return ""
        day, minute = due.date(), due.hour * 60 + due.minute
        blk = next((b for b in self.db.blocks(day)
                    if b["start_min"] <= minute < b["end_min"]), None)
        if blk is None or (blk["category"] or "").lower() in su.skip_categories():
            return ""
        since, every = minute - blk["start_min"], su.every()
        if since < every or since % every:
            return ""
        if self.engine.debt_seconds(day, now) > 0:
            return ""
        if su.count_asked(day) >= int(su.conf.get("max_per_day", 24)):
            return ""
        if not self.db.claim(f"standup:{day}:{minute}"):
            return ""
        su.ask(now, blk["label"])
        return ("and text me: what exactly are you on rn (which problem/page)? "
                "moving fast or drifting? stuck on anything?")

    def _ignored_streak(self, day: date, before: str) -> bool:
        """The last two checks before this one both went unanswered.

        21 Sep: seventeen asks and seventeen "where's the pic" in a row, each one
        worded fresh by the model, spent a third of the day's allowance on
        messages he was not reading. After two in a row the written line is
        just as good, and the allowance stays for when he talks back.
        """
        rows = self.db.q(
            "SELECT state FROM checkpoints WHERE date=? AND due_at < ?"
            " AND state IN ('satisfied','missed','excused') ORDER BY due_at DESC LIMIT 2",
            (day.isoformat(), before))
        return len(rows) == 2 and all(r["state"] == "missed" for r in rows)

    async def cmd_paper(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE,
                        which: str | None = None) -> None:
        """This block is off the computer, so prove it with a photo instead.

        A blocker screenshot is the right proof for two hours in front of a
        laptop and the wrong proof for SAT questions done on the floor. The
        block keeps its name and its hours; only what counts as proof changes,
        and it goes on the record.
        """
        if not self._is_user(update):
            return
        now = self.cfg.now()
        today, minute = now.date(), now.hour * 60 + now.minute
        arg = (which if which is not None
               else update.message.text.partition(" ")[2]).strip()
        blocks = self.db.blocks(today)
        # Never a block that is already over. 10 Oct 16:26: "paper" became
        # "CAS", which matched the CAS block that had ended at 16:14, found
        # nothing to change there, and answered "already wants a photo" --
        # while the open check, on the IELTS block, kept asking for Cold
        # Turkey. With no name given, the block that matters is the one the
        # siren is asking about.
        live = [b for b in blocks if b["end_min"] > minute]
        owed = self.engine.current_checkpoint(today, now)
        owed_blk = next((b for b in blocks if owed is not None and owed["kind"]
                         and owed["kind"].startswith("blocker")
                         and b["start_min"] <= int(owed["due_at"][11:13]) * 60
                         + int(owed["due_at"][14:16]) < b["end_min"]), None)
        named = next((b for b in live if arg and arg.lower() in b["label"].lower()), None)
        blk = (named or owed_blk
               or next((b for b in blocks if b["start_min"] <= minute < b["end_min"]), None))
        if blk is None:
            await update.message.reply_text(
                "which block? you're not in one right now — <code>/paper math</code>",
                parse_mode=ParseMode.HTML)
            return
        # By the block's minutes, not its id: a block id is rewritten by every
        # replan, and a /paper that matches nothing is worse than useless --
        # it says "that already wants a photo" while the siren goes on asking
        # for a screenshot.
        lo = datetime.combine(today, datetime.min.time(), tzinfo=now.tzinfo).replace(
            hour=blk["start_min"] // 60, minute=blk["start_min"] % 60)
        hi = lo + timedelta(minutes=blk["end_min"] - blk["start_min"])
        n = self.db.run(
            "UPDATE checkpoints SET kind='activity' WHERE date=?"
            " AND kind LIKE 'blocker%' AND state IN ('scheduled','asked','overdue')"
            " AND due_at >= ? AND due_at < ?",
            (today.isoformat(), lo.isoformat(), hi.isoformat())).rowcount
        if not n:
            await update.message.reply_text(
                f"<b>{html.escape(blk['label'])}</b> already wants a photo, not a "
                "screenshot. just send it.", parse_mode=ParseMode.HTML)
            return
        self.ledger.append("block_on_paper",
                           {"date": today.isoformat(), "label": blk["label"], "checks": n})
        await update.message.reply_text(
            f"got it — <b>{html.escape(blk['label'])}</b> is on paper. "
            f"{n} check{'s' if n != 1 else ''} left on it want a photo of you "
            "working now, clock in shot.", parse_mode=ParseMode.HTML)

    async def cmd_standup(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        now = self.cfg.now()
        blk = next((b for b in self.db.blocks(now.date())
                    if b["start_min"] <= now.hour * 60 + now.minute < b["end_min"]), None)
        n = self.standups.ask(now, blk["label"] if blk else "")
        await update.message.reply_text(self.standups.question(n))

    # ======================================================================
    # the nightly basics
    # ======================================================================
    def _foundations_text(self, day: date, heading: str) -> str:
        rows = self.foundations.for_day(day)
        left = [r for r in rows if not r["done_at"]]
        lines = [("✅ " if r["done_at"] else "▫️ ") + html.escape(r["text"]) for r in rows]
        tail = ("all done 🙌 good night stuff sorted" if not left
                else "tap when done, or just tell me")
        return f"{heading}\n\n" + "\n".join(lines) + f"\n\n{tail}"

    def _foundations_buttons(self, day: date) -> InlineKeyboardMarkup | None:
        left = self.foundations.left(day)
        if not left:
            return None
        return InlineKeyboardMarkup([[InlineKeyboardButton(
            f"✓ {r['text']}", callback_data=f"basic:{day.isoformat()}:{r['idx']}")]
            for r in left])

    async def _send_foundations(self, day: date, heading: str) -> None:
        try:
            await self.app.bot.send_message(
                self.cfg.user_chat_id, self._foundations_text(day, heading),
                parse_mode=ParseMode.HTML, reply_markup=self._foundations_buttons(day))
        except Forbidden:
            await self._on_blocked()

    async def _steps_tick(self, now: datetime) -> None:
        """Ask for the next small step, on the ten-minute marks of a block."""
        st = self.steps
        if not st.enabled or not self.engine.in_window(now):
            return
        day, minute = now.date(), now.hour * 60 + now.minute
        if self.engine.quiet_now(now) is not None:
            return
        blk = next((b for b in self.db.blocks(day)
                    if b["start_min"] <= minute < b["end_min"]), None)
        if blk is None or (blk["category"] or "").lower() in st.skip_categories():
            return
        every = st.every()
        since = minute - blk["start_min"]
        if since < every or since % every:
            return
        # A photo is due this minute: the question rides along with it rather
        # than arriving as a second notification.
        if self._photo_due_near(day, blk["id"], now):
            return
        if not self.db.claim(f"microstep:{day}:{minute}"):
            return
        open_step = st.open_step(now)
        n = st.ask(now, blk["label"])
        if open_step is not None:
            await self.send_user(
                f"⏱ still on: <i>{html.escape(open_step['step'])}</i>\n"
                "done? say so and tell me the next one. or just send the next step.")
        else:
            await self.send_user(f"⏱ {st.question(n)}")

    async def _handle_step_text(self, update: Update, text: str, now: datetime) -> bool:
        """His side of the loop: a step, or "done". True if it was one.

        Deliberately free: no model, no budget, instant reply. The whole point
        is that answering is cheaper than not answering.
        """
        st = self.steps
        if not st.enabled or len(text) > 300:
            return False
        day = now.date()
        blk = next((b for b in self.db.blocks(day)
                    if b["start_min"] <= now.hour * 60 + now.minute < b["end_min"]), None)
        if DONE_WORDS.match(text):
            row = st.mark_done(now)
            if row is None:
                return False
            done, named = st.score(day)
            rid = st.ask(now, blk["label"] if blk else "")
            await self.send_user(f"✅ {done} done today. {st.question(rid)}")
            return True
        waiting = st.waiting(now)
        if waiting is None:
            return False
        if text.startswith("/") or "?" in text:
            return False
        st.set_step(waiting["id"], text, now)
        await self.send_user("👊 go. tell me when it's done.")
        return True

    async def cmd_step(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/step <what> -- name the next one without waiting to be asked."""
        if not self._is_user(update):
            return
        now = self.cfg.now()
        text = update.message.text.partition(" ")[2].strip()
        if not self.steps.enabled:
            await update.message.reply_text(
                "the step loop is off. turn it on with <code>micro.enabled</code>.",
                parse_mode=ParseMode.HTML)
            return
        if not text:
            cur = self.steps.open_step(now)
            done, named = self.steps.score(now.date())
            body = self.steps.describe(now.date()) or "  nothing yet today"
            await update.message.reply_text(
                f"<b>steps today</b> — {done} done of {named}\n\n{html.escape(body)}"
                + (f"\n\non now: <i>{html.escape(cur['step'])}</i>" if cur else ""),
                parse_mode=ParseMode.HTML)
            return
        blk = next((b for b in self.db.blocks(now.date())
                    if b["start_min"] <= now.hour * 60 + now.minute < b["end_min"]), None)
        self.steps.start_free(text, now, blk["label"] if blk else "")
        await update.message.reply_text("👊 go. tell me when it's done.")

    async def _exam_day_tick(self, now: datetime) -> None:
        """Build and lock a plan for a day whose hours are already decided.

        A quiet window means an exam: the morning is spoken for, he will not be
        at a keyboard, and a missing plan would siren at him while he is sitting
        it. So the day plans itself an hour before the window opens -- the exam
        hours as their own block, the rest filled from his deadlines -- and
        locks without a button. He can /replan it like any other day.

        Only ever for a day with a quiet window, and only if he has not written
        one himself.
        """
        day = now.date()
        quiet = self.engine.quiet_spans(day)
        if not quiet or self.engine.has_plan(day):
            return
        start = self.cfg.enforce_start_for(day)
        minute = now.hour * 60 + now.minute
        if not (start - 60 <= minute < start + 120):
            return
        if not self.db.claim(f"exam_plan:{day}"):
            return
        label = next((r["label"] for r in self.coach.routines.for_day(day) if r["quiet"]),
                     "exam")
        if not self.db.goals(day):
            self.db.ensure_day(day)
            self.db.set_goals(day, [label, "rest after it", "one hour of real work"])
        said = "\n".join(
            f"{s//60:02d}:{s%60:02d}-{e//60:02d}:{e%60:02d} {label}" for s, e in quiet)
        try:
            blocks, note = await self.dayplanner.build(day, said, now)
        except Exception as exc:                              # noqa: BLE001
            log.warning("could not build the exam day: %s", exc)
            # Even with no model: the exam, then an honest unplanned rest of it.
            end = self.cfg.enforce_end_for(day)
            qs, qe = min(s for s, _ in quiet), max(e for _, e in quiet)
            rows = [(max(start, qs), qe, label)]
            if qe < end:
                rows.append((qe, end, "rest and whatever you have left"))
            if qs > start:
                rows.insert(0, (start, qs, "get to the exam"))
            blocks = planner.parse("\n".join(
                f"{s//60:02d}:{s%60:02d}-{e//60:02d}:{e%60:02d} {lbl}" for s, e, lbl in rows))
            note = ""
        text = "\n".join(f"{b.hhmm()} {b.label}" for b in blocks)
        await self._accept_plan(_Reply(None, self.cfg.user_chat_id,
                                       send=self.send_user), day, text)
        self.ledger.append("exam_day_planned", {"date": day.isoformat(), "label": label})
        await self.send_user(
            f"🗓 {day:%a %d %b} is your <b>{html.escape(label)}</b> day, so I've written it "
            f"for you — silent {quiet[0][0]//60:02d}:{quiet[0][0]%60:02d}-"
            f"{quiet[0][1]//60:02d}:{quiet[0][1]%60:02d}, nothing asked of you in there. "
            "/today to see it, /replan if you want it different. good luck 🤞")

    async def _foundations_tick(self, now: datetime) -> None:
        f = self.foundations
        if not f.enabled:
            return
        day = now.date()
        minute = now.hour * 60 + now.minute
        at = _hhmm(f.conf.get("at", "21:50"))
        nudge = _hhmm(f.conf.get("nudge_at", "22:15"))
        at_min, nudge_min = at.hour * 60 + at.minute, nudge.hour * 60 + nudge.minute
        if at_min <= minute < at_min + 60 and self.db.claim(f"basics:{day}"):
            await self._send_foundations(day, "end of day basics 🧦🦷")
        elif (nudge_min <= minute < nudge_min + 60 and f.left(day)
              and self.db.claim(f"basics-nudge:{day}")):
            names = ", ".join(r["text"] for r in f.left(day))
            await self._send_foundations(day, f"before bed — still left: {html.escape(names)}")

    async def _weekly_review(self, now: datetime) -> str:
        """What the week actually bought, per task, against what is still owed."""
        today = now.date()
        week = today - timedelta(days=6)
        rows = self.agenda.all_status(today)
        lines = []
        for r in rows[:8]:
            did = self.agenda.hours_done(r["id"], since=week)
            lines.append(f"{r['title']}: {did:g}h this week"
                         + (f", {r['hours_left']:g}h left" if r["hours_left"] else "")
                         + (f", due {r['due']}" if r["due"] else ""))
        sc = self.db.one(
            "SELECT SUM(state='satisfied') ok, COUNT(*) n FROM checkpoints WHERE date >= ?",
            (week.isoformat(),))
        facts = ("this week, in proven hours:\n" + "\n".join(lines)
                 + f"\nphotos: {int(sc['ok'] or 0)} of {int(sc['n'] or 0)} checks")
        risk = self.agenda.at_risk(today)
        if risk:
            facts += ("\nat risk: " + "; ".join(
                f"{r['title']} ({r['verdict']}"
                + (f", needs {r['per_day']:g}h/day" if r["per_day"] else "") + ")"
                for r in risk[:3]))
        fallback = "📊 <b>the week</b>\n" + html.escape(facts)
        return await self._human(
            "the Sunday night review: what his week actually bought, in hours, and "
            "which deadline needs the most attention next week",
            facts, fallback, now)

    async def cmd_night(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        day = self.foundations.night_of(self.cfg.now())
        await update.message.reply_text(
            self._foundations_text(day, "tonight's basics"), parse_mode=ParseMode.HTML,
            reply_markup=self._foundations_buttons(day))

    async def _on_basic_button(self, q) -> None:
        _, d, idx = q.data.split(":", 2)
        day = date.fromisoformat(d)
        now = self.cfg.now()
        if self.foundations.tick(day, int(idx), now):
            self.ledger.append("basic_done", {"date": d, "idx": int(idx)})
        await q.edit_message_text(
            self._foundations_text(day, "end of day basics 🧦🦷"), parse_mode=ParseMode.HTML,
            reply_markup=self._foundations_buttons(day))

    async def _human(self, job: str, facts: str, fallback: str,
                     now: datetime | None = None) -> str:
        """Let Friday word it. The written line is what goes out if it can't.

        Chat is protected: these never eat the last of the day's allowance,
        because being answered when he writes matters more than the phrasing
        of a nag.
        """
        now = now or self.cfg.now()
        reserve = int(self.coach.conf.get("say_reserve", 30))
        if not self.coach.enabled or self.coach.budget_left(now.date()) <= reserve:
            return fallback
        try:
            said = await self.coach.say(job, facts, now)
        except Exception as exc:                              # noqa: BLE001
            log.warning("could not word it: %s", exc)
            return fallback
        return said.strip() or fallback

    async def _friend_tick(self, now: datetime) -> None:
        conf = self.cfg.raw.get("friend", {}) or {}
        if not conf.get("enabled", True) or not self.lock.enabled:
            return
        day = now.date()
        minute = now.hour * 60 + now.minute

        try:
            await self._foundations_tick(now)
        except Exception as exc:                              # noqa: BLE001
            log.warning("foundations tick failed: %s", exc)

        try:
            await self._standup_tick(now)
        except Exception as exc:                              # noqa: BLE001
            log.warning("standup tick failed: %s", exc)

        try:
            await self._steps_tick(now)
        except Exception as exc:                              # noqa: BLE001
            log.warning("step loop failed: %s", exc)

        try:
            await self._exam_day_tick(now)
        except Exception as exc:                              # noqa: BLE001
            log.warning("exam-day plan failed: %s", exc)

        # before the window: what's on his plate
        at = self.cfg.enforce_start_for(day) - int(conf.get("brief_minutes_before", 20))
        if at <= minute < at + 30 and self.db.claim(f"brief:{day}"):
            text = ""
            if self.coach.enabled and self.coach.budget_left(day) > 0:
                try:
                    text = await self.coach.brief(now)
                except Exception as exc:                      # noqa: BLE001
                    log.warning("brief failed: %s", exc)
            if not text:
                bits = [f"🗓 window opens at {self.cfg.window_label(day).split('-')[0]}."]
                soon = self.tasks.due_within(day, int(conf.get("due_soon_days", 7)))
                if soon:
                    bits.append(f"{soon[0]['title']} is due {soon[0]['due']}.")
                text = " ".join(bits)
            await self.send_user(text)
            return

        # Sunday evening: the week in hours, against what the deadlines need
        review_at = _hhmm(conf.get("weekly_review_at", "21:00"))
        if (day.weekday() == 6 and review_at.hour * 60 + review_at.minute <= minute
                < review_at.hour * 60 + review_at.minute + 40
                and self.db.claim(f"weekly:{day}")):
            await self.send_user(await self._weekly_review(now))
            return

        # halfway through a long block: how's it going
        if not self.engine.in_window(now) or self.engine.debt_seconds(day, now) > 0:
            return
        blk = next((b for b in self.db.blocks(day)
                    if b["start_min"] <= minute < b["end_min"]), None)
        if blk is None:
            return
        length = blk["end_min"] - blk["start_min"]
        if length < int(conf.get("checkin_min_block", 40)):
            return
        if minute < blk["start_min"] + length // 2:
            return
        done_today = self.db.one("SELECT COUNT(*) c FROM flags WHERE key LIKE ?",
                                 (f"checkin:{day}:%",))["c"]
        if done_today >= int(conf.get("checkins_per_day", 4)):
            return
        if not self.db.claim(f"checkin:{day}:{blk['start_min']}"):
            return
        text = ""
        if self.coach.enabled and self.coach.budget_left(day) > 0:
            try:
                text = await self.coach.checkin(blk, now)
            except Exception as exc:                          # noqa: BLE001
                log.warning("checkin failed: %s", exc)
        if not text:
            text = f"yo, how's {blk['label']} going? 👀"
        self.coach.remember(day, now, "coach", text)
        await self.send_user(text)

    # ======================================================================
    # reminders
    # ======================================================================
    async def cmd_remind(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        raw = update.message.text.partition(" ")[2].strip()
        if not raw:
            await update.message.reply_text(
                "what and when?\n\n"
                "<code>/remind 3 sep 14:40 doctor's appointment</code>\n"
                "<code>/remind tomorrow 09:00 call mum</code>",
                parse_mode=ParseMode.HTML)
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
            f"I'll remind you the night before, that morning, and an hour before.\n"
            f"<code>/forget {cur.lastrowid}</code> to drop it.",
            parse_mode=ParseMode.HTML)

    async def cmd_reminders(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        now = self.cfg.now()
        rows = self.db.q("SELECT * FROM reminders WHERE at_ts >= ? ORDER BY at_ts",
                         (now.isoformat(),))
        if not rows:
            await update.message.reply_text("nothing coming up 👌")
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
            await update.message.reply_text("which one? /reminders shows the numbers")
            return
        row = self.db.one("SELECT text FROM reminders WHERE id = ?", (int(parts[1]),))
        if not row:
            await update.message.reply_text("no reminder with that number")
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
                "only your dad can open that, from his phone.")
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
            f"🔓 Your father opened the blocker for <b>{mins} min</b>, until "
            f"{until:%H:%M}. It shuts by itself.\n\n"
            "do what you asked for it for, not the other stuff 👀")

    async def cmd_block(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_guardian(update):
            await update.message.reply_text("Only your father can do that.")
            return
        self.db.set_lock(apps_open_until=None)
        self.ledger.append("apps_relocked", {})
        await update.message.reply_text("🔒 Closed again.")
        await self.send_user("🔒 your dad closed the blocker again.")

    async def cmd_today(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        now = self.cfg.now()
        today = now.date()
        if not self.engine.has_plan(today):
            await update.message.reply_text("no plan for today. /plan")
            return
        blocks = [planner.Block(b["start_min"], b["end_min"], b["label"], b["category"])
                  for b in self.db.blocks(today)]
        sc = self.engine.score(today, now)
        goals = self._goals_text(today)
        head = f"<b>{today:%A %d %B}</b>\n\n"
        if goals:
            head += f"<b>Today is for:</b>\n{goals}\n\n"
        await update.message.reply_text(
            f"{head}{planner.render(blocks)}\n\n"
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
            await update.message.reply_text("not in the window, nothing to push back.")
            return
        row = self.db.ensure_day(today)
        if row["offsets_used"] >= int(self.cfg.offset["max_per_day"]):
            await update.message.reply_text(
                "you already used today's offset. one a day, that was the deal.")
            return
        if len(reason) < 10:
            await update.message.reply_text(
                "what happened? one sentence:\n"
                "<code>/offset stayed back for a department meeting</code>",
                parse_mode=ParseMode.HTML)
            return

        if self.cfg.offset["require_photo"]:
            self.pending[update.effective_chat.id] = {"action": "offset_proof", "reason": reason}
            await update.message.reply_text(
                f"got it: <i>{html.escape(reason)}</i>\n\n"
                "now send a photo of where you are, with the time showing.",
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
            f"⏭ {self.cfg.user_name} used today's one-hour offset.\nReason: <i>{html.escape(reason)}</i>")

    # ======================================================================
    # the shutdown request
    # ======================================================================
    def _why_text(self) -> str | None:
        """Your own reasoning, read fresh from disk each time.

        Not a rule and not a lock -- a mirror. It can't stop you doing anything.
        It just makes sure the version of you that wrote it gets a word in
        before the version that wants out acts.
        """
        path = self.cfg.raw.get("commitment", {}).get("why_file")
        if not path:
            return None
        try:
            text = pathlib.Path(path).expanduser().read_text().strip()
        except Exception:                                     # noqa: BLE001
            return None
        body = "\n".join(l for l in text.splitlines()
                          if l.strip() and not l.lstrip().startswith(("#", ">", "<!--")))
        return body.strip() or None

    async def cmd_why(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        why = self._why_text()
        if not why:
            await update.message.reply_text(
                "you haven't written it yet. put it in the file at "
                "<code>commitment.why_file</code> and I'll read it back whenever "
                "you ask — and whenever you try to stop.",
                parse_mode=ParseMode.HTML)
            return
        await update.message.reply_text(
            f"<b>your words, not mine:</b>\n\n<i>{html.escape(why[:3000])}</i>",
            parse_mode=ParseMode.HTML)

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
            await update.message.reply_text("it's already off.")
            return
        if not self.lock.guardian_id():
            await update.message.reply_text(
                "No guardian registered, so nothing is holding this shut. "
                "Sort that first.")
            return
        why = self._why_text()
        if why:
            # Before anything else, your own argument. Written by you, on a day
            # you were thinking clearly, for exactly this moment.
            await update.message.reply_text(
                f"<b>hold up. you wrote this for exactly this moment:</b>\n\n"
                f"<i>{html.escape(why[:3000])}</i>\n\n"
                f"— — —",
                parse_mode=ParseMode.HTML)
        self.pending[update.effective_chat.id] = {"action": "disable_reason"}
        left = self.lock.days_remaining(self.cfg.now().date())
        await update.message.reply_text(
            (f"You're {left} days from the end of the commitment.\n\n" if left else "")
            + "Still want to? Write why. It goes to your father word for word, "
            "and only he can actually stop it — and not for 24 hours after that.\n\n"
            "/cancel to drop it.")

    async def _handle_disable_reason(self, update: Update, reason: str) -> None:
        if len(reason) < 15:
            await update.message.reply_text(
                "write more than that, your dad's gonna read it.")
            return
        self.pending.pop(update.effective_chat.id, None)
        self.ledger.append("disable_requested", {"reason": reason})
        await self.send_guardian(
            f"⚠️ <b>{self.cfg.user_name} wants the system off.</b>\n\n"
            f"<i>{html.escape(reason)}</i>\n\n"
            "Agree? <code>/stopbot</code>\n"
            "Don't? Do nothing — it stays on. You owe him no reply.")
        await update.message.reply_text(
            "sent it to your dad. if he doesn't answer, it stays on — "
            "that's how you set it up.")

    async def cmd_stopbot(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Agrees to stop — in 24 hours, not now."""
        if not self._is_guardian(update):
            await update.message.reply_text(
                "Only your father can stop this, from his own Telegram.")
            return
        now = self.cfg.now()
        reason = update.message.text.partition(" ")[2].strip() or "no reason given"
        hours = int(self.cfg.raw.get("commitment", {}).get("stop_delay_hours", 24))
        at = self.lock.schedule_stop(reason, now, hours)
        await update.message.reply_text(
            f"⏳ Stops at <b>{at:%H:%M on %d %b}</b> — {hours} hours from now, "
            "not immediately. An urge is a state, and states pass.\n\n"
            "<code>/cancelstop</code> to call it off.\n"
            "<code>/stopnow</code> for a real emergency.",
            parse_mode=ParseMode.HTML)
        await self.send_user(
            f"⏳ <b>your dad said yes — it stops at {at:%H:%M on %d %b}.</b>\n\n"
            f"not now tho, in {hours} hours. everything runs like normal till then, "
            "and if you still want out tomorrow, you're out.\n\n"
            "<code>/nevermind</code> if the feeling's gone.")

    async def cmd_stopnow(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """The emergency door. Deliberately a separate word."""
        if not self._is_guardian(update):
            await update.message.reply_text("Only your father can do that.")
            return
        reason = update.message.text.partition(" ")[2].strip() or "emergency"
        self.lock.cancel_stop()
        self.lock.disable(reason, self.cfg.now())
        await update.message.reply_text("🔴 Stopped, immediately. /startbot when you want it back.")
        await self.send_user(
            "🔴 <b>your dad stopped it.</b>\n\n"
            "It stays off until he restarts it.")

    async def cmd_cancelstop(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_guardian(update):
            await update.message.reply_text("Only your father can do that.")
            return
        if self.lock.cancel_stop():
            await update.message.reply_text("✅ Called off. It keeps running.")
            await self.send_user("your dad called off the stop. it keeps going 💪")
        else:
            await update.message.reply_text("Nothing was scheduled.")

    async def cmd_nevermind(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """You changed your mind during the wait. This one you CAN do."""
        if not self._is_user(update):
            return
        if self.lock.cancel_stop():
            self.ledger.append("stop_withdrawn_by_user", {})
            await update.message.reply_text(
                "called off, it keeps running 💪\n\n"
                "notice how the feeling passed in a day? it usually does.")
            await self.send_guardian(
                f"✅ {self.cfg.user_name} withdrew the request to stop. The urge passed. "
                "Nothing for you to do.")
        else:
            await update.message.reply_text("Nothing was scheduled to stop.")

    async def cmd_startbot(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_guardian(update):
            await update.message.reply_text("Only your father can restart it.")
            return
        self.lock.enable(self.cfg.now())
        await update.message.reply_text("🟢 System running again.")
        await self.send_user("🟢 <b>your dad turned it back on.</b>")

    async def cmd_enable(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self._is_guardian(update):
            await self.cmd_startbot(update, _ctx)
            return
        await update.message.reply_text(
            "not your call bro, your dad sends /startbot.")

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
                "It's running. You don't get to change who holds the switch "
                "halfway through.")
            return
        self.lock.register_guardian(gid)
        await update.message.reply_text(f"✅ Guardian registered: <code>{gid}</code>",
                                        parse_mode=ParseMode.HTML)
        await self.send_guardian(
            f"You're the guardian for {self.cfg.user_name}.\n\n"
            "No password — it listens to this account and nobody else, him "
            "included.\n\nTo start three months:\n\n<code>/commit 3</code>")
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
            f"🔔 TEST siren armed — <b>{secs}s</b>. Starts within a minute.\n\n"
            "While it's going, check it survives: <b>silent mode</b>, "
            "<b>media volume at zero</b>. Ringtone and vibrate still normal.\n\n"
            "if it goes quiet on silent, it's on the wrong audio stream. /stopsiren to stop.",
            parse_mode=ParseMode.HTML)

    async def cmd_stopsiren(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        self.db.set_lock(siren_test_until=None)
        await update.message.reply_text("🔕 test siren off, stops within a minute.")

    async def cmd_more(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Give the task you're on right now more time."""
        if not self._is_user(update):
            return
        now = self.cfg.now()
        today = now.date()
        parts = update.message.text.split()
        minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30

        if not self.engine.in_window(now):
            await update.message.reply_text("not in the window, nothing to extend.")
            return
        if not self.engine.has_plan(today):
            await update.message.reply_text("no plan today. /plan")
            return
        block = self.engine.current_block(today, now)
        if block is None:
            await update.message.reply_text("you're not in a block right now.")
            return

        cap = int(self.cfg.raw.get("extend", {}).get("max_minutes_per_day", 90))
        used = (self.db.day(today)["extend_minutes"] or 0)
        minutes = max(5, min(minutes, 120))
        if used + minutes > cap:
            await update.message.reply_text(
                f"That's {used + minutes} min for the day and the limit is {cap} "
                f"({used} used).\n\nmake tomorrow's blocks bigger.")
            return

        # You can only extend something you have already proven you're doing.
        # Otherwise this is just a way to push the whole day back by claiming to
        # be busy, which is an offset wearing a disguise.
        if not self.engine.block_was_proven(today, block["id"]):
            await update.message.reply_text(
                f"Photo of <b>{html.escape(block['label'])}</b> first. You can "
                "only stretch a task you've shown you're doing.",
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
                "nah bro. muting on a bad evening is just quitting with extra steps.\n\n"
                "if something real happened: /offset, or ask your dad for /pause.")
            return
        parts = update.message.text.split()
        hours = float(parts[1]) if len(parts) > 1 and parts[1].replace(".", "").isdigit() else 3
        hours = max(0.25, min(hours, 24))
        until = now + timedelta(hours=hours)
        self.db.set_lock(muted_until=until.isoformat(), siren_test_until=None)
        self.ledger.append("muted", {"until": until.isoformat()})
        await update.message.reply_text(
            f"🔕 Siren muted until <b>{until:%H:%M}</b>. Checkpoints and misses "
            "carry on. /unmute to end it.\n\n"
            "<i>Stops working once the commitment is live.</i>",
            parse_mode=ParseMode.HTML)

    async def cmd_unmute(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
        self.db.set_lock(muted_until=None)
        await update.message.reply_text("🔔 siren's back on.")

    async def cmd_logs(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Read-only log access, so you never need SSH to diagnose anything.

        This exists to make "your father owns the server" survivable. Without
        it you need shell access to know why something broke, and needing shell
        access is what forces the account back into your hands.
        """
        if not (self._is_user(update) or self._is_guardian(update)):
            return
        import subprocess
        try:
            out = subprocess.run(
                ["tail", "-n", "40", os.environ.get("LOCKSTEP_LOG", "/proc/1/fd/1")],
                capture_output=True, text=True, timeout=10).stdout
        except Exception:                                     # noqa: BLE001
            out = ""
        if not out.strip():
            ok, bad = self.ledger.verify()
            rows = self.db.q("SELECT ts, kind, payload FROM events ORDER BY id DESC LIMIT 15")
            out = "\n".join(f"{r['ts'][11:19]}  {r['kind']}  {r['payload'][:50]}"
                             for r in reversed(rows))
            out = f"(no container log reachable — showing the event ledger)\n\n{out}"
        await update.message.reply_text(
            f"<pre>{html.escape(out[-3500:])}</pre>", parse_mode=ParseMode.HTML)

    async def cmd_restart(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Bounce the process. Docker's restart policy brings it straight back.

        Restarting is the only server operation you actually need day to day,
        so it lives here rather than behind an SSH key. Note it cannot be used
        to stop anything: the supervisor restarts it within seconds, and if it
        somehow doesn't, the external heartbeat tells your father.
        """
        if not (self._is_user(update) or self._is_guardian(update)):
            return
        self.ledger.append("restart_requested", {"by": update.effective_chat.id})
        await update.message.reply_text(
            "🔄 restarting, back in like 10 seconds.\n\n"
            "(not an off switch, it comes right back)")
        import asyncio as _a
        _a.get_running_loop().call_later(1.0, lambda: os._exit(0))

    async def cmd_cancel(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.pending.pop(update.effective_chat.id, None):
            await update.message.reply_text("Dropped.")
        else:
            await update.message.reply_text("nothing pending.")

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
            "• only you can stop it: <code>/stopbot</code>\n"
            "• <code>/pause 24 reason</code> if he's ill or travelling\n"
            "• short report each night\n\n"
            "If he asks you to stop it, you don't have to reply.",
            parse_mode=ParseMode.HTML)
        await self.send_user(
            f"🔒 <b>it's on. your dad started it, runs till {end}.</b>\n\n"
            "only he can stop it. no password anywhere.\n\n"
            "this is the you that wanted it, holding the door for the you "
            "that won't. let's go.")

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
            f"⏸ your dad paused it until <b>{until:%d %b %H:%M}</b>.\n"
            f"Reason: <i>{html.escape(reason)}</i>", )

    async def cmd_resume(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_guardian(update):
            return
        self.lock.enable(self.cfg.now())
        await update.message.reply_text("▶️ Resumed.")
        await self.send_user("▶️ pause is over, we're back.")

    async def cmd_extend(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_guardian(update):
            return
        parts = update.message.text.split()
        months = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 3
        end = self.lock.extend_commitment(months, self.cfg.now().date())
        await update.message.reply_text(f"Commitment now runs to {end}.")
        await self.send_user(f"Commitment extended by {months} months — to <b>{end}</b>.")

    async def cmd_report(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_user(update):
            return
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
                "⚠️ heads up, that pic had your GPS location in it. I removed it, but turn "
                "location off in your camera — other apps won't remove it.")
        # Downscaled and metadata-stripped before it goes to Google or to disk.
        blob, mime = images.prepare(raw, int(priv.get("max_dimension", 1024)))

        if pend.get("action") == "offset_proof":
            path = self._save_photo(blob, today, "offset")
            await self._grant_offset(update, pend["reason"], path)
            return

        if pend.get("action") == "latenight_photo":
            await self._grant_latenight(update, pend, blob, mime, now)
            return

        if pend.get("action") == "outside_photo":
            await self._grant_outside(update, pend, blob, mime, now)
            return

        if pend.get("action") == "rewrite_why":
            await self._rewrite_reason(update, getattr(update.message, "caption", None) or "",
                                       image=blob, mime=mime)
            return

        if not self.lock.enabled:
            await update.message.reply_text("it's off rn, nothing to prove.")
            return

        # The bed photo is owed after the window has already shut, so it has to
        # be handled before the in-window guard turns every late photo away.
        night = self.engine.bed_night(now)
        if night is not None:
            bed = self.engine.bed_checkpoint(night)
            if bed is not None and bed["state"] != "satisfied":
                await self._handle_bed_photo(update, night, bed, blob, mime, now)
                return

        if not self.engine.in_window(now):
            await update.message.reply_text("we're outside the window, nothing needed.")
            return
        if not self.engine.has_plan(today):
            await update.message.reply_text("no plan for today yet, /plan first.")
            return

        cp = self.engine.current_checkpoint(today, now)
        if cp is None:
            await update.message.reply_text("you're all caught up 👍")
            return

        label = cp["label"] or "your planned task"
        category = cp["category"] or "other"
        await update.message.chat.send_action("typing")
        # What he said he was doing this minute, so the photo is checked
        # against the claim and not only the block's name.
        claimed = self.steps.current_text(now) if self.steps.enabled else ""
        for_checker = self.coach.terms.expand(label)
        if claimed:
            for_checker += f" (right now, in his words: {self.coach.terms.expand(claimed)})"
        verdict = await self.validator.check(blob, for_checker, category,
                                             now, mime=mime, kind=cp["kind"] or "activity")
        # Whatever happened, Friday hears it. 10 Oct 20:48 a photo was refused
        # for "your name or login", he wrote "these aren't my name and login",
        # and Friday -- never told -- answered with password-reset advice.
        self._app_note(now, (f"his photo for \"{label}\" was ACCEPTED" if verdict.ok else
                             f"his photo for \"{label}\" was REFUSED "
                             f"({verdict.failure_code}): {verdict.reason[:200]}"))

        if not verdict.ok:
            self.db.run("UPDATE checkpoints SET attempts = attempts + 1, verdict = ? WHERE id = ?",
                        (verdict.to_json(), cp["id"]))
            # A frame rejected for privacy is never written to disk, not even as
            # a thumbnail. Storing the thing we just told you to retake would be
            # a strange way to respect the rule.
            self.ledger.append("photo_rejected",
                               {"checkpoint": cp["id"], "code": verdict.failure_code})
            # A wrong read can be explained; a missing clock cannot. A face or
            # a login is never negotiable -- but "this could place you" is a
            # judgement about an ordinary street, and he is the one who knows
            # whether the poles behind him name his address.
            # "replay" joins these after 10 Oct: a photo of his laptop showing
            # the CAS sheet he was working on came back as a re-photograph.
            # Explaining it changes nothing by itself -- the same checker looks
            # again, with his words in front of it, and still decides.
            explainable = verdict.failure_code in ("mismatch", "privacy_location", "replay",
                                                   "privacy_identity")
            # "work:" asks for a blocker screenshot, which is impossible if the
            # work is on paper. 19 Sep: the planner put "work:" in front of
            # "math sat questions without chair at table, on knees" and then
            # demanded a Cold Turkey window for it.
            on_paper = verdict.failure_code in ("no_blocker", "blocker_off")
            self.last_reject = ({"cp": cp["id"], "blob": blob, "mime": mime, "at": now,
                                 "label": label, "category": category,
                                 "kind": cp["kind"] or "activity",
                                 "reason": verdict.reason, "tries": 0}
                                if explainable else None)
            await update.message.reply_text(
                f"❌ doesn't count.\n\n{html.escape(verdict.reason)}\n\n"
                # A photo of the right work in the wrong block is a schedule
                # problem, not a photo problem. 10 Oct 16:34: CAS on screen,
                # IELTS on the plan, and the only advice was "send another
                # one" -- of IELTS, which he wasn't doing.
                + ("doing something else right now? say so (\"I'm still on CAS\") "
                   "and I'll switch the block, then this same photo counts.\n"
                   "wrong read instead? tell me what it is and I'll look again."
                   if explainable and verdict.failure_code == "mismatch" else
                   "wrong read? tell me what it is (like \"рнкп is my presentation\") "
                   "and I'll look again. or send another one."
                   if explainable else
                   "doing this one on paper? say <code>/paper</code> and I'll take a "
                   "photo of you working instead." if on_paper else
                   "send another one, it's still open."),
                parse_mode=ParseMode.HTML)
            return

        await self._count_photo(update, cp, blob, verdict, now)

    async def _count_photo(self, update: Update, cp, blob: bytes, verdict, now: datetime) -> None:
        today = now.date()
        self.last_reject = None
        path = self._save_photo(blob, today, f"cp{cp['id']}")
        res = self.engine.satisfy(today, now, cp["id"], verdict.to_json(), path)
        try:
            self.tasks.touch(self.tasks.match(cp["label"] or ""), today)
        except Exception as exc:                              # noqa: BLE001
            log.warning("task touch failed: %s", exc)
        self.ledger.append("checkpoint_satisfied",
                           {"checkpoint": cp["id"], "unverified": verdict.unverified})
        self.last_level = 0

        sc = self.engine.score(today, now)
        note = ""
        if res["swept"]:
            note = (f"\n(the {res['swept']} you missed before still count as missed, "
                    "but you're caught up now)")
        if verdict.unverified:
            note += "\n⚠️ The vision model was unreachable; this one is flagged in the log."
            await self.send_guardian("⚠️ Vision model unreachable — a photo was accepted unchecked.")
        await update.message.reply_text(
            f"{self._nice(cp['id'])} {sc['hit']}/{sc['due']} today{note}", parse_mode=ParseMode.HTML)

    async def _handle_bed_photo(self, update: Update, night: date, bed,
                                blob: bytes, mime: str, now: datetime) -> None:
        await update.message.chat.send_action("typing")
        verdict = await self.validator.check(
            blob, "your bed, opened and ready to sleep in", "sleep", now,
            mime=mime, kind="bed")
        if not verdict.ok:
            self.db.run("UPDATE checkpoints SET attempts = attempts + 1, verdict = ? WHERE id = ?",
                        (verdict.to_json(), bed["id"]))
            self.ledger.append("photo_rejected",
                               {"checkpoint": bed["id"], "code": verdict.failure_code})
            await update.message.reply_text(
                f"❌ doesn't count.\n\n{html.escape(verdict.reason)}\n\n"
                "siren's still going.", parse_mode=ParseMode.HTML)
            return
        path = self._save_photo(blob, night, "bed")
        self.db.run(
            "UPDATE checkpoints SET state='satisfied', settled_at=?, verdict=?, photo_path=?"
            " WHERE id=?", (now.isoformat(), verdict.to_json(), path, bed["id"]))
        self.ledger.append("bed_confirmed", {"date": night.isoformat()})
        late = int((now - self.engine.bed_due(night)).total_seconds() // 60)
        await update.message.reply_text(
            f"🛏 <b>Counted. Siren off.</b>\n\n"
            + (f"{late} min past bedtime.\n\n" if late > 0 else "")
            + "Goodnight.", parse_mode=ParseMode.HTML)

    async def _grant_latenight(self, update: Update, pend: dict, blob: bytes,
                               mime: str, now: datetime) -> None:
        night, reason, hours = pend["date"], pend["reason"], pend["hours"]
        await update.message.chat.send_action("typing")
        verdict = await self.validator.check(
            blob, f"being out with people, or deadline work in progress: {reason}",
            "other", now, mime=mime, kind="activity")
        if not verdict.ok:
            await update.message.reply_text(
                f"❌ nope.\n\n{html.escape(verdict.reason)}\n\n"
                "send one that actually shows it, or go to bed.", parse_mode=ParseMode.HTML)
            return

        self.pending.pop(update.effective_chat.id, None)
        self.db.ensure_day(night)
        self.db.run("UPDATE days SET bed_offset_minutes = ?, bed_reason = ? WHERE date = ?",
                    (hours * 60, reason, night.isoformat()))
        # The bed checkpoint was built at the old time, so move it.
        bed = self.engine.bed_checkpoint(night)
        if bed is not None:
            self.db.run(
                "UPDATE checkpoints SET due_at=?, state='scheduled', asked_at=NULL,"
                " settled_at=NULL WHERE id=?",
                (self.engine.bed_due(night).isoformat(), bed["id"]))
        self._save_photo(blob, night, "latenight")
        self.ledger.append("latenight_used",
                           {"date": night.isoformat(), "hours": hours, "reason": reason})
        new_bed = self.engine.bed_due(night)
        await update.message.reply_text(
            f"🌙 <b>{hours} hours.</b> Bed photo now due at "
            f"<b>{new_bed:%H:%M}</b>, siren until "
            f"<b>{self.engine.bed_cutoff(night):%H:%M}</b>.\n\n"
            "that's your one for the week. your dad knows.",
            parse_mode=ParseMode.HTML)
        await self.send_guardian(
            f"🌙 {self.cfg.user_name} used the weekly late night ({hours}h).\n"
            f"Reason: <i>{html.escape(reason)}</i>\n"
            f"Bedtime tonight moves to {new_bed:%H:%M}.")

    async def _grant_outside(self, update: Update, pend: dict, blob: bytes,
                             mime: str, now: datetime) -> None:
        minutes, left = pend["minutes"], pend["left"]
        await update.message.chat.send_action("typing")
        verdict = await self.validator.check(
            blob, "being outside: a street, yard, shop, stairwell or bus stop",
            "other", now, mime=mime, kind="outside")
        if not verdict.ok:
            await update.message.reply_text(
                f"❌ nah.\n\n{html.escape(verdict.reason)}\n\n"
                "try again — the door, the stairs, the yard.",
                parse_mode=ParseMode.HTML)
            return

        self.pending.pop(update.effective_chat.id, None)
        until = self.engine.grant_outside(now, minutes)
        self._save_photo(blob, now.date(), "outside")
        self.ledger.append("outside_granted",
                           {"date": now.date().isoformat(), "minutes": minutes,
                            "until": until.isoformat()})
        await update.message.reply_text(
            f"🚶 <b>ok, no siren till {until:%H:%M}.</b> go. {left} left today.\n\n"
            "stuff due while you're out still counts as missed.", parse_mode=ParseMode.HTML)

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

        if action == "goals":
            await self._accept_goals(update, pend["date"], text)
            return
        if action == "plan":
            await self._accept_plan(update, pend["date"], text)
            return
        if action == "replan":
            await self._accept_replan(update, text)
            return
        if action == "rewrite_why":
            await self._rewrite_reason(update, text)
            return
        if action == "plan_proposal":
            # A plain yes locks it. Anything else -- a change, a question, "why
            # sat first?" -- is read by Friday with the plan in front of her,
            # and she decides: answer, edit, or lock.
            low = text.strip().lower().strip(" .!👍")
            if low in ("ok", "okay", "yes", "yeah", "yep", "lock", "lock it", "да",
                       "ok lock it", "lock it in", "bet", "go", "good", "ок"):
                await self._lock_proposal(update, pend)
                return
        if action == "disable_reason":
            await self._handle_disable_reason(update, text)
            return

        # The ten-minute loop gets first refusal on a short message: it is
        # free, instant, and the answer to "next step?" must not cost him an
        # AI call or arrive forty seconds later.
        if self._is_user(update) and await self._handle_step_text(
                update, text, self.cfg.now()):
            return

        # "i am filling out info still bro" / "need 20 more mins on this" is
        # the other shape, and the one Friday got wrong on 11 Oct: it said it
        # had stretched the block and nothing moved.
        if self._is_user(update):
            more = self._more_request(text)
            if more is not None and await self._quick_more(update, more):
                return

        # "lemme eat for 10 min" is a schedule change with one obvious shape.
        # It is done here, exactly, instead of being handed to a model that
        # on 10 Oct answered "grabbed 10 mins for food" and changed nothing.
        if self._is_user(update):
            mins = self._meal_request(text)
            if mins is not None and await self._quick_meal(update, mins):
                return

        # A bare plan pasted with no command is still obviously a plan.
        if self._is_user(update) and "\n" in text and planner.LINE_RE.match(text.splitlines()[0]):
            await self._accept_plan(update, self.target_plan_date(self.cfg.now()), text)
            return

        if self._is_user(update):
            await self._talk(update, text)
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
                f"You're not registered with this bot yet. {self.cfg.user_name} has been told you "
                "messaged — he'll add you.")
            await self.send_user(
                f"👤 <b>{html.escape(name)}</b> just messaged the bot "
                f"(<code>{chat.id}</code>) and isn't registered.\n\n"
                f"If that's your father:\n<code>/setguardian {chat.id}</code>")

    async def _talk(self, update: Update, text: str) -> None:
        """Anything he types that isn't a command or a plan goes to the coach.

        It is handed today's real numbers before it sees a word he wrote, so it
        cannot be told the day went well. And it can change nothing: no
        checkpoint, no deadline, no siren. Talk is not proof, and a coach that
        could let you off would be the easiest off-switch in the system.
        """
        if not self.coach.enabled:
            await update.message.reply_text("send a pic, or /help")
            return
        now = self.cfg.now()
        if self.coach.budget_left(now.date()) <= 0:
            if self._bed_owed(now) and self._sounds_late(text):
                await update.message.reply_text(
                    "I've used up my AI messages for today, but I got you — "
                    "starting the late night for this:")
                await self.cmd_latenight(update, None, reason=text)
                return
            cap = int(self.coach.conf.get("max_calls_per_day", 40))
            await update.message.reply_text(
                f"I've hit my limit of {cap} AI messages for today, so I can't really "
                "talk till midnight 😩 photos still get checked.\n\n"
                "what still works:\n"
                "/latenight reason — 2 more hours tonight (deadline, friends)\n"
                "/replan — change the rest of today\n"
                "/tasks · /night · /today")
            return
        await update.message.chat.send_action("typing")
        try:
            await self._load_doc(now)
            if self.steps.enabled:
                self.coach.steps_today = self.steps.describe(now.date())
            note = self._reject_note(now)
            waiting = self.pending.get(update.effective_chat.id) or {}
            if waiting.get("action") == "plan_proposal":
                note = (note + "\n" if note else "") + (
                    f"PLAN IS WAITING for {waiting['date']:%a %d %b} -- you proposed it, "
                    "he has NOT locked it yet:\n" + waiting["text"] + "\n"
                    "If his message asks for any change to it, use make_plan with the "
                    "change as text. If he's happy with it, use lock_plan. If he asks "
                    "about it, answer from the plan above.")
            su = self.standups.pending(now) if self.standups.enabled else None
            if su is not None and not note:
                from .coach import STANDUP_NOTE
                note = STANDUP_NOTE.format(label=su["label"] or "his block",
                                           at=su["asked_at"][11:16])
                self.standups.answer(su["id"], text, now)
            out = await self.coach.reply(text, now, note=note)
        except Exception as exc:                              # noqa: BLE001
            log.warning("coach reply failed: %s", exc)
            busy = any(k in str(exc) for k in ("RESOURCE_EXHAUSTED", "429", "503", "UNAVAILABLE"))
            await update.message.reply_text(
                "the AI I run on is maxed out rn 😩 try me again in a bit. "
                "photos still get checked, and /replan still works"
                if busy else
                "something broke on my end, try again in a minute. "
                "/replan and photos still work")
            return
        paper, self.coach.last_on_paper = self.coach.last_on_paper, None
        if paper is not None:
            if out:
                await update.message.reply_text(out[:3900])
            await self.cmd_paper(update, None, which=paper)
            return
        lock, self.coach.last_lock_plan = self.coach.last_lock_plan, False
        waiting = self.pending.get(update.effective_chat.id) or {}
        if lock and waiting.get("action") == "plan_proposal" \
                and self.coach.last_make_plan is None:
            if out:
                await update.message.reply_text(out[:3900])
            await self._lock_proposal(update, waiting)
            return
        want_plan, self.coach.last_make_plan = self.coach.last_make_plan, None
        if want_plan is not None:
            if out:
                await update.message.reply_text(out[:3900])
            await self._chat_make_plan(update, want_plan, now)
            return
        late, self.coach.last_latenight = self.coach.last_latenight, None
        if late is not None:
            if out:
                await update.message.reply_text(out[:3900])
            await self.cmd_latenight(update, None, reason=late or text)
            return
        recheck, self.coach.last_recheck = self.coach.last_recheck, None
        if recheck and self._live_reject(now):
            if out:
                await update.message.reply_text(out[:3900])
            await self._recheck(update, recheck, now)
            return
        edits, self.coach.last_plan_edits = self.coach.last_plan_edits, []
        prepared = self._prepare_chat_plan(edits[0], now, said=text) if edits else None
        if prepared is None:
            if out and self._claims_change(out):
                # It said it moved time around and asked the app to do
                # nothing. 10 Oct 20:06: "grabbed 10 mins for food" -- no
                # replan, the siren kept going, and the next reply was "I
                # literally can't stop it". Say what is true instead.
                self._app_note(now, "your last reply claimed a schedule change but "
                                    "none was made -- the plan is unchanged")
                out += ("\n\n(heads up: that didn't actually change the plan. "
                        "tell me the times, or for food just say \"eat 15 min\", "
                        "and it's done.)")
            if out:
                await update.message.reply_text(out[:3900])
            else:
                await update.message.reply_text("sorry, blanked. say that again?")
                return
        else:
            # The change goes in FIRST, and Friday's words follow only if it
            # did. 10 Oct 16:23: "added 20 mins for CAS" -- and nothing was
            # added. Predicting whether the guard would hold it was the bug;
            # asking the ledger afterwards cannot be wrong.
            mark = self._ledger_mark()
            await self._apply_chat_plan(update, prepared)
            if self._plan_changed_since(mark):
                if out:
                    await update.message.reply_text(out[:3900])
            else:
                self._app_note(now, "the schedule change he asked for was NOT "
                                    "applied (held for a reason, or refused) -- the "
                                    "plan is unchanged; do not say it changed")
        # Last word, after whatever he was told: anything from a list that
        # does not look like it landed.
        missed = self._unsaved_lines(text, out)
        if missed:
            await update.message.reply_text(missed, parse_mode=ParseMode.HTML)

    def _his_words_today(self, now: datetime, said: str = "") -> str:
        rows = self.db.q("SELECT text FROM coach_log WHERE date = ? AND role = 'user'",
                         (now.date().isoformat(),))
        return "\n".join([r["text"] for r in rows] + [said])

    def _prepare_chat_plan(self, edit: dict, now: datetime, said: str = ""):
        """Turn a chat edit into (path, day, plan text), or a message to send.

        path is "replan" (today, window open) or "plan" (anything else).

        "work:" survives only where HE wrote it. 10 Oct 16:37 Friday rewrote
        the evening and every block came back as "work: ..." -- "work: write
        uni essay", "work: prep for physics" -- each now demanding a Cold
        Turkey screenshot he had never agreed to. The planner already had this
        rule; the edit path did not.
        """
        text = (edit.get("plan") or "").strip()
        today = now.date()
        target = today + timedelta(days=1) if str(edit.get("day", "")).lower().startswith("tom") \
            else today
        if target != today or not self.engine.has_plan(today):
            return ("plan", target, text)
        minute = now.hour * 60 + now.minute
        start, end = self.engine.window(today)
        if minute >= end:
            return "today's already done bro. want me to change tomorrow instead?"
        if minute < start:
            return ("plan", today, text)
        try:
            blocks = [b for b in planner.parse(text) if b.end_min > minute]
        except planner.PlanError as exc:
            return f"couldn't make that work: {html.escape(str(exc))}"
        if blocks and blocks[0].start_min > minute:
            # "add eat at 18:00" at 17:56: the minutes before it stay whatever
            # he is doing now.
            here = next((b for b in self.db.blocks(today)
                         if b["start_min"] <= minute < b["end_min"]), None)
            fill = here["label"] if here else "relax"
            text = (f"{minute//60:02d}:{minute%60:02d}-"
                    f"{blocks[0].start_min//60:02d}:{blocks[0].start_min%60:02d} {fill}\n"
                    + "\n".join(f"{b.hhmm()} {b.label}" for b in blocks))
        return ("replan", today, text)

    def _chat_plan_held(self, prepared, now: datetime) -> bool:
        """Would this change stop and ask why? Mirrors _ask_why's arithmetic."""
        if not isinstance(prepared, tuple):
            return False
        conf = self.cfg.raw.get("friend", {}) or {}
        if not conf.get("guard_rewrites", True):
            return False
        path, day, text = prepared
        row = self.db.day(day)
        if not (row and row["plan_locked"]):
            return False
        try:
            new = [(b.start_min, b.end_min, b.label) for b in planner.parse(text)]
        except planner.PlanError:
            return False
        old = [(b["start_min"], b["end_min"], b["label"]) for b in self.db.blocks(day)]
        since = (now.hour * 60 + now.minute) if day == now.date() else 0
        return self._lost_work(old, new, since) >= int(conf.get("rewrite_min_lost", 10))

    async def _recheck_after_swap(self, update: Update, now: datetime) -> None:
        """The plan now says he's doing what the rejected photo showed. Look again.

        17 Sep: a presentation screenshot was rejected during a physics block,
        he swapped the block to the presentation, and the photo that now fits
        the plan was still sitting there rejected.
        """
        r = self.last_reject
        if not r or (now - r["at"]).total_seconds() > 30 * 60:
            return
        # The swap closes the old block's open check as missed and opens one
        # for the new block, so the photo is re-aimed at whatever is open now.
        # That new check is still "scheduled" until the next tick asks it; the
        # photo is its answer, so it is asked here instead.
        self.db.run("UPDATE checkpoints SET state='asked', asked_at=? WHERE date=?"
                    " AND state='scheduled' AND due_at <= ?",
                    (now.isoformat(), now.date().isoformat(), now.isoformat()))
        cp = self.engine.current_checkpoint(now.date(), now)
        if cp is None or (cp["id"] == r["cp"] and (cp["label"] or "") == r["label"]):
            return
        r.update(cp=cp["id"], label=cp["label"] or r["label"],
                 category=cp["category"] or r["category"], kind=cp["kind"] or r["kind"])
        await self._recheck(update, "the plan was just changed to this block", now)

    def _bed_owed(self, now: datetime) -> bool:
        night = self.engine.bed_night(now)
        if night is None:
            return False
        cp = self.engine.bed_checkpoint(night)
        return cp is not None and cp["state"] != "satisfied"

    @staticmethod
    def _sounds_late(text: str) -> bool:
        low = (text or "").lower()
        return any(w in low for w in ('deadline', 'дедлайн', 'hackathon', 'submission', 'due', 'competition', 'olympiad', 'олимпиад', 'project', 'проект', 'presentation', 'friends', "can't sleep", 'cant sleep', 'not sleeping'))

    def _live_reject(self, now: datetime) -> dict | None:
        """The rejected photo, if it can still be looked at again."""
        r = self.last_reject
        if not r:
            return None
        cp = self.engine.current_checkpoint(now.date(), now)
        if (now - r["at"]).total_seconds() > 30 * 60 or cp is None or cp["id"] != r["cp"]:
            self.last_reject = None
            return None
        return r

    def _reject_note(self, now: datetime) -> str:
        r = self._live_reject(now)
        if not r:
            return ""
        return (f"NOTE: his last photo, for \"{r['label']}\" at {r['at']:%H:%M}, was "
                f"REJECTED by the photo checker: \"{r['reason']}\". The photo is still "
                "here. If he's explaining what it shows or what the task means, use "
                "recheck_photo (and define for a shorthand). If he's not, tell him he "
                "can explain it or send another.")

    async def _recheck(self, update: Update, explanation: str, now: datetime) -> None:
        """Look at the same photo again, with his explanation in front of the checker.

        The checker still decides. Explaining only helps when the photo really
        is consistent with what he says -- and it gets two goes per photo, so it
        can't be argued into a yes.
        """
        r = self._live_reject(now)
        if r is None:
            await update.message.reply_text("nothing to look at again — just send the pic.")
            return
        if r["tries"] >= 2:
            self.last_reject = None
            await update.message.reply_text(
                "I already looked twice, it's still a no. send a new pic that shows it clearly.")
            return
        r["tries"] += 1
        await update.message.chat.send_action("typing")
        label = (f"{self.coach.terms.expand(r['label'])}. He explains: \"{explanation[:300]}\". "
                 "Accept only if the photo is genuinely consistent with that explanation "
                 "and shows the work actually happening.")
        verdict = await self.validator.check(r["blob"], label, r["category"], now,
                                             mime=r["mime"], kind=r["kind"])
        self.ledger.append("photo_rechecked", {"checkpoint": r["cp"], "ok": verdict.ok,
                                               "explanation": explanation[:300]})
        if not verdict.ok:
            await update.message.reply_text(
                f"looked again, still a no: {html.escape(verdict.reason)}\n\nsend another one.",
                parse_mode=ParseMode.HTML)
            return
        cp = self.engine.current_checkpoint(now.date(), now)
        await update.message.reply_text("ok my bad, that makes sense 👍")
        await self._count_photo(update, cp, r["blob"], verdict, now)

    # "1. thing", "2) thing", and "6.1 thing" -- he writes sub-items without
    # the trailing dot, and that is exactly the shape that went missing.
    LIST_LINE = re.compile(r"^\s*(\d+(?:\.\d+)+|\d+[.)])\s*(.+)$")

    def _unsaved_lines(self, text: str, reply: str) -> str:
        """Check a numbered list line by line against what came back.

        He sent sixteen numbered things and two never landed. Trusting the
        model to account for its own output is exactly the wrong place to
        trust it, so the lines are counted here, in code, and anything without
        an obvious match is put in front of him.
        """
        lines = [(m.group(1), m.group(2).strip())
                 for m in (self.LIST_LINE.match(l) for l in (text or "").splitlines()) if m]
        if len(lines) < 3:
            return ""
        low = (reply or "").lower()

        # Short words carry the meaning here -- "DS HL IA" is the whole item --
        # so nothing is dropped for being brief, only for being filler. Numbers
        # are ignored: "19/09/2026" comes back as "19 Sep" and would never match.
        stop = {"the", "and", "for", "with", "on", "by", "of", "to", "in", "at",
                "is", "an", "a", "my", "it", "as"}

        def covered(item: str) -> bool:
            words = [w for w in re.findall(r"[^\W\d_]{2,}", item.lower())
                     if w not in stop]
            if not words:
                return True
            hit = sum(1 for w in words if w in low)
            return hit >= max(1, (len(words) + 1) // 2)

        missed = [f"{n}. {html.escape(item)}" for n, item in lines if not covered(item)]
        if not missed:
            return ""
        return ("⚠️ I'm not sure these landed — send them again or say "
                "<code>/task add ...</code>:\n" + "\n".join(missed))

    async def _apply_chat_plan(self, update: Update, prepared) -> None:
        """A schedule change he asked for in chat, through the normal door.

        Nothing here is a shortcut: today's changes go through the replan path
        and everything else through the plan path, so the goals gate, the
        coverage check and the "why are you cutting work?" question all apply.
        """
        if isinstance(prepared, str):
            await update.message.reply_text(prepared, parse_mode=ParseMode.HTML)
            return
        path, day, text = prepared
        if path == "plan":
            await self._accept_plan(update, day, text)
        else:
            await self._accept_replan(update, text)
            await self._recheck_after_swap(update, self.cfg.now())

    # ======================================================================
    # the tick
    # ======================================================================
    async def _poke(self, now: datetime) -> None:
        """Wake the phone from outside, because it cannot wake itself.

        The phone agent polls on a timer, and Android switches that timer off
        the moment the screen goes dark -- on 8 Sep the flow reported itself as
        running while the server saw not one request in two hours. Battery
        settings help and do not fix it: the OS decides, and it decides against
        you.

        A push notification is the one thing it must deliver. Android will
        freeze an app's timers all night and still ring a message through,
        because a message is somebody trying to reach you. So the server stops
        waiting to be asked and pokes instead: while there is debt, one Telegram
        message a minute, which forces the phone awake long enough for the flow
        to run and the siren to sound.

        The previous poke is deleted as the next is sent, so the chat holds one
        at a time rather than sixty. The notification still fires -- that is the
        entire point -- but the history stays readable.
        """
        row = self.db.lock()
        if not self.cfg.esc.get("telegram_poke", True):
            # Switched off: the Automate siren is the alarm, and a message every
            # minute on top of it was just noise. Clear whatever is left over.
            if row["poke_message_id"]:
                await self._delete_poke(row["poke_message_id"])
                self.db.set_lock(poke_message_id=None)
            return
        st = self.engine.agent_state(now)
        if not st.phone_alarm:
            # Clear the last one on the way out, so a solved day leaves nothing.
            if row["poke_message_id"]:
                await self._delete_poke(row["poke_message_id"])
                self.db.set_lock(poke_message_id=None)
            return
        if not self.db.claim(f"poke:{now:%Y-%m-%dT%H:%M}"):
            return
        old = row["poke_message_id"]
        try:
            msg = await self.app.bot.send_message(
                self.cfg.user_chat_id,
                f"🔔 <b>{st.debt_minutes} min behind</b> — {html.escape(st.headline)}",
                parse_mode=ParseMode.HTML)
            self.db.set_lock(poke_message_id=msg.message_id)
        except Forbidden:
            await self._on_blocked()
            return
        except Exception as exc:                              # noqa: BLE001
            log.warning("poke failed: %s", exc)
            return
        if old:
            await self._delete_poke(old)

    async def _delete_poke(self, message_id: int) -> None:
        try:
            await self.app.bot.delete_message(self.cfg.user_chat_id, message_id)
        except Exception:                                     # noqa: BLE001
            pass          # already gone, or too old for Telegram to delete

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
            await self._poke(now)
        except Exception as exc:                              # noqa: BLE001
            log.exception("poke failed: %s", exc)

        pend = self.lock.pending_stop()
        if pend and now >= pend:
            reason = self.db.lock()["stop_reason"] or ""
            self.lock.cancel_stop()
            self.lock.disable(reason, now)
            await self.send_user(
                "🔴 <b>it's off.</b>\n\nyour dad can turn it back on with /startbot.")
            await self.send_guardian(
                f"🔴 The delay has run out and the system has stopped.\n"
                f"Reason given: <i>{html.escape(reason)}</i>\n\n"
                "<code>/startbot</code> whenever you want it back.")

        try:
            await self._fire_reminders(now)
        except Exception as exc:                              # noqa: BLE001
            log.exception("reminders failed: %s", exc)

        try:
            await self._friend_tick(now)
        except Exception as exc:                              # noqa: BLE001
            log.exception("friend tick failed: %s", exc)

        # Tell them when the pressure steps up, not on every single tick.
        st = self.engine.agent_state(now)
        if st.active and st.level > self.last_level:
            self.last_level = st.level
            await self.send_user(await self._human(
                "he has fallen further behind, so the siren just got more frequent",
                f"minutes behind: {st.debt_minutes}\n"
                f"what is owed: {st.headline}\n{st.detail}\n"
                f"the siren now sounds {st.alarm_repeats} times a minute\n"
                "it stops the moment a valid photo lands\n",
                f"🔺 bro, {st.debt_minutes} minutes behind. {html.escape(st.headline)}\n"
                f"siren's going {st.alarm_repeats}× a minute now, send the pic and it stops.",
                now))
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
            grace = self.cfg.grace_min
            cid = int(p.get("checkpoint_id") or 0)
            # Is this the top of the block, or one of the checks inside it?
            minute = due.hour * 60 + due.minute
            starting = any(bk["start_min"] == minute and bk["label"] == p["label"]
                           for bk in self.db.blocks(due.date()))

            def pick(options):
                # Same checkpoint, same words -- but not the same words every time.
                return options[cid % len(options)]

            if kind == "bed":
                body = pick([
                    "bedtime bro 🛏 send me a pic of your bed, opened. siren stops when it lands",
                    "ok that's the day. bed pic (opened) and you're done 😴",
                    "time to sleep man. pic of your bed made up for the night pls",
                ])
            elif kind.startswith("blocker"):
                body = pick([
                    f"{label} time 🔒 screenshot {blocker} running, taskbar clock in it. {grace} min",
                    f"yo, {label} starts now. show me {blocker} on (clock visible) — {grace} min",
                    f"{label} 🔒 {blocker} screenshot with the clock pls, you got {grace} min",
                ])
                if kind == "blocker_allow":
                    body += "\nand the list of what the block allows"
            elif starting:
                body = pick([
                    f"{label} time 👊 send me a pic of you on it, clock in the shot. {grace} min",
                    f"yo it's {due:%H:%M}, {label} now. pic with the time showing, you got {grace} min",
                    f"ok {label}. snap me a pic of you doing it (time visible) — {grace} min",
                    f"{label} starts now bro. pic of it with the clock, {grace} min",
                ])
            else:
                body = pick([
                    f"still on {label}? quick pic with the clock 📸 {grace} min",
                    f"checking in — {label} going? send a pic, {grace} min",
                    f"pic check 👀 {label}, time in the shot. {grace} min",
                    f"you still on {label}? show me, {grace} min",
                ])
            if kind == "activity" and cid % 3 == 0:
                body += "\n(no face or logins in it)"
            shot = ("a screenshot of " + blocker + " running, with the taskbar clock in it"
                    if kind.startswith("blocker") else
                    "a photo of his bed, opened, ready to sleep in" if kind == "bed" else
                    "a photo of him actually doing it, with a clock or timestamp in shot")
            # 10 Oct, his words: "when he asks for photo, can he at least
            # give me specifically what it needs". Friday writes the message,
            # and sometimes writes a friendly one with the details rubbed off.
            # This line is not written by a model: it is the requirement.
            deadline = (due + timedelta(minutes=grace))
            needs = ("📸 needs: " + (
                f"screenshot of {blocker} running, taskbar clock visible"
                if kind.startswith("blocker") else
                "photo of your bed, opened, ready to sleep in" if kind == "bed" else
                f"photo of you doing \"{re.sub(r'(?i)^[ ]*work:[ ]*', '', p['label'])}\", "
                "with a clock or phone "
                "time in the shot")
                + f" — by {deadline:%H:%M}")
            if kind == "activity":
                needs += " (no face, no logins)"
            rider = (self._standup_rider(due, now)
                     if kind == "activity" and not starting else "")
            if rider:
                body += "\n" + rider
            if self._ignored_streak(due.date(), p["due_at"]):
                await self.send_user(f"{body}\n{needs}")
                self.ledger.append("checkpoint_asked",
                                   {"checkpoint": p["checkpoint_id"], "kind": kind})
                return
            body = await self._human(
                "asking him for the proof photo for a block that is starting now"
                if starting else
                "checking in mid-block: the next proof photo is due now",
                f"block: {p['label']}\ndue at: {due:%H:%M}\nhe must send: {shot}\n"
                f"he has {grace} minutes before it counts as missed\n"
                + ("this is the start of the block\n" if starting
                   else "he is already in this block, this is a later check\n")
                + ("no face, no logins, nothing showing where he lives\n"
                   if kind == "activity" else "")
                + (f"in the same message, also ask the 20-minute check: what exactly "
                   f"he's on right now (the precise problem/page), whether he's moving "
                   f"fast, and what he's stuck on. He answers in text.\n" if rider else "")
                + "the app prints the exact requirement under your message, so "
                  "don't list it yourself -- just say the thing like a person\n",
                body)
            await self.send_user(f"{body}\n{needs}")
            self.ledger.append("checkpoint_asked",
                               {"checkpoint": p["checkpoint_id"], "kind": kind})

        elif act.kind == "mark_missed":
            labels = p.get("labels") or [p.get("label", "your planned task")]
            what = ", ".join(html.escape(x) for x in labels)
            n = p.get("count", 1)
            prefix = (f"yo you missed {n} pics" if n > 1 else "yo where's the pic")
            # This used to say "Escalation has started" the moment the grace ran
            # out, while phone_alarm_after_min held the siren back for several
            # more minutes. So the message announced a siren that was not coming
            # yet, and the silence read as a broken system rather than a
            # deliberate wait. Say what is actually true, and when.
            wait = int(self.cfg.esc.get("phone_alarm_after_min", 0))
            when = ("siren's on now, send it and it stops 🔊"
                    if wait <= 0 else
                    f"siren starts in {wait} min if it doesn't come")
            cp = self.db.one("SELECT date, due_at FROM checkpoints WHERE id = ?",
                             (int(p.get("checkpoint_id") or 0),))
            if cp is not None and self._ignored_streak(
                    date.fromisoformat(cp["date"]), cp["due_at"]):
                await self.send_user(f"{prefix} for {what}? {when}")
                self.ledger.append("checkpoint_overdue",
                                   {"checkpoint": p["checkpoint_id"], "count": n})
                return
            await self.send_user(await self._human(
                "his time ran out and the photo never came -- the siren is on now"
                if wait <= 0 else "his time ran out and the photo never came",
                f"block{'s' if n > 1 else ''}: {what}\n"
                f"missed checks: {n}\n"
                + ("the phone siren is sounding right now and stops the moment a "
                   "valid photo lands\n" if wait <= 0 else
                   f"the siren starts in {wait} minutes unless the photo lands first\n"),
                f"{prefix} for {what}? {when}", now))
            self.ledger.append("checkpoint_overdue",
                               {"checkpoint": p["checkpoint_id"], "count": n})

        elif act.kind == "outage_amnesty":
            await self.send_user(
                f"🔌 my bad, server was down ~{p['minutes']} min. {p['excused']} checkpoints "
                f"in that gap are void, not on you.")
            await self.send_guardian(
                f"🔌 System was offline ~{p['minutes']} min; "
                f"{p['excused']} checkpoints voided. Not his doing.")

        elif act.kind == "phone_silent":
            app = self.cfg.esc.get("agent_app", "Automate")
            hours = int(p["minutes"]) // 60
            await self.send_user(await self._human(
                "his phone stopped checking in, so the siren can't sound at all "
                "-- he needs to open the app and start the flows again",
                f"silent for: {p['minutes']} minutes"
                + (f" (about {hours} hours)\n" if hours else "\n")
                + f"app to open: {app}\n"
                "what to do: open it and press Start on both flows\n"
                "checkpoints and photos carry on either way -- only the siren is dead\n",
                f"📵 phone's been quiet {p['minutes']} min so the siren's dead. "
                f"open {app} and hit Start on both flows 🙏", now))
            if p.get("escalate"):
                await self.send_guardian(
                    f"📵 {self.cfg.user_name}'s phone has been silent {p['minutes']} min "
                    "inside the window. Flat battery, a crash — or switched off.")

        elif act.kind == "bed_overdue":
            await self.send_user(await self._human(
                "it is past his bedtime and the siren wants a photo of his bed",
                f"minutes past bedtime: {p['minutes']}\n"
                "he must send: a photo of his bed, opened, ready to sleep in\n"
                "the siren stops when it lands\n"
                "if he is genuinely out or on a real deadline he can send "
                "/latenight <reason> for two more hours, once a week\n",
                f"🛏 bro it's {p['minutes']} min past bedtime. send a pic of your bed, "
                "opened, and the siren stops.", now))

        elif act.kind == "bed_missed":
            await self.send_user(
                f"🛏 no bed pic came, so <b>{p['date']}</b> goes down unfinished.\n\n"
                "just sleep now, fresh start tomorrow.")
            await self.send_guardian(
                f"🛏 No bed photo from {self.cfg.user_name} on {p['date']}. The siren "
                "ran from bedtime to the cutoff.")

        elif act.kind == "plan_missing":
            await self.send_user(
                "⚠️ <b>bro, window's open and there's no plan for today.</b>\n\n"
                "siren runs till you send one. " + PLAN_HELP)

        elif act.kind == "plan_nag":
            await self.send_user(await self._human(
                "tomorrow's plan still isn't written and the deadline for it is coming",
                f"the plan is for: {p['date']}\n"
                f"due by: {self.cfg.plan_due:%H:%M} tonight\n"
                "he writes it with /plan, one block per line\n",
                f"🗓 yo, plan for {p['date']} isn't written yet. "
                f"need it by {self.cfg.plan_due:%H:%M} — /plan", now))

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
                "🏁 <b>you made it — the commitment period is done.</b>\n\n"
                "it keeps running till you decide. your dad can /extend 3, "
                "or you can /disable.")
            await self.send_guardian(
                f"🏁 {self.cfg.user_name}'s commitment period has ended.\n\n"
                "<code>/extend 3</code> to carry on, or let him stop.")

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
        # The photo count says he showed up. The goals say whether it was for
        # anything. A report with only the first number can look like a good
        # day that produced nothing.
        goals = self.db.goals(day)
        if goals:
            hit = sum(1 for g in goals if g["done"])
            lines.append(f"\n<b>Goals {hit}/{len(goals)}</b>")
            lines += [f"{'✅' if g['done'] else '▫️'} {html.escape(g['text'])}"
                      for g in goals]
        else:
            lines.append("\n▫️ No goals were set for this day.")
        if self.standups.enabled:
            done, asked = self.standups.score(day)
            if asked:
                lines.append(f"20-min checks answered {done}/{asked}")
        if self.foundations.enabled:
            rows = self.foundations.for_day(day)
            hit = sum(1 for r in rows if r["done_at"])
            lines.append(f"Basics {hit}/{len(rows)}: " + ", ".join(
                ("✅ " if r["done_at"] else "▫️ ") + html.escape(r["text"]) for r in rows))
        if row and row["offsets_used"]:
            lines.append(f"Offset used: {html.escape(row['offset_reason'] or '')}")
        if not (row and row["plan_locked"]):
            lines.append("⚠️ No plan was submitted for this day.")
        if self.db.lock()["blocked_since"]:
            lines.append("🚫 Bot is currently blocked on Telegram.")
        # The block policy is the one part of this he sets himself, so the guard
        # is not preventing a change but making one visible: loosen an allowlist
        # and the fingerprint stops matching the one his father read yesterday.
        pol = self.cfg.vision.get("blocks") or {}
        if pol:
            lines.append(f"Blocks: {len(pol)} · <code>{vision.policy_fingerprint(pol)}</code>")
        lines.append(f"Log: {'intact' if ok else f'⚠️ BROKEN at event {bad}'} · <code>{tip[:12]}</code>")
        return "\n".join(lines)

    # ======================================================================
    def build(self) -> Application:
        app = Application.builder().token(self.cfg.bot_token).build()
        self.app = app
        for name, fn in [
            ("start", self.cmd_start), ("help", self.cmd_help), ("status", self.cmd_status),
            ("plan", self.cmd_plan), ("today", self.cmd_today), ("offset", self.cmd_offset),
            ("goals", self.cmd_goals), ("done", self.cmd_done),
            ("latenight", self.cmd_latenight), ("outside", self.cmd_outside),
            ("steps", self.cmd_steps),
            ("tasks", self.cmd_tasks), ("task", self.cmd_task),
            ("doc", self.cmd_doc), ("today2", self.cmd_doc),
            ("eat", self.cmd_eat), ("food", self.cmd_eat),
            ("step", self.cmd_step), ("steps2", self.cmd_step),
            ("events", self.cmd_reminders),
            ("night", self.cmd_night), ("basics", self.cmd_night),
            ("standup", self.cmd_standup), ("paper", self.cmd_paper),
            ("plan2", self.cmd_plandays), ("plandays", self.cmd_plandays),
            ("forecast", self.cmd_forecast),
            ("disable", self.cmd_disable), ("enable", self.cmd_enable),
            ("cancel", self.cmd_cancel), ("commit", self.cmd_commit),
            ("setguardian", self.cmd_setguardian),
            ("testsiren", self.cmd_testsiren), ("stopsiren", self.cmd_stopsiren),
            ("mute", self.cmd_mute), ("unmute", self.cmd_unmute),
            ("more", self.cmd_more), ("replan", self.cmd_replan),
            ("stopbot", self.cmd_stopbot), ("startbot", self.cmd_startbot),
            ("stopnow", self.cmd_stopnow), ("cancelstop", self.cmd_cancelstop),
            ("nevermind", self.cmd_nevermind), ("why", self.cmd_why),
            ("logs", self.cmd_logs), ("restart", self.cmd_restart),
            ("remind", self.cmd_remind), ("reminders", self.cmd_reminders),
            ("forget", self.cmd_forget),
            ("unblock", self.cmd_unblock), ("block", self.cmd_block),
            ("pause", self.cmd_pause),
            ("resume", self.cmd_resume), ("extend", self.cmd_extend), ("report", self.cmd_report),
        ]:
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.job_queue.run_repeating(self.tick, interval=30, first=10, name="tick")
        return app


class _Reply:
    """Lets code that expects an Update talk back through any channel."""
    """Lets a button press reuse the code paths written for a message."""

    def __init__(self, q, chat_id, send=None):
        self._q = q
        self._send = send              # for messages the bot starts itself
        self.effective_chat = type("C", (), {"id": chat_id})()
        self.message = self

    async def reply_text(self, text, **kw):
        if self._q is None:
            # send_user already sends as HTML; passing it again is a TypeError.
            kw.pop("parse_mode", None)
            await self._send(text, **kw)
            return
        await self._q.message.reply_text(text, **kw)

    @property
    def chat(self):
        if self._q is None:
            async def _noop(*_a, **_k):
                return None
            return type("C", (), {"send_action": staticmethod(_noop)})()
        return self._q.message.chat
