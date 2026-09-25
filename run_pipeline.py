"""
run_pipeline.py - Master Pipeline Orchestrator for Clinic Appointment Workflow.

Executes the complete end-to-end data pipeline in sequence:
  1. Ingest   - Multi-modal retrieval (SQLite, JSON, CSV) & sanity auditing
  2. Validate - Profiling, business rule validation & issue quarantine logging
  3. Model    - Schema alignment, clock skew correction & interval transformation
  4. Metrics  - Operational KPI calculation & dimensional breakdowns

Features:
  - In-memory dataset chaining between stages with atomic file exports
  - Dual console and file logging (pipeline.log) with execution timestamps
  - Idempotent execution (safe to rerun repeatedly without row duplication)
  - Clear stage-aware error handling and standard process exit codes
"""

import os
import sys
import logging
from datetime import datetime
from typing import Optional

from src.ingest import ingest_all
from src.validate import validate_all
from src.model import build_modeled_dataset
from src.metrics import compute_all_metrics


def setup_logger(log_file: str = "pipeline.log") -> logging.Logger:
    """Configures structured dual-logging to stdout and persistent log file."""
    logger = logging.getLogger("ClinicPipeline")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (overwritten/appended cleanly with run headers)
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def run_pipeline(raw_dir: str = "raw", output_dir: str = "processed") -> bool:
    """
    Executes the 4-stage pipeline sequentially, returning True on success, False on failure.
    """
    logger = setup_logger("pipeline.log")
    start_time = datetime.now()

    logger.info("=" * 60)
    logger.info("CLINIC APPOINTMENT WORKFLOW PIPELINE - RUN STARTED")
    logger.info("=" * 60)

    # -------------------------------------------------------------------------
    # Stage 1: Ingestion
    # -------------------------------------------------------------------------
    logger.info("[STAGE 1/4] Starting multi-modal data ingestion from '%s/'...", raw_dir)
    try:
        raw_data, manifest = ingest_all(raw_dir)
        appts_count = len(raw_data["appointments"])
        events_count = len(raw_data["kiosk_events"])
        billing_count = len(raw_data["billing_log"])

        logger.info(
            "  -> Ingestion completed: %d appointments (SQL), %d kiosk events (JSON), %d billing rows (CSV)",
            appts_count, events_count, billing_count
        )
        if not manifest["all_sources_loaded"]:
            raise ValueError("One or more required raw sources returned empty datasets.")
    except Exception as e:
        logger.error("[STAGE FAILURE] Ingestion stage failed: %s", str(e))
        return False

    # -------------------------------------------------------------------------
    # Stage 2: Profiling & Validation
    # -------------------------------------------------------------------------
    logger.info("[STAGE 2/4] Starting data profiling and business rule validation...")
    try:
        issues, issue_counts = validate_all(raw_data, output_dir)
        logger.info("  -> Validation completed: %d total data quality issues flagged", len(issues))
        for itype, cnt in sorted(issue_counts.items()):
            logger.info("     - %s: %d", itype, cnt)
        logger.info("  -> Saved issue log to '%s/validation_issues.csv'", output_dir)
    except Exception as e:
        logger.error("[STAGE FAILURE] Validation stage failed: %s", str(e))
        return False

    # -------------------------------------------------------------------------
    # Stage 3: Workflow Modeling & Transformation
    # -------------------------------------------------------------------------
    logger.info("[STAGE 3/4] Starting workflow modeling and interval transformation...")
    try:
        modeled_rows = build_modeled_dataset(raw_data, output_dir)
        sched_count = sum(1 for r in modeled_rows if r["status"] != "walk_in")
        walkin_count = sum(1 for r in modeled_rows if r["status"] == "walk_in")
        logger.info(
            "  -> Modeling completed: %d total analytical records (%d scheduled + %d walk-ins)",
            len(modeled_rows), sched_count, walkin_count
        )
        logger.info("  -> Saved unified dataset to '%s/modeled_appointments.csv'", output_dir)
    except Exception as e:
        logger.error("[STAGE FAILURE] Modeling stage failed: %s", str(e))
        return False

    # -------------------------------------------------------------------------
    # Stage 4: KPI & Metrics Generation
    # -------------------------------------------------------------------------
    logger.info("[STAGE 4/4] Calculating operational KPIs and dimensional breakdowns...")
    try:
        metrics_summary = compute_all_metrics(modeled_rows, output_dir)
        hm = metrics_summary["headline_metrics"]
        logger.info("  -> KPI Summary:")
        logger.info("     * Avg Check-in -> Triage Wait: %s mins (n=%d)",
                    hm["metric_1_avg_wait_checkin_to_triage"]["average_minutes"],
                    hm["metric_1_avg_wait_checkin_to_triage"]["valid_sample_count"])
        logger.info("     * Avg Triage -> Consult Wait  : %s mins (n=%d)",
                    hm["metric_2_avg_wait_triage_to_consult"]["average_minutes"],
                    hm["metric_2_avg_wait_triage_to_consult"]["valid_sample_count"])
        logger.info("     * No-Show Rate               : %s%%",
                    hm["metric_3_no_show_rate"]["rate_percent"])
        logger.info("     * Consult Overrun Rate       : %s%% (n=%d)",
                    hm["metric_4_consult_overrun_rate"]["overrun_rate_percent"],
                    hm["metric_4_consult_overrun_rate"]["valid_consult_count"])
        logger.info("     * Cancellation Rate          : %s%%",
                    hm["metric_5_cancellation_rate"]["rate_percent"])
        logger.info("  -> Saved metrics summary to '%s/metrics_summary.json'", output_dir)
    except Exception as e:
        logger.error("[STAGE FAILURE] Metrics calculation stage failed: %s", str(e))
        return False

    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info("=" * 60)
    logger.info("PIPELINE EXECUTION COMPLETED SUCCESSFULLY (Duration: %.2fs)", elapsed)
    logger.info("=" * 60)
    return True


if __name__ == "__main__":
    success = run_pipeline("raw", "processed")
    sys.exit(0 if success else 1)
