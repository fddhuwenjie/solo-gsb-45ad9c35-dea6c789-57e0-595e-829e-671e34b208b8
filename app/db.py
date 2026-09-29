"""SQLite persistence layer. A single connection guarded by a lock keeps the
settlement recompute (read events -> rewrite trips/charges) atomic."""
from __future__ import annotations

import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT PRIMARY KEY,
    passenger_id   TEXT NOT NULL,
    station_id     TEXT NOT NULL,
    tap_type       TEXT NOT NULL CHECK (tap_type IN ('in', 'out')),
    occurred_at    TEXT NOT NULL,          -- ISO-8601, normalised to UTC
    seq            INTEGER NOT NULL,       -- arrival order, tie-breaks equal timestamps
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('paired', 'pending')),
    pending_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_passenger ON events (passenger_id, occurred_at, seq);

CREATE TABLE IF NOT EXISTS trips (
    trip_id       TEXT PRIMARY KEY,        -- "<in_event_id>:<out_event_id>"
    passenger_id  TEXT NOT NULL,
    in_event_id   TEXT NOT NULL,
    out_event_id  TEXT NOT NULL,
    from_station  TEXT NOT NULL,
    to_station    TEXT NOT NULL,
    from_zone     INTEGER NOT NULL,
    to_zone       INTEGER NOT NULL,
    started_at    TEXT NOT NULL,
    ended_at      TEXT NOT NULL,
    day_key       TEXT NOT NULL,           -- UTC date of tap-in, YYYY-MM-DD
    week_key      TEXT NOT NULL            -- ISO week of tap-in, e.g. 2026-W39
);
CREATE INDEX IF NOT EXISTS idx_trips_passenger ON trips (passenger_id, started_at);

CREATE TABLE IF NOT EXISTS trip_charges (
    trip_id                  TEXT PRIMARY KEY,
    passenger_id             TEXT NOT NULL,
    day_key                  TEXT NOT NULL,
    week_key                 TEXT NOT NULL,
    base_fare_cents          INTEGER NOT NULL,
    transfer_discount_cents  INTEGER NOT NULL,
    amount_cents             INTEGER NOT NULL,   -- final charge after caps
    basis                    TEXT NOT NULL       -- JSON: full billing basis
);
CREATE INDEX IF NOT EXISTS idx_charges_passenger ON trip_charges (passenger_id, day_key);

CREATE TABLE IF NOT EXISTS recon_batches (
    batch_id              TEXT PRIMARY KEY,
    payload_hash          TEXT NOT NULL,     -- sha256 of the canonical request
    window_start          TEXT NOT NULL,     -- ISO-8601, normalised to UTC
    window_end            TEXT NOT NULL,     -- half-open window on tap-out time
    tolerance_seconds     INTEGER NOT NULL,
    snapshot_trip_count   INTEGER NOT NULL,
    external_record_count INTEGER NOT NULL,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recon_results (
    batch_id                TEXT NOT NULL REFERENCES recon_batches(batch_id),
    seq                     INTEGER NOT NULL,
    category                TEXT NOT NULL,
    passenger_id            TEXT NOT NULL,
    trip_id                 TEXT,
    serial_no               TEXT,
    internal_amount_cents   INTEGER,
    external_amount_cents   INTEGER,
    difference_cents        INTEGER,
    internal_ended_at       TEXT,
    external_charged_at     TEXT,
    match_method            TEXT,
    reason                  TEXT NOT NULL,
    PRIMARY KEY (batch_id, seq)
);

-- Transfer network validations. A validation_id maps at most one request;
-- the canonical payload hash enforces idempotency (same payload replays the
-- stored result, different payload -> 409). Rows are append-only: a frozen
-- snapshot or a past validation must never be rewritten by a later call.
CREATE TABLE IF NOT EXISTS network_validations (
    validation_id   TEXT PRIMARY KEY,
    payload_hash    TEXT NOT NULL,     -- sha256 of the canonical request
    payload         TEXT NOT NULL,     -- canonical JSON of the normalised request
    effective_start TEXT NOT NULL,     -- ISO-8601, normalised to UTC
    effective_end   TEXT NOT NULL,     -- half-open window
    passed          INTEGER NOT NULL,  -- 1 = no conflict, 0 = conflict found
    result          TEXT NOT NULL,     -- JSON: the exact API response
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS network_snapshots (
    snapshot_id   TEXT PRIMARY KEY,    -- the frozen validation_id
    payload       TEXT NOT NULL,       -- canonical JSON copied at freeze time
    result        TEXT NOT NULL,       -- validation response copied at freeze time
    created_at    TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_network_validations_no_update
BEFORE UPDATE ON network_validations
BEGIN
    SELECT RAISE(ABORT, 'network_validations is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_network_validations_no_delete
BEFORE DELETE ON network_validations
BEGIN
    SELECT RAISE(ABORT, 'network_validations is append-only: DELETE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_network_snapshots_no_update
BEFORE UPDATE ON network_snapshots
BEGIN
    SELECT RAISE(ABORT, 'network_snapshots is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_network_snapshots_no_delete
BEFORE DELETE ON network_snapshots
BEGIN
    SELECT RAISE(ABORT, 'network_snapshots is append-only: DELETE is not allowed');
END;

-- Fare disputes. A dispute row is written once when the dispute is opened and
-- is never updated: the open/resolved state and the optimistic version are
-- derived from whether an immutable refund row exists (version 1 = open,
-- version 2 = resolved). The charge snapshot freezes the trip receivable at
-- creation time; later event corrections recompute the live trip_charges row
-- but can never rewrite the frozen amount or the settled refund.
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id            TEXT PRIMARY KEY,
    payload_hash          TEXT NOT NULL,     -- sha256 of the canonical request
    passenger_id          TEXT NOT NULL,
    trip_id               TEXT NOT NULL,
    claimed_amount_cents  INTEGER NOT NULL,  -- requested refund
    reason                TEXT NOT NULL,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispute_snapshots (
    dispute_id                 TEXT PRIMARY KEY REFERENCES disputes(dispute_id),
    trip_id                    TEXT NOT NULL,
    passenger_id               TEXT NOT NULL,
    amount_cents               INTEGER NOT NULL,  -- frozen receivable
    base_fare_cents            INTEGER NOT NULL,
    transfer_discount_cents    INTEGER NOT NULL,
    basis                      TEXT NOT NULL,     -- JSON: full billing basis
    frozen_at                  TEXT NOT NULL
);

-- Immutable refund ledger. Exactly one row per resolved dispute, written in
-- the same transaction that closes it; no later correction can overwrite it.
CREATE TABLE IF NOT EXISTS refunds (
    refund_id             TEXT PRIMARY KEY,     -- "RF-<dispute_id>"
    dispute_id            TEXT NOT NULL UNIQUE REFERENCES disputes(dispute_id),
    passenger_id          TEXT NOT NULL,
    trip_id               TEXT NOT NULL,
    amount_cents          INTEGER NOT NULL,     -- approved refund, immutable
    payload_hash          TEXT NOT NULL,        -- sha256 of the canonical resolve request
    review_note           TEXT,
    created_at            TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_disputes_no_update
BEFORE UPDATE ON disputes
BEGIN
    SELECT RAISE(ABORT, 'disputes is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_disputes_no_delete
BEFORE DELETE ON disputes
BEGIN
    SELECT RAISE(ABORT, 'disputes is append-only: DELETE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_dispute_snapshots_no_update
BEFORE UPDATE ON dispute_snapshots
BEGIN
    SELECT RAISE(ABORT, 'dispute_snapshots is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_dispute_snapshots_no_delete
BEFORE DELETE ON dispute_snapshots
BEGIN
    SELECT RAISE(ABORT, 'dispute_snapshots is append-only: DELETE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_refunds_no_update
BEFORE UPDATE ON refunds
BEGIN
    SELECT RAISE(ABORT, 'refunds is an immutable ledger: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_refunds_no_delete
BEFORE DELETE ON refunds
BEGIN
    SELECT RAISE(ABORT, 'refunds is an immutable ledger: DELETE is not allowed');
END;

-- Tap-event quality scans. These tables are deliberately isolated from the
-- settlement domain: a scan only ever reads events and appends its own rows.
-- Events, trips, trip_charges, disputes and the audit_log are never touched by
-- a scan, and scan writes do not append audit records. A scan_id maps to at
-- most one request; the canonical payload hash enforces idempotency (same
-- payload replays the stored result, different payload -> 409).
CREATE TABLE IF NOT EXISTS event_quality_scans (
    scan_id                   TEXT PRIMARY KEY,
    payload_hash              TEXT NOT NULL,     -- sha256 of the canonical request
    window_start              TEXT NOT NULL,     -- ISO-8601, normalised to UTC
    window_end                TEXT NOT NULL,     -- half-open window on occurred_at
    passengers                TEXT,              -- JSON allow-list, NULL = all passengers
    duplicate_window_seconds  INTEGER NOT NULL,
    min_travel_seconds        TEXT NOT NULL,     -- JSON: {"OUT": {"IN": seconds}}
    event_count               INTEGER NOT NULL,  -- events scanned inside the window
    alert_count               INTEGER NOT NULL,
    created_at                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_quality_alerts (
    scan_id                   TEXT NOT NULL REFERENCES event_quality_scans(scan_id),
    seq                       INTEGER NOT NULL,  -- 0-based, in scan order
    event_id                  TEXT NOT NULL,     -- the triggering (later) event
    passenger_id              TEXT NOT NULL,
    rule                      TEXT NOT NULL CHECK (rule IN
                                ('duplicate_tap', 'min_travel_violation')),
    actual_interval_seconds   REAL NOT NULL,     -- elapsed gap between evidence events
    threshold_seconds         INTEGER NOT NULL,  -- configured bound that was breached
    evidence                  TEXT NOT NULL,     -- JSON: ordered chain of tap events
    PRIMARY KEY (scan_id, seq)
);

CREATE TRIGGER IF NOT EXISTS trg_event_quality_scans_no_update
BEFORE UPDATE ON event_quality_scans
BEGIN
    SELECT RAISE(ABORT, 'event_quality_scans is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_event_quality_scans_no_delete
BEFORE DELETE ON event_quality_scans
BEGIN
    SELECT RAISE(ABORT, 'event_quality_scans is append-only: DELETE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_event_quality_alerts_no_update
BEFORE UPDATE ON event_quality_alerts
BEGIN
    SELECT RAISE(ABORT, 'event_quality_alerts is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_event_quality_alerts_no_delete
BEFORE DELETE ON event_quality_alerts
BEGIN
    SELECT RAISE(ABORT, 'event_quality_alerts is append-only: DELETE is not allowed');
END;

-- Anonymous k-anonymous trajectory exports. Like the quality scans these
-- tables are deliberately isolated: an export only ever reads trips and
-- appends its own rows. Events, trips, trip_charges, disputes and the
-- audit_log are never touched, and export writes do not append audit records.
-- An export_id maps to at most one request; the canonical payload hash
-- enforces idempotency (same payload replays the stored result, different
-- payload -> 409). Only pseudonymised (HMAC) passenger identifiers are ever
-- stored here; raw passenger ids are never written.
CREATE TABLE IF NOT EXISTS privacy_exports (
    export_id                   TEXT PRIMARY KEY,
    payload_hash                TEXT NOT NULL,     -- sha256 of the canonical request
    window_start                TEXT NOT NULL,     -- ISO-8601, normalised to UTC
    window_end                  TEXT NOT NULL,     -- half-open window on started_at
    bucket_minutes              INTEGER NOT NULL,  -- epoch-anchored UTC buckets
    k                           INTEGER NOT NULL,  -- min distinct passengers per class
    station_groups              TEXT NOT NULL,     -- canonical JSON: {station: group}
    input_trip_count            INTEGER NOT NULL,
    equivalence_class_count     INTEGER NOT NULL,
    retained_class_count        INTEGER NOT NULL,
    suppressed_class_count      INTEGER NOT NULL,
    retained_trip_count         INTEGER NOT NULL,
    suppressed_trip_count       INTEGER NOT NULL,
    created_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS privacy_export_trips (
    export_id        TEXT NOT NULL REFERENCES privacy_exports(export_id),
    seq              INTEGER NOT NULL,  -- 0-based, in export order
    pseudonym        TEXT NOT NULL,     -- HMAC-SHA256 of passenger_id
    bucket_start     TEXT NOT NULL,     -- start of the epoch-anchored UTC bucket
    origin_group     TEXT NOT NULL,
    destination_group TEXT NOT NULL,
    PRIMARY KEY (export_id, seq)
);

CREATE TRIGGER IF NOT EXISTS trg_privacy_exports_no_update
BEFORE UPDATE ON privacy_exports
BEGIN
    SELECT RAISE(ABORT, 'privacy_exports is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_privacy_exports_no_delete
BEFORE DELETE ON privacy_exports
BEGIN
    SELECT RAISE(ABORT, 'privacy_exports is append-only: DELETE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_privacy_export_trips_no_update
BEFORE UPDATE ON privacy_export_trips
BEGIN
    SELECT RAISE(ABORT, 'privacy_export_trips is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_privacy_export_trips_no_delete
BEFORE DELETE ON privacy_export_trips
BEGIN
    SELECT RAISE(ABORT, 'privacy_export_trips is append-only: DELETE is not allowed');
END;

-- Sudden-disruption evacuation replans. Like the quality scans and privacy
-- exports these tables are deliberately isolated: a replan computes purely
-- from the request payload and appends its own frozen result. Events, trips,
-- charges, disputes and the audit_log are never touched, and replan writes do
-- not append audit records. A replan_id maps to at most one request; the
-- canonical payload hash enforces idempotency (same payload replays the stored
-- result, different payload -> 409).
CREATE TABLE IF NOT EXISTS disruption_replans (
    replan_id              TEXT PRIMARY KEY,
    payload_hash           TEXT NOT NULL,     -- sha256 of the canonical request
    payload                TEXT NOT NULL,     -- canonical JSON of the normalised request
    min_transfer_seconds   INTEGER NOT NULL,
    group_count            INTEGER NOT NULL,
    assigned_count         INTEGER NOT NULL,
    stranded_count         INTEGER NOT NULL,
    result                 TEXT NOT NULL,     -- JSON: the exact API response
    created_at             TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_disruption_replans_no_update
BEFORE UPDATE ON disruption_replans
BEGIN
    SELECT RAISE(ABORT, 'disruption_replans is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_disruption_replans_no_delete
BEFORE DELETE ON disruption_replans
BEGIN
    SELECT RAISE(ABORT, 'disruption_replans is append-only: DELETE is not allowed');
END;

-- Append-only, hash-chained business audit log. One global sequence spans all
-- operation types; record_n hashes seq | operation | timestamp | canonical
-- request/result summaries | record_{n-1}'s hash. Rows are only ever inserted
-- inside the same transaction as the business write they describe.
CREATE TABLE IF NOT EXISTS audit_log (
    seq              INTEGER PRIMARY KEY,   -- global monotonic sequence, starts at 1
    operation        TEXT NOT NULL CHECK (operation IN
                         ('event_processed', 'event_corrected',
                          'recon_batch_created', 'network_validated',
                          'network_snapshot_created',
                          'dispute_opened', 'dispute_resolved')),
    occurred_at      TEXT NOT NULL,         -- ISO-8601, normalised to UTC
    request_summary  TEXT NOT NULL,         -- canonical JSON of the normalised request
    result_summary   TEXT NOT NULL,         -- canonical JSON of the normalised result
    prev_hash        TEXT NOT NULL,         -- previous record_hash, '' for the genesis record
    record_hash      TEXT NOT NULL          -- sha256 over this record's canonical content
);

CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE is not allowed');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: DELETE is not allowed');
END;
"""

# Rebuild statement used when upgrading a database created before a newer
# operation type (network validation, then fare disputes): SQLite cannot ALTER
# a CHECK constraint, so the audit table is rebuilt with the rows preserved
# (its hash chain lives entirely in columns).
_AUDIT_OPERATIONS = (
    "event_processed", "event_corrected", "recon_batch_created",
    "network_validated", "network_snapshot_created",
    "dispute_opened", "dispute_resolved",
)

_AUDIT_UPGRADE = """
DROP TRIGGER IF EXISTS trg_audit_no_update;
DROP TRIGGER IF EXISTS trg_audit_no_delete;
ALTER TABLE audit_log RENAME TO audit_log_legacy;
CREATE TABLE audit_log (
    seq              INTEGER PRIMARY KEY,
    operation        TEXT NOT NULL CHECK (operation IN
                         ('event_processed', 'event_corrected',
                          'recon_batch_created', 'network_validated',
                          'network_snapshot_created',
                          'dispute_opened', 'dispute_resolved')),
    occurred_at      TEXT NOT NULL,
    request_summary  TEXT NOT NULL,
    result_summary   TEXT NOT NULL,
    prev_hash        TEXT NOT NULL,
    record_hash      TEXT NOT NULL
);
INSERT INTO audit_log (seq, operation, occurred_at, request_summary,
                       result_summary, prev_hash, record_hash)
SELECT seq, operation, occurred_at, request_summary, result_summary,
       prev_hash, record_hash FROM audit_log_legacy;
DROP TABLE audit_log_legacy;
CREATE TRIGGER trg_audit_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE is not allowed');
END;
CREATE TRIGGER trg_audit_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: DELETE is not allowed');
END;
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(SCHEMA)
            # Upgrade an older database whose audit_log CHECK predates one of
            # the current operation types (network validation, disputes).
            sql = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'"
            ).fetchone()[0]
            if any(op not in sql for op in _AUDIT_OPERATIONS):
                self._conn.executescript(_AUDIT_UPGRADE)

    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def online_backup(
        self,
        dst_path: str,
        *,
        pages: int = 512,
        progress=None,
    ) -> None:
        """Write a consistent snapshot of the live database to ``dst_path``.

        Uses the SQLite online backup API via a *separate* source connection,
        never a file copy: the backup pages through the database while readers
        and writers (tap submissions) keep running, restarting transparently
        whenever the source is modified mid-copy. The application lock is
        deliberately not held, so ``POST /events`` is not blocked.

        ``progress`` receives ``(status, remaining, page_count)`` after each
        batch, exactly like sqlite3's backup callback (``status`` is 1 when
        the copy restarted due to concurrent writes, 0 otherwise), and may
        sleep to throttle.
        """
        if self.path == ":memory:":
            # An in-memory database has no shared backing file; another
            # connection would see an empty database. Fall back to a
            # consistent copy taken under the lock (tests only).
            dest = sqlite3.connect(dst_path)
            try:
                with self._lock:
                    self._conn.backup(dest, pages=pages, progress=progress)
            finally:
                dest.close()
            return

        os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
        # Independent source connection: the backup neither shares the
        # application connection nor holds its lock. A read-only URI
        # connection guarantees the copy never disturbs the live database.
        uri = "file:" + self.path.replace("?", "%3f").replace("#", "%23")
        source = sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=30)
        dest = sqlite3.connect(dst_path, timeout=30)
        try:
            source.execute("PRAGMA busy_timeout=30000")
            source.backup(dest, pages=pages, progress=progress)
        finally:
            dest.close()
            source.close()
