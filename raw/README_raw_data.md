# Raw source data (generated)

Run `python3 generate_data.py` from the project root to (re)create these three files.
Same random seed every time, so the messiness below is stable and your validation
rules can be tested against known cases.

## 1. `scheduling.db` (SQLite) — front-desk booking system
Table: `appointments(appointment_id, patient_id, doctor, scheduled_time, booked_at, status)`
- `status` is one of `completed`, `no_show`, `cancelled`
- ~4 duplicate `appointment_id` rows (double-entry at the front desk)
- ~6 rows with `doctor = NULL` (late-added walk-in slots, unassigned at booking time)

## 2. `kiosk_events.json` — check-in kiosk / nurse station event export
One row per event per appointment: `check_in`, `triage_start`, `triage_end`,
`consult_start`, `consult_end`.
- Only `completed` appointments generate events (no-shows/cancellations never check in)
- `patient_id_kiosk` uses a different format than `patient_id` in scheduling.db
  (e.g. `"129"` vs `"PT0129"`) — a deliberate join mismatch
- Kiosk clock runs ~3 minutes fast relative to the scheduling system (systemic skew,
  not random noise)
- ~5 appointments have `triage_start` logged *before* `check_in` — physically
  impossible, should be caught by validation
- 3 synthetic "true walk-ins": kiosk events with `appointment_id = null`, i.e. no
  matching row in scheduling.db at all
- ~8 events dropped entirely (sensor/kiosk missed a scan)

## 3. `billing_log.csv` — billing system export
- Only `completed` appointments are billed
- ~10% of completed appointments are missing from this file (not yet reconciled)

## Known join keys / gotchas
- `appointment_id` is the join key between scheduling.db and billing_log.csv (same format)
- `appointment_id` also joins kiosk_events.json — EXCEPT for the 3 walk-ins, which
  have no appointment_id at all and must be handled as a separate case
- `patient_id` (scheduling.db) vs `patient_id_kiosk` (kiosk_events.json) are NOT
  directly joinable without a format transform
