"""Live Gemini check. Costs quota and needs network, so it is opt-in:

    .venv/bin/python tests/test_vision_live.py

Not part of run-tests.sh. Run it after changing the prompt in vision.py, or
after switching models — those are the two things that quietly change verdicts.
"""
import asyncio, os, sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import check, report
from server.config import Config
from server.vision import Validator

IMG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures_img")

# (file, committed task, category, expect_ok, expect_code, why)
CASES = [
    ("work_ok.jpg", "work: linear algebra pset ch3", "work", True, "ok",
     "matching work with a fresh visible stamp"),
    ("nothing.jpg", "work: linear algebra pset ch3", "work", False, "mismatch",
     "a dark frame proves nothing"),
    ("work_nostamp.jpg", "work: linear algebra pset ch3", "work", False, "no_timestamp",
     "right activity, no clock in frame"),
    ("work_stale.jpg", "work: linear algebra pset ch3", "work", False, "stale",
     "right activity, clock three hours old"),
    ("gym_ok.jpg", "work: linear algebra pset ch3", "work", False, "mismatch",
     "gym photo submitted for a work block"),
    # Note: gym_ok.jpg fails even as 'gym', because the model correctly spots it
    # as a drawing rather than a photograph. That is desirable behaviour, so it
    # is asserted here rather than worked around.
    ("gym_ok.jpg", "gym", "sport", False, "mismatch",
     "synthetic illustration is not accepted as a real photo"),
]


async def main():
    if not os.path.exists(IMG):
        print("Run tests/make_test_images.py first."); return 1
    cfg = Config.load()
    v = Validator(cfg)
    now = cfg.now()
    print(f"model: {cfg.raw['gemini']['model']}\n")
    for fn, label, cat, want_ok, want_code, why in CASES:
        blob = open(os.path.join(IMG, fn), "rb").read()
        r = await v.check(blob, label, cat, now)
        ok = check(f"{fn:18} — {why}", (r.ok, r.failure_code), (want_ok, want_code))
        if not ok:
            print(f"        saw:  {r.activity_seen[:100]}")
            print(f"        said: {r.reason[:100]}")
    return report("LIVE VISION TESTS")

sys.exit(asyncio.run(main()))
