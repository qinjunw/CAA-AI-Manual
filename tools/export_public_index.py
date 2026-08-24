"""Export an index-only CAA manual database for repository distribution."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


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


def export_public_index(source_db: Path, output_db: Path) -> dict[str, Any]:
    source_db = source_db.resolve()
    output_db = output_db.resolve()
    if source_db == output_db:
        raise ValueError("source and output databases must be different files")
    if not source_db.is_file():
        raise FileNotFoundError(f"source database not found: {source_db}")

    output_db.parent.mkdir(parents=True, exist_ok=True)
    temp_db = output_db.with_name(f".{output_db.name}.tmp")
    temp_db.unlink(missing_ok=True)

    source_uri = f"file:{source_db.as_posix()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True)
    target = sqlite3.connect(temp_db)
    try:
        source_mode = source.execute(
            "SELECT value FROM projection_metadata WHERE key='content_mode'"
        ).fetchone()
        if source_mode and source_mode[0] == "index-only":
            raise ValueError("source database must be a full local build")
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
    except Exception:
        target.close()
        source.close()
        temp_db.unlink(missing_ok=True)
        raise
    else:
        target.close()
        source.close()

    os.replace(temp_db, output_db)
    return {
        "status": "ok",
        "content_mode": "index-only",
        "embedded_official_text": False,
        "output_db": str(output_db),
        "output_bytes": output_db.stat().st_size,
        "fts_available": fts_available,
        "api_examples": example_count,
        "artifact_counts": counts,
    }


def write_public_manifest(
    source_manifest: Path | None,
    output_manifest: Path,
    export_result: dict[str, Any],
) -> None:
    manifest: dict[str, Any] = {}
    if source_manifest and source_manifest.is_file():
        manifest = json.loads(source_manifest.read_text(encoding="utf-8-sig"))
    manifest.update(
        {
            "source": "caadoc://",
            "output": "manual://",
            "content_mode": "index-only",
            "embedded_official_text": False,
            "public_export_schema_version": PUBLIC_EXPORT_SCHEMA_VERSION,
            "artifact_counts": export_result["artifact_counts"],
        }
    )
    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    output_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create an index-only database without embedded CAADoc prose."
    )
    parser.add_argument("--source-db", required=True, help="Full local manual.sqlite.")
    parser.add_argument("--output-db", required=True, help="Public index database path.")
    parser.add_argument("--source-manifest", help="Optional full-build manifest.")
    parser.add_argument("--output-manifest", help="Optional public manifest path.")
    args = parser.parse_args()
    result = export_public_index(Path(args.source_db), Path(args.output_db))
    if args.output_manifest:
        write_public_manifest(
            Path(args.source_manifest) if args.source_manifest else None,
            Path(args.output_manifest),
            result,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
