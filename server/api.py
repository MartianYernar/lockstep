"""Tiny HTTP surface for the enforcement agents.

The laptop agent and the phone both poll GET /agent/state and act on what comes
back. All the rules live on the server, so the agents stay dumb -- which is the
point: a dumb agent has nothing in it worth tampering with, and rewriting one
buys you nothing because the server still knows.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiohttp import web

log = logging.getLogger("api")


def _authorised(request: web.Request, token: str) -> bool:
    header = request.headers.get("X-Lockstep-Token")
    if header and header == token:
        return True
    # Tasker and Shortcuts make query strings much easier than headers.
    return request.query.get("token") == token


def _note_battery(request: web.Request, engine, now) -> None:
    """Record what the phone says about its own power, if it says anything.

    The agent dies when the phone does, and this phone has been at 2% more than
    once. Optional query params so an agent that doesn't send them keeps working
    exactly as before: ?batt=37&chg=1
    """
    batt, chg = request.query.get("batt"), request.query.get("chg")
    if batt is None and chg is None:
        return
    fields = {"phone_batt_at": now.isoformat()}
    try:
        if batt is not None:
            fields["phone_battery"] = max(0, min(100, int(float(batt))))
        if chg is not None:
            fields["phone_charging"] = 1 if str(chg).strip().lower() in (
                "1", "true", "yes", "charging", "ac", "usb") else 0
    except (TypeError, ValueError):
        return                                # a malformed reading is no reading
    engine.db.set_lock(**fields)


def _silent_min(cfg, lock) -> int | None:
    last = lock["last_agent_poll"]
    if not last:
        return None
    try:
        return int((cfg.now() - datetime.fromisoformat(last)).total_seconds() // 60)
    except (TypeError, ValueError):
        return None


def _open_tasks(db) -> int | None:
    """None rather than a 500: the table only exists once the coach has run."""
    try:
        return db.one("SELECT COUNT(*) c FROM tasks WHERE status='open'")["c"]
    except Exception:                                         # noqa: BLE001
        return None


BAD_TOKEN = {"n": 0, "last": None}


@web.middleware
async def _cors(request, handler):
    """The drill app runs on vercel.app and on localhost; it has to be able to
    report a score from either."""
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Lockstep-Token"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def build_app(cfg, engine, ledger) -> web.Application:
    token = cfg.api["token"]
    app = web.Application(middlewares=[_cors])

    async def health(_request: web.Request) -> web.Response:
        """Unauthenticated, so it says what is switched on and nothing more.

        10 Oct 2026: after a rebuild there was no way to tell from outside
        whether the container had picked up the new config, and the volume's
        SFTP was timing out. These booleans answer that in one curl and give
        away nothing -- no ids, no paths, no tokens.
        """
        ok, bad = ledger.verify()
        lock = engine.db.lock()
        return web.json_response({
            "status": "ok",
            "time": cfg.now().isoformat(),
            "ledger_intact": ok,
            "ledger_break_at": bad,
            "armed": bool(lock["enabled"]),
            "guardian_registered": bool(lock["guardian_chat_id"]),
            "commitment_end": lock["commitment_end"],
            "daily_doc": bool((cfg.raw.get("daily_doc") or {}).get("enabled")
                              and (cfg.raw.get("daily_doc") or {}).get("url")),
            "open_tasks": _open_tasks(engine.db),
            # The siren lives on the phone. If this grows, the phone has
            # stopped polling and nothing can make a noise, whatever the
            # server thinks is owed.
            "phone_silent_min": _silent_min(cfg, lock),
            # Something is polling with the wrong token: the phone is alive
            # but talking to a door that will not open.
            "bad_token_polls": BAD_TOKEN["n"],
            "owed_now": engine.agent_state(cfg.now()).debt_minutes,
            "drills_owed": engine.penalty_owed(),
        })

    async def state(request: web.Request) -> web.Response:
        if not _authorised(request, token):
            return web.json_response({"error": "unauthorised"}, status=401)
        # Every poll is a heartbeat. A phone that stops asking is a phone that
        # can no longer nag you, which looks exactly like compliance.
        cfg_now = cfg.now()
        engine.db.set_lock(last_agent_poll=cfg_now.isoformat())
        _note_battery(request, engine, cfg_now)
        st = engine.agent_state(cfg_now)
        return web.json_response(st.as_dict())

    async def ack(request: web.Request) -> web.Response:
        """Agents report what they actually did, so the log reflects reality."""
        if not _authorised(request, token):
            return web.json_response({"error": "unauthorised"}, status=401)
        try:
            body = await request.json()
        except Exception:                                     # noqa: BLE001
            body = {}
        ledger.append("agent_ack", {
            "agent": body.get("agent", "unknown"),
            "opened": body.get("opened"),
            "closed": body.get("closed"),
            "level": body.get("level"),
        })
        return web.json_response({"ok": True})

    async def siren(request: web.Request) -> web.Response:
        """The same answer as /agent/state, in one line of plain text.

        The JSON route needs the phone to parse a body into a dictionary and
        then test a field. That is four steps and it failed silently on the
        real phone: MacroDroid's JSON Parse hands back the string "true", the
        If compares it against a boolean, no match, no siren, no error. A macro
        that fetches correctly and then does nothing is indistinguishable from
        a working one until the night you need it.

        This is a substring test instead. "ALARM 85 10" or "QUIET".
        """
        if not _authorised(request, token):
            log.warning("siren: unauthorised poll from %s", request.remote)
            return web.Response(status=401, text="UNAUTHORISED")
        now = cfg.now()
        engine.db.set_lock(last_agent_poll=now.isoformat())
        _note_battery(request, engine, now)
        st = engine.agent_state(now).as_dict()
        body = (f"ALARM {int(st['alarm_volume_pct'])} {int(st['alarm_repeats'])}"
                if st["phone_alarm"] else "QUIET")
        return web.Response(text=body, content_type="text/plain")

    async def gap(request: web.Request) -> web.Response:
        """One integer: how many seconds until the phone should sound again.

        "0" means nothing is owed. Anything else is both the instruction to
        make a noise and the wait before the next one, so falling further
        behind simply shortens the gap.

        The escalation therefore lives entirely on the server. The phone holds
        no ladder, no thresholds and no arithmetic -- it reads a number and
        sleeps for it. That is the same reason the agents were kept dumb in the
        first place: a dumb agent has nothing worth tampering with, and one
        that does no parsing has nothing to get wrong. The JSON version failed
        for exactly that reason, silently, for a week.
        """
        if not _authorised(request, token):
            # NOT "0". 10 Oct 2026 a wrong token answered "0", which the phone
            # reads as "nothing owed", so one mistyped character disabled the
            # siren permanently and silently -- the worst failure this endpoint
            # has, because everything looks fine from both ends. A refusal has
            # to fail the HTTP request instead: the flow's failure path then
            # treats it exactly like being offline, which sounds the alarm
            # inside the window.
            BAD_TOKEN["n"] += 1
            BAD_TOKEN["last"] = cfg.now().isoformat()
            log.warning("gap: unauthorised poll from %s — wrong or missing token",
                        request.remote)
            return web.Response(status=401, text="UNAUTHORISED")
        now = cfg.now()
        engine.db.set_lock(last_agent_poll=now.isoformat())
        _note_battery(request, engine, now)
        # "-1" means: nothing owed, AND if you lose me shortly, stay quiet.
        #
        # The phone cannot ask anything once it is offline, which is exactly
        # when the fail-closed siren fires. So the answer has to be handed over
        # in advance and remembered. The agent keeps the last thing it was told
        # and reads it again on the failure path; a minus sign is the whole
        # protocol, because the last time this needed parsing it silently did
        # nothing for a week.
        #
        # Bounded to the enforcement window on purpose: bedtime and the missing
        # plan are both outside it, so an afternoon walk can never buy silence
        # at 22:30.
        # "-1" also covers a quiet window, and the half hour before it: the
        # phone caches the last answer and re-reads it when it loses the
        # network, so the answer that keeps it silent in an exam hall has to
        # be handed over while he is still on wifi at home.
        if engine.quiet_soon(now) or (engine.outside_active(now) and engine.in_window(now)):
            return web.Response(text="-1", content_type="text/plain")
        st = engine.agent_state(now).as_dict()
        if not st["phone_alarm"]:
            return web.Response(text="0", content_type="text/plain")
        # alarm_repeats is "times per minute", so the gap is a minute divided
        # by it. Floored at 3s: below that the tone has no room to finish and
        # it stops sounding like an alarm and starts sounding like a fault.
        repeats = max(1, int(st["alarm_repeats"]))
        gap_s = max(3, 60 // repeats)
        # Every minute in which the server asks for noise goes on the record.
        # 18 Sep 02:00 the siren ran for an hour and there was no way to tell
        # afterwards whether the server had asked for it -- it had not, but
        # proving that took a database download and a replay. Now the ledger
        # says, so a 2am siren is one query away from an answer.
        if engine.db.claim(f"siren_told:{now:%Y-%m-%dT%H:%M}"):
            ledger.append("siren_told", {"gap_sec": gap_s,
                                         "why": st.get("headline", "")[:80],
                                         "debt_min": st.get("debt_minutes", 0)})
        return web.Response(text=str(gap_s), content_type="text/plain")

    async def sat(request: web.Request) -> web.Response:
        """His drill app reports a result here, and a penalty is paid.

        Its own token (api.sat_token), because this one lives in a browser he
        can open anywhere: it can pay a penalty and do nothing else -- it
        cannot read his day, mark a photo, or tell the server his phone is
        alive.

        A session id is spent once. Without that, one good 9/10 screenshot
        would clear every penalty he ever earns.
        """
        token = cfg.api.get("sat_token") or cfg.api["token"]
        if not _authorised(request, token):
            log.warning("sat: unauthorised result from %s", request.remote)
            return web.json_response({"error": "unauthorised"}, status=401)
        try:
            body = await request.json()
        except Exception:                                     # noqa: BLE001
            body = dict(request.query)
        from .penalty import Penalties
        pen = Penalties(engine.db, cfg)
        if not pen.enabled:
            return web.json_response({"ok": False, "error": "penalties are off"})
        session = str(body.get("session") or "").strip()[:80]
        try:
            score = int(body.get("score"))
            total = int(body.get("total"))
            seconds = int(body.get("seconds") or 0)
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "need score and total"},
                                     status=400)
        if not session:
            return web.json_response({"ok": False, "error": "need a session id"},
                                     status=400)
        if pen.seen_session(session):
            return web.json_response({"ok": False, "error": "that drill was already sent"})
        now = cfg.now()
        passed, why = pen.judge(score, total, seconds)
        row = pen.next_one()
        pen.record(session, score, total, seconds, passed, now,
                   row["id"] if row and passed else None)
        if not passed:
            if row:
                pen.bump(row["id"])
            ledger.append("drill_failed", {"score": score, "total": total,
                                           "seconds": seconds, "why": why})
            return web.json_response({"ok": False, "cleared": False, "reason": why,
                                      "owed": pen.owed()})
        if row is None:
            ledger.append("drill_done", {"score": score, "total": total, "owed": 0})
            return web.json_response({"ok": True, "cleared": False,
                                      "reason": "nothing was owed — nice anyway",
                                      "owed": 0})
        pen.pay(row["id"], now, {"result": why, "session": session})
        ledger.append("penalty_paid", {"id": row["id"], "result": why,
                                       "reason": row["reason"]})
        left = pen.owed()
        cb = getattr(cfg, "on_penalty_paid", None)
        if cb is not None:
            try:
                await cb(why, left, now)
            except Exception as exc:                          # noqa: BLE001
                log.warning("could not announce the drill: %s", exc)
        return web.json_response({"ok": True, "cleared": True, "result": why,
                                  "owed": left})

    async def reps(request: web.Request) -> web.Response:
        """Push-ups, counted by the phone, paying off the same penalty."""
        token = cfg.api.get("sat_token") or cfg.api["token"]
        if not _authorised(request, token):
            log.warning("reps: unauthorised result from %s", request.remote)
            return web.json_response({"error": "unauthorised"}, status=401)
        try:
            body = await request.json()
        except Exception:                                     # noqa: BLE001
            body = dict(request.query)
        from .penalty import Penalties
        pen = Penalties(engine.db, cfg)
        if not pen.enabled or not pen.pushups_on():
            return web.json_response({"ok": False, "error": "push-ups are off"})
        session = str(body.get("session") or "").strip()[:80]
        try:
            count = int(body.get("reps"))
            seconds = float(body.get("seconds") or 0)
            motion = body.get("motion")
            motion = float(motion) if motion not in (None, "") else None
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "need reps and seconds"},
                                     status=400)
        if not session:
            return web.json_response({"ok": False, "error": "need a session id"},
                                     status=400)
        if pen.seen_session(session):
            return web.json_response({"ok": False, "error": "that set was already sent"})
        now = cfg.now()
        passed, why = pen.judge_reps(count, seconds, motion)
        row = pen.next_one()
        pen.record(session, count, pen.reps(), int(seconds), passed, now,
                   row["id"] if row and passed else None)
        if not passed:
            if row:
                pen.bump(row["id"])
            ledger.append("reps_failed", {"reps": count, "seconds": int(seconds),
                                          "motion": motion, "why": why})
            return web.json_response({"ok": False, "cleared": False, "reason": why,
                                      "owed": pen.owed()})
        if row is None:
            ledger.append("reps_done", {"reps": count, "owed": 0})
            return web.json_response({"ok": True, "cleared": False,
                                      "reason": "nothing was owed — nice anyway",
                                      "owed": 0})
        pen.pay(row["id"], now, {"result": why, "session": session})
        ledger.append("penalty_paid", {"id": row["id"], "result": why,
                                       "reason": row["reason"], "how": "pushups"})
        left = pen.owed()
        cb = getattr(cfg, "on_penalty_paid", None)
        if cb is not None:
            try:
                await cb(why, left, now)
            except Exception as exc:                          # noqa: BLE001
                log.warning("could not announce the set: %s", exc)
        return web.json_response({"ok": True, "cleared": True, "result": why,
                                  "owed": left})

    app.router.add_post("/agent/reps", reps)
    app.router.add_get("/agent/reps", reps)
    app.router.add_post("/agent/sat", sat)
    app.router.add_get("/agent/sat", sat)
    app.router.add_get("/health", health)
    app.router.add_get("/agent/state", state)
    app.router.add_get("/agent/siren", siren)
    app.router.add_get("/agent/gap", gap)
    app.router.add_post("/agent/ack", ack)
    return app


async def heartbeat_loop(cfg, ledger) -> None:
    """Ping an outside service on a schedule, so its silence is the alarm.

    Everything else in this project can be switched off by whoever holds the
    server. This cannot: the alerting lives somewhere else entirely, and the
    signal is the ABSENCE of a ping. Deleting the box, revoking the token,
    pulling the plug -- every one of those makes the monitor fire rather than
    go quiet, because there is nothing to suppress. You cannot delete your way
    out of a thing that reacts to deletion.

    Point it at a free healthchecks.io or UptimeRobot check whose account
    belongs to the guardian, not to you.
    """
    conf = cfg.raw.get("heartbeat", {})
    url = conf.get("url")
    if not url:
        return
    every = int(conf.get("every_sec", 300))
    log.info("heartbeat -> %s every %ds", url.split("/")[2], every)
    import aiohttp
    while True:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status >= 400:
                        log.warning("heartbeat rejected: %s", r.status)
        except Exception as exc:                              # noqa: BLE001
            log.warning("heartbeat failed: %s", exc)
        await asyncio.sleep(every)


async def start(cfg, engine, ledger) -> web.AppRunner:
    app = build_app(cfg, engine, ledger)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.api["host"], cfg.api_port)
    await site.start()
    log.info("agent API listening on %s:%s", cfg.api["host"], cfg.api_port)
    return runner
