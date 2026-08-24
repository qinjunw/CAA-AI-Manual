"""Stable query API for the generated CAA AI manual projection."""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

try:
    from .caa_manual_projection import SOURCE_BASELINE, VERSION_POLICY
except ImportError:
    from caa_manual_projection import SOURCE_BASELINE, VERSION_POLICY


PROJECT_ROOT = Path(__file__).resolve().parents[1]
URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
API_NODE_TYPES = {"api", "function-family"}
INCLUDE_OPTIONS = {"members", "overloads", "evidence", "examples"}


def normalize_term(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").strip().casefold()


def compact(value: str | None, limit: int = 900) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def configured_value(
    cli_value: str | None,
    env_name: str,
    file_values: dict[str, str],
    default: str | None = None,
) -> str | None:
    for value in (cli_value, os.environ.get(env_name), file_values.get(env_name), default):
        if value:
            return value
    return None


def configured_index(
    db: str | None = None,
    manual_root: str | None = None,
    caadoc_root: str | None = None,
    env_file: str | None = None,
) -> "CaaManualIndex":
    initial_env_path = Path(env_file).resolve() if env_file else PROJECT_ROOT / ".env"
    env_values = load_env_file(initial_env_path)
    manual_text = configured_value(
        manual_root, "CAA_AI_MANUAL_ROOT", env_values, str(PROJECT_ROOT)
    )
    resolved_manual = Path(manual_text).resolve()
    if not env_file and initial_env_path.parent != resolved_manual:
        env_values = {**load_env_file(resolved_manual / ".env"), **env_values}
    db_text = configured_value(
        db,
        "CAA_AI_MANUAL_DB",
        env_values,
        str(resolved_manual / "data" / "manual.sqlite"),
    )
    caadoc_text = configured_value(
        caadoc_root, "CAA_CAADOC_ROOT", env_values
    )
    return CaaManualIndex(
        Path(db_text),
        resolved_manual,
        Path(caadoc_text).resolve() if caadoc_text else None,
    )


class _OfficialTextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
        "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5",
        "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
        "table", "tbody", "td", "th", "thead", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.skip_depth += 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def _decode_source(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "iso-8859-1", "cp1252", "gbk"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def _anchor_section(raw_html: str, anchor: str) -> tuple[str, bool]:
    if not anchor:
        return raw_html, True
    tag_re = re.compile(
        r"<a\b[^>]*(?:name|id)\s*=\s*(?:[\"'][^\"']*[\"']|[^\s>]+)[^>]*>",
        re.IGNORECASE,
    )
    value_re = re.compile(
        r"(?:name|id)\s*=\s*(?:[\"'](?P<quoted>[^\"']*)[\"']|(?P<bare>[^\s>]+))",
        re.IGNORECASE,
    )
    target = html.unescape(anchor)
    matches = list(tag_re.finditer(raw_html))
    start_index = -1
    for index, match in enumerate(matches):
        value_match = value_re.search(match.group(0))
        if value_match and html.unescape(value_match.group("quoted") or value_match.group("bare")) == target:
            start_index = index
            break
    if start_index < 0:
        return "", False
    start = matches[start_index].start()
    end = matches[start_index + 1].start() if start_index + 1 < len(matches) else len(raw_html)
    return raw_html[start:end], True


def _json_array(value: str | None) -> list[Any]:
    return json.loads(value or "[]")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class CaaManualIndex:
    """Read-only facade over the materialized Agent query projection."""

    def __init__(self, db_path: Path, manual_root: Path, caadoc_root: Path | None):
        self.db_path = db_path.resolve()
        self.manual_root = manual_root.resolve()
        self.caadoc_root = caadoc_root.resolve() if caadoc_root else None
        self.manifest_path = self.manual_root / "data" / "manifest.json"

    def connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise FileNotFoundError(f"SQLite database not found: {self.db_path}")
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def _metadata(self, connection: sqlite3.Connection) -> dict[str, Any]:
        try:
            rows = connection.execute("SELECT key, value FROM projection_metadata").fetchall()
            values = {row["key"]: row["value"] for row in rows}
        except sqlite3.OperationalError:
            values = {}
        return {
            "source_baseline": values.get("source_baseline", SOURCE_BASELINE),
            "version_policy": values.get("version_policy", VERSION_POLICY),
            "projection_schema_version": int(values.get("projection_schema_version", 0)),
            "content_mode": values.get("content_mode", "full"),
            "embedded_official_text": values.get("embedded_official_text", "true") == "true",
            "score_semantics": "query-relative lexical relevance; not factual confidence",
        }

    def _response(
        self,
        connection: sqlite3.Connection,
        status: str = "ok",
        **payload: Any,
    ) -> dict[str, Any]:
        return {"status": status, "metadata": self._metadata(connection), **payload}

    @staticmethod
    def _node(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "node_id": row["node_id"],
            "parent_id": row["parent_id"],
            "node_type": row["node_type"],
            "english_key": row["english_key"],
            "name_en": row["name_en"],
            "name_zh": row["name_zh"],
            "summary_zh": row["summary_zh"],
            "layer": row["layer"],
            "framework": row["framework"],
            "api_kind": row["api_kind"],
        }

    def _source_available(self, source_uri: str) -> bool:
        try:
            return self.resolve_source_path(source_uri).is_file()
        except (OSError, PermissionError, RuntimeError):
            return False

    def _page(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "page_id": row["page_id"],
            "api_node_id": row["api_node_id"],
            "page_role": row["page_role"],
            "page_template_kind": row["page_template_kind"],
            "signature": row["signature"],
            "summary_en": compact(row["summary_en"], 1200),
            "include_file": row["include_file"],
            "source_uri": row["source_uri"],
            "source_available": self._source_available(row["source_uri"]),
            "tags": _json_array(row["tags_json"]),
        }

    @staticmethod
    def _member(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "member_id": row["member_id"],
            "page_id": row["page_id"],
            "member_group_key": row["member_group_key"],
            "kind": row["kind"],
            "name": row["name"],
            "signature": row["signature"],
            "summary_en": compact(row["summary_en"], 900),
            "anchor": row["anchor"],
        }

    def status(self) -> dict[str, Any]:
        manifest = {}
        if self.manifest_path.is_file():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8-sig"))
        with self._connection() as connection:
            tables = (
                "entities", "relations", "chunks", "evidence", "catalog_nodes",
                "api_pages", "api_members", "api_aliases", "api_examples",
            )
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
            }
            fts = bool(connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='api_search_fts'"
            ).fetchone()[0])
            return self._response(
                connection,
                db_path=str(self.db_path),
                manual_root=str(self.manual_root),
                caadoc_root=str(self.caadoc_root) if self.caadoc_root else "",
                source_roots={
                    "caadoc://": str(self.caadoc_root) if self.caadoc_root else "",
                    "manual://": str(self.manual_root),
                },
                counts=counts,
                api_search_fts_available=fts,
                manifest=manifest,
            )

    def _catalog_resolution(
        self, connection: sqlite3.Connection, reference: str
    ) -> list[sqlite3.Row]:
        if not reference:
            reference = "catalog:root"
        rows = connection.execute(
            """
            SELECT * FROM catalog_nodes
            WHERE node_id = ? OR english_key = ? COLLATE NOCASE
               OR name_en = ? COLLATE NOCASE OR name_zh = ?
            ORDER BY node_type, layer, framework, name_en
            """,
            (reference, reference, reference, reference),
        ).fetchall()
        if rows:
            return rows
        normalized = normalize_term(reference)
        alias_rows = connection.execute(
            "SELECT target_id FROM api_aliases WHERE normalized_alias = ? AND target_type='catalog_node'",
            (normalized,),
        ).fetchall()
        if not alias_rows:
            return []
        placeholders = ",".join("?" for _ in alias_rows)
        return connection.execute(
            f"SELECT * FROM catalog_nodes WHERE node_id IN ({placeholders})",
            tuple(row["target_id"] for row in alias_rows),
        ).fetchall()

    def _catalog_path(
        self, connection: sqlite3.Connection, node: sqlite3.Row
    ) -> list[dict[str, Any]]:
        path = [self._node(node)]
        parent_id = node["parent_id"]
        while parent_id:
            parent = connection.execute(
                "SELECT * FROM catalog_nodes WHERE node_id = ?", (parent_id,)
            ).fetchone()
            if not parent:
                break
            path.append(self._node(parent))
            parent_id = parent["parent_id"]
        path.reverse()
        return path

    def catalog(self, parent: str = "catalog:root", depth: int = 1, limit: int = 200) -> dict[str, Any]:
        depth = max(1, min(int(depth), 3))
        limit = max(1, min(int(limit), 500))
        with self._connection() as connection:
            matches = self._catalog_resolution(connection, parent)
            if not matches:
                return self._response(connection, "not_found", reference=parent, candidates=[])
            if len(matches) > 1:
                return self._response(
                    connection,
                    "ambiguous",
                    reference=parent,
                    requires_selection=True,
                    candidates=[self._node(row) for row in matches],
                )
            root = matches[0]
            frontier = [(root["node_id"], 1)]
            children: list[dict[str, Any]] = []
            while frontier and len(children) < limit:
                parent_id, level = frontier.pop(0)
                rows = connection.execute(
                    """
                    SELECT * FROM catalog_nodes WHERE parent_id = ?
                    ORDER BY sort_order, name_en LIMIT ?
                    """,
                    (parent_id, limit - len(children)),
                ).fetchall()
                for row in rows:
                    item = self._node(row)
                    item["depth"] = level
                    children.append(item)
                    if level < depth:
                        frontier.append((row["node_id"], level + 1))
            return self._response(
                connection,
                reference=parent,
                path=self._catalog_path(connection, root),
                children=children,
                truncated=bool(frontier),
            )

    def _candidate_node(
        self, connection: sqlite3.Connection, api_node_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM catalog_nodes WHERE node_id = ? AND node_type IN ('api','function-family')",
            (api_node_id,),
        ).fetchone()

    def search(
        self,
        query: str,
        limit: int = 8,
        layer: str = "",
        framework: str = "",
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(int(limit), 30))
        normalized = normalize_term(query)
        with self._connection() as connection:
            candidate_map: dict[str, dict[str, Any]] = {}
            catalog_matches: list[dict[str, Any]] = []

            def allowed(node: sqlite3.Row) -> bool:
                return (not layer or node["layer"] == layer) and (
                    not framework or node["framework"] == framework
                )

            def add(
                api_node_id: str,
                score: float,
                tier: str,
                reason: str,
                matched_key_en: str,
                matched_field: str,
                matched_member: dict[str, Any] | None = None,
            ) -> None:
                node = self._candidate_node(connection, api_node_id)
                if not node or not allowed(node):
                    return
                detail = {
                    "match_score": round(score, 3),
                    "match_tier": tier,
                    "match_reason": reason,
                    "matched_key_en": matched_key_en,
                    "matched_field": matched_field,
                }
                if matched_member:
                    detail["matched_member"] = matched_member
                current = candidate_map.get(api_node_id)
                if current is None:
                    page_rows = connection.execute(
                        "SELECT source_uri FROM api_pages WHERE api_node_id=?",
                        (api_node_id,),
                    ).fetchall()
                    current = {
                        **self._node(node),
                        "page_count": len(page_rows),
                        "all_sources_available": bool(page_rows) and all(
                            self._source_available(page["source_uri"])
                            for page in page_rows
                        ),
                        "match_score": round(score, 3),
                        "match_tier": tier,
                        "match_reason": reason,
                        "matched_key_en": matched_key_en,
                        "match_details": [],
                    }
                    candidate_map[api_node_id] = current
                current["match_details"].append(detail)
                if score > current["match_score"]:
                    current.update({
                        "match_score": round(score, 3),
                        "match_tier": tier,
                        "match_reason": reason,
                        "matched_key_en": matched_key_en,
                    })

            api_rows = connection.execute(
                "SELECT * FROM catalog_nodes WHERE node_type IN ('api','function-family')"
            ).fetchall()
            for node in api_rows:
                key = normalize_term(node["name_en"])
                if key == normalized:
                    add(node["node_id"], 1.0, "exact-official", "官方英文键完全匹配", node["name_en"], "name_en")
                elif key.startswith(normalized):
                    add(node["node_id"], 0.86, "prefix-official", "官方英文键前缀匹配", node["name_en"], "name_en")
                elif normalized in key:
                    add(node["node_id"], 0.76, "substring-official", "官方英文键包含查询词", node["name_en"], "name_en")

            alias_rows = connection.execute(
                "SELECT * FROM api_aliases WHERE normalized_alias = ? OR normalized_alias LIKE ?",
                (normalized, f"{normalized}%"),
            ).fetchall()
            for alias in alias_rows:
                exact = alias["normalized_alias"] == normalized
                if alias["target_type"] == "api_node":
                    add(
                        alias["target_id"],
                        0.97 if exact else 0.84,
                        "exact-zh-alias" if exact else "prefix-zh-alias",
                        "中文查询键完全匹配" if exact else "中文查询键前缀匹配",
                        alias["english_key"],
                        "alias_zh",
                    )
                    continue
                target = connection.execute(
                    "SELECT * FROM catalog_nodes WHERE node_id = ?", (alias["target_id"],)
                ).fetchone()
                if not target:
                    continue
                catalog_matches.append({
                    **self._node(target),
                    "matched_alias_zh": alias["alias"],
                    "matched_key_en": alias["english_key"],
                    "match_score": 0.97 if exact else 0.84,
                })
                if exact and target["node_type"] == "capability":
                    page_rows = connection.execute(
                        "SELECT DISTINCT api_node_id FROM api_pages WHERE tags_json LIKE ? LIMIT ?",
                        (f'%"{target["english_key"]}"%', limit * 4),
                    ).fetchall()
                    for page_row in page_rows:
                        add(
                            page_row["api_node_id"], 0.82, "capability-alias",
                            "中文能力键映射到维护者检索标签", target["english_key"], "capability",
                        )

            member_rows = connection.execute(
                """
                SELECT m.*, p.api_node_id FROM api_members m
                JOIN api_pages p ON p.page_id=m.page_id
                WHERE m.name = ? COLLATE NOCASE OR m.name LIKE ? COLLATE NOCASE
                LIMIT ?
                """,
                (query, f"{query}%", limit * 8),
            ).fetchall()
            for member in member_rows:
                exact = normalize_term(member["name"]) == normalized
                add(
                    member["api_node_id"], 0.92 if exact else 0.8,
                    "exact-member" if exact else "prefix-member",
                    "成员英文键完全匹配" if exact else "成员英文键前缀匹配",
                    member["name"], "member_name", self._member(member),
                )

            fts_rows = []
            if not any(item["match_score"] >= 0.92 for item in candidate_map.values()):
                try:
                    fts_query = '"' + query.replace('"', '""') + '"'
                    fts_rows = connection.execute(
                        """
                        SELECT *, bm25(api_search_fts) rank FROM api_search_fts
                        WHERE api_search_fts MATCH ? ORDER BY rank LIMIT ?
                        """,
                        (fts_query, limit * 5),
                    ).fetchall()
                except sqlite3.OperationalError:
                    fts_rows = []
            for position, row in enumerate(fts_rows):
                member = None
                if row["member_id"]:
                    member_row = connection.execute(
                        "SELECT * FROM api_members WHERE member_id=?", (row["member_id"],)
                    ).fetchone()
                    member = self._member(member_row) if member_row else None
                add(
                    row["api_node_id"], max(0.45, 0.68 - position * 0.01),
                    "fts-lexical", "FTS5 索引字段词法匹配", row["name"],
                    "member_index" if member else "api_index", member,
                )

            candidates = sorted(
                candidate_map.values(),
                key=lambda item: (-item["match_score"], item["name_en"], item["framework"]),
            )[:limit]
            top_tied = len(candidates) > 1 and candidates[0]["match_score"] == candidates[1]["match_score"]
            return self._response(
                connection,
                query=query,
                normalized_query=normalized,
                filters={"layer": layer, "framework": framework},
                catalog_matches=catalog_matches,
                candidate_groups=candidates,
                requires_selection=top_tied,
            )

    def _resolve_api(
        self, connection: sqlite3.Connection, reference: str
    ) -> tuple[list[sqlite3.Row], str, str]:
        selected_page_id = ""
        selected_member_id = ""
        node = self._candidate_node(connection, reference)
        if node:
            return [node], selected_page_id, selected_member_id
        page = connection.execute(
            "SELECT * FROM api_pages WHERE page_id=? OR source_uri=?", (reference, reference)
        ).fetchone()
        if page:
            node = self._candidate_node(connection, page["api_node_id"])
            return ([node] if node else []), page["page_id"], selected_member_id
        member = connection.execute(
            "SELECT * FROM api_members WHERE member_id=?", (reference,)
        ).fetchone()
        if member:
            page = connection.execute(
                "SELECT * FROM api_pages WHERE page_id=?", (member["page_id"],)
            ).fetchone()
            node = self._candidate_node(connection, page["api_node_id"])
            return ([node] if node else []), page["page_id"], member["member_id"]
        alias_rows = connection.execute(
            "SELECT target_id FROM api_aliases WHERE normalized_alias=? AND target_type='api_node'",
            (normalize_term(reference),),
        ).fetchall()
        if alias_rows:
            nodes = [self._candidate_node(connection, row["target_id"]) for row in alias_rows]
            return [node for node in nodes if node], selected_page_id, selected_member_id
        rows = connection.execute(
            """
            SELECT * FROM catalog_nodes
            WHERE node_type IN ('api','function-family')
              AND (name_en=? COLLATE NOCASE OR name_zh=?)
            ORDER BY layer, framework, name_en
            """,
            (reference, reference),
        ).fetchall()
        return rows, selected_page_id, selected_member_id

    def _pages_for_node(
        self, connection: sqlite3.Connection, node_id: str
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT * FROM api_pages WHERE api_node_id=?
            ORDER BY CASE page_role WHEN 'aggregate' THEN 0 ELSE 1 END, source_uri
            """,
            (node_id,),
        ).fetchall()

    def _resolve_result(
        self, connection: sqlite3.Connection, reference: str
    ) -> tuple[dict[str, Any] | None, sqlite3.Row | None, str, str]:
        nodes, page_id, member_id = self._resolve_api(connection, reference)
        if not nodes:
            return self._response(connection, "not_found", reference=reference, candidates=[]), None, "", ""
        if len(nodes) > 1:
            return self._response(
                connection,
                "ambiguous",
                reference=reference,
                requires_selection=True,
                candidates=[self._node(node) for node in nodes],
            ), None, "", ""
        return None, nodes[0], page_id, member_id

    def get_api(self, reference: str, include: Iterable[str] | None = None) -> dict[str, Any]:
        requested = list(dict.fromkeys(include or []))
        unknown = sorted(set(requested) - INCLUDE_OPTIONS)
        if unknown:
            raise ValueError(f"Unsupported include values: {', '.join(unknown)}")
        with self._connection() as connection:
            early, node, selected_page_id, selected_member_id = self._resolve_result(connection, reference)
            if early:
                return early
            pages = self._pages_for_node(connection, node["node_id"])
            selected_page = next((page for page in pages if page["page_id"] == selected_page_id), None)
            aggregate = next((page for page in pages if page["page_role"] == "aggregate"), None)
            details = [page for page in pages if page["page_role"] == "detail"]
            preferred = selected_page or aggregate or (details[0] if len(details) == 1 else None)
            requires_overload_selection = (
                node["node_type"] == "function-family"
                and preferred is None
                and len(details) > 1
            )
            payload: dict[str, Any] = {
                "reference": reference,
                "api": self._node(node),
                "selected_page_id": selected_page_id,
                "selected_member_id": selected_member_id,
                "page_count": len(pages),
                "preferred_source": self._page(preferred) if preferred else None,
                "requires_overload_selection": requires_overload_selection,
                "available_includes": sorted(INCLUDE_OPTIONS),
                "included": requested,
            }
            if "overloads" in requested or requires_overload_selection:
                payload["overloads"] = [self._page(page) for page in pages]

            member_rows: list[sqlite3.Row] = []
            if "members" in requested or "evidence" in requested:
                page_ids = [page["page_id"] for page in pages]
                placeholders = ",".join("?" for _ in page_ids)
                member_rows = connection.execute(
                    f"""
                    SELECT * FROM api_members WHERE page_id IN ({placeholders})
                    ORDER BY kind, name, signature, anchor
                    """,
                    tuple(page_ids),
                ).fetchall()
            if "members" in requested:
                grouped: dict[str, dict[str, Any]] = {}
                for member in member_rows:
                    group = grouped.setdefault(member["member_group_key"], {
                        "member_group_key": member["member_group_key"],
                        "kind": member["kind"],
                        "name": member["name"],
                        "overloads": [],
                    })
                    group["overloads"].append(self._member(member))
                payload["members"] = list(grouped.values())

            if "evidence" in requested:
                evidence_ids: set[str] = set()
                for page in pages:
                    evidence_ids.update(_json_array(page["evidence_ids_json"]))
                for member in member_rows:
                    evidence_ids.update(_json_array(member["evidence_ids_json"]))
                if evidence_ids:
                    placeholders = ",".join("?" for _ in evidence_ids)
                    evidence_rows = connection.execute(
                        f"SELECT * FROM evidence WHERE id IN ({placeholders}) ORDER BY source_path, anchor",
                        tuple(sorted(evidence_ids)),
                    ).fetchall()
                else:
                    evidence_rows = []
                payload["evidence"] = [{
                    "evidence_id": row["id"],
                    "source_uri": row["source_path"],
                    "anchor": row["anchor"],
                    "official_span": row["quote_or_span"],
                    "content_embedded": bool(row["quote_or_span"]),
                    "extraction_method": row["extraction_method"],
                    "trust_level": row["trust_level"],
                } for row in evidence_rows]

            if "examples" in requested:
                has_public_examples = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_examples'"
                ).fetchone()
                if has_public_examples:
                    example_rows = connection.execute(
                        """
                        SELECT chunk_id AS id, source_uri AS source_path,
                               '[]' AS heading_path_json, '' AS text, symbols_json
                        FROM api_examples WHERE api_node_id=?
                        ORDER BY source_uri LIMIT 20
                        """,
                        (node["node_id"],),
                    ).fetchall()
                else:
                    raw_entity_ids: set[str] = set()
                    for page in pages:
                        raw_entity_ids.update(_json_array(page["raw_entity_ids_json"]))
                    if raw_entity_ids:
                        placeholders = ",".join("?" for _ in raw_entity_ids)
                        example_rows = connection.execute(
                            f"""
                            SELECT DISTINCT c.* FROM relations r
                            JOIN chunks c ON c.id=r.src_id
                            WHERE r.type='DEMONSTRATES' AND r.dst_id IN ({placeholders})
                            ORDER BY c.source_path LIMIT 20
                            """,
                            tuple(sorted(raw_entity_ids)),
                        ).fetchall()
                    else:
                        example_rows = []
                payload["examples"] = [{
                    "chunk_id": row["id"],
                    "source_uri": row["source_path"],
                    "heading_path": _json_array(row["heading_path_json"]),
                    "official_text": compact(row["text"], 1800),
                    "content_embedded": bool(row["text"]),
                    "symbols": _json_array(row["symbols_json"]),
                } for row in example_rows]

            status = "requires_overload_selection" if requires_overload_selection else "ok"
            return self._response(connection, status, **payload)

    def _safe_join(self, root: Path, relative_path: str) -> Path:
        parts: list[str] = []
        for part in relative_path.replace("\\", "/").split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                raise PermissionError(f"Path traversal is not allowed: {relative_path}")
            parts.append(part)
        target = root.joinpath(*parts).resolve()
        if not _is_relative_to(target, root):
            raise PermissionError(f"Path is outside allowed root: {target}")
        return target

    def resolve_source_path(self, source_uri: str) -> Path:
        if source_uri.startswith("caadoc://"):
            if not self.caadoc_root:
                raise RuntimeError("CAA_CAADOC_ROOT is not configured; cannot read caadoc:// sources")
            return self._safe_join(self.caadoc_root, source_uri[len("caadoc://"):])
        if source_uri.startswith("manual://"):
            return self._safe_join(self.manual_root, source_uri[len("manual://"):])
        if URI_RE.match(source_uri):
            raise PermissionError(f"Unsupported source URI scheme: {source_uri}")
        target = Path(source_uri).expanduser().resolve()
        roots = [self.manual_root, *([self.caadoc_root] if self.caadoc_root else [])]
        if not any(_is_relative_to(target, root) for root in roots):
            raise PermissionError(f"Path is outside allowed roots: {target}")
        return target

    def read_source(
        self,
        reference: str,
        anchor: str = "",
        format: str = "text",
        max_chars: int = 12000,
    ) -> dict[str, Any]:
        if format not in {"text", "raw_html"}:
            raise ValueError("format must be 'text' or 'raw_html'")
        max_chars = max(100, min(int(max_chars), 40000))
        with self._connection() as connection:
            source_uri = reference if URI_RE.match(reference) else ""
            selected_anchor = anchor
            page: sqlite3.Row | None = None
            if not source_uri:
                early, node, page_id, member_id = self._resolve_result(connection, reference)
                if early:
                    return early
                pages = self._pages_for_node(connection, node["node_id"])
                if page_id:
                    page = next((item for item in pages if item["page_id"] == page_id), None)
                if member_id:
                    member = connection.execute(
                        "SELECT * FROM api_members WHERE member_id=?", (member_id,)
                    ).fetchone()
                    if member and not selected_anchor:
                        selected_anchor = member["anchor"]
                aggregate = next((item for item in pages if item["page_role"] == "aggregate"), None)
                details = [item for item in pages if item["page_role"] == "detail"]
                page = page or aggregate or (details[0] if len(details) == 1 else None)
                if page is None:
                    return self._response(
                        connection,
                        "requires_overload_selection",
                        reference=reference,
                        requires_selection=True,
                        overloads=[self._page(item) for item in pages],
                    )
                source_uri = page["source_uri"]
            target = self.resolve_source_path(source_uri)
            if not target.is_file():
                raise FileNotFoundError(f"Source file not found: {target}")
            raw_html, encoding = _decode_source(target.read_bytes())
            selected_html, anchor_found = _anchor_section(raw_html, selected_anchor)
            if selected_anchor and not anchor_found:
                return self._response(
                    connection,
                    "anchor_not_found",
                    reference=reference,
                    source_uri=source_uri,
                    anchor=selected_anchor,
                    resolved_path=str(target),
                    format=format,
                    encoding=encoding,
                    truncated=False,
                    content="",
                )
            if format == "raw_html":
                content = selected_html
            else:
                parser = _OfficialTextExtractor()
                parser.feed(selected_html)
                content = parser.text()
            truncated = len(content) > max_chars
            if truncated:
                content = content[:max_chars]
            return self._response(
                connection,
                reference=reference,
                source_uri=source_uri,
                anchor=selected_anchor,
                anchor_found=anchor_found,
                resolved_path=str(target),
                format=format,
                encoding=encoding,
                truncated=truncated,
                content=content,
            )
