"""Print one day of the live record: chat, plan, checkpoints, events.

Read-only. Run inside the container, where the database lives:

    railway ssh -- python /app/scripts/day_dump.py 2026-10-10

Exists because on 10 Oct 2026 the volume's file transfer timed out all day,
and every bug report became a guess. A guess is how two fixes that day were
explained wrongly. Reading the record is cheaper than guessing.

Events are written in UTC and the chat in local time. Everything printed here
is local (Asia/Almaty), so the two interleave in the order they happened.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("LOCKSTEP_TZ", "Asia/Almaty"))
UTC = ZoneInfo("UTC")
db = os.environ.get("LOCKSTEP_DB", "/data/lockstep.db")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
day = sys.argv[1] if len(sys.argv) > 1 else \
    con.execute("SELECT MAX(date) FROM days").fetchone()[0]


def local(ts: str) -> str:
    d = datetime.fromisoformat(ts)
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(TZ).strftime("%H:%M:%S")


def show(title, rows, fmt):
    print(f"\n===== {title} =====")
    for r in rows:
        print(fmt(r))


lo = datetime.fromisoformat(day).replace(tzinfo=TZ)
hi = lo + timedelta(days=1)
rows = []
for r in con.execute("SELECT ts, role, text FROM coach_log WHERE date = ? ORDER BY id", (day,)):
    who = "YOU " if r["role"] == "user" else "FRI "
    rows.append((local(r["ts"]), who, (r["text"] or "").replace("\n", " / ")[:600]))
for r in con.execute("SELECT ts, kind, payload FROM events ORDER BY id"):
    d = datetime.fromisoformat(r["ts"])
    d = d if d.tzinfo else d.replace(tzinfo=UTC)
    if not lo <= d < hi:
        continue
    p = r["payload"] or ""
    try:
        p = json.dumps(json.loads(p), ensure_ascii=False)
    except ValueError:
        pass
    rows.append((local(r["ts"]), "EV  ", f"{r['kind']} {p[:300]}"))
rows.sort()
show(f"{day} -- chat and events (local time)", rows, lambda r: f"{r[0]} {r[1]}| {r[2]}")

show("plan (blocks)", con.execute(
    "SELECT id, start_min, end_min, label, category FROM blocks WHERE date=? ORDER BY start_min",
    (day,)),
    lambda b: f"  #{b['id']:<4} {b['start_min']//60:02d}:{b['start_min']%60:02d}-"
              f"{b['end_min']//60:02d}:{b['end_min']%60:02d}  {b['label']}  [{b['category']}]")


def verdict(c):
    if not c["verdict"]:
        return ""
    v = json.loads(c["verdict"])
    return f"  verdict={v.get('failure_code')}:{v.get('reason', '')[:90]}"


show("checkpoints", con.execute(
    "SELECT c.id, c.due_at, c.kind, c.state, c.block_id, b.label, c.verdict "
    "FROM checkpoints c LEFT JOIN blocks b ON b.id=c.block_id WHERE c.date=? ORDER BY c.due_at",
    (day,)),
    lambda c: f"  #{c['id']:<5} {c['due_at'][11:16]} {c['kind']:<15} {c['state']:<10} "
              f"blk={c['block_id']} {(c['label'] or '-')[:45]}" + verdict(c))

show("proposals", con.execute(
    "SELECT id, created_at, used_at, text FROM proposals WHERE date=? ORDER BY id", (day,)),
    lambda p: f"  #{p['id']} made {p['created_at'][11:16]} used {(p['used_at'] or '')[11:16]}: "
              + p["text"].replace("\n", " | ")[:400])

table = next((t for (t,) in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'lock%'")), None)
if table:
    lock = con.execute(f"SELECT * FROM {table}").fetchone()
    print(f"\n===== {table} =====")
    print({k: lock[k] for k in lock.keys()} if lock else "(empty)")
