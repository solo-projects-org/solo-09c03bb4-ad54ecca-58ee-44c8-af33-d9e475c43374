"""Quarantine: permanent isolation of anonymous test cards.

Covers: immediate loss of current results, all event numbers/revisions being
excluded (including events later reassigned to another card), whole-batch
rejection of later uploads, cross-midnight co-publication, failure/restart
not restoring old numbers, and quarantine racing an upload.
"""

import pytest

from app import db, service
from app.service import QuarantinedUploadError

from helpers import day, make_csv, pairs, state, tap, upload


def _event_rows(db_path, event_no):
    conn = db.connect(db_path)
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT event_no, card_id, station, revision FROM events "
                "WHERE event_no = ? ORDER BY revision",
                (event_no,),
            ).fetchall()
        ]
    finally:
        conn.close()


def test_quarantine_immediately_invalidates_and_recomputes(db_path):
    upload(db_path, [
        tap("e1", "c1", "2024-05-01T08:00:00Z", "A", "in"),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "B", "out"),
        tap("e3", "c2", "2024-05-01T09:00:00Z", "A", "in"),
        tap("e4", "c2", "2024-05-01T09:30:00Z", "C", "out"),
    ])
    assert pairs(day(db_path, "2024-05-01")) == {("A", "B"): 1, ("A", "C"): 1}

    # Accept quarantine without running the worker: old numbers must already
    # be unavailable (rows deleted, state stale).
    result = service.quarantine_card(db_path, "c1", "测试卡混入")
    assert result.affected_days == ["2024-05-01"]
    assert set(result.quarantined_events) == {"e1", "e2"}
    stale = day(db_path, "2024-05-01")
    assert stale["status"] == "stale"
    assert stale["station_pairs"] == []
    assert stale["pending"] == []
    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT COUNT(*) AS n FROM results").fetchone()["n"]
        assert rows == 0
    finally:
        conn.close()

    service.run_pending_jobs(db_path)
    detail = day(db_path, "2024-05-01")
    assert detail["status"] == "current"
    assert pairs(detail) == {("A", "C"): 1}
    assert detail["pending_total"] == 0


def test_quarantine_covers_old_revisions_and_reassigned_events(db_path):
    # e1/e2 start on the test card, then a correction moves e2 to another card.
    upload(db_path, [
        tap("e1", "TEST", "2024-05-01T08:00:00Z", "A", "in"),
        tap("e2", "TEST", "2024-05-01T08:20:00Z", "B", "out"),
    ])
    upload(db_path, [tap("e2", "OTHER", "2024-05-01T08:20:00Z", "B", "out", revision=1)])
    # Reassigning e2 already breaks the pair: both taps are now isolated.
    assert day(db_path, "2024-05-01")["trip_total"] == 0
    assert {p["event_no"] for p in day(db_path, "2024-05-01")["pending"]} == {"e1", "e2"}

    result = service.quarantine_card(db_path, "TEST")
    # e2 is recorded even though its latest revision belongs to OTHER.
    assert set(result.quarantined_events) == {"e1", "e2"}
    service.run_pending_jobs(db_path)

    detail = day(db_path, "2024-05-01")
    assert detail["status"] == "current"
    assert detail["trip_total"] == 0          # e2 no longer pairs for anyone
    assert detail["pending_total"] == 0       # nor is it a pending record

    # A later upload reviving e2's old revision (or any revision) is refused.
    with pytest.raises(QuarantinedUploadError):
        service.accept_upload(db_path, "revive.csv", make_csv([
            tap("e2", "OTHER", "2024-05-01T08:20:00Z", "B", "out", revision=0),
        ]))
    # And the physical old revision still never appears in derivations.
    conn = db.connect(db_path)
    try:
        events = service.load_latest_events(conn)
        assert {e.event_no for e in events} == set()
    finally:
        conn.close()


def test_upload_naming_quarantined_card_is_rejected_whole(db_path):
    upload(db_path, [
        tap("e1", "c1", "2024-05-01T08:00:00Z", "A", "in"),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "B", "out"),
    ])
    service.quarantine_card(db_path, "c1")
    service.run_pending_jobs(db_path)

    before_batches = _count(db_path, "batches")
    before_events = _count(db_path, "events")
    computed_before = state(db_path, "2024-05-01")["computed_at"]

    with pytest.raises(QuarantinedUploadError) as exc:
        service.accept_upload(db_path, "mixed.csv", make_csv([
            tap("n1", "c9", "2024-05-02T08:00:00Z", "X", "in"),
            tap("n2", "c9", "2024-05-02T08:20:00Z", "Y", "out"),
            tap("n3", "c1", "2024-05-02T09:00:00Z", "Z", "in"),
        ]))
    assert "c1" in str(exc.value)
    # Whole batch rejected: no batch row, no events, no invalidation.
    assert _count(db_path, "batches") == before_batches
    assert _count(db_path, "events") == before_events
    assert state(db_path, "2024-05-01")["computed_at"] == computed_before
    assert state(db_path, "2024-05-02") is None


def test_quarantine_publishes_cross_midnight_days_together(db_path):
    # A cross-midnight journey by a valid card plus a test card on both days.
    upload(db_path, [
        tap("in1", "GOOD", "2024-05-01T23:50:00Z", "A", "in"),
        tap("out1", "GOOD", "2024-05-02T00:20:00Z", "B", "out"),
        tap("t1", "TEST", "2024-05-01T10:00:00Z", "A", "in"),
        tap("t2", "TEST", "2024-05-01T10:20:00Z", "B", "out"),
        tap("t3", "TEST", "2024-05-02T10:00:00Z", "C", "in"),
        tap("t4", "TEST", "2024-05-02T10:20:00Z", "D", "out"),
    ])
    result = service.quarantine_card(db_path, "TEST")
    assert result.affected_days == ["2024-05-01", "2024-05-02"]
    service.run_pending_jobs(db_path)

    d1, d2 = day(db_path, "2024-05-01"), day(db_path, "2024-05-02")
    assert d1["status"] == d2["status"] == "current"
    assert pairs(d1) == {("A", "B"): 1}
    assert d2["trip_total"] == 0
    # Both sides of the cross-midnight link were published by one transaction.
    assert state(db_path, "2024-05-01")["computed_at"] == state(db_path, "2024-05-02")["computed_at"]


def test_failed_recompute_after_quarantine_never_restores_old_results(db_path, monkeypatch):
    upload(db_path, [
        tap("e1", "c1", "2024-05-01T08:00:00Z", "A", "in"),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "B", "out"),
    ])
    original = service._publish

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(service, "_publish", boom)
    service.quarantine_card(db_path, "c1")
    service.run_pending_jobs(db_path)

    detail = day(db_path, "2024-05-01")
    assert detail["status"] == "stale"
    assert detail["station_pairs"] == []
    assert "disk full" in detail["last_error"]

    # Restart: recovery must not resurrect the quarantined day.
    service.recover(db_path)
    assert state(db_path, "2024-05-01")["status"] == "stale"
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM results").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM pending_taps").fetchone()["n"] == 0
    finally:
        conn.close()

    monkeypatch.setattr(service, "_publish", original)
    service.run_pending_jobs(db_path)
    detail = day(db_path, "2024-05-01")
    assert detail["status"] == "current"
    assert detail["trip_total"] == 0
    assert detail["pending_total"] == 0


def test_quarantine_during_recompute_fences_old_snapshot(db_path):
    """A quarantine landing between a job's snapshot and publish aborts the
    publish and is never overwritten by the pre-quarantine computation."""
    upload(db_path, [
        tap("e1", "c1", "2024-05-01T08:00:00Z", "A", "in"),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "B", "out"),
    ])
    # Accept a new revision so a job is pending, but don't run it yet.
    service.accept_upload(db_path, "rev.csv", make_csv([
        tap("e2", "c1", "2024-05-01T08:20:00Z", "C", "out", revision=1),
    ]))

    fired = {"done": False}

    def hook(stage, conn, info):
        if stage == "after_compute" and not fired["done"]:
            fired["done"] = True
            service.quarantine_card(db_path, "c1")

    service.run_pending_jobs(db_path, hook=hook)
    # The fenced job was re-queued; drain it.
    service.run_pending_jobs(db_path)

    detail = day(db_path, "2024-05-01")
    assert detail["status"] == "current"
    assert detail["trip_total"] == 0
    assert detail["pending_total"] == 0


def _late_upload_worker(args):
    """Commit an upload after a short delay (module-level so spawn can pickle)."""
    import time

    db_path, payload = args
    time.sleep(0.8)  # stay asleep while the parent quarantines
    from app import service as svc
    from app.ingest import UploadError as IngestError

    try:
        svc.accept_upload(db_path, "late.csv", payload)
    except IngestError as exc:
        return "rejected:" + str(exc)
    return "accepted"


def test_concurrent_upload_and_quarantine_are_serialised(db_path):
    """An upload committing after a quarantine must be refused; one that
    commits before it must have its dates invalidated by the quarantine."""
    import multiprocessing as mp
    import time

    upload(db_path, [
        tap("e1", "c1", "2024-05-01T08:00:00Z", "A", "in"),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "B", "out"),
    ])
    payload = make_csv([
        tap("e1", "c1", "2024-05-01T08:00:00Z", "A", "in", revision=1),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "Z", "out", revision=1),
    ])

    ctx = mp.get_context("spawn")
    with ctx.Pool(1) as pool:
        pending = pool.apply_async(_late_upload_worker, ((str(db_path), payload),))
        time.sleep(0.2)                       # worker is still in its delay
        service.quarantine_card(db_path, "c1")  # quarantine wins the write lock
        outcome = pending.get(timeout=30)
        service.run_pending_jobs(db_path)

    assert outcome.startswith("rejected:")
    assert day(db_path, "2024-05-01")["trip_total"] == 0
    assert _count(db_path, "events") == 2  # original rows only; no rev 1 added


def test_quarantine_is_idempotent_and_lists(db_path):
    upload(db_path, [
        tap("e1", "c1", "2024-05-01T08:00:00Z", "A", "in"),
        tap("e2", "c1", "2024-05-01T08:20:00Z", "B", "out"),
    ])
    first = service.quarantine_card(db_path, "c1", "测试")
    assert first.already_quarantined is False
    second = service.quarantine_card(db_path, "c1")
    assert second.already_quarantined is True
    assert second.quarantined_events == ["e1", "e2"]
    conn = db.connect(db_path)
    try:
        listing = service.list_quarantine(conn)
    finally:
        conn.close()
    assert [c["card_id"] for c in listing["cards"]] == ["c1"]
    assert {e["event_no"] for e in listing["events"]} == {"e1", "e2"}


def test_quarantine_unknown_card_only_blacklists_for_future(db_path):
    result = service.quarantine_card(db_path, "ghost")
    assert result.affected_days == []
    with pytest.raises(QuarantinedUploadError):
        service.accept_upload(db_path, "g.csv", make_csv([
            tap("g1", "ghost", "2024-05-01T08:00:00Z", "A", "in"),
        ]))


def _count(db_path, table):
    conn = db.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    finally:
        conn.close()
