"""
metrics.py - KPI calculation engine for clinic appointment workflow.

Computes core operational KPIs with explicit denominator accounting:
  1. Avg Wait: Check-in -> Triage Start
  2. Avg Wait: Triage End -> Consult Start (Doctor Seen)
  3. No-Show Rate (% of scheduled appointments)
  4. Consult Overrun Rate (% of completed consults exceeding standard 20-min slot)
  5. Cancellation Rate (% of scheduled appointments)
  6. Operational Breakdowns: Wait times and overruns by doctor and hour of day.

Every metric tracks valid sample sizes, exclusion counts, and explicit rationale,
avoiding silent zero-imputations.
"""

import os
import csv
import json
from datetime import datetime
from collections import defaultdict
from typing import Dict, List, Any, Optional


STANDARD_SLOT_MINUTES = 20.0


def parse_float(val: Any) -> Optional[float]:
    """Safely cast value to float or return None if empty/invalid."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def calculate_wait_checkin_to_triage(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Metric 1: Average wait time from patient check-in to triage start.
    Denominator: Completed appointments with valid, chronologically sound check-in and triage_start.
    """
    # Restrict to scheduled appointments (walk-ins tracked separately)
    scheduled_rows = [r for r in rows if r["status"] != "walk_in"]
    total_scheduled = len(scheduled_rows)

    valid_values = []
    excluded_no_show_cancelled = 0
    excluded_quarantined_out_of_order = 0
    excluded_unresolvable_duplicate = 0
    excluded_missing_scan = 0

    for r in scheduled_rows:
        status = r.get("status")
        flag = r.get("data_quality_flag", "")
        val = parse_float(r.get("wait_checkin_to_triage_mins"))

        if status in ("no_show", "cancelled"):
            excluded_no_show_cancelled += 1
        elif "UNRESOLVABLE_DUPLICATE_EVENTS" in flag:
            excluded_unresolvable_duplicate += 1
        elif "OUT_OF_ORDER_TIMESTAMPS" in flag and val is None:
            excluded_quarantined_out_of_order += 1
        elif val is None:
            excluded_missing_scan += 1
        else:
            valid_values.append(val)

    avg_wait = round(sum(valid_values) / len(valid_values), 2) if valid_values else None

    return {
        "metric_name": "avg_wait_checkin_to_triage_minutes",
        "description": "Average duration (minutes) from patient kiosk check-in to nurse triage start.",
        "average_minutes": avg_wait,
        "valid_sample_count": len(valid_values),
        "total_scheduled_appointments": total_scheduled,
        "exclusions": {
            "no_show_or_cancelled": excluded_no_show_cancelled,
            "quarantined_out_of_order": excluded_quarantined_out_of_order,
            "unresolvable_duplicate_stream": excluded_unresolvable_duplicate,
            "missing_sensor_scan": excluded_missing_scan,
            "total_excluded": total_scheduled - len(valid_values)
        },
        "formula": "sum(valid_wait_checkin_to_triage_mins) / count(valid_completed_appointments)"
    }


def calculate_wait_triage_to_consult(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Metric 2: Average wait time from triage completion to doctor consult start.
    Denominator: Completed appointments with valid triage_end and consult_start.
    """
    scheduled_rows = [r for r in rows if r["status"] != "walk_in"]
    total_scheduled = len(scheduled_rows)

    valid_values = []
    excluded_no_show_cancelled = 0
    excluded_unresolvable_duplicate = 0
    excluded_missing_scan = 0

    for r in scheduled_rows:
        status = r.get("status")
        flag = r.get("data_quality_flag", "")
        val = parse_float(r.get("wait_triage_to_consult_mins"))

        if status in ("no_show", "cancelled"):
            excluded_no_show_cancelled += 1
        elif "UNRESOLVABLE_DUPLICATE_EVENTS" in flag:
            excluded_unresolvable_duplicate += 1
        elif val is None:
            excluded_missing_scan += 1
        else:
            valid_values.append(val)

    avg_wait = round(sum(valid_values) / len(valid_values), 2) if valid_values else None

    return {
        "metric_name": "avg_wait_triage_to_consult_minutes",
        "description": "Average duration (minutes) from triage completion to doctor consultation start.",
        "average_minutes": avg_wait,
        "valid_sample_count": len(valid_values),
        "total_scheduled_appointments": total_scheduled,
        "exclusions": {
            "no_show_or_cancelled": excluded_no_show_cancelled,
            "unresolvable_duplicate_stream": excluded_unresolvable_duplicate,
            "missing_sensor_scan": excluded_missing_scan,
            "total_excluded": total_scheduled - len(valid_values)
        },
        "formula": "sum(valid_wait_triage_to_consult_mins) / count(valid_completed_appointments)"
    }


def calculate_no_show_rate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Metric 3: No-show rate as a percentage of all scheduled appointments.
    Denominator: Total distinct scheduled appointments in scheduling.db.
    """
    scheduled_rows = [r for r in rows if r["status"] != "walk_in"]
    total_scheduled = len(scheduled_rows)
    no_show_count = sum(1 for r in scheduled_rows if r.get("status") == "no_show")

    rate_pct = round((no_show_count / total_scheduled) * 100.0, 2) if total_scheduled > 0 else 0.0

    return {
        "metric_name": "no_show_rate_percent",
        "description": "Percentage of booked appointments where patient never arrived/checked in.",
        "rate_percent": rate_pct,
        "no_show_count": no_show_count,
        "total_scheduled_appointments": total_scheduled,
        "formula": "(count(status == 'no_show') / count(total_scheduled_appointments)) * 100"
    }


def calculate_consult_overrun_rate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Metric 4: Consult overrun rate (% of consults exceeding STANDARD_SLOT_MINUTES).
    Denominator: Completed appointments with valid, non-null consult durations.
    """
    scheduled_rows = [r for r in rows if r["status"] != "walk_in"]
    total_scheduled = len(scheduled_rows)

    valid_consults = []
    overrun_count = 0
    excluded_no_show_cancelled = 0
    excluded_unresolvable_duplicate = 0
    excluded_missing_scan = 0

    for r in scheduled_rows:
        status = r.get("status")
        flag = r.get("data_quality_flag", "")
        val = parse_float(r.get("consult_duration_mins"))

        if status in ("no_show", "cancelled"):
            excluded_no_show_cancelled += 1
        elif "UNRESOLVABLE_DUPLICATE_EVENTS" in flag:
            excluded_unresolvable_duplicate += 1
        elif val is None:
            excluded_missing_scan += 1
        else:
            valid_consults.append(val)
            if val > STANDARD_SLOT_MINUTES:
                overrun_count += 1

    rate_pct = round((overrun_count / len(valid_consults)) * 100.0, 2) if valid_consults else 0.0
    avg_consult_duration = round(sum(valid_consults) / len(valid_consults), 2) if valid_consults else None

    return {
        "metric_name": "consult_overrun_rate_percent",
        "description": f"Percentage of completed consultations exceeding the {STANDARD_SLOT_MINUTES}-minute scheduled slot benchmark.",
        "overrun_rate_percent": rate_pct,
        "overrun_count": overrun_count,
        "valid_consult_count": len(valid_consults),
        "avg_actual_consult_duration_minutes": avg_consult_duration,
        "standard_slot_minutes": STANDARD_SLOT_MINUTES,
        "exclusions": {
            "no_show_or_cancelled": excluded_no_show_cancelled,
            "unresolvable_duplicate_stream": excluded_unresolvable_duplicate,
            "missing_sensor_scan": excluded_missing_scan,
            "total_excluded": total_scheduled - len(valid_consults)
        },
        "formula": f"(count(consult_duration > {STANDARD_SLOT_MINUTES}) / count(valid_consult_durations)) * 100"
    }


def calculate_cancellation_rate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Metric 5: Cancellation rate as a percentage of all scheduled appointments.
    Denominator: Total distinct scheduled appointments in scheduling.db.
    """
    scheduled_rows = [r for r in rows if r["status"] != "walk_in"]
    total_scheduled = len(scheduled_rows)
    cancelled_count = sum(1 for r in scheduled_rows if r.get("status") == "cancelled")

    rate_pct = round((cancelled_count / total_scheduled) * 100.0, 2) if total_scheduled > 0 else 0.0

    return {
        "metric_name": "cancellation_rate_percent",
        "description": "Percentage of booked appointments cancelled prior to or on appointment day.",
        "rate_percent": rate_pct,
        "cancelled_count": cancelled_count,
        "total_scheduled_appointments": total_scheduled,
        "formula": "(count(status == 'cancelled') / count(total_scheduled_appointments)) * 100"
    }


def calculate_doctor_breakdown(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Segmented operational metrics grouped by doctor.
    Shows where consultation overruns and wait times concentrate by provider.
    """
    scheduled_rows = [r for r in rows if r["status"] != "walk_in"]
    by_doctor: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "total_booked": 0,
        "completed": 0,
        "no_shows": 0,
        "cancelled": 0,
        "wait_checkin_vals": [],
        "wait_triage_vals": [],
        "consult_dur_vals": [],
        "overruns": 0
    })

    for r in scheduled_rows:
        doc = r.get("doctor") or "Unassigned"
        status = r.get("status")
        by_doctor[doc]["total_booked"] += 1

        if status == "no_show":
            by_doctor[doc]["no_shows"] += 1
        elif status == "cancelled":
            by_doctor[doc]["cancelled"] += 1
        elif status == "completed":
            by_doctor[doc]["completed"] += 1
            w1 = parse_float(r.get("wait_checkin_to_triage_mins"))
            w2 = parse_float(r.get("wait_triage_to_consult_mins"))
            cdur = parse_float(r.get("consult_duration_mins"))

            if w1 is not None:
                by_doctor[doc]["wait_checkin_vals"].append(w1)
            if w2 is not None:
                by_doctor[doc]["wait_triage_vals"].append(w2)
            if cdur is not None:
                by_doctor[doc]["consult_dur_vals"].append(cdur)
                if cdur > STANDARD_SLOT_MINUTES:
                    by_doctor[doc]["overruns"] += 1

    summary = {}
    for doc, stats in sorted(by_doctor.items()):
        w1_list = stats["wait_checkin_vals"]
        w2_list = stats["wait_triage_vals"]
        cd_list = stats["consult_dur_vals"]

        summary[doc] = {
            "total_booked": stats["total_booked"],
            "completed": stats["completed"],
            "no_shows": stats["no_shows"],
            "no_show_rate_pct": round((stats["no_shows"] / stats["total_booked"]) * 100.0, 1) if stats["total_booked"] > 0 else 0.0,
            "avg_wait_checkin_to_triage_mins": round(sum(w1_list) / len(w1_list), 1) if w1_list else None,
            "avg_wait_triage_to_consult_mins": round(sum(w2_list) / len(w2_list), 1) if w2_list else None,
            "avg_consult_duration_mins": round(sum(cd_list) / len(cd_list), 1) if cd_list else None,
            "consult_overrun_rate_pct": round((stats["overruns"] / len(cd_list)) * 100.0, 1) if cd_list else 0.0,
            "valid_consult_samples": len(cd_list)
        }

    return summary


def calculate_hourly_bottleneck_breakdown(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Segmented wait times by scheduled hour of day (e.g. 08:00, 09:00, ..., 15:00).
    Demonstrates where delay accumulates as the clinic day progresses.
    """
    scheduled_rows = [r for r in rows if r["status"] != "walk_in"]
    by_hour: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "scheduled_count": 0,
        "wait_checkin_vals": [],
        "wait_triage_vals": [],
        "total_wait_vals": []
    })

    for r in scheduled_rows:
        sched_time_str = r.get("scheduled_time")
        if not sched_time_str:
            continue
        try:
            dt = datetime.fromisoformat(sched_time_str)
            hour_key = f"{dt.hour:02d}:00"
        except ValueError:
            continue

        by_hour[hour_key]["scheduled_count"] += 1
        w1 = parse_float(r.get("wait_checkin_to_triage_mins"))
        w2 = parse_float(r.get("wait_triage_to_consult_mins"))

        if w1 is not None:
            by_hour[hour_key]["wait_checkin_vals"].append(w1)
        if w2 is not None:
            by_hour[hour_key]["wait_triage_vals"].append(w2)
        if w1 is not None and w2 is not None:
            by_hour[hour_key]["total_wait_vals"].append(w1 + w2)

    summary = {}
    for hour, stats in sorted(by_hour.items()):
        w1_list = stats["wait_checkin_vals"]
        w2_list = stats["wait_triage_vals"]
        tw_list = stats["total_wait_vals"]

        summary[hour] = {
            "scheduled_appointments": stats["scheduled_count"],
            "avg_wait_checkin_to_triage_mins": round(sum(w1_list) / len(w1_list), 1) if w1_list else None,
            "avg_wait_triage_to_consult_mins": round(sum(w2_list) / len(w2_list), 1) if w2_list else None,
            "avg_combined_wait_mins": round(sum(tw_list) / len(tw_list), 1) if tw_list else None,
            "sample_size": len(tw_list)
        }

    return summary


def compute_all_metrics(
    modeled_rows: List[Dict[str, Any]],
    output_dir: str = "processed"
) -> Dict[str, Any]:
    """
    Computes all headline KPIs and dimensional breakdowns, then exports metrics_summary.json.
    """
    headline_metrics = {
        "metric_1_avg_wait_checkin_to_triage": calculate_wait_checkin_to_triage(modeled_rows),
        "metric_2_avg_wait_triage_to_consult": calculate_wait_triage_to_consult(modeled_rows),
        "metric_3_no_show_rate": calculate_no_show_rate(modeled_rows),
        "metric_4_consult_overrun_rate": calculate_consult_overrun_rate(modeled_rows),
        "metric_5_cancellation_rate": calculate_cancellation_rate(modeled_rows),
    }

    dimensional_breakdowns = {
        "by_doctor": calculate_doctor_breakdown(modeled_rows),
        "by_scheduled_hour": calculate_hourly_bottleneck_breakdown(modeled_rows)
    }

    full_summary = {
        "project_kpi_question": "Where does patient wait time accumulate, and which factors predict a longer wait?",
        "generated_at": datetime.now().isoformat(),
        "headline_metrics": headline_metrics,
        "dimensional_breakdowns": dimensional_breakdowns
    }

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "metrics_summary.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(full_summary, f, indent=2)

    return full_summary


if __name__ == "__main__":
    modeled_path = "processed/modeled_appointments.csv"
    if not os.path.exists(modeled_path):
        print(f"Error: {modeled_path} not found. Please run model.py first.")
        exit(1)

    with open(modeled_path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print("Computing operational metrics from modeled appointments...")
    summary = compute_all_metrics(rows, "processed")

    hm = summary["headline_metrics"]
    print("\n=======================================================")
    print("           CLINIC OPERATIONAL METRICS SUMMARY          ")
    print("=======================================================")
    print(f"1. Avg Wait (Check-in -> Triage)   : {hm['metric_1_avg_wait_checkin_to_triage']['average_minutes']} mins (sample: {hm['metric_1_avg_wait_checkin_to_triage']['valid_sample_count']})")
    print(f"2. Avg Wait (Triage -> Consult)    : {hm['metric_2_avg_wait_triage_to_consult']['average_minutes']} mins (sample: {hm['metric_2_avg_wait_triage_to_consult']['valid_sample_count']})")
    print(f"3. No-Show Rate                    : {hm['metric_3_no_show_rate']['rate_percent']}% ({hm['metric_3_no_show_rate']['no_show_count']}/{hm['metric_3_no_show_rate']['total_scheduled_appointments']})")
    print(f"4. Consult Overrun Rate (>20 mins) : {hm['metric_4_consult_overrun_rate']['overrun_rate_percent']}% ({hm['metric_4_consult_overrun_rate']['overrun_count']}/{hm['metric_4_consult_overrun_rate']['valid_consult_count']})")
    print(f"5. Cancellation Rate               : {hm['metric_5_cancellation_rate']['rate_percent']}% ({hm['metric_5_cancellation_rate']['cancelled_count']}/{hm['metric_5_cancellation_rate']['total_scheduled_appointments']})")
    print("=======================================================")
    print("Saved complete summary to -> processed/metrics_summary.json")
