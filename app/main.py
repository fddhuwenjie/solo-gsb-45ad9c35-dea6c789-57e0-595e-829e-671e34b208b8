"""Public transit fare settlement API."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from .audit import append_audit, canonical_json, verify_chain
from .config import FareConfig, load_config
from .db import Database
from .disputes import (
    canonical_dispute_payload,
    canonical_resolve_payload,
    dispute_response,
    payload_hash,
    refund_id_for,
)
from .disruption import (
    build_replan,
    canonical_replan_payload,
    normalise_input,
    payload_hash as replan_payload_hash,
)
from .event_quality import (
    canonical_scan_payload,
    load_window_events,
    payload_hash as scan_payload_hash,
    scan_events,
)
from .models import (
    Adjustment,
    AuditEntry,
    AuditList,
    AuditVerification,
    Bill,
    DisputeIn,
    DisputeOut,
    DisputeResolveIn,
    DisruptionReplanIn,
    DisruptionReplanOut,
    EventQualityScanIn,
    EventQualityScanOut,
    EventResult,
    NetworkSnapshotIn,
    NetworkSnapshotOut,
    NetworkValidateIn,
    NetworkValidateOut,
    PendingEvent,
    PeriodTotal,
    PrivacyEquivalenceClass,
    PrivacyExportIn,
    PrivacyExportOut,
    PrivacyExportTrip,
    QualityAlert,
    QualityEvidenceEvent,
    ReconBatchIn,
    ReconBatchOut,
    ReconResultItem,
    ReconSummary,
    TapEventIn,
    TripOut,
)
from .network import NetEdge, validate_network
from .privacy import (
    UnmappedStationsError,
    build_export,
    canonical_export_payload,
    classes_from_trip_rows,
    load_station_groups,
    load_window_trips,
    payload_hash as export_payload_hash,
    stored_groups_json,
)
from .reconcile import CATEGORIES, ExternalRecord, SnapshotTrip, reconcile
from .settlement import iso_str, normalize_ts, settle_passenger

DB_PATH = os.environ.get("DB_PATH", "data/transit.db")

# Server-side secret for stable HMAC passenger pseudonymisation in privacy
# exports. It must come from configuration (never from the request) and stay
# stable across restarts: changing it rotates every pseudonym. Missing/empty
# configuration fails startup rather than silently weakening anonymity.
PRIVACY_SALT = os.environ.get("PRIVACY_SALT", "").strip()

# Paging parameters for the online backup. The defaults copy in reasonably
# sized batches without throttling; tests override these to stretch the copy
# and prove concurrent writes keep landing while a backup is running.
BACKUP_PAGES_PER_BATCH = 512
BACKUP_PROGRESS_CALLBACK = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not PRIVACY_SALT:
        raise RuntimeError(
            "PRIVACY_SALT is not configured: set it to a long, server-side"
            " secret string (see README). It keeps passenger pseudonyms"
            " stable; changing it rotates every pseudonym."
        )
    app.state.config = load_config()
    app.state.privacy_salt = PRIVACY_SALT
    app.state.db = Database(DB_PATH)
    yield
    app.state.db.close()


app = FastAPI(
    title="Transit Fare Settlement API",
    version="1.8.0",
    lifespan=lifespan,
)


def get_config(request: Request) -> FareConfig:
    return request.app.state.config


def get_db(request: Request) -> Database:
    return request.app.state.db


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/config/fares")
def fare_config(cfg: FareConfig = Depends(get_config)) -> dict:
    return {
        "currency": cfg.currency,
        "transfer_window_minutes": cfg.transfer_window_minutes,
        "transfer_discount_cents": cfg.transfer_discount_cents,
        "daily_cap_cents": cfg.daily_cap_cents,
        "weekly_cap_cents": cfg.weekly_cap_cents,
        "stations": cfg.stations,
        "zone_fares": cfg.zone_fares,
    }


@app.post("/events", response_model=EventResult, status_code=200)
def submit_event(
    body: TapEventIn,
    cfg: FareConfig = Depends(get_config),
    db: Database = Depends(get_db),
) -> EventResult:
    if cfg.zone_of(body.station_id) is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown station_id '{body.station_id}'; see GET /config/fares",
        )
    occurred = normalize_ts(body.occurred_at)
    occurred_s = iso_str(occurred)

    with db.lock(), db.conn:
        existing = db.conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (body.event_id,)
        ).fetchone()

        if existing is not None:
            identical = (
                existing["passenger_id"] == body.passenger_id
                and existing["station_id"] == body.station_id
                and existing["tap_type"] == body.tap_type
                and existing["occurred_at"] == occurred_s
            )
            if identical:
                return EventResult(
                    event_id=body.event_id,
                    status="duplicate",
                    event_pairing=existing["status"],
                    pending_reason=existing["pending_reason"],
                    adjustments=[],
                )
            # Correction: same event id, different payload -> update and re-settle.
            db.conn.execute(
                "UPDATE events SET passenger_id = ?, station_id = ?, tap_type = ?,"
                " occurred_at = ? WHERE event_id = ?",
                (body.passenger_id, body.station_id, body.tap_type, occurred_s, body.event_id),
            )
            status = "corrected"
            affected = {existing["passenger_id"], body.passenger_id}
        else:
            seq = db.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events").fetchone()[0]
            db.conn.execute(
                "INSERT INTO events (event_id, passenger_id, station_id, tap_type,"
                " occurred_at, seq) VALUES (?,?,?,?,?,?)",
                (body.event_id, body.passenger_id, body.station_id, body.tap_type, occurred_s, seq),
            )
            status = "processed"
            affected = {body.passenger_id}

        adjustments: list[dict] = []
        for pid in affected:
            adjustments.extend(settle_passenger(db.conn, cfg, pid))

        row = db.conn.execute(
            "SELECT status, pending_reason FROM events WHERE event_id = ?", (body.event_id,)
        ).fetchone()

        result = EventResult(
            event_id=body.event_id,
            status=status,
            event_pairing=row["status"],
            pending_reason=row["pending_reason"],
            adjustments=[Adjustment(**a) for a in adjustments],
        )
        # Audit inside the same transaction: if the business write rolled
        # back, this record must not exist either.
        append_audit(
            db.conn,
            "event_processed" if status == "processed" else "event_corrected",
            {
                "event_id": body.event_id,
                "passenger_id": body.passenger_id,
                "station_id": body.station_id,
                "tap_type": body.tap_type,
                "occurred_at": occurred_s,
            },
            result.model_dump(),
        )

    return result


@app.get("/passengers/{passenger_id}/bill", response_model=Bill)
def passenger_bill(
    passenger_id: str,
    day: str | None = Query(default=None, description="UTC day, YYYY-MM-DD"),
    week: str | None = Query(default=None, description="ISO week, e.g. 2026-W39"),
    cfg: FareConfig = Depends(get_config),
    db: Database = Depends(get_db),
) -> Bill:
    sql = (
        "SELECT t.*, c.base_fare_cents, c.transfer_discount_cents, c.amount_cents, c.basis"
        " FROM trips t JOIN trip_charges c ON c.trip_id = t.trip_id"
        " WHERE t.passenger_id = ?"
    )
    params: list = [passenger_id]
    if day:
        sql += " AND t.day_key = ?"
        params.append(day)
    if week:
        sql += " AND t.week_key = ?"
        params.append(week)
    sql += " ORDER BY t.started_at, t.trip_id"

    with db.lock():
        rows = db.conn.execute(sql, params).fetchall()

    trips = [
        TripOut(
            trip_id=r["trip_id"],
            from_station=r["from_station"],
            to_station=r["to_station"],
            from_zone=r["from_zone"],
            to_zone=r["to_zone"],
            started_at=r["started_at"],
            ended_at=r["ended_at"],
            day_key=r["day_key"],
            week_key=r["week_key"],
            base_fare_cents=r["base_fare_cents"],
            transfer_discount_cents=r["transfer_discount_cents"],
            amount_cents=r["amount_cents"],
            basis=json.loads(r["basis"]),
        )
        for r in rows
    ]

    daily: dict[str, dict] = {}
    weekly: dict[str, dict] = {}
    for t in trips:
        d = daily.setdefault(t.day_key, {"amount": 0, "count": 0})
        d["amount"] += t.amount_cents
        d["count"] += 1
        w = weekly.setdefault(t.week_key, {"amount": 0, "count": 0})
        w["amount"] += t.amount_cents
        w["count"] += 1

    return Bill(
        passenger_id=passenger_id,
        currency=cfg.currency,
        trips=trips,
        daily_totals=[
            PeriodTotal(key=k, amount_cents=v["amount"], cap_cents=cfg.daily_cap_cents, trip_count=v["count"])
            for k, v in sorted(daily.items())
        ],
        weekly_totals=[
            PeriodTotal(key=k, amount_cents=v["amount"], cap_cents=cfg.weekly_cap_cents, trip_count=v["count"])
            for k, v in sorted(weekly.items())
        ],
        total_cents=sum(t.amount_cents for t in trips),
    )


@app.get("/pending", response_model=list[PendingEvent])
def pending_events(
    passenger_id: str | None = Query(default=None),
    db: Database = Depends(get_db),
) -> list[PendingEvent]:
    sql = "SELECT * FROM events WHERE status = 'pending'"
    params: list = []
    if passenger_id:
        sql += " AND passenger_id = ?"
        params.append(passenger_id)
    sql += " ORDER BY occurred_at, seq"
    with db.lock():
        rows = db.conn.execute(sql, params).fetchall()
    return [
        PendingEvent(
            event_id=r["event_id"],
            passenger_id=r["passenger_id"],
            station_id=r["station_id"],
            tap_type=r["tap_type"],
            occurred_at=r["occurred_at"],
            reason=r["pending_reason"],
        )
        for r in rows
    ]


def _recon_response(meta: dict, items: list[dict], status: str, currency: str) -> ReconBatchOut:
    counts = {c: 0 for c in CATEGORIES}
    for it in items:
        counts[it["category"]] += 1
    return ReconBatchOut(
        batch_id=meta["batch_id"],
        status=status,
        window_start=meta["window_start"],
        window_end=meta["window_end"],
        tolerance_seconds=meta["tolerance_seconds"],
        currency=currency,
        summary=ReconSummary(
            **counts,
            snapshot_trip_count=meta["snapshot_trip_count"],
            external_record_count=meta["external_record_count"],
        ),
        results=[ReconResultItem(**it) for it in items],
    )


def _load_recon_batch(db: Database, batch_id: str) -> tuple[dict, list[dict]]:
    b = db.conn.execute(
        "SELECT * FROM recon_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if b is None:
        raise HTTPException(
            status_code=404, detail=f"reconciliation batch '{batch_id}' not found"
        )
    rows = db.conn.execute(
        "SELECT category, passenger_id, trip_id, serial_no, internal_amount_cents,"
        " external_amount_cents, difference_cents, internal_ended_at,"
        " external_charged_at, match_method, reason"
        " FROM recon_results WHERE batch_id = ? ORDER BY seq",
        (batch_id,),
    ).fetchall()
    meta = {
        "batch_id": b["batch_id"],
        "window_start": b["window_start"],
        "window_end": b["window_end"],
        "tolerance_seconds": b["tolerance_seconds"],
        "snapshot_trip_count": b["snapshot_trip_count"],
        "external_record_count": b["external_record_count"],
    }
    return meta, [dict(r) for r in rows]


@app.post("/reconciliations", response_model=ReconBatchOut, status_code=200)
def create_reconciliation(
    body: ReconBatchIn,
    cfg: FareConfig = Depends(get_config),
    db: Database = Depends(get_db),
) -> ReconBatchOut:
    window_start = normalize_ts(body.window_start)
    window_end = normalize_ts(body.window_end)
    if window_end <= window_start:
        raise HTTPException(status_code=422, detail="window_end must be after window_start")
    serials = [r.serial_no for r in body.records]
    if len(set(serials)) != len(serials):
        raise HTTPException(status_code=422, detail="duplicate serial_no in records")

    records = [
        ExternalRecord(
            serial_no=r.serial_no,
            passenger_id=r.passenger_id,
            charged_at=normalize_ts(r.charged_at),
            amount_cents=r.amount_cents,
            trip_id=r.trip_id,
        )
        for r in body.records
    ]
    # Canonical payload: timestamps normalised, records sorted by serial_no,
    # so semantically identical submissions hash identically regardless of
    # record order or offset notation.
    canonical_payload = {
        "batch_id": body.batch_id,
        "window_start": iso_str(window_start),
        "window_end": iso_str(window_end),
        "tolerance_seconds": body.tolerance_seconds,
        "records": sorted(
            (
                {
                    "serial_no": r.serial_no,
                    "passenger_id": r.passenger_id,
                    "charged_at": iso_str(r.charged_at),
                    "amount_cents": r.amount_cents,
                    "trip_id": r.trip_id,
                }
                for r in records
            ),
            key=lambda d: d["serial_no"],
        ),
    }
    payload_hash = hashlib.sha256(
        canonical_json(canonical_payload).encode("utf-8")
    ).hexdigest()

    with db.lock(), db.conn:
        existing = db.conn.execute(
            "SELECT payload_hash FROM recon_batches WHERE batch_id = ?", (body.batch_id,)
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise HTTPException(
                    status_code=409,
                    detail=f"batch_id '{body.batch_id}' already exists with a different payload",
                )
            meta, items = _load_recon_batch(db, body.batch_id)
            return _recon_response(meta, items, "duplicate", cfg.currency)

        # Snapshot: internal trip charges whose tap-out time falls inside the
        # half-open window. Filtered in Python because stored ISO strings may
        # carry optional fractional seconds, which breaks lexicographic
        # comparison at the boundary.
        rows = db.conn.execute(
            "SELECT t.trip_id, t.passenger_id, t.ended_at, c.amount_cents"
            " FROM trips t JOIN trip_charges c ON c.trip_id = t.trip_id"
        ).fetchall()
        snapshot = []
        for r in rows:
            ended = normalize_ts(datetime.fromisoformat(r["ended_at"].replace("Z", "+00:00")))
            if window_start <= ended < window_end:
                snapshot.append(
                    SnapshotTrip(
                        trip_id=r["trip_id"],
                        passenger_id=r["passenger_id"],
                        ended_at=ended,
                        amount_cents=r["amount_cents"],
                    )
                )
        snapshot.sort(key=lambda t: (t.ended_at, t.trip_id))

        items = reconcile(snapshot, records, body.tolerance_seconds)
        meta = {
            "batch_id": body.batch_id,
            "window_start": iso_str(window_start),
            "window_end": iso_str(window_end),
            "tolerance_seconds": body.tolerance_seconds,
            "snapshot_trip_count": len(snapshot),
            "external_record_count": len(records),
        }
        db.conn.execute(
            "INSERT INTO recon_batches (batch_id, payload_hash, window_start, window_end,"
            " tolerance_seconds, snapshot_trip_count, external_record_count, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                body.batch_id,
                payload_hash,
                meta["window_start"],
                meta["window_end"],
                body.tolerance_seconds,
                len(snapshot),
                len(records),
                iso_str(datetime.now(timezone.utc)),
            ),
        )
        db.conn.executemany(
            "INSERT INTO recon_results (batch_id, seq, category, passenger_id, trip_id,"
            " serial_no, internal_amount_cents, external_amount_cents, difference_cents,"
            " internal_ended_at, external_charged_at, match_method, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    body.batch_id,
                    seq,
                    it["category"],
                    it["passenger_id"],
                    it["trip_id"],
                    it["serial_no"],
                    it["internal_amount_cents"],
                    it["external_amount_cents"],
                    it["difference_cents"],
                    it["internal_ended_at"],
                    it["external_charged_at"],
                    it["match_method"],
                    it["reason"],
                )
                for seq, it in enumerate(items)
            ],
        )

        response = _recon_response(meta, items, "created", cfg.currency)
        # Audit inside the same transaction as the batch insert: a failed
        # business write rolls the audit record back as well.
        append_audit(
            db.conn,
            "recon_batch_created",
            canonical_payload,
            response.model_dump(),
        )

    return response


# ---------------------------------------------------------------------------
# Transfer network validation
# ---------------------------------------------------------------------------


def _canonical_network_payload(body: NetworkValidateIn, start, end) -> dict:
    """Canonical request: timestamps normalised to UTC, station/hub lists and
    edge lists sorted, so semantically identical submissions hash identically
    regardless of array order or offset notation."""
    return {
        "validation_id": body.validation_id,
        "effective_start": iso_str(start),
        "effective_end": iso_str(end),
        "hubs": sorted(body.hubs),
        "stations": sorted(body.stations),
        "segments": sorted(
            (
                {
                    "line_id": s.line_id,
                    "segment_id": s.segment_id,
                    "from_station": s.from_station,
                    "to_station": s.to_station,
                    "start_at": iso_str(normalize_ts(s.start_at)),
                    "end_at": iso_str(normalize_ts(s.end_at)),
                }
                for s in body.segments
            ),
            key=lambda d: (
                d["line_id"], d["segment_id"], d["from_station"], d["to_station"],
                d["start_at"], d["end_at"],
            ),
        ),
        "transfers": sorted(
            (
                {
                    "from_station": t.from_station,
                    "to_station": t.to_station,
                    "start_at": iso_str(normalize_ts(t.start_at)),
                    "end_at": iso_str(normalize_ts(t.end_at)),
                }
                for t in body.transfers
            ),
            key=lambda d: (d["from_station"], d["to_station"], d["start_at"], d["end_at"]),
        ),
    }


def _validate_network_input(body: NetworkValidateIn, start, end):
    """Structural checks that make validation meaningless; returns an HTTP 422
    detail string, or None when the request is structurally sound."""
    if end <= start:
        return "effective_end must be after effective_start"
    if len(set(body.hubs)) != len(body.hubs):
        return "duplicate hub in hubs"
    if len(set(body.stations)) != len(body.stations):
        return "duplicate station in stations"
    unknown_hubs = sorted(set(body.hubs) - set(body.stations))
    if unknown_hubs:
        return f"hubs must themselves be served stations; unknown hubs: {unknown_hubs}"
    known = set(body.stations)
    for s in body.segments:
        s_start, s_end = normalize_ts(s.start_at), normalize_ts(s.end_at)
        if s_end <= s_start:
            return f"segment '{s.segment_id}' end_at must be after start_at"
        if s.from_station not in known:
            return f"segment '{s.segment_id}' references unknown from_station '{s.from_station}'"
        if s.to_station not in known:
            return f"segment '{s.segment_id}' references unknown to_station '{s.to_station}'"
    for t in body.transfers:
        if normalize_ts(t.end_at) <= normalize_ts(t.start_at):
            return "a walking transfer's end_at must be after start_at"
        if t.from_station not in known:
            return f"transfer references unknown from_station '{t.from_station}'"
        if t.to_station not in known:
            return f"transfer references unknown to_station '{t.to_station}'"
    return None


def _network_edges(body: NetworkValidateIn) -> list[NetEdge]:
    edges = [
        NetEdge(
            kind="segment",
            from_station=s.from_station,
            to_station=s.to_station,
            start=normalize_ts(s.start_at),
            end=normalize_ts(s.end_at),
            line_id=s.line_id,
            segment_id=s.segment_id,
        )
        for s in body.segments
    ]
    edges += [
        NetEdge(
            kind="transfer",
            from_station=t.from_station,
            to_station=t.to_station,
            start=normalize_ts(t.start_at),
            end=normalize_ts(t.end_at),
        )
        for t in body.transfers
    ]
    return edges


def _stored_validation_response(row, status: str) -> NetworkValidateOut:
    stored = json.loads(row["result"])
    stored["status"] = status  # the stored body always carries "created"
    return NetworkValidateOut(**stored)


@app.post("/network/validate", response_model=NetworkValidateOut, status_code=200)
def validate_transfer_network(
    body: NetworkValidateIn,
    db: Database = Depends(get_db),
) -> NetworkValidateOut:
    start = normalize_ts(body.effective_start)
    end = normalize_ts(body.effective_end)
    problem = _validate_network_input(body, start, end)
    if problem is not None:
        raise HTTPException(status_code=422, detail=problem)

    canonical_payload = _canonical_network_payload(body, start, end)
    payload_hash = hashlib.sha256(
        canonical_json(canonical_payload).encode("utf-8")
    ).hexdigest()

    with db.lock(), db.conn:
        existing = db.conn.execute(
            "SELECT * FROM network_validations WHERE validation_id = ?",
            (body.validation_id,),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"validation_id '{body.validation_id}' already exists"
                        " with a different payload"
                    ),
                )
            # Idempotent replay: the stored result is never recomputed; only
            # the replay marker is overwritten for the response.
            return _stored_validation_response(existing, "duplicate")

        report = validate_network(
            start, end, body.stations, body.hubs, _network_edges(body)
        )
        response = NetworkValidateOut(
            validation_id=body.validation_id,
            status="created",
            passed=report["conflict"] is None,
            effective_start=iso_str(start),
            effective_end=iso_str(end),
            slice_count=report["slice_count"],
            slices=report["slices"],
            conflict=report["conflict"],
        )

        db.conn.execute(
            "INSERT INTO network_validations (validation_id, payload_hash, payload,"
            " effective_start, effective_end, passed, result, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                body.validation_id,
                payload_hash,
                canonical_json(canonical_payload),
                iso_str(start),
                iso_str(end),
                1 if response.passed else 0,
                canonical_json(response.model_dump()),
                iso_str(datetime.now(timezone.utc)),
            ),
        )
        append_audit(
            db.conn,
            "network_validated",
            canonical_payload,
            response.model_dump(),
        )

    return response


@app.post("/network/snapshots", response_model=NetworkSnapshotOut, status_code=200)
def freeze_network_snapshot(
    body: NetworkSnapshotIn,
    db: Database = Depends(get_db),
) -> NetworkSnapshotOut:
    with db.lock(), db.conn:
        validation = db.conn.execute(
            "SELECT * FROM network_validations WHERE validation_id = ?",
            (body.validation_id,),
        ).fetchone()
        if validation is None:
            raise HTTPException(
                status_code=404,
                detail=f"validation_id '{body.validation_id}' not found; run POST /network/validate first",
            )

        existing = db.conn.execute(
            "SELECT * FROM network_snapshots WHERE snapshot_id = ?",
            (body.validation_id,),
        ).fetchone()
        if existing is not None:
            return _snapshot_response(existing, "duplicate")

        if not validation["passed"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"validation_id '{body.validation_id}' did not pass; only"
                    " validations without conflicts can be frozen"
                ),
            )

        frozen_at = iso_str(datetime.now(timezone.utc))
        # Copy the payload and result at freeze time: the snapshot is an
        # immutable artefact and no later validation can rewrite it.
        db.conn.execute(
            "INSERT INTO network_snapshots (snapshot_id, payload, result, created_at)"
            " VALUES (?,?,?,?)",
            (body.validation_id, validation["payload"], validation["result"], frozen_at),
        )
        row = db.conn.execute(
            "SELECT * FROM network_snapshots WHERE snapshot_id = ?",
            (body.validation_id,),
        ).fetchone()
        response = _snapshot_response(row, "created")
        append_audit(
            db.conn,
            "network_snapshot_created",
            {"validation_id": body.validation_id},
            response.model_dump(),
        )

    return response


def _snapshot_response(row, status: str) -> NetworkSnapshotOut:
    return NetworkSnapshotOut(
        snapshot_id=row["snapshot_id"],
        status=status,
        effective_start=json.loads(row["result"])["effective_start"],
        effective_end=json.loads(row["result"])["effective_end"],
        frozen_at=row["created_at"],
        validation=NetworkValidateOut(**json.loads(row["result"])),
    )


@app.get("/network/snapshots/{snapshot_id}", response_model=NetworkSnapshotOut)
def get_network_snapshot(
    snapshot_id: str,
    db: Database = Depends(get_db),
) -> NetworkSnapshotOut:
    with db.lock():
        row = db.conn.execute(
            "SELECT * FROM network_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"snapshot '{snapshot_id}' not found"
        )
    return _snapshot_response(row, "created")


# ---------------------------------------------------------------------------
# Fare disputes and refund settlement
# ---------------------------------------------------------------------------


@app.post("/disputes", response_model=DisputeOut, status_code=200)
def create_dispute(
    body: DisputeIn,
    db: Database = Depends(get_db),
) -> DisputeOut:
    canonical_payload = canonical_dispute_payload(body)
    p_hash = payload_hash(canonical_payload)

    with db.lock(), db.conn:
        existing = db.conn.execute(
            "SELECT * FROM disputes WHERE dispute_id = ?", (body.dispute_id,)
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != p_hash:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"dispute_id '{body.dispute_id}' already exists"
                        " with a different payload"
                    ),
                )
            # Idempotent replay: the frozen snapshot and any settled refund
            # are never recomputed or rewritten.
            return dispute_response(db.conn, existing, "duplicate")

        charge = db.conn.execute(
            "SELECT t.passenger_id, c.amount_cents, c.base_fare_cents,"
            " c.transfer_discount_cents, c.basis"
            " FROM trip_charges c JOIN trips t ON t.trip_id = c.trip_id"
            " WHERE c.trip_id = ?",
            (body.trip_id,),
        ).fetchone()
        if charge is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"trip_id '{body.trip_id}' not found; only paired trips with"
                    " a settled charge can be disputed"
                ),
            )
        if charge["passenger_id"] != body.passenger_id:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"trip_id '{body.trip_id}' belongs to passenger"
                    f" '{charge['passenger_id']}', not '{body.passenger_id}'"
                ),
            )
        if body.claimed_amount_cents > charge["amount_cents"]:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"claimed_amount_cents {body.claimed_amount_cents} exceeds the"
                    f" frozen snapshot receivable {charge['amount_cents']}"
                ),
            )

        frozen_at = iso_str(datetime.now(timezone.utc))
        db.conn.execute(
            "INSERT INTO disputes (dispute_id, payload_hash, passenger_id, trip_id,"
            " claimed_amount_cents, reason, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                body.dispute_id,
                p_hash,
                body.passenger_id,
                body.trip_id,
                body.claimed_amount_cents,
                body.reason,
                frozen_at,
            ),
        )
        # Freeze the charge snapshot at creation time: later event corrections
        # recompute trip_charges but never mutate this copy.
        db.conn.execute(
            "INSERT INTO dispute_snapshots (dispute_id, trip_id, passenger_id,"
            " amount_cents, base_fare_cents, transfer_discount_cents, basis, frozen_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                body.dispute_id,
                body.trip_id,
                charge["passenger_id"],
                charge["amount_cents"],
                charge["base_fare_cents"],
                charge["transfer_discount_cents"],
                charge["basis"],
                frozen_at,
            ),
        )
        row = db.conn.execute(
            "SELECT * FROM disputes WHERE dispute_id = ?", (body.dispute_id,)
        ).fetchone()
        response = dispute_response(db.conn, row, "created")
        append_audit(
            db.conn,
            "dispute_opened",
            canonical_payload,
            response.model_dump(),
        )

    return response


@app.post("/disputes/{dispute_id}/resolve", response_model=DisputeOut, status_code=200)
def resolve_dispute(
    dispute_id: str,
    body: DisputeResolveIn,
    db: Database = Depends(get_db),
) -> DisputeOut:
    canonical_payload = canonical_resolve_payload(body, dispute_id)
    p_hash = payload_hash(canonical_payload)

    with db.lock(), db.conn:
        dispute = db.conn.execute(
            "SELECT * FROM disputes WHERE dispute_id = ?", (dispute_id,)
        ).fetchone()
        if dispute is None:
            raise HTTPException(
                status_code=404, detail=f"dispute_id '{dispute_id}' not found"
            )

        existing_refund = db.conn.execute(
            "SELECT * FROM refunds WHERE dispute_id = ?", (dispute_id,)
        ).fetchone()
        if existing_refund is not None:
            if existing_refund["payload_hash"] != p_hash:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"dispute '{dispute_id}' is already resolved (version 2);"
                        " the settled refund is immutable and cannot be re-resolved"
                    ),
                )
            # Idempotent replay of the identical resolution.
            return dispute_response(db.conn, dispute, "duplicate")

        if body.expected_version != 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"version conflict: dispute '{dispute_id}' is at version 1"
                    f" (open); expected_version {body.expected_version} is stale"
                ),
            )
        if body.approved_amount_cents > dispute["claimed_amount_cents"]:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"approved_amount_cents {body.approved_amount_cents} exceeds the"
                    f" claimed amount {dispute['claimed_amount_cents']}"
                ),
            )

        resolved_at = iso_str(datetime.now(timezone.utc))
        rid = refund_id_for(dispute_id)
        # One immutable ledger row, written in the same transaction as the
        # audit record; the no-update/no-delete triggers guard it afterwards.
        db.conn.execute(
            "INSERT INTO refunds (refund_id, dispute_id, passenger_id, trip_id,"
            " amount_cents, payload_hash, review_note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                rid,
                dispute_id,
                dispute["passenger_id"],
                dispute["trip_id"],
                body.approved_amount_cents,
                p_hash,
                body.review_note,
                resolved_at,
            ),
        )
        response = dispute_response(db.conn, dispute, "created")
        append_audit(
            db.conn,
            "dispute_resolved",
            canonical_payload,
            response.model_dump(),
        )

    return response


@app.get("/disputes/{dispute_id}", response_model=DisputeOut)
def get_dispute(
    dispute_id: str,
    db: Database = Depends(get_db),
) -> DisputeOut:
    with db.lock():
        dispute = db.conn.execute(
            "SELECT * FROM disputes WHERE dispute_id = ?", (dispute_id,)
        ).fetchone()
        if dispute is None:
            raise HTTPException(
                status_code=404, detail=f"dispute_id '{dispute_id}' not found"
            )
        # The live trip charge is read here, so corrections that happened
        # after resolution surface as top-up/clawback differences without ever
        # touching the frozen snapshot or the settled refund.
        return dispute_response(db.conn, dispute, "created")


# ---------------------------------------------------------------------------
# Tap-event quality scans (read-only analysis, isolated append-only tables)
# ---------------------------------------------------------------------------


def _scan_response(row, alerts: list[dict], status: str) -> EventQualityScanOut:
    return EventQualityScanOut(
        scan_id=row["scan_id"],
        status=status,
        window_start=row["window_start"],
        window_end=row["window_end"],
        passengers=json.loads(row["passengers"]) if row["passengers"] is not None else None,
        duplicate_window_seconds=row["duplicate_window_seconds"],
        min_travel_seconds=json.loads(row["min_travel_seconds"]),
        event_count=row["event_count"],
        alert_count=row["alert_count"],
        alerts=[QualityAlert(**a) for a in alerts],
    )


def _load_scan(db: Database, scan_id: str) -> tuple:
    scan = db.conn.execute(
        "SELECT * FROM event_quality_scans WHERE scan_id = ?", (scan_id,)
    ).fetchone()
    if scan is None:
        raise HTTPException(
            status_code=404, detail=f"event quality scan '{scan_id}' not found"
        )
    rows = db.conn.execute(
        "SELECT event_id, passenger_id, rule, actual_interval_seconds,"
        " threshold_seconds, evidence FROM event_quality_alerts"
        " WHERE scan_id = ? ORDER BY seq",
        (scan_id,),
    ).fetchall()
    alerts = []
    for r in rows:
        a = {
            "event_id": r["event_id"],
            "passenger_id": r["passenger_id"],
            "rule": r["rule"],
            "actual_interval_seconds": r["actual_interval_seconds"],
            "threshold_seconds": r["threshold_seconds"],
            "evidence": [
                QualityEvidenceEvent(**e) for e in json.loads(r["evidence"])
            ],
        }
        alerts.append(a)
    return scan, alerts


@app.post("/event-quality/scans", response_model=EventQualityScanOut, status_code=200)
def create_event_quality_scan(
    body: EventQualityScanIn,
    db: Database = Depends(get_db),
) -> EventQualityScanOut:
    window_start = normalize_ts(body.window_start)
    window_end = normalize_ts(body.window_end)
    if window_end <= window_start:
        raise HTTPException(status_code=422, detail="window_end must be after window_start")
    passengers = sorted(set(body.passengers)) if body.passengers is not None else None
    for out_station, pairs in body.min_travel_seconds.items():
        if not pairs:
            raise HTTPException(
                status_code=422,
                detail=f"min_travel_seconds['{out_station}'] must not be empty",
            )
        for in_station, seconds in pairs.items():
            if seconds < 0:
                raise HTTPException(
                    status_code=422,
                    detail="min_travel_seconds values must be >= 0",
                )

    canonical_payload = canonical_scan_payload(
        body.scan_id,
        window_start,
        window_end,
        passengers,
        body.duplicate_window_seconds,
        body.min_travel_seconds,
    )
    p_hash = scan_payload_hash(canonical_payload)

    with db.lock(), db.conn:
        existing = db.conn.execute(
            "SELECT * FROM event_quality_scans WHERE scan_id = ?", (body.scan_id,)
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != p_hash:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"scan_id '{body.scan_id}' already exists with a different"
                        " payload"
                    ),
                )
            # Idempotent replay: the stored scan result is never recomputed;
            # scans see the same frozen evidence they produced.
            row, alerts = _load_scan(db, body.scan_id)
            return _scan_response(row, alerts, "duplicate")

        grouped = load_window_events(
            db.conn, window_start, window_end, passengers
        )
        alerts: list = []
        event_count = 0
        for pid in sorted(grouped):
            per_passenger = scan_events(
                grouped[pid],
                duplicate_window_seconds=body.duplicate_window_seconds,
                min_travel_seconds=body.min_travel_seconds,
            )
            event_count += len(grouped[pid])
            alerts.extend(per_passenger)

        # Merge the per-passenger sequences into one global scan ordering.
        alerts.sort(key=lambda a: (
            a.evidence[-1]["occurred_at"],
            a.evidence[-1]["seq"],
            a.passenger_id,
            a.rule,
        ))
        for i, a in enumerate(alerts):
            a.seq = i

        created_at = iso_str(datetime.now(timezone.utc))
        db.conn.execute(
            "INSERT INTO event_quality_scans (scan_id, payload_hash, window_start,"
            " window_end, passengers, duplicate_window_seconds, min_travel_seconds,"
            " event_count, alert_count, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                body.scan_id,
                p_hash,
                iso_str(window_start),
                iso_str(window_end),
                json.dumps(passengers) if passengers is not None else None,
                body.duplicate_window_seconds,
                canonical_json(canonical_payload["min_travel_seconds"]),
                event_count,
                len(alerts),
                created_at,
            ),
        )
        db.conn.executemany(
            "INSERT INTO event_quality_alerts (scan_id, seq, event_id, passenger_id,"
            " rule, actual_interval_seconds, threshold_seconds, evidence)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    body.scan_id,
                    a.seq,
                    a.event_id,
                    a.passenger_id,
                    a.rule,
                    a.actual_interval_seconds,
                    a.threshold_seconds,
                    canonical_json(a.evidence),
                )
                for a in alerts
            ],
        )
        # Scans are read-only analysis: no audit record is appended, and
        # events/trips/charges/disputes are never written.
        row, stored_alerts = _load_scan(db, body.scan_id)

    return _scan_response(row, stored_alerts, "created")


@app.get("/event-quality/scans/{scan_id}", response_model=EventQualityScanOut)
def get_event_quality_scan(
    scan_id: str,
    db: Database = Depends(get_db),
) -> EventQualityScanOut:
    with db.lock():
        row, alerts = _load_scan(db, scan_id)
    return _scan_response(row, alerts, "created")


# ---------------------------------------------------------------------------
# Anonymous k-anonymous trajectory exports (read-only analysis, isolated
# append-only tables; no audit records)
# ---------------------------------------------------------------------------


def _export_response(row, trip_rows, status: str) -> PrivacyExportOut:
    classes = classes_from_trip_rows(trip_rows)
    return PrivacyExportOut(
        export_id=row["export_id"],
        status=status,
        window_start=row["window_start"],
        window_end=row["window_end"],
        bucket_minutes=row["bucket_minutes"],
        k=row["k"],
        station_groups=load_station_groups(row["station_groups"]),
        input_trip_count=row["input_trip_count"],
        equivalence_class_count=row["equivalence_class_count"],
        retained_class_count=row["retained_class_count"],
        suppressed_class_count=row["suppressed_class_count"],
        retained_trip_count=row["retained_trip_count"],
        suppressed_trip_count=row["suppressed_trip_count"],
        classes=[PrivacyEquivalenceClass(**c) for c in classes],
        trips=[
            PrivacyExportTrip(
                pseudonym=r["pseudonym"],
                bucket_start=r["bucket_start"],
                origin_group=r["origin_group"],
                destination_group=r["destination_group"],
            )
            for r in trip_rows
        ],
    )


def _load_export(db: Database, export_id: str):
    row = db.conn.execute(
        "SELECT * FROM privacy_exports WHERE export_id = ?", (export_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"privacy export '{export_id}' not found"
        )
    trip_rows = db.conn.execute(
        "SELECT pseudonym, bucket_start, origin_group, destination_group"
        " FROM privacy_export_trips WHERE export_id = ? ORDER BY seq",
        (export_id,),
    ).fetchall()
    return row, trip_rows


@app.post("/privacy/exports", response_model=PrivacyExportOut, status_code=200)
def create_privacy_export(
    body: PrivacyExportIn,
    request: Request,
    db: Database = Depends(get_db),
) -> PrivacyExportOut:
    window_start = normalize_ts(body.window_start)
    window_end = normalize_ts(body.window_end)
    if window_end <= window_start:
        raise HTTPException(status_code=422, detail="window_end must be after window_start")
    station_groups = dict(body.station_groups)

    canonical_payload = canonical_export_payload(
        body.export_id,
        window_start,
        window_end,
        body.bucket_minutes,
        body.k,
        station_groups,
    )
    p_hash = export_payload_hash(canonical_payload)

    with db.lock(), db.conn:
        existing = db.conn.execute(
            "SELECT * FROM privacy_exports WHERE export_id = ?", (body.export_id,)
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != p_hash:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"export_id '{body.export_id}' already exists with a"
                        " different payload"
                    ),
                )
            # Idempotent replay: the frozen pseudonyms and suppression
            # decisions are never recomputed.
            row, trip_rows = _load_export(db, body.export_id)
            return _export_response(row, trip_rows, "duplicate")

        trips = load_window_trips(db.conn, window_start, window_end)
        try:
            result = build_export(
                trips,
                station_groups,
                body.bucket_minutes,
                body.k,
                request.app.state.privacy_salt,
            )
        except UnmappedStationsError as exc:
            raise HTTPException(
                status_code=422,
                detail=(
                    "every in-window trip's origin and destination station must"
                    f" appear in station_groups; unmapped: {exc.stations}"
                ),
            )

        created_at = iso_str(datetime.now(timezone.utc))
        db.conn.execute(
            "INSERT INTO privacy_exports (export_id, payload_hash, window_start,"
            " window_end, bucket_minutes, k, station_groups, input_trip_count,"
            " equivalence_class_count, retained_class_count, suppressed_class_count,"
            " retained_trip_count, suppressed_trip_count, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                body.export_id,
                p_hash,
                iso_str(window_start),
                iso_str(window_end),
                body.bucket_minutes,
                body.k,
                stored_groups_json(station_groups),
                result["input_trip_count"],
                result["equivalence_class_count"],
                result["retained_class_count"],
                result["suppressed_class_count"],
                result["retained_trip_count"],
                result["suppressed_trip_count"],
                created_at,
            ),
        )
        db.conn.executemany(
            "INSERT INTO privacy_export_trips (export_id, seq, pseudonym,"
            " bucket_start, origin_group, destination_group)"
            " VALUES (?,?,?,?,?,?)",
            [
                (
                    body.export_id,
                    seq,
                    t["pseudonym"],
                    t["bucket_start"],
                    t["origin_group"],
                    t["destination_group"],
                )
                for seq, t in enumerate(result["trips"])
            ],
        )
        # Exports are read-only analysis: no audit record is appended, and
        # events/trips/charges/disputes are never written.
        row, trip_rows = _load_export(db, body.export_id)

    return _export_response(row, trip_rows, "created")


@app.get("/privacy/exports/{export_id}", response_model=PrivacyExportOut)
def get_privacy_export(
    export_id: str,
    db: Database = Depends(get_db),
) -> PrivacyExportOut:
    with db.lock():
        row, trip_rows = _load_export(db, export_id)
    return _export_response(row, trip_rows, "created")


# ---------------------------------------------------------------------------
# Sudden-disruption evacuation replanning (read-only analysis, isolated
# append-only tables; no audit records)
# ---------------------------------------------------------------------------


def _replan_response(row, status: str) -> DisruptionReplanOut:
    stored = json.loads(row["result"])
    stored["status"] = status  # the stored body always carries "created"
    return DisruptionReplanOut(**stored)


@app.post("/disruption/replans", response_model=DisruptionReplanOut, status_code=200)
def create_disruption_replan(
    body: DisruptionReplanIn,
    db: Database = Depends(get_db),
) -> DisruptionReplanOut:
    # Structural timetable checks are rejected before any persistence:
    # duplicate service/group ids, repeated stops inside one service, and
    # non-monotonic arrival/departure times never leave a row behind.
    try:
        min_transfer_seconds, services, groups = normalise_input(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    canonical_payload = canonical_replan_payload(
        body.replan_id, min_transfer_seconds, services, groups
    )
    p_hash = replan_payload_hash(canonical_payload)

    with db.lock(), db.conn:
        existing = db.conn.execute(
            "SELECT * FROM disruption_replans WHERE replan_id = ?",
            (body.replan_id,),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != p_hash:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"replan_id '{body.replan_id}' already exists with a"
                        " different payload"
                    ),
                )
            # Idempotent replay: the frozen allocation is never recomputed.
            return _replan_response(existing, "duplicate")

        result = build_replan(min_transfer_seconds, services, groups)
        created_at = iso_str(datetime.now(timezone.utc))
        db.conn.execute(
            "INSERT INTO disruption_replans (replan_id, payload_hash, payload,"
            " min_transfer_seconds, group_count, assigned_count,"
            " stranded_count, result, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                body.replan_id,
                p_hash,
                canonical_json(canonical_payload),
                min_transfer_seconds,
                result["summary"]["group_count"],
                result["summary"]["assigned_count"],
                result["summary"]["stranded_count"],
                canonical_json(
                    {"replan_id": body.replan_id, "status": "created", **result}
                ),
                created_at,
            ),
        )
        # Replans are read-only analysis computed solely from the request:
        # no business table is touched and no audit record is appended.
        row = db.conn.execute(
            "SELECT * FROM disruption_replans WHERE replan_id = ?",
            (body.replan_id,),
        ).fetchone()

    return _replan_response(row, "created")


@app.get("/disruption/replans/{replan_id}", response_model=DisruptionReplanOut)
def get_disruption_replan(
    replan_id: str,
    db: Database = Depends(get_db),
) -> DisruptionReplanOut:
    with db.lock():
        row = db.conn.execute(
            "SELECT * FROM disruption_replans WHERE replan_id = ?",
            (replan_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"disruption replan '{replan_id}' not found"
        )
    return _replan_response(row, "created")


@app.get("/audit", response_model=AuditList)
def list_audit(
    operation: str | None = Query(
        default=None,
        pattern="^(event_processed|event_corrected|recon_batch_created"
                "|network_validated|network_snapshot_created"
                "|dispute_opened|dispute_resolved)$",
        description="Filter by operation type",
    ),
    after: int = Query(default=0, ge=0, description="Return records with seq > after"),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Database = Depends(get_db),
) -> AuditList:
    sql = (
        "SELECT seq, operation, occurred_at, request_summary, result_summary,"
        " prev_hash, record_hash FROM audit_log WHERE seq > ?"
    )
    params: list = [after]
    if operation:
        sql += " AND operation = ?"
        params.append(operation)
    sql += " ORDER BY seq LIMIT ?"
    params.append(limit)
    with db.lock():
        rows = db.conn.execute(sql, params).fetchall()
    entries = [
        AuditEntry(
            seq=r["seq"],
            operation=r["operation"],
            occurred_at=r["occurred_at"],
            request_summary=json.loads(r["request_summary"]),
            result_summary=json.loads(r["result_summary"]),
            prev_hash=r["prev_hash"],
            record_hash=r["record_hash"],
        )
        for r in rows
    ]
    return AuditList(count=len(entries), entries=entries)


@app.get("/audit/verify", response_model=AuditVerification)
def audit_verify(db: Database = Depends(get_db)) -> AuditVerification:
    with db.lock():
        report = verify_chain(db.conn)
    return AuditVerification(**report)


# ---------------------------------------------------------------------------
# Online database backup (administration)
# ---------------------------------------------------------------------------


def _remove_backup_artifacts(path: str) -> None:
    """Best-effort cleanup of a temp backup file and any SQLite sidecars."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _verify_backup_file(path: str) -> None:
    """Open the freshly written copy with an independent connection and run
    PRAGMA integrity_check (and foreign_key_check). Raises on any defect."""
    check = sqlite3.connect(path, timeout=30)
    try:
        check.execute("PRAGMA query_only=ON")
        rows = check.execute("PRAGMA integrity_check").fetchall()
        if len(rows) != 1 or rows[0][0] != "ok":
            detail = "; ".join(r[0] for r in rows) or "integrity_check returned no rows"
            raise sqlite3.DatabaseError(f"backup integrity_check failed: {detail}")
        fk_violations = check.execute("PRAGMA foreign_key_check").fetchall()
        if fk_violations:
            raise sqlite3.DatabaseError(
                f"backup foreign_key_check found {len(fk_violations)} violation(s)"
            )
    finally:
        check.close()


def _sha256_file(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


@app.get("/admin/backup")
def admin_backup(db: Database = Depends(get_db)) -> FileResponse:
    """Online, consistent SQLite backup while the service keeps serving writes.

    The snapshot is produced with the SQLite backup API from an independent
    read-only connection (the live database file is never copied directly,
    and the application write lock is not held), then reopened with another
    independent connection for PRAGMA integrity_check. Only after the check
    passes is the file streamed back with its SHA-256 digest in a response
    header. The temporary file is removed after the response is sent, or as
    soon as any step fails.
    """
    if db.path == ":memory:":
        tmp_dir = None  # tempfile.mkdtemp-style default temp directory
    else:
        tmp_dir = os.path.dirname(os.path.abspath(db.path))
    fd, tmp_path = tempfile.mkstemp(prefix="transit-backup-", suffix=".db", dir=tmp_dir)
    os.close(fd)  # sqlite opens the path itself
    try:
        db.online_backup(
            tmp_path,
            pages=BACKUP_PAGES_PER_BATCH,
            progress=BACKUP_PROGRESS_CALLBACK,
        )
        _verify_backup_file(tmp_path)
        digest, size = _sha256_file(tmp_path)
    except Exception as exc:  # any failure: clean up and report explicitly
        _remove_backup_artifacts(tmp_path)
        raise HTTPException(
            status_code=500,
            detail=f"database backup failed: {exc}",
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = {
        "X-Backup-SHA256": digest,
        "X-Backup-Size-Bytes": str(size),
    }
    return FileResponse(
        tmp_path,
        media_type="application/vnd.sqlite3",
        filename=f"transit-backup-{stamp}.db",
        headers=headers,
        background=BackgroundTask(_remove_backup_artifacts, tmp_path),
    )
