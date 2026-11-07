"""The plugin's own tables. Plain SQL that runs on PostgreSQL and SQLite.

Lasting state lives here and never in the plugin's settings, because saving settings
replaces the whole settings object. Stream URLs, viewer details and pictures are never
written: content checks keep only picture fingerprints, as numbers.
"""

from __future__ import annotations

import json

PREFIX = "plugin_stream_demote_"
TABLE_NAMES = ("failure", "session", "demoted", "journal", "token", "rate", "account", "stop", "guard", "sample", "hash", "meta")

SCORED_STATES = ("pending", "counted")
DEMOTE_ACTIONS = ("demote", "would_demote")


def _schema(id_column):
    return (
        f"""CREATE TABLE IF NOT EXISTS {PREFIX}failure (
            id {id_column}, at DOUBLE PRECISION NOT NULL, channel_uuid TEXT NOT NULL,
            channel_id BIGINT NOT NULL, stream_id BIGINT NOT NULL, account_key TEXT,
            cause TEXT NOT NULL, weight DOUBLE PRECISION NOT NULL, state TEXT NOT NULL,
            due_at DOUBLE PRECISION, outcome TEXT, attempt_at DOUBLE PRECISION, proof_at DOUBLE PRECISION)""",
        f"CREATE INDEX IF NOT EXISTS {PREFIX}failure_due ON {PREFIX}failure (state, due_at)",
        f"CREATE INDEX IF NOT EXISTS {PREFIX}failure_pair ON {PREFIX}failure (channel_id, stream_id, at)",
        f"CREATE INDEX IF NOT EXISTS {PREFIX}failure_account ON {PREFIX}failure (account_key, at)",
        f"""CREATE TABLE IF NOT EXISTS {PREFIX}session (
            channel_uuid TEXT PRIMARY KEY, channel_id BIGINT NOT NULL, stream_id BIGINT NOT NULL,
            account_key TEXT, since DOUBLE PRECISION NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS {PREFIX}demoted (
            channel_id BIGINT NOT NULL, stream_id BIGINT NOT NULL, account_key TEXT,
            state TEXT NOT NULL, state_at DOUBLE PRECISION NOT NULL,
            from_position INTEGER NOT NULL, strikes INTEGER NOT NULL, cause TEXT,
            missing_since DOUBLE PRECISION,
            PRIMARY KEY (channel_id, stream_id))""",
        f"""CREATE TABLE IF NOT EXISTS {PREFIX}journal (
            id {id_column}, at DOUBLE PRECISION NOT NULL, action TEXT NOT NULL,
            channel_id BIGINT, stream_id BIGINT, account_key TEXT, cause TEXT, actor TEXT,
            from_position INTEGER, to_position INTEGER, before_order TEXT, after_order TEXT,
            undone_at DOUBLE PRECISION)""",
        f"CREATE INDEX IF NOT EXISTS {PREFIX}journal_at ON {PREFIX}journal (at)",
        f"""CREATE TABLE IF NOT EXISTS {PREFIX}token (
            id {id_column}, token_hash TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL, last_used_at DOUBLE PRECISION,
            revoked_at DOUBLE PRECISION)""",
        f"""CREATE TABLE IF NOT EXISTS {PREFIX}rate (
            key TEXT PRIMARY KEY, started DOUBLE PRECISION NOT NULL, hits INTEGER NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS {PREFIX}account (
            account_key TEXT PRIMARY KEY, stall_at DOUBLE PRECISION, stop_at DOUBLE PRECISION,
            paused_until DOUBLE PRECISION, outage_since DOUBLE PRECISION,
            last_outage_at DOUBLE PRECISION)""",
        f"""CREATE TABLE IF NOT EXISTS {PREFIX}stop (
            account_key TEXT NOT NULL, channel_id BIGINT NOT NULL, at DOUBLE PRECISION NOT NULL)""",
        f"CREATE INDEX IF NOT EXISTS {PREFIX}stop_account ON {PREFIX}stop (account_key, at)",
        f"""CREATE TABLE IF NOT EXISTS {PREFIX}guard (
            channel_id BIGINT PRIMARY KEY, expected TEXT NOT NULL, links TEXT NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL, pending TEXT, pending_at DOUBLE PRECISION)""",
        f"""CREATE TABLE IF NOT EXISTS {PREFIX}sample (
            id {id_column}, at DOUBLE PRECISION NOT NULL, channel_id BIGINT NOT NULL, stream_id BIGINT NOT NULL,
            since DOUBLE PRECISION NOT NULL, problems TEXT NOT NULL, audible INTEGER NOT NULL,
            looped INTEGER NOT NULL, radio INTEGER NOT NULL, still TEXT, hashes TEXT NOT NULL,
            cpu DOUBLE PRECISION)""",
        f"CREATE INDEX IF NOT EXISTS {PREFIX}sample_pair ON {PREFIX}sample (channel_id, stream_id, at)",
        f"""CREATE TABLE IF NOT EXISTS {PREFIX}hash (
            picture TEXT NOT NULL, channel_id BIGINT NOT NULL, at DOUBLE PRECISION NOT NULL,
            taught INTEGER NOT NULL, PRIMARY KEY (picture, channel_id))""",
        f"""CREATE TABLE IF NOT EXISTS {PREFIX}meta (
            name TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    )


# Columns added since the first version, for a database made by it.
_ADDED_COLUMNS = (
    ("failure", "attempt_at", "DOUBLE PRECISION"),
    ("failure", "proof_at", "DOUBLE PRECISION"),
    ("demoted", "missing_since", "DOUBLE PRECISION"),
)


class Store:
    def __init__(self, cursor_factory, vendor="postgresql"):
        self._cursor = cursor_factory
        self._vendor = vendor
        self._ready = False

    def ensure(self):
        if self._ready:
            return
        id_column = "INTEGER PRIMARY KEY AUTOINCREMENT" if self._vendor == "sqlite" else "BIGSERIAL PRIMARY KEY"
        for statement in _schema(id_column):
            try:
                self._exec(statement)
            except Exception:
                # Two workers can race on CREATE. The second one's error is harmless,
                # and a real problem shows up on the retry.
                self._exec(statement)
        if self._vendor != "sqlite":
            for table, column, kind in _ADDED_COLUMNS:
                self._exec(f"ALTER TABLE {PREFIX}{table} ADD COLUMN IF NOT EXISTS {column} {kind}")
        self._ready = True

    def _exec(self, sql, params=()):
        with self._cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def _rows(self, sql, params=()):
        with self._cursor() as cur:
            cur.execute(sql, params)
            names = [column[0] for column in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def _one(self, sql, params=()):
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _value(self, sql, params=()):
        with self._cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None

    # Who is playing what

    def put_session(self, channel_uuid, channel_id, stream_id, account_key, since):
        self._exec(
            f"""INSERT INTO {PREFIX}session (channel_uuid, channel_id, stream_id, account_key, since)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (channel_uuid) DO UPDATE SET channel_id = excluded.channel_id,
                stream_id = excluded.stream_id, account_key = excluded.account_key, since = excluded.since""",
            (channel_uuid, channel_id, stream_id, account_key, since),
        )

    def get_session(self, channel_uuid):
        return self._one(f"SELECT * FROM {PREFIX}session WHERE channel_uuid = %s", (channel_uuid,))

    def delete_session(self, channel_uuid):
        self._exec(f"DELETE FROM {PREFIX}session WHERE channel_uuid = %s", (channel_uuid,))

    def all_sessions(self):
        return self._rows(f"SELECT * FROM {PREFIX}session ORDER BY since")

    def sessions_for_account(self, account_key, started_before):
        return self._rows(
            f"SELECT * FROM {PREFIX}session WHERE account_key = %s AND since <= %s",
            (account_key, started_before),
        )

    def sessions_on_demoted(self, started_before):
        return self._rows(
            f"""SELECT s.* FROM {PREFIX}session s JOIN {PREFIX}demoted d
                ON d.channel_id = s.channel_id AND d.stream_id = s.stream_id
                WHERE s.since <= %s""",
            (started_before,),
        )

    # Failures

    def add_failure(self, at, channel_uuid, channel_id, stream_id, account_key, cause, weight, state,
                    due_at=None, outcome=None, attempt_at=None, proof_at=None):
        self._exec(
            f"""INSERT INTO {PREFIX}failure
                (at, channel_uuid, channel_id, stream_id, account_key, cause, weight, state, due_at, outcome, attempt_at, proof_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (at, channel_uuid, channel_id, stream_id, account_key, cause, weight, state, due_at, outcome, attempt_at, proof_at),
        )

    def failure_seen(self, channel_id, stream_id, cause, since):
        # A second delivery of the same event must not count twice.
        return bool(
            self._value(
                f"""SELECT COUNT(*) FROM {PREFIX}failure
                    WHERE channel_id = %s AND stream_id = %s AND cause = %s AND at >= %s""",
                (channel_id, stream_id, cause, since),
            )
        )

    def due_failures(self, now):
        return self._rows(
            f"SELECT * FROM {PREFIX}failure WHERE state = 'pending' AND due_at <= %s ORDER BY at, id",
            (now,),
        )

    def waiting_channels(self):
        """Running channels with a failure still waiting on another stream to play."""
        return self._rows(
            f"""SELECT DISTINCT channel_id, channel_uuid FROM {PREFIX}failure
                WHERE state = 'pending' AND proof_at IS NULL"""
        )

    def claim_failure(self, failure_id):
        # Only one worker wins this update, so a failure is judged once.
        return (
            self._exec(
                f"UPDATE {PREFIX}failure SET state = 'counted' WHERE id = %s AND state = 'pending'",
                (failure_id,),
            )
            == 1
        )

    def wait_longer(self, failure_id, due_at):
        self._exec(
            f"UPDATE {PREFIX}failure SET state = 'pending', due_at = %s WHERE id = %s",
            (due_at, failure_id),
        )

    def settle_failure(self, failure_id, outcome, state=None):
        if state:
            self._exec(
                f"UPDATE {PREFIX}failure SET outcome = %s, state = %s WHERE id = %s",
                (outcome, state, failure_id),
            )
        else:
            self._exec(f"UPDATE {PREFIX}failure SET outcome = %s WHERE id = %s", (outcome, failure_id))

    def failure_proof(self, failure_id):
        return self._value(f"SELECT proof_at FROM {PREFIX}failure WHERE id = %s", (failure_id,))

    def mark_proof(self, channel_id, playing_stream, at):
        """A stream has shown it can play, which answers for every other stream that failed before it."""
        self._exec(
            f"""UPDATE {PREFIX}failure SET proof_at = %s
                WHERE channel_id = %s AND stream_id <> %s AND proof_at IS NULL AND at <= %s
                AND state = 'pending' AND cause NOT LIKE 'content%%'""",
            (at, channel_id, playing_stream, at),
        )

    def guarded_buckets(self, channel_id, stream_id, since, bucket_seconds):
        """In how many separate spells of time this stream failed but was let off."""
        rows = self._rows(
            f"""SELECT at FROM {PREFIX}failure WHERE channel_id = %s AND stream_id = %s AND at >= %s
                AND outcome IN ('zap_guard', 'stall_guard')""",
            (channel_id, stream_id, since),
        )
        return len({int(row["at"] // bucket_seconds) for row in rows})

    def score(self, channel_id, stream_id, since):
        marks = ", ".join(["%s"] * len(SCORED_STATES))
        value = self._value(
            f"""SELECT SUM(weight) FROM {PREFIX}failure
                WHERE channel_id = %s AND stream_id = %s AND at >= %s AND state IN ({marks})""",
            (channel_id, stream_id, since, *SCORED_STATES),
        )
        return float(value or 0.0)

    def spend_strikes(self, channel_id, stream_id):
        marks = ", ".join(["%s"] * len(SCORED_STATES))
        self._exec(
            f"""UPDATE {PREFIX}failure SET state = 'spent'
                WHERE channel_id = %s AND stream_id = %s AND state IN ({marks})""",
            (channel_id, stream_id, *SCORED_STATES),
        )

    def void_strikes(self, channel_id, stream_id=None, outcome="void"):
        marks = ", ".join(["%s"] * len(SCORED_STATES))
        sql = f"""UPDATE {PREFIX}failure SET state = 'void', outcome = %s
                  WHERE channel_id = %s AND state IN ({marks})"""
        params = [outcome, channel_id, *SCORED_STATES]
        if stream_id is not None:
            sql += " AND stream_id = %s"
            params.append(stream_id)
        self._exec(sql, params)

    def void_account_strikes(self, account_key, since, outcome):
        marks = ", ".join(["%s"] * len(SCORED_STATES))
        return self._exec(
            f"""UPDATE {PREFIX}failure SET state = 'void', outcome = %s
                WHERE account_key = %s AND at >= %s AND state IN ({marks})""",
            (outcome, account_key, since, *SCORED_STATES),
        )

    def failed_streams_on_channel(self, channel_id, since):
        rows = self._rows(
            f"""SELECT DISTINCT stream_id FROM {PREFIX}failure WHERE channel_id = %s AND at >= %s
                AND (outcome IS NULL OR outcome NOT IN ('recovered', 'zap_guard', 'stall_guard', 'tool_guard'))""",
            (channel_id, since),
        )
        return {row["stream_id"] for row in rows}

    def content_failure_since(self, channel_id, stream_id, since):
        return bool(
            self._value(
                f"""SELECT COUNT(*) FROM {PREFIX}failure WHERE channel_id = %s AND stream_id = %s
                    AND at >= %s AND cause LIKE 'content%%'""",
                (channel_id, stream_id, since),
            )
        )

    def outage_spread(self, account_key, since):
        """How many streams and channels on an account failed since a time."""
        row = self._one(
            f"""SELECT COUNT(DISTINCT stream_id) AS streams, COUNT(DISTINCT channel_id) AS channels
                FROM {PREFIX}failure WHERE account_key = %s AND at >= %s
                AND (outcome IS NULL OR outcome NOT IN ('recovered', 'zap_guard', 'stall_guard', 'tool_guard'))""",
            (account_key, since),
        )
        return int(row["streams"] or 0), int(row["channels"] or 0)

    def other_failure_near(self, account_key, stream_id, start, end):
        return bool(
            self._value(
                f"""SELECT COUNT(*) FROM {PREFIX}failure
                    WHERE account_key = %s AND stream_id <> %s AND at >= %s AND at <= %s""",
                (account_key, stream_id, start, end),
            )
        )

    def failed_earlier(self, channel_id, stream_id, start, end):
        return bool(
            self._value(
                f"""SELECT COUNT(*) FROM {PREFIX}failure
                    WHERE channel_id = %s AND stream_id = %s AND at >= %s AND at < %s
                    AND weight > 0 AND state <> 'void'""",
                (channel_id, stream_id, start, end),
            )
        )

    def count_recent(self, since):
        rows = self._rows(
            f"SELECT outcome, COUNT(*) AS n FROM {PREFIX}failure WHERE at >= %s GROUP BY outcome",
            (since,),
        )
        return {row["outcome"] or "waiting": int(row["n"]) for row in rows}

    # Accounts

    def account(self, account_key):
        return self._one(f"SELECT * FROM {PREFIX}account WHERE account_key = %s", (account_key,)) or {}

    def _touch_account(self, account_key):
        self._exec(
            f"INSERT INTO {PREFIX}account (account_key) VALUES (%s) ON CONFLICT (account_key) DO NOTHING",
            (account_key,),
        )

    def set_stall(self, account_key, at):
        self._touch_account(account_key)
        self._exec(f"UPDATE {PREFIX}account SET stall_at = %s WHERE account_key = %s", (at, account_key))

    def add_stop(self, account_key, channel_id, at):
        self._exec(
            f"INSERT INTO {PREFIX}stop (account_key, channel_id, at) VALUES (%s, %s, %s)",
            (account_key, channel_id, at),
        )

    def other_channel_stopped(self, account_key, channel_id, start, end):
        """Every stop is kept for a while, because later ones mustn't hide the one that matters."""
        return bool(
            self._value(
                f"""SELECT COUNT(*) FROM {PREFIX}stop
                    WHERE account_key = %s AND channel_id <> %s AND at > %s AND at < %s""",
                (account_key, channel_id, start, end),
            )
        )

    def record_outage(self, account_key, now, paused_until, giveup_seconds):
        self._touch_account(account_key)
        current = self.account(account_key)
        last = current.get("last_outage_at")
        since = current.get("outage_since")
        if not since or not last or now - last > giveup_seconds:
            since = now
        self._exec(
            f"""UPDATE {PREFIX}account SET paused_until = %s, outage_since = %s, last_outage_at = %s
                WHERE account_key = %s""",
            (paused_until, since, now, account_key),
        )

    def clear_outage(self, account_key):
        self._exec(
            f"""UPDATE {PREFIX}account SET paused_until = NULL, outage_since = NULL, last_outage_at = NULL
                WHERE account_key = %s""",
            (account_key,),
        )

    def paused_accounts(self, now):
        rows = self._rows(f"SELECT account_key FROM {PREFIX}account WHERE paused_until > %s", (now,))
        return [row["account_key"] for row in rows]

    # Demoted streams

    def demoted(self, channel_id, stream_id):
        return self._one(
            f"SELECT * FROM {PREFIX}demoted WHERE channel_id = %s AND stream_id = %s",
            (channel_id, stream_id),
        )

    def mark_demoted(self, channel_id, stream_id, account_key, at, from_position, cause):
        existing = self.demoted(channel_id, stream_id)
        if existing:
            # Keep the place it first fell from, so earning its way back returns it there.
            self._exec(
                f"""UPDATE {PREFIX}demoted SET state = 'demoted', state_at = %s, strikes = strikes + 1, cause = %s,
                    missing_since = NULL WHERE channel_id = %s AND stream_id = %s""",
                (at, cause, channel_id, stream_id),
            )
            return
        self._exec(
            f"""INSERT INTO {PREFIX}demoted
                (channel_id, stream_id, account_key, state, state_at, from_position, strikes, cause)
                VALUES (%s, %s, %s, 'demoted', %s, %s, 1, %s)""",
            (channel_id, stream_id, account_key, at, from_position, cause),
        )

    def mark_on_parole(self, channel_id, stream_id, at):
        self._exec(
            f"""UPDATE {PREFIX}demoted SET state = 'parole', state_at = %s
                WHERE channel_id = %s AND stream_id = %s""",
            (at, channel_id, stream_id),
        )

    def forget_demoted(self, channel_id, stream_id):
        self._exec(
            f"DELETE FROM {PREFIX}demoted WHERE channel_id = %s AND stream_id = %s",
            (channel_id, stream_id),
        )

    def demoted_rows(self, state=None):
        if state:
            return self._rows(f"SELECT * FROM {PREFIX}demoted WHERE state = %s ORDER BY state_at, stream_id", (state,))
        return self._rows(f"SELECT * FROM {PREFIX}demoted ORDER BY state_at, stream_id")

    def demoted_on_channel(self, channel_id):
        """Streams being kept at the bottom of a channel, oldest demotion first."""
        return self._rows(
            f"""SELECT * FROM {PREFIX}demoted WHERE channel_id = %s AND state = 'demoted'
                ORDER BY state_at, stream_id""",
            (channel_id,),
        )

    def set_position(self, channel_id, stream_id, position):
        """Where another tool has ranked a demoted stream, which is now where it would go back to."""
        self._exec(
            f"UPDATE {PREFIX}demoted SET from_position = %s WHERE channel_id = %s AND stream_id = %s",
            (position, channel_id, stream_id),
        )
        self._exec(
            f"""UPDATE {PREFIX}journal SET from_position = %s
                WHERE channel_id = %s AND stream_id = %s AND action = 'demote' AND undone_at IS NULL""",
            (position, channel_id, stream_id),
        )

    def set_missing(self, channel_id, stream_id, at):
        self._exec(
            f"UPDATE {PREFIX}demoted SET missing_since = %s WHERE channel_id = %s AND stream_id = %s",
            (at, channel_id, stream_id),
        )

    # The order we last left a channel in

    def guard(self, channel_id):
        return self._one(f"SELECT * FROM {PREFIX}guard WHERE channel_id = %s", (channel_id,))

    def all_guards(self):
        return self._rows(f"SELECT * FROM {PREFIX}guard")

    def put_guard(self, channel_id, expected, links, at):
        self._exec(
            f"""INSERT INTO {PREFIX}guard (channel_id, expected, links, updated_at, pending, pending_at)
                VALUES (%s, %s, %s, %s, NULL, NULL)
                ON CONFLICT (channel_id) DO UPDATE SET expected = excluded.expected, links = excluded.links,
                updated_at = excluded.updated_at, pending = NULL, pending_at = NULL""",
            (channel_id, json.dumps(expected), json.dumps(links), at),
        )

    def set_guard_pending(self, channel_id, pending, at):
        self._exec(
            f"UPDATE {PREFIX}guard SET pending = %s, pending_at = %s WHERE channel_id = %s",
            (pending, at, channel_id),
        )

    def delete_guard(self, channel_id):
        self._exec(f"DELETE FROM {PREFIX}guard WHERE channel_id = %s", (channel_id,))

    def guards_with_pending(self):
        return [row["channel_id"] for row in self._rows(f"SELECT channel_id FROM {PREFIX}guard WHERE pending IS NOT NULL")]

    def orphan_guards(self):
        rows = self._rows(
            f"""SELECT g.channel_id FROM {PREFIX}guard g WHERE NOT EXISTS (
                SELECT 1 FROM {PREFIX}demoted d WHERE d.channel_id = g.channel_id AND d.state = 'demoted')"""
        )
        return [row["channel_id"] for row in rows]

    # Content samples

    def add_sample(self, at, channel_id, stream_id, since, problems, audible, looped, radio, still, hashes, cpu):
        self._exec(
            f"""INSERT INTO {PREFIX}sample
                (at, channel_id, stream_id, since, problems, audible, looped, radio, still, hashes, cpu)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (at, channel_id, stream_id, since, ",".join(problems), int(bool(audible)), int(bool(looped)),
             int(bool(radio)), still, json.dumps(hashes), cpu),
        )

    def session_samples(self, channel_id, stream_id, since):
        """This session's samples on this stream, newest first."""
        rows = self._rows(
            f"""SELECT * FROM {PREFIX}sample WHERE channel_id = %s AND stream_id = %s AND since >= %s
                ORDER BY at DESC, id DESC""",
            (channel_id, stream_id, since),
        )
        for row in rows:
            row["problems"] = [item for item in row["problems"].split(",") if item]
            row["hashes"] = json.loads(row["hashes"])
        return rows

    def last_sample_at(self, channel_id, stream_id, since):
        value = self._value(
            f"SELECT MAX(at) FROM {PREFIX}sample WHERE channel_id = %s AND stream_id = %s AND since >= %s",
            (channel_id, stream_id, since),
        )
        return float(value) if value is not None else None

    def clean_sample_since(self, channel_id, other_than, since):
        """Has another stream on this channel given a sample with nothing wrong since a time?"""
        return bool(
            self._value(
                f"""SELECT COUNT(*) FROM {PREFIX}sample WHERE channel_id = %s AND stream_id <> %s AND at >= %s
                    AND problems = '' AND looped = 0""",
                (channel_id, other_than, since),
            )
        )

    def clean_sample_count(self, channel_id, stream_id, since):
        return int(
            self._value(
                f"""SELECT COUNT(*) FROM {PREFIX}sample WHERE channel_id = %s AND stream_id = %s AND at >= %s
                    AND problems = '' AND looped = 0""",
                (channel_id, stream_id, since),
            )
            or 0
        )

    def sample_totals(self, since):
        row = self._one(
            f"""SELECT COUNT(*) AS n, AVG(cpu) AS cpu, SUM(radio) AS radio, MAX(at) AS last
                FROM {PREFIX}sample WHERE at >= %s""",
            (since,),
        )
        return row or {}

    # Picture fingerprints

    def add_picture(self, picture, channel_id, at, taught=False):
        self._exec(
            f"""INSERT INTO {PREFIX}hash (picture, channel_id, at, taught) VALUES (%s, %s, %s, %s)
                ON CONFLICT (picture, channel_id) DO UPDATE SET at = excluded.at,
                taught = CASE WHEN {PREFIX}hash.taught = 1 THEN 1 ELSE excluded.taught END""",
            (f"{picture:016x}", channel_id, at, int(bool(taught))),
        )

    def known_pictures(self):
        rows = self._rows(f"SELECT picture, channel_id, taught FROM {PREFIX}hash")
        return [(int(row["picture"], 16), row["channel_id"], bool(row["taught"])) for row in rows]

    def picture_counts(self):
        return {
            "kept": int(self._value(f"SELECT COUNT(DISTINCT picture) FROM {PREFIX}hash") or 0),
            "taught": int(self._value(f"SELECT COUNT(DISTINCT picture) FROM {PREFIX}hash WHERE taught = 1") or 0),
        }

    def forget_pictures(self):
        return self._exec(f"DELETE FROM {PREFIX}hash")

    # Journal

    def add_journal(self, at, action, channel_id=None, stream_id=None, account_key=None, cause=None,
                    actor=None, from_position=None, to_position=None, before=None, after=None):
        self._exec(
            f"""INSERT INTO {PREFIX}journal (at, action, channel_id, stream_id, account_key, cause, actor,
                from_position, to_position, before_order, after_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (at, action, channel_id, stream_id, account_key, cause, actor, from_position, to_position,
             json.dumps(before) if before is not None else None,
             json.dumps(after) if after is not None else None),
        )

    def journal_recent(self, limit=50):
        return self._rows(f"SELECT * FROM {PREFIX}journal ORDER BY at DESC, id DESC LIMIT %s", (limit,))

    def undoable(self, channel_id=None, account_key=None, since=None, limit=None, skip_flags=False):
        """Demotions still in force, newest first, which is the order to reverse them in."""
        sql = f"""SELECT j.* FROM {PREFIX}journal j JOIN {PREFIX}demoted d
                  ON d.channel_id = j.channel_id AND d.stream_id = j.stream_id
                  WHERE j.action = 'demote' AND j.undone_at IS NULL AND d.state = 'demoted'"""
        params = []
        if skip_flags:
            sql += " AND j.cause <> 'flag'"
        if channel_id is not None:
            sql += " AND j.channel_id = %s"
            params.append(channel_id)
        if account_key is not None:
            sql += " AND j.account_key = %s"
            params.append(account_key)
        if since is not None:
            sql += " AND j.at >= %s"
            params.append(since)
        sql += " ORDER BY j.at DESC, j.id DESC"
        if limit:
            sql += " LIMIT %s"
            params.append(limit)
        return self._rows(sql, params)

    def close_demote_rows(self, channel_id, stream_id, at):
        """This demotion is over, one way or another, so Undo must leave it alone from now on."""
        self._exec(
            f"""UPDATE {PREFIX}journal SET undone_at = %s
                WHERE channel_id = %s AND stream_id = %s AND action = 'demote' AND undone_at IS NULL""",
            (at, channel_id, stream_id),
        )

    def demote_count(self, since, channel_id=None):
        marks = ", ".join(["%s"] * len(DEMOTE_ACTIONS))
        sql = f"SELECT COUNT(*) FROM {PREFIX}journal WHERE at >= %s AND action IN ({marks})"
        params = [since, *DEMOTE_ACTIONS]
        if channel_id is not None:
            sql += " AND channel_id = %s"
            params.append(channel_id)
        return int(self._value(sql, params) or 0)

    def flagged_streams(self, channel_id, since):
        marks = ", ".join(["%s"] * len(DEMOTE_ACTIONS))
        rows = self._rows(
            f"""SELECT DISTINCT stream_id FROM {PREFIX}journal
                WHERE channel_id = %s AND at >= %s AND cause = 'flag' AND action IN ({marks})""",
            (channel_id, since, *DEMOTE_ACTIONS),
        )
        return {row["stream_id"] for row in rows}

    def journal_counts(self, since):
        rows = self._rows(
            f"SELECT action, COUNT(*) AS n FROM {PREFIX}journal WHERE at >= %s GROUP BY action",
            (since,),
        )
        return {row["action"]: int(row["n"]) for row in rows}

    # Tokens

    def add_token(self, token_hash, label, at):
        self._exec(
            f"INSERT INTO {PREFIX}token (token_hash, label, created_at) VALUES (%s, %s, %s)",
            (token_hash, label, at),
        )

    def find_token(self, token_hash):
        return self._one(
            f"SELECT * FROM {PREFIX}token WHERE token_hash = %s AND revoked_at IS NULL",
            (token_hash,),
        )

    def touch_token(self, token_id, at):
        self._exec(f"UPDATE {PREFIX}token SET last_used_at = %s WHERE id = %s", (at, token_id))

    def list_tokens(self):
        return self._rows(
            f"SELECT id, label, created_at, last_used_at, revoked_at FROM {PREFIX}token ORDER BY id"
        )

    def revoke_token(self, label, at):
        return self._exec(
            f"UPDATE {PREFIX}token SET revoked_at = %s WHERE label = %s AND revoked_at IS NULL",
            (at, label),
        )

    def label_in_use(self, label):
        return bool(
            self._value(
                f"SELECT COUNT(*) FROM {PREFIX}token WHERE label = %s AND revoked_at IS NULL",
                (label,),
            )
        )

    # Rate limits, "once in a while" jobs and short claims

    def hit(self, key, now, window):
        """Count a hit in a window. Returns (hits so far, seconds until the window ends)."""
        cutoff = now - window
        rows = self._rows(
            f"""INSERT INTO {PREFIX}rate (key, started, hits) VALUES (%s, %s, 1)
                ON CONFLICT (key) DO UPDATE SET
                    hits = CASE WHEN {PREFIX}rate.started <= %s THEN 1 ELSE {PREFIX}rate.hits + 1 END,
                    started = CASE WHEN {PREFIX}rate.started <= %s THEN %s ELSE {PREFIX}rate.started END
                RETURNING hits, started""",
            (key, now, cutoff, cutoff, now),
        )
        row = rows[0]
        return int(row["hits"]), max(0.0, float(row["started"]) + window - now)

    def once_every(self, key, now, window):
        return self.hit(key, now, window)[0] == 1

    def hits_now(self, key, now, window):
        row = self._one(f"SELECT started, hits FROM {PREFIX}rate WHERE key = %s", (key,))
        if not row or float(row["started"]) <= now - window:
            return 0
        return int(row["hits"])

    def started_at(self, key):
        value = self._value(f"SELECT started FROM {PREFIX}rate WHERE key = %s", (key,))
        return float(value) if value is not None else None

    def claim(self, key, now, ttl):
        """True for one caller until released or until it runs out. Safe across workers."""
        return (
            self._exec(
                f"""INSERT INTO {PREFIX}rate (key, started, hits) VALUES (%s, %s, 1)
                    ON CONFLICT (key) DO UPDATE SET started = excluded.started, hits = 1
                    WHERE {PREFIX}rate.started <= %s""",
                (key, now, now - ttl),
            )
            == 1
        )

    def release(self, key):
        self._exec(f"DELETE FROM {PREFIX}rate WHERE key = %s", (key,))

    # This install

    def meta(self, name):
        return self._value(f"SELECT value FROM {PREFIX}meta WHERE name = %s", (name,))

    def set_meta(self, name, value):
        self._exec(
            f"""INSERT INTO {PREFIX}meta (name, value) VALUES (%s, %s)
                ON CONFLICT (name) DO UPDATE SET value = excluded.value""",
            (name, str(value)),
        )

    def forget_install(self, at):
        """The plugin was removed and put back. Old tokens and demotions must not come back with it."""
        for table in ("failure", "session", "demoted", "guard", "sample", "hash", "rate", "account", "stop"):
            self._exec(f"DELETE FROM {PREFIX}{table}")
        self._exec(f"UPDATE {PREFIX}token SET revoked_at = %s WHERE revoked_at IS NULL", (at,))
        self._exec(f"UPDATE {PREFIX}journal SET undone_at = %s WHERE action = 'demote' AND undone_at IS NULL", (at,))

    # Housekeeping

    def prune(self, now, journal_days):
        self._exec(f"DELETE FROM {PREFIX}journal WHERE at < %s", (now - journal_days * 86400,))
        self._exec(f"DELETE FROM {PREFIX}failure WHERE at < %s", (now - 30 * 86400,))
        self._exec(f"DELETE FROM {PREFIX}rate WHERE started < %s", (now - 7 * 86400,))
        self._exec(f"DELETE FROM {PREFIX}session WHERE since < %s", (now - 7 * 86400,))
        self._exec(f"DELETE FROM {PREFIX}stop WHERE at < %s", (now - 3600,))
        self._exec(f"DELETE FROM {PREFIX}sample WHERE at < %s", (now - 86400,))
        self._exec(f"DELETE FROM {PREFIX}hash WHERE at < %s AND taught = 0", (now - 30 * 86400,))
        self._exec(f"DELETE FROM {PREFIX}hash WHERE at < %s", (now - 90 * 86400,))
        self._exec(
            f"DELETE FROM {PREFIX}demoted WHERE state = 'parole' AND state_at < %s",
            (now - 30 * 86400,),
        )

    def erase(self):
        for table in TABLE_NAMES:
            self._exec(f"DELETE FROM {PREFIX}{table}")
