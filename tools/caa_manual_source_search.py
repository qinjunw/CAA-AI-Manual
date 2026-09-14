"""Optional private FTS cache; official prose never enters the public index."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

try:
    from .caa_manual_query import _OfficialTextExtractor, _decode_source
except ImportError:
    from caa_manual_query import _OfficialTextExtractor, _decode_source


def cache_path(index):
    return index.manual_root / "cache" / "source-search.sqlite"


def build_source_cache(index):
    if not index.caadoc_root or not index.caadoc_root.is_dir():
        raise RuntimeError("Configure CAA_CAADOC_ROOT before building the private source cache")
    with index._connection() as db:
        uris = {row[0] for row in db.execute("SELECT source_uri FROM api_pages") if row[0].startswith("caadoc://")}
    for path in (index.caadoc_root / "Doc/online").rglob("*.htm*"):
        uris.add("caadoc://" + path.relative_to(index.caadoc_root).as_posix())
    for folder in index.caadoc_root.glob("*.edu"):
        for path in folder.rglob("*.cpp"):
            uris.add("caadoc://" + path.relative_to(index.caadoc_root).as_posix())
    target = cache_path(index)
    target.parent.mkdir(parents=True, exist_ok=True)
    count, skipped = 0, 0
    with closing(sqlite3.connect(target)) as db, db:
        # One transaction preserves an existing cache if rebuilding fails.
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS source_fts USING fts5(source_uri UNINDEXED, content, mtime UNINDEXED, size UNINDEXED)")
        db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT)")
        db.execute("DELETE FROM source_fts")
        for uri in sorted(uris):
            path = index.resolve_source_path(uri)
            if not path.is_file() or path.stat().st_size > 2_000_000:
                skipped += 1
                continue
            stat = path.stat()
            content, _ = _decode_source(path.read_bytes())
            if path.suffix.lower() in {".htm", ".html"}:
                parser = _OfficialTextExtractor()
                parser.feed(content)
                content = parser.text()
            db.execute("INSERT INTO source_fts VALUES (?,?,?,?)", (uri, content, str(stat.st_mtime_ns), stat.st_size))
            count += 1
        metadata = {"source_root": str(index.caadoc_root), "built_at": datetime.now(timezone.utc).isoformat(),
                    "document_count": count, "skipped_count": skipped, "content_mode": "private-fulltext"}
        db.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", [(k, json.dumps(v)) for k, v in metadata.items()])
    return {"status": "ok", **metadata, "cache_path": str(target)}


def search_sources(index, query: str, limit: int = 5, offset: int = 0):
    if not query.strip():
        raise ValueError("query must not be empty")
    target = cache_path(index)
    if not target.is_file():
        return {"status": "not_indexed", "next_action": "Run CLI index-sources to build a private local cache", "results": []}
    limit, offset = max(1, min(int(limit), 30)), max(0, int(offset))
    # User words are literals, not executable SQL or FTS operators. AND matching
    # supports phrases whose words are separated by HTML block boundaries.
    expression = '"' + query.strip().replace('"', '""') + '"'
    query_mode = "literal_phrase"
    with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as db:
        metadata = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM metadata")}
        if metadata.get("source_root") != str(index.caadoc_root):
            return {"status": "source_root_changed", "next_action": "Rebuild with CLI index-sources", "results": []}
        total = db.execute("SELECT count(*) FROM source_fts WHERE source_fts MATCH ?", (expression,)).fetchone()[0]
        if not total:
            expression = " AND ".join('"' + word.replace('"', '""') + '"' for word in query.split())
            query_mode = "all_words_fallback"
            total = db.execute("SELECT count(*) FROM source_fts WHERE source_fts MATCH ?", (expression,)).fetchone()[0]
        rows = db.execute("""SELECT source_uri, snippet(source_fts,1,'[',']','...',48), mtime, size
                             FROM source_fts WHERE source_fts MATCH ?
                             ORDER BY bm25(source_fts), source_uri LIMIT ? OFFSET ?""", (expression, limit, offset)).fetchall()
    results = []
    for uri, snippet, mtime, size in rows:
        path = index.resolve_source_path(uri)
        stat = path.stat() if path.is_file() else None
        stale = stat is None or (str(stat.st_mtime_ns), stat.st_size) != (mtime, int(size))
        results.append({"source_uri": uri, "snippet": snippet, "source_changed_since_index": stale})
    return {"status": "ok", "query": query, "built_at": metadata["built_at"],
            "query_mode": query_mode,
            "content_mode": "private-fulltext", "total_count": total, "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None, "results": results,
            "scope": "Indexed API pages, Doc/online HTML, and .edu C++ files at build time; phrase search then English token AND fallback"}
