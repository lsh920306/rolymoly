"""Native PostgreSQL app-schema backup and disposable restoration verification.

Run ``check-tools`` first. Install matching PostgreSQL client tools if absent;
there is deliberately no partial CSV fallback. ``create`` uses a read-only
exported snapshot shared with pg_dump. Backups contain private account/session
data and stay under .data/backups, outside Git and the deployment package.

``verify --backup-dir ...`` restores only this tool's trusted native archive to
a newly generated QA schema, compares every table and its structural metadata,
checks sequence values, and rolls the entire transaction back (success or error).
It never restores over the operational schema or accepts a target schema name.
This covers the app schema, not Supabase project roles/settings/extensions.
"""
from __future__ import annotations

import argparse
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roly.postgres import SCHEMA_VERSION, _regions, _script_statements, validate_schema
from roly.storage_config import DEFAULT_SECRETS, load_config

BACKUP_ROOT = ROOT / ".data" / "backups"
IDENT = r"[a-z_][a-z0-9_]*"
QA = re.compile(r"rolymoly_qa_[a-f0-9]{32}\Z")


class BackupError(ValueError):
    """Only static messages, never driver/tool output or connection secrets."""


def tools_available(bin_dir=None):
    found = {}
    for name in ("pg_dump", "pg_restore"):
        candidate = (Path(bin_dir) / (name + (".exe" if os.name == "nt" else ""))) if bin_dir else None
        path = str(candidate) if candidate and candidate.is_file() else (shutil.which(name) if not bin_dir else None)
        if not path:
            raise BackupError("PostgreSQL pg_dump and pg_restore are required; install client tools or pass --pg-bin.")
        result = subprocess.run([path, "--version"], capture_output=True, check=False, timeout=15)
        version = re.search(rb"\(PostgreSQL\) (\d+)\.", result.stdout)
        if result.returncode or not version:
            raise BackupError("PostgreSQL client tool version could not be verified.")
        found[name] = path
        found[name + "_major"] = int(version.group(1))
    if found["pg_dump_major"] != found["pg_restore_major"]:
        raise BackupError("pg_dump and pg_restore major versions must match.")
    return found


def _native_environment(config):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PG")}
    env.update(PGHOST=config.host, PGPORT=str(config.port), PGUSER=config.user,
               PGPASSWORD=config.password, PGDATABASE=config.database, PGSSLMODE="require",
               PGCONNECT_TIMEOUT="10", PGAPPNAME="rolymoly-private-backup",
               PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=120000")
    return env


def _native(command, *, env=None):
    # Connection values are environment entries, never argv or printed stderr.
    result = subprocess.run(command, env=env, capture_output=True, check=False, timeout=600)
    if result.returncode:
        raise BackupError("PostgreSQL backup tool failed; no restore verification was completed.")
    return result.stdout


def _private_path(path):
    root = BACKUP_ROOT.resolve()
    candidate = Path(path).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise BackupError("Backup directories must be inside the private .data/backups directory.")
    # Reject linked path components, including Windows junctions.
    for item in (Path(path), *Path(path).parents):
        if item.exists() and (item.is_symlink() or getattr(item.stat(), "st_file_attributes", 0) & 0x400):
            raise BackupError("Linked backup paths are not allowed.")
        if item == ROOT:
            break
    return candidate


def _normalize(value, schema):
    return value.replace('"' + schema + '".', '"APP".').replace(schema + ".", "APP.") if isinstance(value, str) else value


def inspect_schema(connection, schema):
    """Canonical table contents, columns, constraints and indexes, no row output."""
    from psycopg import sql
    validate_schema(schema)
    tables = [row[0] for row in connection.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind='r' ORDER BY c.relname", (schema,))]
    unsupported = connection.execute(
        "SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind NOT IN ('r','i','S')) OR "
        "EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=%s)",
        (schema, schema)).fetchone()[0]
    if unsupported or not tables or "_schema_migrations" not in tables:
        raise BackupError("The app schema has unsupported objects or is not initialized; verification is stopped.")
    version = connection.execute(sql.SQL("SELECT MAX(version) FROM {}._schema_migrations").format(sql.Identifier(schema))).fetchone()[0]
    if version != SCHEMA_VERSION:
        raise BackupError("The backup tool and app schema versions differ; use matching source code.")
    result = {"schema_version": version, "tables": {}}
    for table in tables:
        digest, count = hashlib.sha256(), 0
        query = sql.SQL("SELECT row_to_json(t)::text FROM {}.{} t ORDER BY row_to_json(t)::text COLLATE \"C\"").format(
            sql.Identifier(schema), sql.Identifier(table))
        # A server cursor avoids holding member/session history in process memory.
        with connection.cursor(name="backup_" + uuid.uuid4().hex) as cursor:
            cursor.execute(query)
            for row in cursor:
                payload = row[0].encode("utf-8")
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
                count += 1
        columns = list(connection.execute(
            "SELECT column_name,data_type,udt_name,is_nullable,column_default,is_identity,identity_generation "
            "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, table)))
        constraints = list(connection.execute(
            "SELECT con.conname,con.contype,pg_get_constraintdef(con.oid) FROM pg_constraint con "
            "JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s ORDER BY con.conname", (schema, table)))
        indexes = list(connection.execute(
            "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s ORDER BY indexname", (schema, table)))
        result["tables"][table] = {"rows": count, "sha256": digest.hexdigest(),
                                  "columns": [[_normalize(v, schema) for v in row] for row in columns],
                                  "constraints": [[_normalize(v, schema) for v in row] for row in constraints],
                                  "indexes": [[_normalize(v, schema) for v in row] for row in indexes]}
    return result


def create_backup(config, native, *, source_schema="rolymoly", connect=None):
    import psycopg
    validate_schema(source_schema)
    connect = connect or psycopg.connect
    directory = _private_path(BACKUP_ROOT / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]))
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.mkdir(mode=0o700)
    archive = directory / "application.dump"
    connection = None
    try:
        connection = connect(**config.connect_kwargs())
        connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '120s'")
        connection.execute("SET LOCAL search_path TO pg_catalog")
        server_major = int(connection.execute("SHOW server_version_num").fetchone()[0]) // 10000
        if native["pg_dump_major"] < server_major:
            raise BackupError("pg_dump is older than the PostgreSQL server; upgrade client tools.")
        snapshot = connection.execute("SELECT pg_export_snapshot()").fetchone()[0]
        fingerprint = inspect_schema(connection, source_schema)
        _native([native["pg_dump"], "--format=custom", "--no-owner", "--no-comments",
                 "--no-security-labels", "--schema=" + source_schema, "--snapshot=" + snapshot,
                 "--file=" + str(archive)], env=_native_environment(config))
        if not archive.is_file() or archive.stat().st_size < 5 or archive.read_bytes()[:5] != b"PGDMP":
            raise BackupError("A valid native PostgreSQL archive was not produced.")
        manifest = {"format": 1, "scope": "complete_private_app_schema", "schema": source_schema,
                    "created_at": datetime.now(timezone.utc).isoformat(), "postgres_major": server_major,
                    "pg_dump_major": native["pg_dump_major"], "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "read_only_exported_snapshot": True, "fingerprint": fingerprint,
                    "includes": "accounts, sessions, members, all app tables, constraints, indexes and sequences",
                    "excludes": "Supabase project configuration, roles, extensions and external assets"}
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        archive.chmod(0o600)
        (directory / "manifest.json").chmod(0o600)
        return {"ok": True, "backup_directory": str(directory.relative_to(ROOT)), "tables": len(fingerprint["tables"]),
                "schema_version": fingerprint["schema_version"], "restore_verified": False}
    except Exception:
        # Only paths exclusively created by this invocation are removed.
        for name in ("application.dump", "manifest.json"):
            with suppress(OSError):
                (directory / name).unlink()
        with suppress(OSError):
            directory.rmdir()
        raise
    finally:
        if connection:
            with suppress(Exception):
                connection.execute("ROLLBACK")
            connection.close()


def _dump_operations(text):
    """Split native SQL, preserving COPY payload bytes and quoted text verbatim."""
    pending, copy_header, payload = "", None, []
    for line in text.splitlines(keepends=True):
        if copy_header is not None:
            if line.rstrip("\r\n") == r"\.":
                yield copy_header, "".join(payload)
                copy_header, payload = None, []
            else:
                payload.append(line)
            continue
        if not pending.strip() and re.fullmatch(r"\\(?:un)?restrict [a-zA-Z0-9]+\s*", line):
            continue  # pg_restore's psql-only connection restriction marker.
        pending += line
        try:
            regions = list(_regions(pending))
        except sqlite3.ProgrammingError:
            continue  # A multiline quoted token has not ended yet.
        code = "".join(value for kind, value in regions if kind != "comment").strip()
        if not code:
            pending = ""
            continue
        if not code.endswith(";"):
            continue
        statements = list(_script_statements(pending))
        pending = ""
        for statement in statements:
            clean = "".join(value for kind, value in _regions(statement) if kind != "comment").strip()
            if re.match(r"COPY\s", clean):
                if copy_header is not None or not re.fullmatch(r"COPY .+ FROM stdin", clean):
                    raise BackupError("Unsupported native archive COPY command.")
                copy_header = clean
            else:
                yield clean, None
    if copy_header is not None or pending.strip():
        raise BackupError("The native archive SQL is incomplete.")


def remap_operation(statement, source, target):
    """Fail closed on SQL outside this app's tables/indexes/identity sequences."""
    validate_schema(source)
    if not QA.fullmatch(target) or target == source:
        raise BackupError("Restore requires a new generated QA schema.")
    regions = list(_regions(statement))
    # Quoted column names (e.g. "position") are normal pg_dump output. Reduce
    # them to simple identifiers for validation, without altering row literals.
    structural = ""
    for kind, text in regions:
        if kind == "quoted" and text.startswith('"'):
            if not re.fullmatch('"' + IDENT + '"', text):
                raise BackupError("Unsupported quoted identifier in native archive.")
            structural += text[1:-1]
        else:
            structural += text
    prefix = re.escape(source) + r"\." + IDENT
    if structural == "CREATE SCHEMA " + source:
        return None
    if re.fullmatch(r"SET (?:statement_timeout|lock_timeout|idle_in_transaction_session_timeout|transaction_timeout|client_encoding|standard_conforming_strings|check_function_bodies|xmloption|client_min_messages|row_security|default_tablespace|default_table_access_method) = [^;]+", statement):
        return statement
    if statement == "SELECT pg_catalog.set_config('search_path', '', false)":
        return statement
    sequence = re.fullmatch(r"SELECT pg_catalog\.setval\('" + re.escape(source) + r"\.(" + IDENT + r")', (-?\d+), (true|false)\)", statement)
    if sequence:
        return f"SELECT pg_catalog.setval('{target}.{sequence[1]}', {sequence[2]}, {sequence[3]})"
    patterns = (
        r"CREATE TABLE " + prefix + r"\s*\(",
        r"ALTER TABLE (?:ONLY )?" + prefix + r"\s+",
        r"CREATE SEQUENCE " + prefix + r"\s+",
        r"ALTER SEQUENCE " + prefix + r"\s+",
        r"CREATE (?:UNIQUE )?INDEX " + IDENT + r" ON " + prefix + r"\s+",
        r"COPY " + prefix + r" \([a-z_0-9, ]+\) FROM stdin\Z",
    )
    if not any(re.match(pattern, structural) for pattern in patterns):
        raise BackupError("Unsupported SQL in native archive; automatic QA restoration is stopped.")
    structural_code = "".join(text if kind == "code" else " " for kind, text in _regions(structural))
    if re.search(r"\b(?:RENAME|SET\s+SCHEMA|OWNER\s+TO|ATTACH|DETACH|DISABLE\s+TRIGGER|ENABLE\s+TRIGGER)\b", structural_code):
        raise BackupError("Archive SQL cannot move objects or alter ownership/triggers during QA restoration.")
    qualifiers = re.findall(r"\b(" + IDENT + r")\.(?=[a-z_])", structural_code)
    if set(qualifiers) - {source, "pg_catalog"}:
        raise BackupError("Cross-schema dependencies are not supported by QA restoration.")
    output = []
    for index, (kind, text) in enumerate(regions):
        if kind == "code":
            output.append(re.sub(r"\b" + re.escape(source) + r"(?=\.)", target, text))
        elif kind == "quoted" and text.startswith("$"):
            raise BackupError("Procedural SQL is not supported by QA restoration.")
        elif kind == "quoted" and text == '"' + source + '"':
            output.append('"' + target + '"')
        elif (kind == "quoted" and index + 1 < len(regions)
              and re.match(r"\s*::regclass\b", regions[index + 1][1])):
            # Identity sequences are native DDL; legacy nextval defaults also
            # have a schema-qualified regclass string in schema-only SQL.
            if not re.fullmatch("'" + re.escape(source) + r"\." + IDENT + "'", text):
                raise BackupError("Unsupported sequence reference in native archive.")
            output.append("'" + target + text[len(source) + 1:])
        else:
            output.append(text)
    return "".join(output)


def verify_backup(config, native, directory, *, connect=None):
    import psycopg
    from psycopg import sql
    directory = _private_path(directory)
    archive = directory / "application.dump"
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or manifest.get("scope") != "complete_private_app_schema":
        raise BackupError("Unsupported private backup manifest.")
    source = validate_schema(manifest["schema"])
    if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest.get("archive_sha256"):
        raise BackupError("Archive checksum mismatch; restoration was not attempted.")
    if native["pg_restore_major"] < manifest["pg_dump_major"]:
        raise BackupError("pg_restore is older than this archive; upgrade client tools.")
    text = _native([native["pg_restore"], "--no-owner", "--no-acl", "--no-comments",
                    "--no-security-labels", "--file=-", str(archive)]).decode("utf-8")
    target = "rolymoly_qa_" + uuid.uuid4().hex
    operations = [(remap_operation(statement, source, target), payload)
                  for statement, payload in _dump_operations(text)]
    connect = connect or psycopg.connect
    connection, restored = None, False
    try:
        connection = connect(**config.connect_kwargs(readonly=False))
        connection.execute("BEGIN")
        connection.execute("SET LOCAL statement_timeout = '120s'")
        # No IF NOT EXISTS: even an extremely unlikely name collision stops.
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(target)))
        connection.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(target)))
        for statement, payload in operations:
            if statement is None:
                continue
            if payload is None:
                connection.execute(statement)
            else:
                with connection.cursor() as cursor:
                    with cursor.copy(statement) as stream:
                        stream.write(payload.encode("utf-8"))
        # Search path must match source inspection for qualified FK definitions.
        connection.execute("SELECT pg_catalog.set_config('search_path', '', false)")
        actual = inspect_schema(connection, target)
        if actual != manifest["fingerprint"]:
            raise BackupError("Restored schema or row fingerprints differ from the snapshot.")
        expected_sequences = {}
        for statement, payload in operations:
            match = re.fullmatch(r"SELECT pg_catalog\.setval\('" + target + r"\.(" + IDENT + r")', (-?\d+), (true|false)\)", statement or "")
            if match:
                expected_sequences[match[1]] = (int(match[2]), match[3] == "true")
        actual_sequences = [row[0] for row in connection.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind='S'", (target,))]
        if set(actual_sequences) != set(expected_sequences):
            raise BackupError("Restored sequence inventory differs from the native archive.")
        for name in actual_sequences:
            actual_value = tuple(connection.execute(sql.SQL("SELECT last_value,is_called FROM {}.{}").format(sql.Identifier(target), sql.Identifier(name))).fetchone())
            if actual_value != expected_sequences[name]:
                raise BackupError("Restored identity sequence differs from the native archive.")
        restored = True
    finally:
        if connection:
            try:
                connection.execute("ROLLBACK")  # Created QA schema/data never commit.
                remains = connection.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)", (target,)).fetchone()[0]
                if remains:
                    raise BackupError("QA rollback could not be confirmed; no cleanup outside this transaction was attempted.")
            finally:
                connection.close()
    return {"ok": restored, "tables": len(manifest["fingerprint"]["tables"]),
            "sequences": len(expected_sequences), "schema_version": manifest["fingerprint"]["schema_version"],
            "row_and_structure_hashes_match": restored, "qa_schema_removed": True,
            "operational_schema_written": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check-tools", "create", "verify"))
    parser.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS)
    parser.add_argument("--pg-bin", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        native = tools_available(args.pg_bin)  # Fails before loading secrets/DB.
        if args.command == "check-tools":
            result = {"ok": True, "postgres_client_major": native["pg_dump_major"], "database_connected": False}
        elif args.command == "create":
            result = create_backup(load_config(args.secrets_file), native)
        else:
            if not args.backup_dir:
                raise BackupError("verify requires --backup-dir inside .data/backups.")
            result = verify_backup(load_config(args.secrets_file), native, args.backup_dir)
    except BackupError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2
    except Exception:
        print(json.dumps({"ok": False, "error": "Backup verification failed. Connection values and raw tool/driver errors are hidden."}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
