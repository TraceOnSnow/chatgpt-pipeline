"""Transactional, offline SQLite index. Exports are always opened read-only."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3

from .normalize import VERSION, normalize
from .verify import encoded, read_source, sha, verify

SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_meta (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
 account_key TEXT PRIMARY KEY, source_root TEXT NOT NULL,
 current_catalog_sha256 TEXT, indexed_at TEXT);
CREATE TABLE IF NOT EXISTS commits (
 account_key TEXT NOT NULL, commit_sha256 TEXT NOT NULL, catalog_sha256 TEXT NOT NULL,
 source_path TEXT NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY(account_key, commit_sha256));
CREATE TABLE IF NOT EXISTS catalogs (
 account_key TEXT NOT NULL, catalog_sha256 TEXT NOT NULL, source_path TEXT NOT NULL,
 metadata_json TEXT NOT NULL, PRIMARY KEY(account_key, catalog_sha256));
CREATE TABLE IF NOT EXISTS catalog_entries (
 account_key TEXT NOT NULL, catalog_sha256 TEXT NOT NULL, conversation_id TEXT NOT NULL,
 body_status TEXT NOT NULL, object_path TEXT, entry_json TEXT NOT NULL,
 PRIMARY KEY(account_key, catalog_sha256, conversation_id));
CREATE TABLE IF NOT EXISTS objects (
 account_key TEXT NOT NULL, conversation_id TEXT NOT NULL, object_path TEXT NOT NULL,
 byte_sha256 TEXT NOT NULL, raw_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL,
 source_path TEXT NOT NULL, url TEXT, readable_status TEXT, received_at TEXT NOT NULL,
 PRIMARY KEY(account_key, object_path));
CREATE TABLE IF NOT EXISTS commit_objects (
 account_key TEXT NOT NULL, commit_sha256 TEXT NOT NULL, object_path TEXT NOT NULL,
 PRIMARY KEY(account_key, commit_sha256, object_path));
CREATE TABLE IF NOT EXISTS normalizations (
 account_key TEXT NOT NULL, object_path TEXT NOT NULL, normalization_version TEXT NOT NULL,
 branch TEXT NOT NULL, current_node TEXT, input_fingerprint TEXT NOT NULL,
 state TEXT NOT NULL, issues_json TEXT NOT NULL, processed_at TEXT NOT NULL,
 PRIMARY KEY(account_key, object_path, normalization_version));
CREATE TABLE IF NOT EXISTS messages (
 account_key TEXT NOT NULL, conversation_id TEXT NOT NULL, object_path TEXT NOT NULL,
 normalization_version TEXT NOT NULL, position INTEGER NOT NULL, message_id TEXT NOT NULL,
 role TEXT, text TEXT NOT NULL, exclusion TEXT, message_json TEXT NOT NULL,
 PRIMARY KEY(account_key, object_path, normalization_version, position));
CREATE TABLE IF NOT EXISTS analysis_inputs (
 account_key TEXT NOT NULL, conversation_id TEXT NOT NULL, normalization_version TEXT NOT NULL,
 input_fingerprint TEXT NOT NULL, messages_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(account_key, conversation_id, normalization_version, input_fingerprint));
CREATE TABLE IF NOT EXISTS ingest_attempts (
 id INTEGER PRIMARY KEY, account_key TEXT NOT NULL, source_path TEXT NOT NULL,
 status TEXT NOT NULL, reason TEXT, recorded_at TEXT NOT NULL);
CREATE VIEW IF NOT EXISTS current_conversations AS
 SELECT e.*, n.normalization_version, n.input_fingerprint, n.state, n.issues_json
 FROM accounts a JOIN catalog_entries e ON e.account_key=a.account_key
 AND e.catalog_sha256=a.current_catalog_sha256
 LEFT JOIN normalizations n ON n.account_key=e.account_key AND n.object_path=e.object_path;
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def json_text(value):
    return encoded(value, sort=True).decode()


def connect(database):
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1):
        db.close()
        raise ValueError("Unsupported index schema version")
    db.executescript(SCHEMA)
    if not db.execute("SELECT 1 FROM pipeline_meta").fetchone():
        db.execute("INSERT INTO pipeline_meta VALUES (1)")
    db.execute("PRAGMA user_version=1")
    db.commit()
    return db


def check_database_path(database, exports):
    database = Path(database).expanduser().resolve()
    exports = Path(exports).expanduser().resolve()
    if database.is_relative_to(exports) or database.is_relative_to(exports.parent / "handoff"):
        raise ValueError("Database must be outside read-only exports and handoff")
    if database.exists() and not database.is_file():
        raise ValueError("Database path is not a file")
    database.parent.mkdir(parents=True, exist_ok=True)
    if not database.exists():
        try:
            descriptor = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
    database.chmod(0o600)
    return database


def ingest(exports, database):
    exports = Path(exports).expanduser().absolute()
    database = check_database_path(database, exports)
    reports = []
    with closing(connect(database)) as db:
        for root in sorted(exports.iterdir()):
            if not root.is_dir():
                continue
            report = {"account_key": root.name, "verified_commits": 0, "failed_commits": 0,
                      "current_updated": False, "errors": []}
            matching = set()
            try:
                latest_sha = sha(read_source(root, "catalog.json", r"catalog\.json"))
            except (OSError, ValueError) as error:
                latest_sha = None
                report["errors"].append({"source": str(root / "catalog.json"), "reason": str(error)})
            for commit_path in sorted((root / "commits").glob("*.commit.json")):
                try:
                    batch = verify(root, commit_path)
                    persist(db, root, batch)
                    report["verified_commits"] += 1
                    matching.add(batch["catalog_sha256"])
                except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
                    report["failed_commits"] += 1
                    reason = f"{type(error).__name__}: {error}"
                    report["errors"].append({"source": str(commit_path), "reason": reason})
                    with db:
                        db.execute("INSERT INTO ingest_attempts(account_key,source_path,status,reason,recorded_at) VALUES (?,?,?,?,?)",
                                   (root.name, str(commit_path), "failed", reason, now()))
            if latest_sha in matching:
                try:
                    # Re-read the live pointer after all immutable inputs have been verified.
                    if sha(read_source(root, "catalog.json", r"catalog\.json")) != latest_sha:
                        raise ValueError("Latest catalog changed during ingestion; current index retained")
                    with db:
                        db.execute("UPDATE accounts SET current_catalog_sha256=?, indexed_at=? WHERE account_key=?",
                                   (latest_sha, now(), root.name))
                    report["current_updated"] = True
                except (OSError, ValueError) as error:
                    report["errors"].append({"source": str(root / "catalog.json"), "reason": str(error)})
            else:
                report["errors"].append({"source": str(root / "catalog.json"),
                                         "reason": "No verified commit matches latest catalog; current index retained"})
            reports.append(report)
        return {"database": str(database), "accounts": reports, "index": statistics(db)}


def persist(db, root, batch):
    timestamp = now()
    account = batch["account_key"]
    commit = batch["commit_sha256"]
    catalog_hash = batch["catalog_sha256"]
    catalog = batch["catalog"]
    with db:
        db.execute("INSERT OR IGNORE INTO accounts(account_key,source_root) VALUES (?,?)", (account, str(root)))
        existing_root = db.execute("SELECT source_root FROM accounts WHERE account_key=?", (account,)).fetchone()[0]
        if existing_root != str(root):
            raise ValueError("Account already indexed from another source root")
        db.execute("INSERT OR IGNORE INTO commits VALUES (?,?,?,?,?)",
                   (account, commit, catalog_hash, batch["commit_path"], timestamp))
        db.execute("INSERT OR IGNORE INTO catalogs VALUES (?,?,?,?)",
                   (account, catalog_hash, batch["catalog_path"], json_text({k: v for k, v in catalog.items() if k != "conversations"})))
        for entry in catalog["conversations"]:
            db.execute("INSERT OR IGNORE INTO catalog_entries VALUES (?,?,?,?,?,?)",
                       (account, catalog_hash, entry["id"], entry["body_status"], entry.get("object"), json_text(entry)))
        for name, source in batch["packages"].items():
            package = source["package"]
            identity = package["conversation_id"]
            old = db.execute("SELECT byte_sha256 FROM objects WHERE account_key=? AND object_path=?", (account, name)).fetchone()
            if old and old[0] != source["byte_sha256"]:
                raise ValueError("Previously indexed immutable object changed bytes")
            db.execute("INSERT OR IGNORE INTO objects VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (account, identity, name, source["byte_sha256"], package["raw_sha256"], package["content_sha256"],
                        str(root / name), package.get("url"), package.get("readable_status"), timestamp))
            db.execute("INSERT OR IGNORE INTO commit_objects VALUES (?,?,?)", (account, commit, name))
            if db.execute("SELECT 1 FROM normalizations WHERE account_key=? AND object_path=? AND normalization_version=?",
                          (account, name, VERSION)).fetchone():
                continue
            result = normalize(package)
            db.execute("INSERT INTO normalizations VALUES (?,?,?,?,?,?,?,?,?)",
                       (account, name, VERSION, result["branch"], result["current_node"], result["input_fingerprint"],
                        result["state"], json_text(result["issues"]), timestamp))
            for message in result["messages"]:
                db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (account, identity, name, VERSION, message["position"], message["id"], message["role"],
                            message["text"], message["exclusion"], json_text(message)))
            if result["state"] == "ready":
                db.execute("INSERT OR IGNORE INTO analysis_inputs VALUES (?,?,?,?,?,?)",
                           (account, identity, VERSION, result["input_fingerprint"],
                            json_text(result["analysis_messages"]), timestamp))


def statistics(db):
    counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in ("accounts", "commits", "catalogs", "objects", "messages", "analysis_inputs")}
    counts["current"] = [dict(row) for row in db.execute(
        "SELECT account_key,body_status,state,COUNT(*) AS conversations FROM current_conversations "
        "WHERE normalization_version IS NULL OR normalization_version=? GROUP BY account_key,body_status,state", (VERSION,))]
    counts["current_messages"] = db.execute(
        "SELECT COUNT(*) FROM messages m JOIN current_conversations c ON m.account_key=c.account_key "
        "AND m.object_path=c.object_path AND m.normalization_version=c.normalization_version "
        "WHERE c.normalization_version=?", (VERSION,)).fetchone()[0]
    counts["current_analysis_messages"] = db.execute(
        "SELECT COUNT(*) FROM messages m JOIN current_conversations c ON m.account_key=c.account_key "
        "AND m.object_path=c.object_path AND m.normalization_version=c.normalization_version "
        "WHERE c.body_status='current' AND c.state='ready' AND c.normalization_version=? AND m.exclusion IS NULL", (VERSION,)).fetchone()[0]
    counts["current_message_exclusions"] = [dict(row) for row in db.execute(
        "SELECT m.exclusion,COUNT(*) AS messages FROM messages m JOIN current_conversations c "
        "ON m.account_key=c.account_key AND m.object_path=c.object_path "
        "AND m.normalization_version=c.normalization_version WHERE c.normalization_version=? "
        "GROUP BY m.exclusion", (VERSION,))]
    return counts


def status(database):
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return statistics(db)
