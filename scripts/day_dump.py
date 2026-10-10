"""Print one day of the live record: chat, plan, checkpoints, events.

Read-only. Run inside the container, where the database lives:

    railway ssh -- python /app/scripts/day_dump.py 2026-10-10

Exists because on 10 Oct 2026 the volume's file transfer timed out all day,
and every bug report became a guess. A guess is how two fixes that day were
explained wrongly. Reading the record is cheaper than guessing.
"""
import json
import os
import sqlite3
import sys

db = os.environ.get("LOCKSTEP_DB", "/data/lockstep.db")
day = sys.argv[1] if len(sys.argv) > 1 else None
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
if day is None:
    day = con.execute("SELECT MAX(date) FROM days").fetchone()[0]


def show(title, rows, fmt):
    print(f"\n===== {title} =====")
    for r in rows:
        print(fmt(r))


rows = []
for r in con.execute("SELECT ts, role, text FROM coach_log WHERE ts LIKE ? ORDER BY id",
                     (day + "%",)):
    who = "YOU " if r["role"] == "user" else "FRI "
    rows.append((r["ts"][11:19], who, (r["text"] or "").replace("\n", " / ")[:600]))
for r in con.execute("SELECT ts, kind, payload FROM events WHERE ts LIKE ? ORDER BY id",
                     (day + "%",)):
    p = r["payload"] or ""
    try:
        p = json.dumps(json.loads(p), ensure_ascii=False)
    except ValueError:
        pass
    rows.append((r["ts"][11:19], "EV  ", f"{r['kind']} {p[:300]}"))
rows.sort()
show(f"{day} -- chat and events", rows, lambda r: f"{r[0]} {r[1]}| {r[2]}")

show("plan (blocks)", con.execute(
    "SELECT id, start_min, end_min, label, category FROM blocks WHERE date=? ORDER BY start_min",
    (day,)),
    lambda b: f"  #{b['id']:<4} {b['start_min']//60:02d}:{b['start_min']%60:02d}-"
              f"{b['end_min']//60:02d}:{b['end_min']%60:02d}  {b['label']}  [{b['category']}]")

show("checkpoints", con.execute(
    "SELECT c.id, c.due_at, c.kind, c.state, c.block_id, b.label, c.attempts, c.verdict "
    "FROM checkpoints c LEFT JOIN blocks b ON b.id=c.block_id WHERE c.date=? ORDER BY c.due_at",
    (day,)),
    lambda c: f"  #{c['id']:<5} {c['due_at'][11:16]} {c['kind']:<15} {c['state']:<10} "
              f"blk={c['block_id']} {(c['label'] or '-')[:45]}"
              + (f"  verdict={json.loads(c['verdict']).get('failure_code')}:"
                 f"{json.loads(c['verdict']).get('reason','')[:90]}"
                 if c["verdict"] else ""))

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
