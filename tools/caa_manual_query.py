"""Stable query API for the generated CAA AI manual projection."""

from __future__ import annotations

import ast
import difflib
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
from urllib.parse import unquote

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


def _anchor_identity(value: str) -> str:
    """Normalize locator representation, not C++ type equivalence."""
    text = re.sub(r"\s+", " ", html.unescape(unicodedata.normalize("NFKC", value))).strip()
    # Keep word boundaries: 'unsigned int' is not the identifier 'unsignedint'.
    return re.sub(r"\s*([()<>:,*&\[\]])\s*", r"\1", text)


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
        self.in_script = False
        self.script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.skip_depth += 1
            if tag == "script":
                self.in_script = True
                self.script_parts = []
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.skip_depth:
            if tag == "script":
                # CAADoc writes signature types as literal activateLink calls.
                # Parse only the two string arguments; never execute JavaScript.
                literal = r'''(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")'''
                for match in re.finditer(r"\bactivateLink\(\s*(" + literal + r")\s*,\s*(" + literal + r")\s*\)", "".join(self.script_parts)):
                    try:
                        self.parts.append(html.unescape(ast.literal_eval(match.group(2))))
                    except (SyntaxError, ValueError):
                        continue
                self.in_script = False
                self.script_parts = []
            self.skip_depth -= 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_script:
            self.script_parts.append(data)
        elif not self.skip_depth:
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
    end = len(raw_html)
    # A short member anchor is often immediately followed by its signature anchor.
    for next_match in matches[start_index + 1:]:
        between = raw_html[start:next_match.start()]
        if re.sub(r"<[^>]*>", "", between).strip():
            end = next_match.start()
            break
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
        self._inheritance_cache: tuple[Any, dict[str, str]] = (None, {})

    def _owner_chain(self, connection: sqlite3.Connection, node: sqlite3.Row
                     ) -> tuple[list[sqlite3.Row], str]:
        """Read CAADoc's literal parent map without evaluating its JavaScript."""
        if not self.caadoc_root:
            return [node], "source_unavailable"
        path = self.caadoc_root / "Doc/generated/refman/_index/jsTree.js"
        if not path.is_file():
            return [node], "source_unavailable"
        stamp = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        if self._inheritance_cache[0] != stamp:
            source, _ = _decode_source(path.read_bytes())
            parents = dict(re.findall(r'fatherLink\["([^"\r\n]+)"\]\s*=\s*"([^"\r\n]+)"', source))
            self._inheritance_cache = (stamp, parents)
        parents = self._inheritance_cache[1]
        chain = [node]
        seen = {node["node_id"]}
        while True:
            pages = self._pages_for_node(connection, chain[-1]["node_id"])
            parent_stems = {parents[Path(page["source_uri"]).stem] for page in pages
                            if page["source_uri"].startswith("caadoc://Doc/generated/refman/")
                            and Path(page["source_uri"]).stem in parents}
            if not parent_stems:
                return chain, "resolved" if len(chain) > 1 else "no_parent_record"
            if len(parent_stems) != 1:
                return chain, "ambiguous_parent"
            stem = parent_stems.pop()
            suffix = f"/{stem}.htm"
            rows = connection.execute(
                "SELECT DISTINCT api_node_id FROM api_pages WHERE substr(source_uri, -?)=?",
                (len(suffix), suffix),
            ).fetchall()
            rows = [self._candidate_node(connection, row["api_node_id"]) for row in rows]
            rows = [row for row in rows if row and row["layer"] == node["layer"]]
            if len(rows) != 1:
                return chain, "parent_not_indexed" if not rows else "ambiguous_parent"
            parent = rows[0]
            if parent["node_id"] in seen:
                return chain, "cycle_detected"
            chain.append(parent)
            seen.add(parent["node_id"])

    def _qualified_members(self, connection: sqlite3.Connection, reference: str,
                           layer: str = "", framework: str = "") -> tuple[list[dict[str, Any]], list[str]]:
        # Split before the member; parameter types can themselves contain ::.
        owner, member_reference = unicodedata.normalize("NFKC", reference).split("::", 1)
        owner = owner.strip()
        name = member_reference.split("(", 1)[0].strip()
        selector = _anchor_identity(member_reference) if "(" in member_reference else ""
        nodes, _, _ = self._resolve_api(connection, owner)
        matches = []
        states = []
        for node in nodes:
            if (layer and node["layer"] != layer) or (framework and node["framework"] != framework):
                continue
            chain, state = self._owner_chain(connection, node)
            states.append(state)
            for declaration in chain:
                declared_members = connection.execute(
                    """SELECT m.*, p.api_node_id FROM api_members m
                       JOIN api_pages p ON p.page_id=m.page_id
                       WHERE p.api_node_id=? AND m.name=? COLLATE NOCASE
                       ORDER BY m.signature, m.member_id""",
                    (declaration["node_id"], name),
                ).fetchall()
                members = [row for row in declared_members if not selector or _anchor_identity(row["anchor"]) == selector]
                for row in members:
                    matches.append({**dict(row), "requested_owner": node["name_en"],
                                    "declared_in": declaration["name_en"],
                                    "inheritance_chain": [item["name_en"] for item in chain],
                                    "inheritance_state": state})
                # C++ name hiding: do not silently merge a base overload set.
                if declared_members:
                    break
        return matches, states

    def _qualified_context(self, connection: sqlite3.Connection, reference: str) -> dict[str, Any]:
        reference = unicodedata.normalize("NFKC", reference)
        if "::" not in reference or URI_RE.match(reference):
            return {}
        matches, _ = self._qualified_members(connection, reference)
        if len(matches) != 1:
            return {}
        match = matches[0]
        return {"qualified_resolution": {
            "requested_owner": match["requested_owner"], "declared_in": match["declared_in"],
            "inheritance_chain": match["inheritance_chain"], "inheritance_state": match["inheritance_state"],
            "inheritance_source_uri": "caadoc://Doc/generated/refman/_index/jsTree.js",
            "member_scope": "Inherited members are callable through the derived owner; declared_in identifies the declaration, not exclusive ownership.",
        }}

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

    def search_source(self, query: str, limit: int = 5, offset: int = 0) -> dict[str, Any]:
        try:
            from .caa_manual_source_search import search_sources
        except ImportError:
            from caa_manual_source_search import search_sources
        return search_sources(self, query, limit, offset)

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

    def catalog(self, parent: str = "catalog:root", depth: int = 1, limit: int = 200, offset: int = 0) -> dict[str, Any]:
        depth = max(1, min(int(depth), 3))
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
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
            while frontier:
                parent_id, level = frontier.pop(0)
                rows = connection.execute(
                    """
                    SELECT * FROM catalog_nodes WHERE parent_id = ?
                    ORDER BY sort_order, name_en, node_id
                    """,
                    (parent_id,),
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
                children=children[offset:offset + limit],
                total_count=len(children), offset=offset,
                next_offset=offset + limit if offset + limit < len(children) else None,
                truncated=offset + limit < len(children),
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
        query = unicodedata.normalize("NFKC", query).strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(int(limit), 30))
        normalized = normalize_term(query)
        with self._connection() as connection:
            # Models commonly write "Class Member" instead of "Class::Member".
            # Interpret only two identifiers whose owner is present in the index.
            spaced = re.fullmatch(r"([A-Za-z_][A-Za-z_0-9]*)\s+([A-Za-z_][A-Za-z_0-9]*)", query)
            if spaced and self._resolve_api(connection, spaced[1])[0]:
                qualified = f"{spaced[1]}::{spaced[2]}"
                result = self.search(qualified, limit, layer, framework)
                result["query"] = query
                result["query_interpretation"] = {"kind": "qualified_member", "reference": qualified,
                                                   "basis": "Two identifiers with an indexed owner; member existence is checked separately."}
                return result
            candidate_map: dict[str, dict[str, Any]] = {}
            catalog_matches: list[dict[str, Any]] = []
            expansions: list[dict[str, Any]] = []
            inheritance_states: list[str] = []

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
                apply_filters: bool = True,
            ) -> None:
                node = self._candidate_node(connection, api_node_id)
                if not node or (apply_filters and not allowed(node)):
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
                    current = {
                        **self._node(node),
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

            literal_query = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            api_rows = connection.execute(
                """SELECT * FROM catalog_nodes WHERE node_type IN ('api','function-family')
                   AND name_en LIKE ? ESCAPE '\\'
                   AND (?='' OR layer=?) AND (?='' OR framework=?)""",
                (f"%{literal_query}%", layer, layer, framework, framework),
            ).fetchall()
            for node in api_rows:
                key = normalize_term(node["name_en"])
                if key == normalized:
                    add(node["node_id"], 1.0, "exact-official", "官方英文键完全匹配", node["name_en"], "name_en")
                elif key.startswith(normalized):
                    add(node["node_id"], 0.86, "prefix-official", "官方英文键前缀匹配", node["name_en"], "name_en")
                elif normalized in key:
                    add(node["node_id"], 0.76, "substring-official", "官方英文键包含查询词", node["name_en"], "name_en")

            if "::" in query:
                matches, inheritance_states = self._qualified_members(connection, query, layer, framework)
                for match in matches:
                    member = {**self._member(match), "requested_owner": match["requested_owner"],
                              "declared_in": match["declared_in"], "inheritance_chain": match["inheritance_chain"],
                              "inheritance_state": match["inheritance_state"],
                              "inheritance_source_uri": "caadoc://Doc/generated/refman/_index/jsTree.js"}
                    # Filters apply to the requested owner, not its base framework.
                    add(match["api_node_id"], 1.0, "qualified-member",
                        "限定成员名；声明位置来自索引和本机官方继承目录", query, "member_name", member,
                        apply_filters=False)

            config_path = self.manual_root / "config/catalog_zh.yaml"
            if config_path.is_file() and "::" not in query and not any(
                    normalize_term(node["name_en"]) == normalized for node in api_rows):
                config = json.loads(config_path.read_text(encoding="utf-8-sig"))
                for rule in config.get("query_expansions", []):
                    terms = [term for term in rule["terms"] if normalize_term(term) in normalized]
                    if not terms:
                        continue
                    expansions.append({"terms": terms, "targets": rule["targets"], "basis": rule["basis"]})
                    for rank, target in enumerate(rule["targets"]):
                        nodes, _, _ = self._resolve_api(connection, target)
                        for node in nodes:
                            add(node["node_id"], 0.90 - min(rank, 10) * 0.005, "curated-expansion",
                                "维护者任务词展开；候选用途仍须读取官方原文核实", target, "query_expansion")

            alias_rows = connection.execute(
                "SELECT * FROM api_aliases WHERE normalized_alias = ? OR normalized_alias LIKE ? ESCAPE '\\'",
                (normalized, f"{literal_query}%"),
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
                        "SELECT DISTINCT api_node_id FROM api_pages WHERE tags_json LIKE ?",
                        (f'%"{target["english_key"]}"%',),
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
                JOIN catalog_nodes n ON n.node_id=p.api_node_id
                WHERE (m.name = ? COLLATE NOCASE OR m.name LIKE ? ESCAPE '\\')
                  AND (?='' OR n.layer=?) AND (?='' OR n.framework=?)
                ORDER BY m.name, p.api_node_id, m.member_id
                """,
                (query, f"{literal_query}%", layer, layer, framework, framework),
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
                        SELECT api_search_fts.*, bm25(api_search_fts) rank FROM api_search_fts
                        JOIN catalog_nodes n ON n.node_id=api_search_fts.api_node_id
                        WHERE api_search_fts MATCH ? AND (?='' OR n.layer=?) AND (?='' OR n.framework=?)
                        ORDER BY rank LIMIT ?
                        """,
                        (fts_query, layer, layer, framework, framework, limit * 5),
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
            for candidate in candidates:
                page_rows = self._pages_for_node(connection, candidate["node_id"])
                candidate["page_count"] = len(page_rows)
                candidate["all_sources_available"] = bool(page_rows) and all(
                    self._source_available(page["source_uri"]) for page in page_rows)
            top_tied = len(candidates) > 1 and candidates[0]["match_score"] == candidates[1]["match_score"]
            return self._response(
                connection,
                query=query,
                normalized_query=normalized,
                query_expansions=expansions,
                inheritance_states=inheritance_states,
                filters={"layer": layer, "framework": framework},
                catalog_matches=catalog_matches,
                candidate_groups=candidates,
                requires_selection=top_tied,
                next_action=("Use Class::Member for a known member, or caa_search_source for English prose. An empty symbol index result does not establish absence in CAADoc."
                             if not candidates else "Read candidate source before concluding API behavior."),
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
        if not URI_RE.match(reference):
            reference = unicodedata.normalize("NFKC", reference)
        if "::" in reference and not URI_RE.match(reference):
            matches, states = self._qualified_members(connection, reference)
            if len(matches) != 1:
                return self._response(
                    connection, "ambiguous" if matches else "not_found", reference=reference,
                    requires_selection=bool(matches), inheritance_states=states,
                    candidates=[{**self._member(row), "declared_in": row["declared_in"],
                                 "requested_owner": row["requested_owner"]} for row in matches],
                    next_action="Select a returned member_id. If a parameter-list reference has no exact anchor match, use Class::Member to list overloads. Locator matching does not perform C++ conversions or prove API absence.",
                ), None, "", ""
            match = matches[0]
            return None, self._candidate_node(connection, match["api_node_id"]), match["page_id"], match["member_id"]
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

    def get_api(self, reference: str, include: Iterable[str] | None = None,
                member: str = "", member_offset: int = 0, member_limit: int = 100,
                example_offset: int = 0, example_limit: int = 20,
                inherited: bool = False) -> dict[str, Any]:
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
                **self._qualified_context(connection, reference),
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
                member_pages = pages
                if inherited:
                    chain, state = self._owner_chain(connection, node)
                    payload["inheritance"] = {"state": state, "chain": [self._node(item) for item in chain],
                                              "source_uri": "caadoc://Doc/generated/refman/_index/jsTree.js"}
                    member_pages = [page for owner in chain for page in self._pages_for_node(connection, owner["node_id"])]
                page_owners = {page["page_id"]: page["api_node_id"] for page in member_pages}
                page_ids = list(page_owners)
                placeholders = ",".join("?" for _ in page_ids)
                member_rows = connection.execute(
                    f"""
                    SELECT * FROM api_members WHERE page_id IN ({placeholders})
                    ORDER BY kind, name, signature, anchor
                    """,
                    tuple(page_ids),
                ).fetchall()
                if selected_member_id:
                    member_rows = [row for row in member_rows if row["member_id"] == selected_member_id]
                elif member:
                    known_names = sorted({row["name"] for row in member_rows})
                    member_rows = [row for row in member_rows if normalize_term(row["name"]) == normalize_term(member)]
                    if not member_rows:
                        normalized_names = {normalize_term(name): name for name in known_names}
                        close_names = difflib.get_close_matches(normalize_term(member), normalized_names, n=5, cutoff=0.6)
                        payload["member_name_suggestions"] = {
                            "basis": "Lexical similarity among indexed declarations, not aliases or equivalent APIs. Read each signature before selection.",
                            "candidates": [normalized_names[name] for name in close_names],
                            "next_action": "Retry get_api with include=['members'] and one candidate as member; verify the overload in read_source.",
                        }
            if "members" in requested:
                payload["member_scope"] = "documented_declarations_in_chain" if inherited else "declared_only; use inherited=true to include documented base declarations"
                grouped: dict[str, dict[str, Any]] = {}
                for row in member_rows:
                    owner_id = page_owners[row["page_id"]]
                    group = grouped.setdefault(owner_id + ":" + row["member_group_key"], {
                        "member_group_key": row["member_group_key"],
                        "kind": row["kind"],
                        "name": row["name"],
                        "declared_in": owner_id,
                        "overloads": [],
                    })
                    group["overloads"].append(self._member(row))
                all_members = list(grouped.values())
                member_offset = max(0, int(member_offset))
                member_limit = max(1, min(int(member_limit), 500))
                payload["members"] = all_members[member_offset:member_offset + member_limit]
                payload["members_pagination"] = {
                    "total_count": len(all_members), "offset": member_offset,
                    "next_offset": member_offset + member_limit if member_offset + member_limit < len(all_members) else None,
                    "has_more": member_offset + member_limit < len(all_members),
                }

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
                payload["examples_scope"] = "Indexed source references only. Zero matches do not establish absence of official examples or local runtime cases."
                has_public_examples = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_examples'"
                ).fetchone()
                if has_public_examples:
                    example_rows = connection.execute(
                        """
                        SELECT chunk_id AS id, source_uri AS source_path,
                               '[]' AS heading_path_json, '' AS text, symbols_json
                        FROM api_examples WHERE api_node_id=?
                        ORDER BY source_uri, chunk_id
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
                            ORDER BY c.source_path, c.id
                            """,
                            tuple(sorted(raw_entity_ids)),
                        ).fetchall()
                    else:
                        example_rows = []
                example_rows = list({row["source_path"]: row for row in example_rows}.values())
                example_offset = max(0, int(example_offset))
                example_limit = max(1, min(int(example_limit), 100))
                payload["examples_pagination"] = {
                    "total_count": len(example_rows), "offset": example_offset,
                    "next_offset": example_offset + example_limit if example_offset + example_limit < len(example_rows) else None,
                    "has_more": example_offset + example_limit < len(example_rows),
                }
                payload["examples"] = [{
                    "chunk_id": row["id"],
                    "source_uri": row["source_path"],
                    "heading_path": _json_array(row["heading_path_json"]),
                    "official_text": compact(row["text"], 1800),
                    "content_embedded": bool(row["text"]),
                    "symbols": _json_array(row["symbols_json"])[:20],
                    "symbols_total_count": len(_json_array(row["symbols_json"])),
                    "symbols_truncated": len(_json_array(row["symbols_json"])) > 20,
                } for row in example_rows[example_offset:example_offset + example_limit]]

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
        offset: int = 0,
        include_context: bool = False,
    ) -> dict[str, Any]:
        if format not in {"text", "raw_html"}:
            raise ValueError("format must be 'text' or 'raw_html'")
        max_chars = max(100, min(int(max_chars), 40000))
        offset = max(0, int(offset))
        with self._connection() as connection:
            source_uri = reference if URI_RE.match(reference) else ""
            selected_anchor = anchor
            if source_uri and "#" in source_uri:
                source_uri, fragment = source_uri.split("#", 1)
                selected_anchor = selected_anchor or unquote(fragment)
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
            if target.suffix.lower() not in {".htm", ".html", ".cpp", ".h", ".hpp", ".c", ".cxx", ".hxx", ".js", ".md", ".txt"}:
                raise PermissionError("read_source accepts document and code files, not environment, database, or credential configuration files")
            if not target.is_file():
                raise FileNotFoundError(f"Source file not found: {target}")
            raw_html, encoding = _decode_source(target.read_bytes())
            is_html = target.suffix.lower() in {".htm", ".html"}
            selected_html, anchor_found = _anchor_section(raw_html, selected_anchor) if is_html else (raw_html, not selected_anchor)
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
            if format == "raw_html" or not is_html:
                content = selected_html
            else:
                parser = _OfficialTextExtractor()
                parser.feed(selected_html)
                content = parser.text()
            total_chars = len(content)
            truncated = offset + max_chars < total_chars
            content = content[offset:offset + max_chars]
            context = {}
            if include_context and selected_anchor and is_html:
                overview = re.split(r"<(?:h2|a\s+(?:name|id))\b", raw_html, maxsplit=1, flags=re.I)[0]
                parser = _OfficialTextExtractor()
                parser.feed(overview)
                overview_text = parser.text()
                context = {"api_context": overview_text[:6000], "api_context_truncated": len(overview_text) > 6000}
            return self._response(
                connection,
                **self._qualified_context(connection, reference),
                reference=reference,
                source_uri=source_uri,
                anchor=selected_anchor,
                anchor_found=anchor_found,
                resolved_path=str(target),
                format=format,
                encoding=encoding,
                truncated=truncated,
                offset=offset, total_chars=total_chars,
                next_offset=offset + max_chars if truncated else None,
                source_kind="html" if is_html else "code" if target.suffix.lower() in {".cpp", ".h", ".c", ".hpp"} else "text",
                content=content,
                **context,
            )
