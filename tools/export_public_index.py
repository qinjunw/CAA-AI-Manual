"""Export an index-only CAA manual database for repository distribution."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from collections import defaultdict
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from typing import Any

try:
    from .caa_manual_layout import PROJECT_ROOT, RELEASE_ID, file_sha256, indexed_release, public_database, release_id
except ImportError:
    from caa_manual_layout import PROJECT_ROOT, RELEASE_ID, file_sha256, indexed_release, public_database, release_id

PUBLIC_EXPORT_SCHEMA_VERSION = 1


def _json_terms(value: str | None) -> str:
    try:
        items = json.loads(value or "[]")
    except json.JSONDecodeError:
        return ""
    return " ".join(str(item) for item in items)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def _clear_embedded_text(connection: sqlite3.Connection) -> None:
    updates = {
        "entities": "UPDATE entities SET summary=''",
        "chunks": "UPDATE chunks SET heading_path_json='[]', text=''",
        "evidence": "UPDATE evidence SET quote_or_span=''",
        "api_pages": "UPDATE api_pages SET summary_en=''",
        "api_members": "UPDATE api_members SET summary_en=''",
    }
    for table, statement in updates.items():
        if _table_exists(connection, table):
            connection.execute(statement)


def _materialize_example_links(connection: sqlite3.Connection) -> int:
    connection.execute("DROP TABLE IF EXISTS api_examples")
    connection.execute(
        """
        CREATE TABLE api_examples (
          api_node_id TEXT NOT NULL,
          chunk_id TEXT NOT NULL,
          source_uri TEXT NOT NULL,
          symbols_json TEXT NOT NULL,
          PRIMARY KEY(api_node_id, chunk_id)
        )
        """
    )
    if not all(
        _table_exists(connection, table)
        for table in ("api_pages", "api_members", "relations", "chunks")
    ):
        return 0

    page_nodes: dict[str, str] = {}
    raw_to_nodes: dict[str, set[str]] = defaultdict(set)
    for page_id, api_node_id, raw_ids_json in connection.execute(
        "SELECT page_id, api_node_id, raw_entity_ids_json FROM api_pages"
    ):
        page_nodes[page_id] = api_node_id
        for raw_id in json.loads(raw_ids_json or "[]"):
            raw_to_nodes[str(raw_id)].add(api_node_id)
    for page_id, raw_entity_id in connection.execute(
        "SELECT page_id, raw_entity_id FROM api_members"
    ):
        if raw_entity_id and page_id in page_nodes:
            raw_to_nodes[raw_entity_id].add(page_nodes[page_id])

    chunks = {
        chunk_id: (source_uri, symbols_json)
        for chunk_id, source_uri, symbols_json in connection.execute(
            "SELECT id, source_path, symbols_json FROM chunks"
        )
    }
    records: set[tuple[str, str, str, str]] = set()
    for chunk_id, raw_entity_id in connection.execute(
        "SELECT src_id, dst_id FROM relations WHERE type='DEMONSTRATES'"
    ):
        chunk = chunks.get(chunk_id)
        if not chunk:
            continue
        for api_node_id in raw_to_nodes.get(raw_entity_id, set()):
            records.add((api_node_id, chunk_id, chunk[0], chunk[1]))
    connection.executemany(
        "INSERT INTO api_examples VALUES (?,?,?,?)", sorted(records)
    )
    return len(records)


def _clear_raw_audit_rows(connection: sqlite3.Connection) -> None:
    for table in ("entities", "relations", "chunks"):
        if _table_exists(connection, table):
            connection.execute(f"DELETE FROM {table}")


def _drop_fts_tables(connection: sqlite3.Connection) -> None:
    for table in ("entities_fts", "chunks_fts", "api_search_fts"):
        if _table_exists(connection, table):
            connection.execute(f"DROP TABLE {table}")


def _rebuild_fts_tables(connection: sqlite3.Connection) -> bool:
    try:
        connection.executescript(
            """
            CREATE VIRTUAL TABLE entities_fts USING fts5(
              id UNINDEXED, name, kind, framework, summary, signature, tags
            );
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
              id UNINDEXED, heading, text, symbols, capabilities
            );
            CREATE VIRTUAL TABLE api_search_fts USING fts5(
              api_node_id UNINDEXED, page_id UNINDEXED, member_id UNINDEXED,
              record_type UNINDEXED, name, owner_name, framework, summary,
              signature, tags, aliases
            );
            """
        )
    except sqlite3.OperationalError as exc:
        if "no such module: fts5" in str(exc).casefold():
            return False
        raise

    if _table_exists(connection, "entities"):
        rows = connection.execute(
            "SELECT id, name, kind, framework, signature, tags_json FROM entities"
        ).fetchall()
        connection.executemany(
            "INSERT INTO entities_fts VALUES (?,?,?,?,?,?,?)",
            [
                (
                    row[0], row[1], row[2], row[3], "", row[4], _json_terms(row[5])
                )
                for row in rows
            ],
        )

    if _table_exists(connection, "chunks"):
        rows = connection.execute(
            "SELECT id, symbols_json, capabilities_json FROM chunks"
        ).fetchall()
        connection.executemany(
            "INSERT INTO chunks_fts VALUES (?,?,?,?,?)",
            [
                (row[0], "", "", _json_terms(row[1]), _json_terms(row[2]))
                for row in rows
            ],
        )

    aliases_by_node: dict[str, list[str]] = defaultdict(list)
    for target_id, alias in connection.execute(
        "SELECT target_id, alias FROM api_aliases WHERE target_type='api_node'"
    ):
        aliases_by_node[target_id].append(alias)

    pages_by_node: dict[str, list[sqlite3.Row]] = defaultdict(list)
    connection.row_factory = sqlite3.Row
    for page in connection.execute("SELECT * FROM api_pages"):
        pages_by_node[page["api_node_id"]].append(page)

    api_nodes = {
        row["node_id"]: row
        for row in connection.execute(
            "SELECT * FROM catalog_nodes WHERE node_type IN ('api','function-family')"
        )
    }
    connection.executemany(
        "INSERT INTO api_search_fts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                node_id,
                "",
                "",
                "api_node",
                node["name_en"],
                "",
                node["framework"],
                "",
                " ".join(page["signature"] for page in pages_by_node[node_id]),
                " ".join(
                    sorted(
                        {
                            tag
                            for page in pages_by_node[node_id]
                            for tag in json.loads(page["tags_json"] or "[]")
                        }
                    )
                ),
                " ".join(aliases_by_node.get(node_id, [])),
            )
            for node_id, node in api_nodes.items()
        ],
    )

    page_index = {
        row["page_id"]: row for row in connection.execute("SELECT * FROM api_pages")
    }
    members = connection.execute("SELECT * FROM api_members").fetchall()
    connection.executemany(
        "INSERT INTO api_search_fts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                page_index[member["page_id"]]["api_node_id"],
                member["page_id"],
                member["member_id"],
                "api_member",
                member["name"],
                api_nodes[page_index[member["page_id"]]["api_node_id"]]["name_en"],
                api_nodes[page_index[member["page_id"]]["api_node_id"]]["framework"],
                "",
                member["signature"],
                "",
                "",
            )
            for member in members
        ],
    )
    return True


def _set_public_metadata(connection: sqlite3.Connection, fts_available: bool) -> None:
    values = {
        "content_mode": "index-only",
        "embedded_official_text": "false",
        "public_export_schema_version": str(PUBLIC_EXPORT_SCHEMA_VERSION),
        "api_search_fts_available": "true" if fts_available else "false",
    }
    connection.executemany(
        """
        INSERT INTO projection_metadata(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        values.items(),
    )


def destination_release(output_db: Path) -> str | None:
    if not output_db.is_relative_to(PROJECT_ROOT / "data"):
        return None
    release = output_db.parent.name.upper()
    if RELEASE_ID.fullmatch(release) and output_db == public_database(PROJECT_ROOT, release):
        return release
    raise ValueError("Repository indexes must use data/r<number>/manual.sqlite")


def _scrub_database(source_db: Path, clean_db: Path, expected_release: str | None,
                    source_manifest: Path | None) -> dict[str, Any]:
    with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as source, \
            closing(sqlite3.connect(clean_db)) as target:
        source_mode = source.execute(
            "SELECT value FROM projection_metadata WHERE key='content_mode'"
        ).fetchone()
        if source_mode and source_mode[0] == "index-only":
            raise ValueError("source database must be a full local build")
        source_metadata = dict(source.execute("SELECT key,value FROM projection_metadata"))
        actual_release = indexed_release(source_metadata)
        if expected_release and actual_release != expected_release:
            raise ValueError(f"Cannot export {actual_release} into the {expected_release} release directory")
        _read_source_manifest(source_manifest, source_metadata)
        source.backup(target)
        target.execute("PRAGMA foreign_keys=ON")
        _clear_embedded_text(target)
        example_count = _materialize_example_links(target)
        _clear_raw_audit_rows(target)
        _drop_fts_tables(target)
        fts_available = _rebuild_fts_tables(target)
        _set_public_metadata(target, fts_available)
        foreign_key_errors = target.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise ValueError(f"foreign key check failed: {foreign_key_errors[:5]}")
        target.commit()
        target.execute("VACUUM")
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"integrity check failed: {integrity}")
        count_tables = (
            "entities", "relations", "chunks", "evidence", "catalog_nodes",
            "api_pages", "api_members", "api_aliases", "api_examples",
        )
        counts = {
            table: target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in count_tables if _table_exists(target, table)
        }
    return {
        "status": "ok",
        "content_mode": "index-only",
        "embedded_official_text": False,
        "output_bytes": clean_db.stat().st_size,
        "database_sha256": file_sha256(clean_db),
        "fts_available": fts_available,
        "api_examples": example_count,
        "artifact_counts": counts,
        "source_metadata": {key: source_metadata[key] for key in (
            "source_release", "source_baseline", "version_policy", "catalog_source_baseline",
            "source_tree_sha256") if key in source_metadata},
    }


def _read_source_manifest(source_manifest: Path | None,
                          source_metadata: dict[str, str]) -> dict[str, Any]:
    manifest: dict[str, Any] = {}
    if source_manifest:
        manifest = json.loads(source_manifest.read_text(encoding="utf-8-sig"))
    for key in ("source_release", "source_baseline", "version_policy",
                "catalog_source_baseline", "source_tree_sha256"):
        if key in manifest and manifest[key] != source_metadata.get(key):
            raise ValueError(f"Source manifest {key} does not match the source database")
    manifest.update(source_metadata)
    return manifest


def _manifest_text(source_manifest: Path | None, result: dict[str, Any]) -> str:
    manifest = _read_source_manifest(source_manifest, result.get("source_metadata", {}))
    manifest.update(
        source="caadoc://", output="manual://", content_mode="index-only",
        embedded_official_text=False, public_export_schema_version=PUBLIC_EXPORT_SCHEMA_VERSION,
        artifact_counts=result["artifact_counts"],
    )
    if "database_sha256" in result:
        manifest["database_sha256"] = result["database_sha256"]
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def _stage_path(stack: ExitStack, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    directory = stack.enter_context(TemporaryDirectory(dir=destination.parent, prefix=".publish-"))
    return Path(directory) / destination.name


def write_public_manifest(source_manifest: Path | None, output_manifest: Path, export_result: dict[str, Any]) -> None:
    content = _manifest_text(source_manifest, export_result)
    with ExitStack() as stack:
        stage = _stage_path(stack, output_manifest)
        stage.write_text(content, encoding="utf-8")
        if output_manifest.exists():
            shutil.copymode(output_manifest, stage)
        os.replace(stage, output_manifest)


def _publish(clean_db: Path, database: Path, manifest: Path | None, content: str, lock_dir: Path) -> None:
    with ExitStack() as stack:
        staged_db = _stage_path(stack, database)
        shutil.copyfile(clean_db, staged_db)
        if manifest is not None:
            staged_manifest = _stage_path(stack, manifest)
            staged_manifest.write_text(content, encoding="utf-8")
            # SQLite releases this interprocess lock even when the exporting process exits.
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context(closing(sqlite3.connect(lock_dir / ".publish.lock", timeout=60)))
            lock.execute("BEGIN EXCLUSIVE")
        if database.exists():
            shutil.copymode(database, staged_db)
        if manifest is None:
            os.replace(staged_db, database)
            return
        previous = staged_manifest.with_name(staged_manifest.name + ".previous")
        if manifest.exists():
            shutil.copy2(manifest, previous)
            shutil.copymode(manifest, staged_manifest)
        os.replace(staged_manifest, manifest)
        try:
            os.replace(staged_db, database)
        except BaseException:
            if previous.exists():
                os.replace(previous, manifest)
            else:
                manifest.unlink()
            raise


def export_public_index(source_db: Path, output_db: Path, source_manifest: Path | None = None,
                        output_manifest: Path | None = None) -> dict[str, Any]:
    requested = destination_release(output_db.absolute())
    source_db, output_db = source_db.resolve(), output_db.resolve()
    if source_db == output_db:
        raise ValueError("source and output databases must be different files")
    if not source_db.is_file():
        raise FileNotFoundError(f"source database not found: {source_db}")
    expected = destination_release(output_db)
    if requested and requested != expected:
        raise ValueError("The selected release directory redirects outside its distribution path")
    manifest = output_manifest.resolve() if output_manifest else None
    source_manifest = source_manifest.resolve() if source_manifest else None
    if expected:
        sibling = output_db.with_name("manifest.json")
        if manifest and manifest != sibling:
            raise ValueError(f"The public manifest must be written to {sibling}")
        manifest = sibling
        if source_manifest is None and source_db.with_name("manifest.json").is_file():
            source_manifest = source_db.with_name("manifest.json")
    elif manifest and manifest.is_relative_to(PROJECT_ROOT / "data"):
        raise ValueError("A repository manifest must accompany its release database")
    if manifest and manifest in (source_db, output_db, source_manifest):
        raise ValueError("The output manifest must be separate from the databases and source manifest")

    private_parent = Path(gettempdir()).resolve()
    if private_parent.is_relative_to(PROJECT_ROOT):
        private_parent = source_db.parent
    if private_parent.is_relative_to(PROJECT_ROOT):
        raise ValueError("Select a private system temp directory outside the checkout for the full database")
    with TemporaryDirectory(dir=private_parent, prefix="caa-scrub-") as private:
        clean_db = Path(private) / "manual.sqlite"
        result = _scrub_database(source_db, clean_db, expected, source_manifest)
        content = _manifest_text(source_manifest, result) if manifest else ""
        lock_dir = PROJECT_ROOT / "cache" / expected.lower() if expected else output_db.parent
        _publish(clean_db, output_db, manifest, content, lock_dir)
    return {**result, "output_db": str(output_db)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create an index-only database without embedded CAADoc prose."
    )
    parser.add_argument("--source-db", required=True, help="Full local manual.sqlite.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--release", type=release_id, metavar="Rnn", help="Install the public index and sibling manifest for this release.")
    target.add_argument("--output-db", help="Explicit public index database path.")
    parser.add_argument("--source-manifest", help="Optional full-build manifest.")
    parser.add_argument("--output-manifest", help="Optional public manifest path.")
    args = parser.parse_args()
    source_db = Path(args.source_db).resolve()
    output = public_database(PROJECT_ROOT, args.release) if args.release else Path(args.output_db)
    source_manifest = Path(args.source_manifest).resolve() if args.source_manifest else None
    manifest = Path(args.output_manifest).resolve() if args.output_manifest else None
    try:
        result = export_public_index(source_db, output, source_manifest, manifest)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
