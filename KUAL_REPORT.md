# Known, Unknown, Assumption, and Limitation (KUAL) Report

> **Project**: Clinic Appointment Workflow & Operational KPI Pipeline  
> **Author**: Nachiketas Iyer (Forward Deployed Engineer)  
> **Target Audience**: Clinic Leadership, Clinical Operations, Lead Data Engineer  

---

## 1. Knowns (Grounded Facts from Validated Data)

These facts are definitively established by our multi-source ingestion and validation engine:

1. **Scheduled Booking Volume & Attrition**:
   - Out of **220 distinct scheduled appointments**, exactly **179 (81.36%)** were completed, **28 (12.73%)** resulted in no-shows, and **13 (5.91%)** were cancelled.
2. **Physical Clinical Stage Durations**:
   - On valid completed visits, the average patient wait from kiosk check-in to nurse triage is **19.80 minutes** ($N=166$).
   - The average wait from triage completion to seeing the physician is **20.57 minutes** ($N=173$).
   - The average physician consultation duration is **19.73 minutes** ($N=171$).
3. **Severe Consult Overrun Frequency**:
   - **47.37% (81 / 171)** of completed consultations exceed the standard 20.0-minute appointment slot benchmark.
   - Provider overruns range from **42.2% (Dr. Okafor)** to **52.8% (Dr. Alvarez)**.
4. **Billing Leakage / Reconciliation Gap**:
   - Exactly **19 out of 179 completed appointments (10.61%)** have no record in `billing_log.csv`, representing unsubmitted or delayed revenue claims.
5. **Systemic Telemetry Characteristics**:
   - Kiosk hardware operates on a consistent **+3.0 minute clock skew** relative to scheduling database timestamps.
   - Kiosk exports utilize numeric-only patient identifiers (e.g. `"42"`) vs. the canonical scheduling format (`"PT0042"`).

---

## 2. Unknowns (Gaps in Client Instrumentation)

These data points cannot be determined from the available client datasets and require operational investigation:

1. **Reason for Missing Billing Records**:
   - Whether the 19 unbilled visits reflect provider documentation delays, billing system export batch cutoff discrepancies, or rejected claims cannot be determined from `billing_log.csv` alone.
2. **Identity & Clinical Context of Walk-in Patients**:
   - The 3 walk-in series (`WALKIN_W1`, `WALKIN_W2`, `WALKIN_W3`) capture full triage and consult durations, but lack patient medical history, billing records, or doctor assignments.
3. **Root Cause of Kiosk Scan Dropouts**:
   - The 8 missing stage events (e.g., missing `check_in` or `consult_start`) could be caused by hardware sensor failure, patient badge scan avoidance, or nurse station manual bypass.
4. **Scheduled Appointment Type / Intended Slot Duration**:
   - `scheduling.db` logs appointment times in 20-minute grid increments but does not explicitly store the scheduled appointment type (e.g. new patient vs follow-up).

---

## 3. Assumptions (Engineering & Operational Premises)

1. **Standard 20.0-Minute Slot Benchmark**:
   - *Assumption*: All scheduled appointments are evaluated against a standard 20.0-minute slot length.
   - *Rationale*: Confirmed by front-desk scheduling booking increments (`range(0, 480, 20)` in `generate_data.py`).
2. **Deterministic Kiosk Clock Skew Offset (-3 Minutes)**:
   - *Assumption*: The +3-minute delta in `kiosk_events.json` is a fixed hardware clock offset, not random sensor jitter.
   - *Rationale*: Telemetry demonstrates a constant systemic shift across all event types. Adjusting by -3 minutes aligns arrival drift relative to `scheduled_time`.
3. **Deduplication Priority (Keep First)**:
   - *Assumption*: For the 4 duplicate appointment IDs in `scheduling.db`, the first chronologically ingested record represents the primary booking intent.
4. **Zero-Imputation Rejection**:
   - *Assumption*: Corrupted or missing stage timestamps must **never** be imputed as 0 minutes or filled with sample medians, as this would artificially deflate wait-time metrics.

---

## 4. Limitations (Constraints of Current Analysis)

1. **Unresolvable Duplicate Telemetry Streams (`A0077`, `A0089`, `A0151`, `A0164`)**:
   - Because front-desk duplicate bookings caused the kiosk system to record two distinct 5-event streams for the same `appointment_id` without session or device tokens, it is physically impossible to determine which stream corresponds to the true patient encounter.
   - *Mitigation*: These 4 appointments are preserved in the master model with their booking status, but all interval durations are quarantined (`None`) and excluded from wait-time denominators.
2. **Fixed vs. Variable Slot Sizing**:
   - Billing logs contain codes suggesting variable clinical intensity (`GEN-20`, `GEN-40`, `FOLLOWUP-15`). However, because scheduling.db lacks slot-length metadata, consult overruns are evaluated uniformly against 20 minutes.
3. **Sample Size for Provider-Level Metrics**:
   - With ~50 appointments per doctor over a 2-week observation window, provider-level rates should be interpreted as directional rather than statistically definitive. The sample size is too small for strong statistical confidence without multi-month pooling.
4. **Duplicate Billing Log Records**:
   - The 4 duplicate front-desk bookings also produced duplicate rows in `billing_log.csv` with differing billing codes and durations. While this does not impact our operational KPI pipeline (which strictly uses physical kiosk telemetry for consult durations rather than billing claims), any financial analysis summing `billed_minutes` would require primary-key deduplication prior to revenue calculations.

---

## 5. FDE Judgment Calls & Defensible Decisions

### Trade-Off 1: Surgical Interval Quarantine vs. Full Record Deletion
* **The Scenario**: 5 appointments (`A0015`, `A0035`, `A0073`, `A0129`, `A0140`) had `triage_start` logged before `check_in` due to kiosk scanning inversion.
* **Alternative Considered**: Drop the 5 appointments entirely from all downstream models.
* **FDE Decision**: **Surgically quarantine only the corrupted interval (`wait_checkin_to_triage`)**, while keeping valid subsequent stages (`wait_triage_to_consult`, `consult_duration`).
* **Justification**: Dropping the entire appointment would bias the clinic's completion rate, no-show rate, and doctor consultation duration metrics. A broken initial scan does not invalidate the subsequent 25-minute consultation.

### Trade-Off 2: Explicit Quarantine Table vs. In-Place Silent Imputation
* **The Scenario**: Missing billing entries, duplicate IDs, and dropped scans were detected across the sources.
* **Alternative Considered**: Silently coerce missing values (e.g. fill missing billed minutes with average consult duration; drop duplicate IDs silently).
* **FDE Decision**: **Write all 47 flagged anomalies to `processed/validation_issues.csv` with explicit `issue_type`, `severity`, and `fde_action` attributes.**
* **Justification**: Silent data cleaning destroys auditability. By preserving raw data and logging issues explicitly, clinic leadership can use this report to trigger workflow corrections (e.g., fixing front-desk double-entry training and kiosk sensor maintenance).
