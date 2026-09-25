"""
model.py - Workflow modeling and event interval transformation engine.

Harmonizes data from scheduling, kiosk events, and billing into an analytical
appointment-grain dataset. Explicitly handles:
  - Patient ID standardization (numeric -> PT####)
  - Kiosk clock skew adjustment (-3 minutes systemic drift)
  - Lifecycle interval calculations (waits and stage durations)
  - Preserves anomaly flags and quarantines corrupted intervals without silent dropping.
"""

import os
import csv
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple


# Kiosk clock is systemically 3 minutes ahead of scheduling clock
KIOSK_CLOCK_SKEW_MINUTES = 3
STANDARD_SLOT_MINUTES = 20.0


def parse_dt(ts_str: Optional[str]) -> Optional[datetime]:
    """Safely parse ISO timestamp string into datetime."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def format_dt(dt: Optional[datetime]) -> str:
    """Format datetime back to ISO string or empty string."""
    return dt.isoformat() if dt else ""


def normalize_patient_id(kiosk_pid: Optional[str]) -> str:
    """Standardizes kiosk patient IDs (e.g. '42' -> 'PT0042', 'W1' -> 'W1')."""
    if not kiosk_pid:
        return ""
    kiosk_pid = str(kiosk_pid).strip()
    if kiosk_pid.isdigit():
        return f"PT{int(kiosk_pid):04d}"
    return kiosk_pid


def calc_minutes(start_dt: Optional[datetime], end_dt: Optional[datetime]) -> Optional[float]:
    """Calculates duration in minutes between two datetimes, returning None if invalid or missing."""
    if not start_dt or not end_dt:
        return None
    diff_sec = (end_dt - start_dt).total_seconds()
    return round(diff_sec / 60.0, 2)


def deduplicate_appointments(appointments: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Deduplicates scheduling records by appointment_id, keeping first occurrence
    and tracking duplicates for explicit modeling metadata.
    """
    deduped = []
    seen = set()
    dup_counts = defaultdict(int)

    for appt in appointments:
        aid = appt.get("appointment_id")
        dup_counts[aid] += 1
        if aid not in seen:
            seen.add(aid)
            deduped.append(appt)

    return deduped, dict(dup_counts)


def process_kiosk_events(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Groups, adjusts clock skew, and organizes kiosk events per appointment_id.
    Detects ambiguous multi-stream collisions (like A0151) and out-of-order stages.
    """
    skew_delta = timedelta(minutes=KIOSK_CLOCK_SKEW_MINUTES)
    
    # Group raw events by appointment_id (or walk-in identifier)
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for ev in events:
        aid = ev.get("appointment_id")
        if aid is None:
            pid = ev.get("patient_id_kiosk", "UNKNOWN")
            grouped[f"WALKIN_{pid}"].append(ev)
        else:
            grouped[aid].append(ev)

    processed_events = {}

    for key, ev_list in grouped.items():
        # Check for ambiguous event collision: more than 5 events for a single appointment ID
        # e.g., A0151 has 10 events from two duplicate booking streams
        if len(ev_list) > 5 and not key.startswith("WALKIN_"):
            processed_events[key] = {
                "is_unresolvable_duplicate": True,
                "stages": {},
                "event_count": len(ev_list),
                "is_out_of_order": False,
                "missing_stages": []
            }
            continue

        # Extract timestamps and adjust for systemic 3-minute kiosk clock skew
        stages = {}
        for ev in ev_list:
            stype = ev.get("event_type")
            raw_ts = parse_dt(ev.get("event_time"))
            if raw_ts and stype:
                # Normalizing kiosk clock onto clinic scheduling baseline
                stages[stype] = raw_ts - skew_delta

        # Check sequence order
        is_out_of_order = False
        seq = ["check_in", "triage_start", "triage_end", "consult_start", "consult_end"]
        present_seq = [s for s in seq if s in stages]
        for i in range(len(present_seq) - 1):
            if stages[present_seq[i+1]] < stages[present_seq[i]]:
                is_out_of_order = True
                break

        missing = [s for s in seq if s not in stages]

        processed_events[key] = {
            "is_unresolvable_duplicate": False,
            "stages": stages,
            "event_count": len(ev_list),
            "is_out_of_order": is_out_of_order,
            "missing_stages": missing
        }

    return processed_events


def build_modeled_dataset(
    raw_data: Dict[str, List[Dict[str, Any]]],
    output_dir: str = "processed"
) -> List[Dict[str, Any]]:
    """
    Transforms and joins scheduling, kiosk, and billing sources into a cohesive
    appointment-grain analytical dataset.
    """
    raw_appts = raw_data.get("appointments", [])
    raw_events = raw_data.get("kiosk_events", [])
    raw_billing = raw_data.get("billing_log", [])

    # 1. Deduplicate appointments
    appointments, dup_counts = deduplicate_appointments(raw_appts)

    # 2. Process and normalize kiosk telemetry
    kiosk_map = process_kiosk_events(raw_events)

    # 3. Index billing records by appointment_id
    billing_map = {}
    for b in raw_billing:
        aid = b.get("appointment_id")
        if aid and aid not in billing_map:
            billed_raw = b.get("billed_minutes")
            billed_val = None
            if billed_raw not in (None, ""):
                try:
                    billed_val = int(billed_raw)
                except ValueError:
                    billed_val = None
            billing_map[aid] = {
                "billed_minutes": billed_val,
                "billing_code": b.get("billing_code", "")
            }

    modeled_rows = []

    for appt in appointments:
        aid = appt["appointment_id"]
        pid = appt["patient_id"]
        doctor = appt.get("doctor")
        status = appt.get("status", "unknown")
        sched_dt = parse_dt(appt.get("scheduled_time"))
        booked_dt = parse_dt(appt.get("booked_at"))

        # Lead time from booking date to scheduled date
        lead_time_days = None
        if sched_dt and booked_dt:
            lead_time_days = round((sched_dt - booked_dt).total_seconds() / 86400.0, 1)

        # Billing lookup
        billing_info = billing_map.get(aid, {"billed_minutes": None, "billing_code": ""})

        # Telemetry lookup
        kiosk_info = kiosk_map.get(aid)
        stages = kiosk_info["stages"] if kiosk_info else {}

        # Quality flags
        flags = []
        if not doctor:
            flags.append("UNASSIGNED_DOCTOR")
        if dup_counts.get(aid, 0) > 1:
            flags.append("DUPLICATE_BOOKING_RECORD")
        if status == "completed" and aid not in billing_map:
            flags.append("UNRECONCILED_BILLING")

        # Initialize interval metrics
        arrival_drift_mins = None
        wait_checkin_to_triage = None
        triage_duration = None
        wait_triage_to_consult = None
        consult_duration = None
        total_clinic_duration = None
        is_consult_overrun = None

        if kiosk_info:
            if kiosk_info["is_unresolvable_duplicate"]:
                # Checkpoint: A0151 has 10 colliding events from duplicate booking.
                # Preserved in modeled dataset with scheduling metadata, but all intervals
                # are quarantined to prevent corrupted metrics.
                flags.append("UNRESOLVABLE_DUPLICATE_EVENTS")
            elif kiosk_info["is_out_of_order"]:
                # Flagged out-of-order timestamps (e.g. triage_start < check_in)
                flags.append("OUT_OF_ORDER_TIMESTAMPS")

                # Compute only intervals that maintain strictly valid chronological ordering
                t_in = stages.get("check_in")
                t_tstart = stages.get("triage_start")
                t_tend = stages.get("triage_end")
                t_cstart = stages.get("consult_start")
                t_cend = stages.get("consult_end")

                # Arrival drift relative to schedule
                if t_in and sched_dt:
                    arrival_drift_mins = calc_minutes(sched_dt, t_in)

                # Quarantine wait_checkin_to_triage if out of order
                if t_in and t_tstart and t_tstart >= t_in:
                    wait_checkin_to_triage = calc_minutes(t_in, t_tstart)

                if t_tstart and t_tend and t_tend >= t_tstart:
                    triage_duration = calc_minutes(t_tstart, t_tend)

                if t_tend and t_cstart and t_cstart >= t_tend:
                    wait_triage_to_consult = calc_minutes(t_tend, t_cstart)

                if t_cstart and t_cend and t_cend >= t_cstart:
                    consult_duration = calc_minutes(t_cstart, t_cend)
                    is_consult_overrun = consult_duration > STANDARD_SLOT_MINUTES

                if t_in and t_cend and t_cend >= t_in:
                    total_clinic_duration = calc_minutes(t_in, t_cend)
            else:
                # Clean / Standard stage sequence
                if kiosk_info["missing_stages"]:
                    flags.append("PARTIAL_EVENTS")

                t_in = stages.get("check_in")
                t_tstart = stages.get("triage_start")
                t_tend = stages.get("triage_end")
                t_cstart = stages.get("consult_start")
                t_cend = stages.get("consult_end")

                if t_in and sched_dt:
                    arrival_drift_mins = calc_minutes(sched_dt, t_in)

                if t_in and t_tstart:
                    wait_checkin_to_triage = calc_minutes(t_in, t_tstart)

                if t_tstart and t_tend:
                    triage_duration = calc_minutes(t_tstart, t_tend)

                if t_tend and t_cstart:
                    wait_triage_to_consult = calc_minutes(t_tend, t_cstart)

                if t_cstart and t_cend:
                    consult_duration = calc_minutes(t_cstart, t_cend)
                    is_consult_overrun = consult_duration > STANDARD_SLOT_MINUTES

                if t_in and t_cend:
                    total_clinic_duration = calc_minutes(t_in, t_cend)
        else:
            if status == "completed":
                flags.append("MISSING_KIOSK_DATA")

        data_quality_flag = ";".join(flags) if flags else "CLEAN"

        row = {
            "appointment_id": aid,
            "patient_id": pid,
            "doctor": doctor if doctor else "Unassigned",
            "scheduled_time": appt.get("scheduled_time", ""),
            "booked_at": appt.get("booked_at", ""),
            "status": status,
            "check_in_time": format_dt(stages.get("check_in")),
            "triage_start_time": format_dt(stages.get("triage_start")),
            "triage_end_time": format_dt(stages.get("triage_end")),
            "consult_start_time": format_dt(stages.get("consult_start")),
            "consult_end_time": format_dt(stages.get("consult_end")),
            "billed_minutes": billing_info["billed_minutes"] if billing_info["billed_minutes"] is not None else "",
            "billing_code": billing_info["billing_code"],
            "lead_time_days": lead_time_days if lead_time_days is not None else "",
            "arrival_drift_mins": arrival_drift_mins if arrival_drift_mins is not None else "",
            "wait_checkin_to_triage_mins": wait_checkin_to_triage if wait_checkin_to_triage is not None else "",
            "triage_duration_mins": triage_duration if triage_duration is not None else "",
            "wait_triage_to_consult_mins": wait_triage_to_consult if wait_triage_to_consult is not None else "",
            "consult_duration_mins": consult_duration if consult_duration is not None else "",
            "total_clinic_duration_mins": total_clinic_duration if total_clinic_duration is not None else "",
            "is_consult_overrun": is_consult_overrun if is_consult_overrun is not None else "",
            "data_quality_flag": data_quality_flag
        }
        modeled_rows.append(row)

    # 4. Handle unmatched walk-ins (WALKIN_W1, WALKIN_W2, WALKIN_W3)
    # They have kiosk telemetry but no scheduling record
    for key, kinfo in kiosk_map.items():
        if key.startswith("WALKIN_"):
            stages = kinfo["stages"]
            pid_raw = key.replace("WALKIN_", "")
            t_in = stages.get("check_in")
            t_tstart = stages.get("triage_start")
            t_tend = stages.get("triage_end")
            t_cstart = stages.get("consult_start")
            t_cend = stages.get("consult_end")

            w_c_dur = calc_minutes(t_cstart, t_cend) if t_cstart and t_cend else None

            walkin_row = {
                "appointment_id": key,
                "patient_id": f"WALKIN_{pid_raw}",
                "doctor": "Unassigned",
                "scheduled_time": "",
                "booked_at": "",
                "status": "walk_in",
                "check_in_time": format_dt(t_in),
                "triage_start_time": format_dt(t_tstart),
                "triage_end_time": format_dt(t_tend),
                "consult_start_time": format_dt(t_cstart),
                "consult_end_time": format_dt(t_cend),
                "billed_minutes": "",
                "billing_code": "",
                "lead_time_days": 0.0,
                "arrival_drift_mins": "",
                "wait_checkin_to_triage_mins": calc_minutes(t_in, t_tstart) if t_in and t_tstart else "",
                "triage_duration_mins": calc_minutes(t_tstart, t_tend) if t_tstart and t_tend else "",
                "wait_triage_to_consult_mins": calc_minutes(t_tend, t_cstart) if t_tend and t_cstart else "",
                "consult_duration_mins": w_c_dur if w_c_dur is not None else "",
                "total_clinic_duration_mins": calc_minutes(t_in, t_cend) if t_in and t_cend else "",
                "is_consult_overrun": (w_c_dur > STANDARD_SLOT_MINUTES) if w_c_dur is not None else "",
                "data_quality_flag": "UNMATCHED_WALKIN"
            }
            modeled_rows.append(walkin_row)

    # 5. Write to processed/modeled_appointments.csv
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "modeled_appointments.csv")

    fieldnames = list(modeled_rows[0].keys())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(modeled_rows)

    return modeled_rows


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.ingest import ingest_all

    print("Running workflow modeling & transformation engine...")
    data, manifest = ingest_all("raw")
    modeled = build_modeled_dataset(data, "processed")

    scheduled_rows = [r for r in modeled if r["status"] != "walk_in"]
    walkin_rows = [r for r in modeled if r["status"] == "walk_in"]
    a0151_row = next((r for r in modeled if r["appointment_id"] == "A0151"), None)

    print(f"\nModeled Dataset Summary:")
    print(f"  - Total Modeled Records: {len(modeled)} ({len(scheduled_rows)} scheduled + {len(walkin_rows)} walk-ins)")
    print(f"  - Output File Saved: processed/modeled_appointments.csv")
    if a0151_row:
        print(f"\nA0151 Checkpoint Verification:")
        print(f"  - appointment_id: {a0151_row['appointment_id']}")
        print(f"  - status: {a0151_row['status']}")
        print(f"  - data_quality_flag: {a0151_row['data_quality_flag']}")
        print(f"  - wait_checkin_to_triage_mins: '{a0151_row['wait_checkin_to_triage_mins']}' (quarantined/empty)")
        print(f"  - wait_triage_to_consult_mins: '{a0151_row['wait_triage_to_consult_mins']}' (quarantined/empty)")
