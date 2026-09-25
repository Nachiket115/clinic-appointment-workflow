"""
validate.py - Data profiling and business rule validation engine.

Evaluates raw data across scheduling, kiosk events, and billing sources.
Performs explicit issue logging (quarantine/audit table) instead of silently
dropping or mutating invalid records.
"""

import os
import csv
from datetime import datetime
from collections import defaultdict
from typing import Dict, List, Any, Tuple


EXPECTED_KIOSK_STAGES = [
    "check_in",
    "triage_start",
    "triage_end",
    "consult_start",
    "consult_end"
]


def parse_iso(ts_str: str) -> datetime:
    """Helper to parse ISO datetime strings safely."""
    return datetime.fromisoformat(ts_str)


def validate_scheduling(appointments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Validates scheduling records for duplicate appointment IDs, unassigned doctors,
    and invalid booking timestamp sequences.
    """
    issues = []
    seen_ids = set()
    dup_ids = set()

    for appt in appointments:
        appt_id = appt.get("appointment_id")
        
        # Check duplicate appointment_id (e.g. front-desk double entry)
        if appt_id in seen_ids and appt_id not in dup_ids:
            dup_ids.add(appt_id)
            issues.append({
                "source_system": "scheduling.db",
                "entity_id": appt_id,
                "issue_type": "DUPLICATE_APPOINTMENT_ID",
                "severity": "WARNING",
                "description": f"Duplicate appointment_id '{appt_id}' found in scheduling database.",
                "fde_action": "DEDUPLICATE_KEEP_FIRST"
            })
        seen_ids.add(appt_id)

        # Check missing/null doctor
        if not appt.get("doctor"):
            issues.append({
                "source_system": "scheduling.db",
                "entity_id": appt_id,
                "issue_type": "MISSING_DOCTOR",
                "severity": "AUDIT_NOTE",
                "description": f"Appointment '{appt_id}' has no assigned doctor (walk-in slot or unassigned).",
                "fde_action": "PRESERVE_WITH_UNASSIGNED_FLAG"
            })

        # Check booking timing logic: booked_at should precede or equal scheduled_time
        booked_at = appt.get("booked_at")
        sched_time = appt.get("scheduled_time")
        if booked_at and sched_time:
            try:
                if parse_iso(booked_at) > parse_iso(sched_time):
                    issues.append({
                        "source_system": "scheduling.db",
                        "entity_id": appt_id,
                        "issue_type": "INVALID_BOOKING_DATE",
                        "severity": "ERROR",
                        "description": f"Appointment '{appt_id}' booked_at ({booked_at}) is after scheduled_time ({sched_time}).",
                        "fde_action": "FLAG_FOR_AUDIT"
                    })
            except ValueError:
                pass

    return issues


def validate_kiosk_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Validates kiosk and nurse-station event streams:
      - Chronological ordering of lifecycle stages
      - Dropped / incomplete stage events
      - Unmatched walk-in event series (appointment_id is None)
    """
    issues = []
    
    # Group events by appointment_id
    grouped_by_appt: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    walkin_events: List[Dict[str, Any]] = []

    for ev in events:
        appt_id = ev.get("appointment_id")
        if appt_id is None:
            walkin_events.append(ev)
        else:
            grouped_by_appt[appt_id].append(ev)

    # 1. Flag unmatched walk-in events (kiosk events without scheduled appointment)
    if walkin_events:
        walkin_patients = {ev.get("patient_id_kiosk") for ev in walkin_events}
        for pid in sorted(filter(None, walkin_patients)):
            sample_ev = next(e for e in walkin_events if e.get("patient_id_kiosk") == pid)
            issues.append({
                "source_system": "kiosk_events.json",
                "entity_id": f"WALKIN_{pid}",
                "issue_type": "UNMATCHED_WALKIN_EVENTS",
                "severity": "WARNING",
                "description": f"Walk-in patient kiosk events detected for patient_id_kiosk='{pid}' with no matching appointment_id.",
                "fde_action": "SEGREGATE_WALKIN_METRICS"
            })

    # 2. Check stage completeness & chronological consistency for each appointment
    for appt_id, appt_events in grouped_by_appt.items():
        event_map = {e["event_type"]: e for e in appt_events}
        stages_present = set(event_map.keys())

        # Check for dropped / missing stage scans
        missing_stages = [s for s in EXPECTED_KIOSK_STAGES if s not in stages_present]
        if missing_stages:
            issues.append({
                "source_system": "kiosk_events.json",
                "entity_id": appt_id,
                "issue_type": "INCOMPLETE_KIOSK_EVENTS",
                "severity": "WARNING",
                "description": f"Appointment '{appt_id}' has incomplete stages. Missing: {missing_stages}.",
                "fde_action": "CALCULATE_PARTIAL_INTERVALS"
            })

        # Check chronological ordering where adjacent stages are both present
        stage_sequence = [s for s in EXPECTED_KIOSK_STAGES if s in event_map]
        for i in range(len(stage_sequence) - 1):
            s_curr = stage_sequence[i]
            s_next = stage_sequence[i + 1]
            t_curr = parse_iso(event_map[s_curr]["event_time"])
            t_next = parse_iso(event_map[s_next]["event_time"])

            if t_next < t_curr:
                issues.append({
                    "source_system": "kiosk_events.json",
                    "entity_id": appt_id,
                    "issue_type": "OUT_OF_ORDER_TIMESTAMPS",
                    "severity": "ERROR",
                    "description": (
                        f"Appointment '{appt_id}' has impossible ordering: "
                        f"{s_next} ({event_map[s_next]['event_time']}) is before {s_curr} ({event_map[s_curr]['event_time']})."
                    ),
                    "fde_action": "QUARANTINE_AFFECTED_INTERVALS"
                })

    return issues


def validate_billing(billing_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Validates billing log records for missing/invalid billed_minutes values.
    """
    issues = []
    for row in billing_records:
        appt_id = row.get("appointment_id", "UNKNOWN")
        billed_raw = row.get("billed_minutes")

        # Explicitly capture missing or unparsable billed_minutes instead of silent casting
        if billed_raw is None or billed_raw == "":
            issues.append({
                "source_system": "billing_log.csv",
                "entity_id": appt_id,
                "issue_type": "MISSING_BILLED_MINUTES",
                "severity": "WARNING",
                "description": f"Billing entry for appointment '{appt_id}' exists but has empty/null billed_minutes.",
                "fde_action": "EXCLUDE_FROM_BILLING_ANALYSIS"
            })
        else:
            try:
                val = int(billed_raw)
                if val <= 0:
                    issues.append({
                        "source_system": "billing_log.csv",
                        "entity_id": appt_id,
                        "issue_type": "INVALID_BILLED_MINUTES",
                        "severity": "WARNING",
                        "description": f"Billing entry for appointment '{appt_id}' has non-positive billed_minutes ({val}).",
                        "fde_action": "EXCLUDE_FROM_BILLING_ANALYSIS"
                    })
            except ValueError:
                issues.append({
                    "source_system": "billing_log.csv",
                    "entity_id": appt_id,
                    "issue_type": "UNPARSABLE_BILLED_MINUTES",
                    "severity": "ERROR",
                    "description": f"Billing entry for appointment '{appt_id}' has unparsable billed_minutes value '{billed_raw}'.",
                    "fde_action": "EXCLUDE_FROM_BILLING_ANALYSIS"
                })

    return issues


def validate_cross_system_coverage(
    appointments: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    billing_records: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Validates coverage and integrity across disparate systems:
      - Completed appointments missing from billing log (~10% reconciliation gap)
      - Completed appointments with no kiosk scans
      - Non-completed appointments (no_show / cancelled) that unexpectedly have kiosk scans
      - Patient ID format mismatch between systems
    """
    issues = []

    # Map distinct appointment statuses
    appt_status_map = {}
    for a in appointments:
        aid = a.get("appointment_id")
        if aid not in appt_status_map:
            appt_status_map[aid] = a.get("status")

    kiosk_appt_ids = {e.get("appointment_id") for e in events if e.get("appointment_id")}
    billing_appt_ids = {b.get("appointment_id") for b in billing_records if b.get("appointment_id")}

    # 1. Check unreconciled billing: completed appointments with no billing record
    for aid, status in appt_status_map.items():
        if status == "completed" and aid not in billing_appt_ids:
            issues.append({
                "source_system": "billing_log.csv",
                "entity_id": aid,
                "issue_type": "UNRECONCILED_BILLING",
                "severity": "WARNING",
                "description": f"Completed appointment '{aid}' is missing from billing_log.csv (unreconciled / late entry).",
                "fde_action": "FLAG_UNRECONCILED_REVENUE"
            })

    # 2. Check completed appointments missing all kiosk events
    for aid, status in appt_status_map.items():
        if status == "completed" and aid not in kiosk_appt_ids:
            issues.append({
                "source_system": "cross_system",
                "entity_id": aid,
                "issue_type": "MISSING_KIOSK_EVENTS",
                "severity": "WARNING",
                "description": f"Completed appointment '{aid}' has no recorded kiosk events.",
                "fde_action": "EXCLUDE_FROM_WAIT_TIME_METRICS"
            })

    # 3. Check no-show / cancelled appointments that unexpectedly have kiosk events
    for aid in kiosk_appt_ids:
        status = appt_status_map.get(aid)
        if status in ("no_show", "cancelled"):
            issues.append({
                "source_system": "cross_system",
                "entity_id": aid,
                "issue_type": "STATUS_EVENT_INCONSISTENCY",
                "severity": "ERROR",
                "description": f"Appointment '{aid}' has status='{status}' in scheduling but has kiosk event activity.",
                "fde_action": "FLAG_FOR_OPERATIONAL_REVIEW"
            })

    # 4. Check patient ID format mismatch
    # Scheduling uses 'PT0042', kiosk uses '42'
    kiosk_pids = {e.get("patient_id_kiosk") for e in events if e.get("patient_id_kiosk")}
    non_pt_kiosk_ids = [p for p in kiosk_pids if not p.startswith("PT") and not p.startswith("W")]
    if non_pt_kiosk_ids:
        sample_k_id = non_pt_kiosk_ids[0]
        sample_sched_id = f"PT{int(sample_k_id):04d}" if sample_k_id.isdigit() else sample_k_id
        issues.append({
            "source_system": "cross_system",
            "entity_id": "ALL_KIOSK_PATIENTS",
            "issue_type": "PATIENT_ID_FORMAT_MISMATCH",
            "severity": "AUDIT_NOTE",
            "description": (
                f"[Systemic Schema Pattern] Kiosk system exports numeric-only patient IDs across all "
                f"{len(non_pt_kiosk_ids)} scheduled patients (e.g. '{sample_k_id}' vs '{sample_sched_id}' in scheduling.db). "
                f"Resolved via deterministic prefix padding in modeling."
            ),
            "fde_action": "STANDARDIZE_IN_MODELING"
        })

    return issues


def validate_all(
    raw_data: Dict[str, List[Dict[str, Any]]],
    output_dir: str = "processed"
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Executes all validation suites, logs structured issues, and writes validation_issues.csv.
    """
    all_issues = []
    all_issues.extend(validate_scheduling(raw_data.get("appointments", [])))
    all_issues.extend(validate_kiosk_events(raw_data.get("kiosk_events", [])))
    all_issues.extend(validate_billing(raw_data.get("billing_log", [])))
    all_issues.extend(validate_cross_system_coverage(
        raw_data.get("appointments", []),
        raw_data.get("kiosk_events", []),
        raw_data.get("billing_log", [])
    ))

    # Assign deterministic issue IDs
    for idx, issue in enumerate(all_issues, start=1):
        issue["issue_id"] = f"ISS-{idx:04d}"

    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "validation_issues.csv")

    fieldnames = [
        "issue_id",
        "source_system",
        "entity_id",
        "issue_type",
        "severity",
        "description",
        "fde_action"
    ]

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_issues)

    # Issue counts by type
    counts_by_type = defaultdict(int)
    for issue in all_issues:
        counts_by_type[issue["issue_type"]] += 1

    return all_issues, dict(counts_by_type)


if __name__ == "__main__":
    import sys
    # Allow running directly as a script or via python -m src.validate
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.ingest import ingest_all

    print("Running validation engine across raw sources...")
    data, manifest = ingest_all("raw")
    issues, counts = validate_all(data, "processed")

    print(f"\nTotal Validation Issues Flagged: {len(issues)}")
    print("--------------------------------------------------")
    for itype, count in sorted(counts.items()):
        print(f"  - {itype:<30}: {count:>3} occurrences")
    print("--------------------------------------------------")
    print("Saved explicit issue log to -> processed/validation_issues.csv")

