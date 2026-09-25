"""
generate_data.py

Produces the raw, messy source data for the clinic appointment KPI project.
Simulates three separate systems a real clinic would actually have:

  1. scheduling.db   (SQLite)     - the front-desk booking system
  2. kiosk_events.json (JSON)     - check-in kiosk / nurse station event export
  3. billing_log.csv (CSV)       - billing system's visit duration export

The three systems don't agree on IDs, timestamps, or even which patients
exist, on purpose - that's the point of the exercise. Re-running this script
regenerates all three files from scratch (same random seed = same messiness
each time, so your validation rules can be tested against a stable target).
"""

import sqlite3
import json
import csv
import random
from datetime import datetime, timedelta

random.seed(42)

N_APPTS = 220
DOCTORS = ["Dr. Alvarez", "Dr. Chen", "Dr. Okafor", "Dr. Patel"]
STATUSES = ["completed", "no_show", "cancelled"]
STATUS_WEIGHTS = [0.80, 0.12, 0.08]

BASE_DATE = datetime(2026, 9, 1, 8, 0, 0)


def rand_patient_id(i):
    return f"PT{i:04d}"


def make_appointments():
    appts = []
    for i in range(1, N_APPTS + 1):
        day_offset = random.randint(0, 13)
        slot_minute = random.choice(range(0, 480, 20))  # 20-min slots, 8hr day
        scheduled_time = BASE_DATE + timedelta(days=day_offset, minutes=slot_minute)
        booked_at = scheduled_time - timedelta(days=random.randint(1, 21))
        status = random.choices(STATUSES, weights=STATUS_WEIGHTS)[0]

        appts.append({
            "appointment_id": f"A{i:04d}",
            "patient_id": rand_patient_id(i),
            "doctor": random.choice(DOCTORS),
            "scheduled_time": scheduled_time.isoformat(),
            "booked_at": booked_at.isoformat(),
            "status": status,
        })

    # inject a handful of duplicate appointment_ids (front desk double-entry)
    for _ in range(4):
        dup = random.choice(appts).copy()
        appts.append(dup)

    # inject a few nulled-out doctor fields (walk-in slots added late, unassigned)
    for _ in range(6):
        random.choice(appts)["doctor"] = None

    random.shuffle(appts)
    return appts


def make_kiosk_events(appts):
    """
    Kiosk/nurse-station timestamps, in a slightly different clock (kiosk clock
    runs a few minutes fast) and using a DIFFERENT patient id format than the
    scheduling system on purpose (e.g. "42" instead of "PT0042"), which is a
    very common real-world join problem.
    """
    events = []

    for appt in appts:
        if appt["status"] != "completed":
            continue  # no-shows/cancellations never generate kiosk events

        sched = datetime.fromisoformat(appt["scheduled_time"])
        # patient usually arrives a bit before/after scheduled time
        arrival_drift = timedelta(minutes=random.randint(-10, 25))
        check_in = sched + arrival_drift

        triage_wait = timedelta(minutes=random.randint(2, 35))
        triage_start = check_in + triage_wait
        triage_len = timedelta(minutes=random.randint(4, 12))
        triage_end = triage_start + triage_len

        consult_wait = timedelta(minutes=random.randint(1, 40))
        consult_start = triage_end + consult_wait
        consult_len = timedelta(minutes=random.randint(8, 30))
        consult_end = consult_start + consult_len

        # kiosk clock skew - a systemic offset, not random noise
        skew = timedelta(minutes=3)

        # numeric-only patient id format used by the kiosk system
        kiosk_patient_id = appt["patient_id"].replace("PT", "").lstrip("0") or "0"

        stage_times = {
            "check_in": check_in,
            "triage_start": triage_start,
            "triage_end": triage_end,
            "consult_start": consult_start,
            "consult_end": consult_end,
        }

        for stage, ts in stage_times.items():
            events.append({
                "event_id": f"E{len(events)+1:05d}",
                "appointment_id": appt["appointment_id"],
                "patient_id_kiosk": kiosk_patient_id,
                "event_type": stage,
                "event_time": (ts + skew).isoformat(),
            })

    # inject a few physically-impossible orderings (triage_start logged before check_in)
    swap_candidates = [e for e in events if e["event_type"] == "check_in"]
    for _ in range(5):
        c = random.choice(swap_candidates)
        matching_triage = next(
            (e for e in events
             if e["appointment_id"] == c["appointment_id"] and e["event_type"] == "triage_start"),
            None,
        )
        if matching_triage:
            c["event_time"], matching_triage["event_time"] = (
                matching_triage["event_time"], c["event_time"]
            )

    # inject a few true walk-ins: kiosk events with NO matching appointment at all
    for i in range(3):
        walk_in_id = f"WALKIN{i+1}"
        base = BASE_DATE + timedelta(days=random.randint(0, 13), minutes=random.randint(0, 480))
        for j, stage in enumerate(["check_in", "triage_start", "triage_end", "consult_start", "consult_end"]):
            events.append({
                "event_id": f"E{len(events)+1:05d}",
                "appointment_id": None,
                "patient_id_kiosk": f"W{i+1}",
                "event_type": stage,
                "event_time": (base + timedelta(minutes=j * 10)).isoformat(),
            })

    # drop a few events entirely (sensor/kiosk missed a scan)
    for _ in range(8):
        if events:
            events.remove(random.choice(events))

    random.shuffle(events)
    return events


def make_billing_log(appts):
    """Billing system CSV - only has completed visits, own duration calc,
    and occasionally missing rows (billing entered late / not yet reconciled)."""
    rows = []
    for appt in appts:
        if appt["status"] != "completed":
            continue
        if random.random() < 0.10:
            continue  # not yet billed / missing row
        visit_minutes = random.randint(12, 55)
        rows.append({
            "appointment_id": appt["appointment_id"],
            "billed_minutes": visit_minutes,
            "billing_code": random.choice(["GEN-20", "GEN-40", "FOLLOWUP-15"]),
        })
    return rows


def write_sqlite(appts, path="raw/scheduling.db"):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS appointments")
    cur.execute("""
        CREATE TABLE appointments (
            appointment_id TEXT,
            patient_id TEXT,
            doctor TEXT,
            scheduled_time TEXT,
            booked_at TEXT,
            status TEXT
        )
    """)
    for a in appts:
        cur.execute(
            "INSERT INTO appointments VALUES (?, ?, ?, ?, ?, ?)",
            (a["appointment_id"], a["patient_id"], a["doctor"],
             a["scheduled_time"], a["booked_at"], a["status"]),
        )
    conn.commit()
    conn.close()


def write_json(events, path="raw/kiosk_events.json"):
    with open(path, "w") as f:
        json.dump(events, f, indent=2)


def write_csv(rows, path="raw/billing_log.csv"):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["appointment_id", "billed_minutes", "billing_code"])
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    import os
    os.makedirs("raw", exist_ok=True)

    appts = make_appointments()
    events = make_kiosk_events(appts)
    billing = make_billing_log(appts)

    write_sqlite(appts)
    write_json(events)
    write_csv(billing)

    print(f"Generated {len(appts)} appointment rows -> raw/scheduling.db")
    print(f"Generated {len(events)} kiosk events   -> raw/kiosk_events.json")
    print(f"Generated {len(billing)} billing rows   -> raw/billing_log.csv")
