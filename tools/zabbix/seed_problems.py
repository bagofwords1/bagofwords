#!/usr/bin/env python3
"""Seed a realistic event/problem stream from the fleet triggers (SQL-based)."""
import csv
import io
import random
import subprocess
import time

DB = "zabbix-zabbix-db-1"
random.seed(7)
NOW = int(time.time())
DAYS = 7
START = NOW - DAYS * 86400


def q(sql):
    return subprocess.run(["docker", "exec", "-i", DB, "psql", "-U", "zabbix",
                           "-d", "zabbix", "-tAF,", "-c", sql],
                          text=True, capture_output=True).stdout.strip()


def copy(table, cols, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow(["" if x is None else x for x in r])
    p = subprocess.run(["docker", "exec", "-i", DB, "psql", "-U", "zabbix", "-d", "zabbix",
                        "-c", f"COPY {table} ({','.join(cols)}) FROM STDIN WITH (FORMAT csv, NULL '')"],
                       input=buf.getvalue(), text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr[:600])


# my triggers with host + priority
rows = q("""
SELECT t.triggerid, t.priority, h.host
FROM triggers t
JOIN functions f ON f.triggerid=t.triggerid
JOIN items i ON i.itemid=f.itemid
JOIN hosts h ON h.hostid=i.hostid
WHERE h.host ~ '^(web|app|db|cache|lb)-[0-9]+$'
GROUP BY t.triggerid, t.priority, h.host
""").splitlines()
trigs = []
for line in rows:
    tid, prio, host = line.split(",")
    trigs.append((int(tid), int(prio), host))
print(f"{len(trigs)} fleet triggers")

ev_base = int(q("SELECT COALESCE(MAX(eventid),0) FROM events")) + 1
eid = ev_base
events, problems = [], []

# 70 problem events across the week; the most recent ~18 stay OPEN (active).
picks = [random.choice(trigs) for _ in range(70)]
picks.sort(key=lambda _: random.random())
for i, (tid, prio, host) in enumerate(picks):
    desc = ("High CPU utilization" if prio == 4 else "Disk space critically low")
    name = f"{desc} on {host}.acme.internal"
    start = START + int((i / len(picks)) * DAYS * 86400) + random.randint(0, 3600)
    ack = random.randint(0, 1)
    open_problem = i >= (len(picks) - 18)   # newest 18 stay active
    # PROBLEM event (value=1)
    events.append((eid, 0, 0, tid, start, 1, ack, 0, name, prio))
    if open_problem:
        problems.append((eid, 0, 0, tid, start, 0, None, 0, name, ack, prio))
    else:
        dur = random.randint(600, 8 * 3600)
        r_clock = min(start + dur, NOW - 60)
        reid = eid + 500000
        events.append((reid, 0, 0, tid, r_clock, 0, ack, 0, name, prio))  # RECOVERY (value=0)
        problems.append((eid, 0, 0, tid, start, 0, reid, r_clock, name, ack, prio))
    eid += 1

copy("events", ["eventid", "source", "object", "objectid", "clock", "value",
                "acknowledged", "ns", "name", "severity"], events)
copy("problem", ["eventid", "source", "object", "objectid", "clock", "ns",
                 "r_eventid", "r_clock", "name", "acknowledged", "severity"], problems)
active = sum(1 for p in problems if p[6] is None)
print(f"inserted {len(events)} events, {len(problems)} problems ({active} active/open)")
