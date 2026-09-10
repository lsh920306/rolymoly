"""Durable auction change versions and shared, authenticated read snapshots.

Table triggers include writes from other app processes and the deadline worker.
Versions are committed with the data; PostgreSQL NOTIFY only wakes readers. A
missed notification is repaired by reading the versions again. Session tokens
and the returned actor map are private to the server, never broadcast payloads.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import time

from .core import SESSION_SELECT, now


_VERSION_DDL = """CREATE TABLE IF NOT EXISTS _auction_versions(
    event_id BIGINT PRIMARY KEY CHECK(event_id>=0),
    revision BIGINT NOT NULL DEFAULT 0 CHECK(revision>=0),
    detail_revision BIGINT NOT NULL DEFAULT 0 CHECK(detail_revision>=0))"""

# The zero row invalidates identity/profile data across all watched auctions.
# Event rows deliberately have no foreign key: deleting/recreating a session or
# an event cannot reset its revision while an old browser is still connected.
_EVENT_TABLES = {
    "competition_events": ("id", True),
    "competition_teams": ("event_id", True),
    "competition_players": ("event_id", True),
    "competition_games": ("event_id", True),
    "live_sessions": ("event_id", False),
    "live_lots": ("event_id", False),
    "live_bids": ("event_id", False),
    "live_events": ("event_id", False),
}
_GLOBAL_TABLES = {
    "accounts": True,  # Session permissions and the displayed host name.
    "sessions": False,
    "registration_requests": False,
    "members": True,
    "member_ranks": True,
    "riot_profiles": True,
}
_LOT_DETAILS = ("id", "event_id", "member_id", "sequence", "attempt", "status", "opened_at", "closed_at")
_VERSION_SELECT = """SELECT g.revision+COALESCE(e.revision,0) AS revision,
    g.detail_revision+COALESCE(e.detail_revision,0) AS detail_revision
    FROM _auction_versions g LEFT JOIN _auction_versions e ON e.event_id=?
    WHERE g.event_id=0"""


def notification_channel(schema):
    """Return the schema-private channel used by the PostgreSQL triggers."""
    return "roly_auction_" + hashlib.md5(str(schema).encode(), usedforsecurity=False).hexdigest()[:24]


def _bump_sql(event, detail):
    return f"""INSERT INTO _auction_versions(event_id,revision,detail_revision)
        VALUES({event},1,{detail}) ON CONFLICT(event_id) DO UPDATE SET
        revision=_auction_versions.revision+1,
        detail_revision=_auction_versions.detail_revision+excluded.detail_revision;"""


def initialize_changes(db, *, postgres=False):
    """Install an additive migration inside the caller's write transaction."""
    if not db.in_transaction:
        raise ValueError("Auction change tracking requires an existing transaction.")
    db.execute(_VERSION_DDL)
    db.execute("INSERT INTO _auction_versions(event_id,revision,detail_revision) VALUES(0,0,0) ON CONFLICT(event_id) DO NOTHING")
    if postgres:
        _initialize_postgres(db)
    else:
        _initialize_sqlite(db)


def _initialize_sqlite(db):
    for table, (column, detailed) in _EVENT_TABLES.items():
        for operation in ("INSERT", "UPDATE", "DELETE"):
            source = "OLD" if operation == "DELETE" else "NEW"
            detail = str(int(detailed))
            if table == "live_lots":
                detail = "1" if operation != "UPDATE" else "CASE WHEN " + " OR ".join(
                    f"NEW.{name} IS NOT OLD.{name}" for name in _LOT_DETAILS) + " THEN 1 ELSE 0 END"
            body = _bump_sql(f"{source}.{column}", detail)
            if operation == "UPDATE":
                # IDs normally never move, but raw admin/maintenance writes
                # must invalidate both the old and new event if they do.
                body += f"""INSERT INTO _auction_versions(event_id,revision,detail_revision)
                    SELECT OLD.{column},1,1 WHERE OLD.{column} IS NOT NEW.{column}
                    ON CONFLICT(event_id) DO UPDATE SET
                    revision=_auction_versions.revision+1,
                    detail_revision=_auction_versions.detail_revision+1;"""
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS auction_change_{table}_{operation.lower()}
                AFTER {operation} ON {table} BEGIN {body} END""")
    for table, detailed in _GLOBAL_TABLES.items():
        for operation in ("INSERT", "UPDATE", "DELETE"):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS auction_change_{table}_{operation.lower()}
                AFTER {operation} ON {table} BEGIN {_bump_sql('0', str(int(detailed)))} END""")


def _initialize_postgres(db):
    # No SECURITY DEFINER: the writer already needs permission to change these
    # private tables. All names supplied as trigger arguments are local literals.
    db.execute("""CREATE OR REPLACE FUNCTION _auction_record_change() RETURNS trigger
        LANGUAGE plpgsql SET search_path FROM CURRENT AS $auction_change$
        DECLARE
            event_key bigint;
            old_key bigint;
            detail bigint := TG_ARGV[1]::bigint;
            previous_row jsonb;
            current_row jsonb;
            channel text := 'roly_auction_' || substr(md5(TG_TABLE_SCHEMA),1,24);
        BEGIN
            IF TG_OP <> 'INSERT' THEN previous_row := to_jsonb(OLD); END IF;
            IF TG_OP <> 'DELETE' THEN current_row := to_jsonb(NEW); END IF;
            IF TG_ARGV[0] = 'global' THEN
                event_key := 0;
            ELSE
                event_key := COALESCE(current_row,previous_row)->>TG_ARGV[0];
            END IF;
            IF TG_TABLE_NAME = 'live_lots' THEN
                IF TG_OP <> 'UPDATE' OR
                    (current_row - ARRAY['highest_bid','highest_team_id','closes_at']) IS DISTINCT FROM
                    (previous_row - ARRAY['highest_bid','highest_team_id','closes_at']) THEN detail := 1; END IF;
            END IF;
            INSERT INTO _auction_versions(event_id,revision,detail_revision)
                VALUES(event_key,1,detail) ON CONFLICT(event_id) DO UPDATE SET
                revision=_auction_versions.revision+1,
                detail_revision=_auction_versions.detail_revision+excluded.detail_revision;
            PERFORM pg_catalog.pg_notify(channel,event_key::text);
            IF TG_OP = 'UPDATE' AND TG_ARGV[0] <> 'global' THEN
                old_key := previous_row->>TG_ARGV[0];
                IF old_key IS DISTINCT FROM event_key THEN
                    INSERT INTO _auction_versions(event_id,revision,detail_revision)
                        VALUES(old_key,1,1) ON CONFLICT(event_id) DO UPDATE SET
                        revision=_auction_versions.revision+1,
                        detail_revision=_auction_versions.detail_revision+1;
                    PERFORM pg_catalog.pg_notify(channel,old_key::text);
                END IF;
            END IF;
            RETURN NULL;
        END
        $auction_change$""")
    for table, (column, detailed) in _EVENT_TABLES.items():
        db.execute(f"DROP TRIGGER IF EXISTS auction_change ON {table}")
        db.execute(f"""CREATE TRIGGER auction_change AFTER INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION _auction_record_change('{column}','{int(detailed)}')""")
    for table, detailed in _GLOBAL_TABLES.items():
        db.execute(f"DROP TRIGGER IF EXISTS auction_change ON {table}")
        db.execute(f"""CREATE TRIGGER auction_change AFTER INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION _auction_record_change('global','{int(detailed)}')""")


def _actor_statement(tokens):
    digests = {hashlib.sha256(str(token).encode()).hexdigest(): token for token in tokens if token}
    if not digests:
        return None, digests
    query = SESSION_SELECT.replace("SELECT ", "SELECT s.token_hash AS _token_hash,", 1)
    query = query.replace("s.token_hash=?", "s.token_hash IN (" + ",".join("?" for _ in digests) + ")")
    return (query, (*digests, now())), digests


def _batches(live, event_id, tokens, state_statements=()):
    """One owned read snapshot and one PostgreSQL pipeline per call."""
    actor_statement, digests = _actor_statement(tokens)
    statements = [(_VERSION_SELECT, (event_id,))]
    if actor_statement:
        statements.append(actor_statement)
    statements.extend(state_statements)
    db_clock = live.core.is_postgres and not live._injected_clock
    if db_clock:
        statements.append(("SELECT EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::double precision", None))
    if live.core.is_postgres:
        with closing(live.core.connect()) as db:
            batches = db.fetch_snapshot_batches(statements)
            received = time.monotonic()
    else:
        with live.core.read_snapshot() as db:
            batches = [db.execute(query, parameters or ()).fetchall() for query, parameters in statements]
            received = time.monotonic()
    sampled_clock = float(batches.pop()[0][0]) if db_clock else live._clock()
    version = dict(batches.pop(0)[0])
    version = {key: int(value) for key, value in version.items()}
    actors = {token: None for token in tokens}
    if actor_statement:
        for row in batches.pop(0):
            actor = dict(row)
            token = digests.get(actor.pop("_token_hash"))
            if token is not None:
                actors[token] = actor
    return {**version, "actors": actors, "server_now": sampled_clock, "sampled_at": received}, batches


def _hot_statements(live, event_id):
    # Topology, participant assignments/budgets and profiles only change with a
    # detail revision. Ordinary bids fetch the header, one lot and bounded logs.
    statements = live._view_statements(event_id)
    return [
        statements[0],
        ("""SELECT l.* FROM live_lots l JOIN live_sessions s ON s.current_lot_id=l.id
            WHERE s.event_id=? AND l.event_id=?""", (event_id, event_id)),
        (statements[2][0].replace("LIMIT 300", "LIMIT 30"), (event_id,)),
        (statements[3][0].replace("LIMIT 300", "LIMIT 30"), (event_id,)),
    ]


def _hot_state(live, base, batches, sample):
    header, current, bids, events = batches
    if not header:
        return None
    state = deepcopy(base)
    head = dict(header[0])
    state["event"].update(status=head.pop("workflow_status"), created_by=head.pop("host_id"),
                          kind=head.pop("event_kind"), title=head.pop("event_title"),
                          host_name=head.pop("host_name"), has_games=bool(head.pop("has_games")))
    state.update(head)
    from .live_auction import BID_EXTENSION_SECONDS, TRANSITION_SECONDS, _effective_bid_seconds
    state.update(reset_on_bid=True, extension_seconds=BID_EXTENSION_SECONDS,
                 bid_seconds=_effective_bid_seconds(state["bid_seconds"]), transition_seconds=TRANSITION_SECONDS)
    state["max_remaining_seconds"] = state["bid_seconds"]
    if current:
        updated = dict(current[0])
        lot = next((item for item in state["lots"] if item["id"] == updated["id"]), None)
        if lot is None:
            # A detail revision race is handled before this function. Reaching
            # this point indicates an invalid cache supplied by the caller.
            raise ValueError("Auction base snapshot is missing its current lot.")
        lot.update(updated)
        lot["highest_team_name"] = next((team["name"] for team in state["teams"]
                                          if team["id"] == lot["highest_team_id"]), None)
        state["current_lot"] = lot
    else:
        state["current_lot"] = None
    state["bids"] = [dict(row) for row in bids]
    state["events"] = [dict(row) for row in events]
    state["worker_error"] = live._worker_error()
    return age_state(state, sample["server_now"], sample["sampled_at"])


def age_state(state, server_now, sampled_at):
    """Refresh a private state copy using measured monotonic elapsed time."""
    if state is None:
        return None
    state["server_now"] = server_now + max(0, time.monotonic() - sampled_at)
    for lot in state["lots"]:
        lot["remaining_seconds"] = (state["pause_remaining"] if state["status"] == "PAUSED"
                                     else max(0.0, lot["closes_at"] - state["server_now"])) if lot["status"] == "OPEN" else 0.0
    state["next_in_seconds"] = max(0.0, state["next_at"] - state["server_now"]) if state["next_at"] is not None else None
    if state["status"] == "PAUSED" and state["paused_phase"] == "WAITING":
        state["next_in_seconds"] = state["pause_remaining"]
    return state


def _finish(sample, state, *, changed, details_changed):
    # Session expiry uses the application's UTC clock throughout Core and the
    # bid validator. SQL's cutoff precedes pool waits and row conversion, so
    # check again immediately before publishing a completed read. Auction
    # deadlines retain their independent authoritative PostgreSQL clock.
    finished_at = now()
    for token, actor in sample["actors"].items():
        if actor is not None and actor["expires_at"] <= finished_at:
            sample["actors"][token] = None
    return {**sample, "state": state, "changed": changed, "details_changed": details_changed}


def read_snapshot(live, event_id, tokens=(), *, base_state=None, base_version=None):
    """Read one shared event plus all viewers' current, revocable identities.

    ``base_version`` is the preceding result (or a dict containing its two
    version fields). An unchanged revision returns ``state=None``; the caller
    retains its cached state and may age a private copy with ``age_state``.
    A normal bid returns a complete compatible state reusing cached details.
    Every nonempty state, version and actor map comes from the same snapshot.
    """
    tokens = tuple(dict.fromkeys(tokens))
    if base_state is not None and base_version is not None:
        sample, _ = _batches(live, event_id, tokens)
        if sample["revision"] == base_version["revision"]:
            return _finish(sample, None, changed=False, details_changed=False)
        if sample["detail_revision"] == base_version["detail_revision"]:
            sample, batches = _batches(live, event_id, tokens, _hot_statements(live, event_id))
            if sample["detail_revision"] == base_version["detail_revision"]:
                state = _hot_state(live, base_state, batches, sample)
                return _finish(sample, state, changed=True, details_changed=False)
            # A profile/roster update committed between the probe and hot
            # snapshot. Discard those rows; the next full snapshot is atomic.
    sample, batches = _batches(live, event_id, tokens, live._view_statements(event_id))
    state = live._assemble_view(event_id, batches, sampled_clock=sample["server_now"], received=sample["sampled_at"])
    return _finish(sample, state, changed=True, details_changed=True)
