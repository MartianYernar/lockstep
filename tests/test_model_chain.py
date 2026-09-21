"""Walking the model chain when models fail in the ways they really fail.

17 Sep, 18:24: the coach's first model was out of daily quota (429), and the
next one answered 400 INVALID_ARGUMENT because it refuses thinking_budget=0.
The 400 was raised instead of skipped, so every message he sent got "can't
think straight". Vision had the same flaw one model later. These tests replay
exactly that with a fake client.
"""
import asyncio
import copy
import os
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from server.coach import Coach
from server.vision import Validator

run = asyncio.get_event_loop().run_until_complete
NOW = datetime(2026, 9, 17, 18, 30, tzinfo=ZoneInfo("Asia/Almaty"))


class FakeModels:
    """Behaves like the real models did on 17 Sep."""
    def __init__(self, rules):
        self.rules = rules            # model -> "quota" | "no-thinking" | "ok" | "busy"
        self.calls = []

    async def generate_content(self, model, contents, config):
        thinking = getattr(config, "thinking_config", None) is not None
        self.calls.append((model, thinking))
        rule = self.rules.get(model, "ok")
        if rule == "quota":
            raise RuntimeError("429 RESOURCE_EXHAUSTED. quota")
        if rule == "busy":
            raise RuntimeError("503 UNAVAILABLE. busy")
        if rule == "no-thinking" and thinking:
            raise RuntimeError("400 INVALID_ARGUMENT. {'error': 'Request contains an invalid argument.'}")
        if rule == "broken":
            raise RuntimeError("401 UNAUTHENTICATED")
        text = ('{"ok": true, "reason": "fine", "confidence": 0.9, "failure_code": "ok"}'
                if getattr(config, "response_schema", None) is not None and "vision" in model
                else "hey")
        return types.SimpleNamespace(text=text)


def client(rules):
    fm = FakeModels(rules)
    return types.SimpleNamespace(aio=types.SimpleNamespace(models=fm)), fm


CHAIN = ["m-first", "m-second", "m-third", "m-last"]

print("=== the coach, replaying 17 Sep ===")
raw = copy.deepcopy(RAW)
raw["gemini"] = dict(raw["gemini"], models=CHAIN)
cfg, db, led, eng, lk = build(raw, now=NOW)
coach = Coach(cfg, db, eng)
# coach walks the chain backwards: m-last, m-third, m-second, m-first
coach._client, fm = client({"m-last": "quota", "m-third": "no-thinking",
                             "m-second": "no-thinking", "m-first": "ok"})
out = run(coach._ask("sys", ["hi"], NOW))
check("an answer comes back", out, "hey")
check("the refused setting is retried on the same model, without it",
      fm.calls[:3], [("m-last", True), ("m-third", True), ("m-third", False)])
check("one allowance spent, not four", coach.spent(NOW.date()), 1)

coach._client, fm = client({"m-last": "quota", "m-third": "busy",
                             "m-second": "no-thinking", "m-first": "quota"})
out = run(coach._ask("sys", ["hi"], NOW))
check("busy and quota are both skipped", out, "hey")
check("and it lands on the model that only needed thinking off",
      fm.calls[-1], ("m-second", False))

coach._client, fm = client({m: "quota" for m in CHAIN})
try:
    run(coach._ask("sys", ["hi"], NOW))
    check("everything out of quota raises", False, True)
except RuntimeError as exc:
    check("everything out of quota raises", "429" in str(exc), True)

coach._client, fm = client({"m-last": "broken"})
try:
    run(coach._ask("sys", ["hi"], NOW))
    check("a real error is not swallowed", False, True)
except RuntimeError as exc:
    check("a real error is not swallowed", "401" in str(exc), True)

print("\n=== thinking where it pays, cheap where it doesn't ===")
# Chat, the replan judge and the planner are where Friday has to understand
# him: the strongest model, with room to think. Worded nags don't need it, and
# they never touch the strongest model, so they can't use up its quota.
raw = copy.deepcopy(RAW)
raw["gemini"] = dict(raw["gemini"], models=CHAIN)
cfg, db, led, eng, lk = build(raw, now=NOW)
coach = Coach(cfg, db, eng)
check("smart starts at the strongest model", coach._models("smart")[0], "m-last")
check("cheap never touches it", "m-last" in coach._models("cheap"), False)
coach._client, fm = client({})
run(coach._ask("sys", ["hi"], NOW))
check("smart thinks", fm.calls[-1], ("m-last", True))
coach._client, fm = client({})
run(coach._ask("sys", ["hi"], NOW, tier="cheap"))
check("cheap asks the next model down", fm.calls[-1][0], "m-third")

tiers = []


async def spy(system, contents, now, max_tokens=400, schema=None, tier="smart"):
    tiers.append(tier)
    return '{"reply": "ok", "actions": []}'
coach._ask = spy
run(coach.say("nag", "facts", NOW))
run(coach.reply("yo", NOW))
check("nags are cheap, chat is smart", tiers, ["cheap", "smart"])

print("\n=== vision, one model later ===")
raw = copy.deepcopy(RAW)
raw["gemini"] = dict(raw["gemini"], models=["vision-a", "vision-b", "vision-c"],
                     thinking_budget=0)
cfg, db, led, eng, lk = build(raw, now=NOW)
v = Validator(cfg)
v._client, fm = client({"vision-a": "quota", "vision-b": "no-thinking"})
v._model_chain = lambda: ["vision-a", "vision-b", "vision-c"]
data = run(v._ask(b"img", "image/jpeg", "prompt"))
check("the photo check still gets an answer", data.get("ok"), True)
check("from the second model, thinking off",
      (v.last_model, fm.calls[-1]), ("vision-b", ("vision-b", False)))

print("\n=== what he's told when it all fails ===")
from test_friend_fakes import FakeUpdate, FakeBot, StubValidator  # noqa: E402
from server.bot import LockstepBot  # noqa: E402
cfg, db, led, eng, lk = build(copy.deepcopy(RAW), now=NOW)
bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
bot.app = types.SimpleNamespace(bot=FakeBot())
bot.coach._client, fm = client({m: "quota" for m in bot.coach._models()})
u = FakeUpdate("bro")
run(bot.on_text(u, None))
check("says the AI is maxed out, not 'can't think straight'",
      "maxed out" in u.message.last and "think straight" not in u.message.last, True)
check("and that photos still work", "photos still get checked" in u.message.last, True)

sys.exit(report("MODEL CHAIN TESTS"))
