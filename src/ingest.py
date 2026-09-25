"""
ingest.py - Multi-modal data ingestion for clinic workflow sources.

Retrieves data across three distinct source formats:
  1. SQL database query (SQLite) -> scheduling.db
  2. Structured JSON file reader -> kiosk_events.json
  3. Delimited text parser (CSV) -> billing_log.csv

Demonstrates retrieval completeness checks and preserves raw inputs as immutable.
"""

import os
import sqlite3
import json
import csv
from typing import Dict, List, Any, Tuple


def load_scheduling_db(db_path: str = "raw/scheduling.db") -> List[Dict[str, Any]]:
    """
    Retrieves appointment bookings from front-desk SQLite database via SQL query.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Scheduling database not found at {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # Enables dictionary-like column access
    cursor = conn.cursor()

    query = """
        SELECT 
            appointment_id,
            patient_id,
            doctor,
            scheduled_time,
            booked_at,
            status
        FROM appointments
    """
    cursor.execute(query)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def load_kiosk_events(json_path: str = "raw/kiosk_events.json") -> List[Dict[str, Any]]:
    """
    Retrieves check-in kiosk and nurse station timestamp events from JSON export.
    """
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"Kiosk event file not found at {json_path}")

    with open(json_path, "r", encoding="utf-8") as f:
        events = json.load(f)

    if not isinstance(events, list):
        raise ValueError(f"Expected JSON list in {json_path}, got {type(events).__name__}")

    return events


def load_billing_log(csv_path: str = "raw/billing_log.csv") -> List[Dict[str, Any]]:
    """
    Retrieves completed visit billing records from CSV export.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Billing log CSV not found at {csv_path}")

    records = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Cast numeric field where applicable
            record = {
                "appointment_id": row["appointment_id"],
                "billed_minutes": row.get("billed_minutes", "").strip(),
                "billing_code": row.get("billing_code", "").strip()
            }
            records.append(record)

    return records


def ingest_all(raw_dir: str = "raw") -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    """
    Loads all raw sources and builds an audit manifest to confirm retrieval sanity.
    """
    db_path = os.path.join(raw_dir, "scheduling.db")
    json_path = os.path.join(raw_dir, "kiosk_events.json")
    csv_path = os.path.join(raw_dir, "billing_log.csv")

    raw_data = {
        "appointments": load_scheduling_db(db_path),
        "kiosk_events": load_kiosk_events(json_path),
        "billing_log": load_billing_log(csv_path),
    }

    # Ingestion audit manifest (records retrieved & basic non-empty sanity check)
    manifest = {
        "sources": {
            "scheduling_db": {
                "path": db_path,
                "retrieval_mode": "SQL query (sqlite3)",
                "records_retrieved": len(raw_data["appointments"]),
                "is_empty": len(raw_data["appointments"]) == 0
            },
            "kiosk_events": {
                "path": json_path,
                "retrieval_mode": "JSON document parse",
                "records_retrieved": len(raw_data["kiosk_events"]),
                "is_empty": len(raw_data["kiosk_events"]) == 0
            },
            "billing_log": {
                "path": csv_path,
                "retrieval_mode": "Delimited CSV stream",
                "records_retrieved": len(raw_data["billing_log"]),
                "is_empty": len(raw_data["billing_log"]) == 0
            }
        },
        "all_sources_loaded": all(
            len(data) > 0 for data in raw_data.values()
        )
    }

    return raw_data, manifest


if __name__ == "__main__":
    print("Running ingestion test across all 3 source modes...")
    data, manifest = ingest_all("raw")
    print(f"  [SQL DB]   Loaded {manifest['sources']['scheduling_db']['records_retrieved']} appointments")
    print(f"  [JSON]     Loaded {manifest['sources']['kiosk_events']['records_retrieved']} kiosk events")
    print(f"  [CSV]      Loaded {manifest['sources']['billing_log']['records_retrieved']} billing records")
    print(f"Ingestion source load check: {'PASSED' if manifest['all_sources_loaded'] else 'FAILED'}")
