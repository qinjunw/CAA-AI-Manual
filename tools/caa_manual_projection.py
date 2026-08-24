"""Build the stable, agent-facing read model for the CAA manual index."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SOURCE_BASELINE = "CATIA V5R21 CAADoc"
VERSION_POLICY = (
    "R21 documentation baseline; results may be reusable across nearby releases "
    "but are not version guarantees."
)
PROJECTION_SCHEMA_VERSION = 1

API_PAGE_KINDS = {
    "interface", "class", "function", "enum", "define", "typedef",
    "struct", "collection", "notification", "exception",
}
MEMBER_KINDS = {"method", "property", "data-member"}
PAGE_RE = re.compile(
    r"^(?P<kind>interface|class|function|enum|define|typedef|struct|collection|"
    r"notification|exception)_(?P<name>.+?)(?:_\d+)?\.htm$",
    re.IGNORECASE,
)


@dataclass
class Projection:
    catalog_nodes: list[dict[str, Any]]
    api_pages: list[dict[str, Any]]
    api_members: list[dict[str, Any]]
    api_aliases: list[dict[str, Any]]

    def counts(self) -> dict[str, int]:
        return {
            "catalog_nodes": len(self.catalog_nodes),
            "api_pages": len(self.api_pages),
            "api_members": len(self.api_members),
            "api_aliases": len(self.api_aliases),
        }


def projection_id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(part) for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def normalize_term(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").strip().casefold()


def load_catalog(path: Path) -> dict[str, Any]:
    """Load the JSON-compatible YAML 1.2 catalog without a YAML dependency."""
    if not path.exists():
        return {
            "schema_version": PROJECTION_SCHEMA_VERSION,
            "source_baseline": SOURCE_BASELINE,
            "layers": {},
            "capabilities": {},
            "frameworks": {},
            "api_aliases": [],
        }
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("schema_version") != PROJECTION_SCHEMA_VERSION:
        raise ValueError(f"Unsupported catalog schema_version in {path}")
    if data.get("source_baseline", SOURCE_BASELINE) != SOURCE_BASELINE:
        raise ValueError(f"Unsupported source_baseline in {path}")
    for key in ("layers", "capabilities", "frameworks"):
        if not isinstance(data.get(key, {}), dict):
            raise ValueError(f"catalog field {key!r} must be an object")
    if not isinstance(data.get("api_aliases", []), list):
        raise ValueError("catalog field 'api_aliases' must be an array")
    return data


class PureUriPath:
    """URI basename helper that does not interpret caadoc:// as a drive."""

    def __init__(self, value: str):
        self.value = value.replace("\\", "/")

    @property
    def name(self) -> str:
        return self.value.rsplit("/", 1)[-1]


def source_context(source_uri: str) -> tuple[str, str]:
    normalized = source_uri.replace("\\", "/")
    for marker, layer in (
        ("/generated/refman/", "CAA-refman"),
        ("/generated/interfaces/", "Automation"),
    ):
        if marker in normalized:
            remainder = normalized.split(marker, 1)[1]
            return layer, remainder.split("/", 1)[0]
    return "", ""


def page_template(source_uri: str) -> tuple[str, str, str]:
    name = PureUriPath(source_uri).name
    if name.startswith("_function_") and name.endswith(".htm"):
        return "function", name[len("_function_"):-4], "aggregate"
    match = PAGE_RE.match(name)
    if not match:
        return "document", Path(name).stem, "detail"
    return match.group("kind").lower(), match.group("name"), "detail"


def resolve_source_available(source_uri: str, caadoc_root: Path | None) -> bool:
    if not source_uri.startswith("caadoc://") or not caadoc_root:
        return False
    relative = source_uri[len("caadoc://"):].replace("/", os.sep)
    return (caadoc_root / relative).is_file()


def _json_list(values: Iterable[str]) -> str:
    return json.dumps(sorted({value for value in values if value}), ensure_ascii=False)


def _best_summary(entities: Iterable[dict[str, Any]]) -> str:
    values = {
        " ".join(str(entity.get("summary", "")).split())
        for entity in entities
        if str(entity.get("summary", "")).strip()
    }
    if not values:
        return ""
    informative = [value for value in values if len(value) >= 12]
    return min(informative or values, key=lambda value: (len(value), value))


def _include_file(entities: Iterable[dict[str, Any]]) -> str:
    for entity in entities:
        if entity.get("include_file"):
            return entity["include_file"]
        extra = entity.get("extra")
        if isinstance(extra, dict) and extra.get("include_file"):
            return extra["include_file"]
    return ""


def _evidence_methods(entity: dict[str, Any], evidence: dict[str, dict[str, Any]]) -> set[str]:
    return {
        evidence[evidence_id].get("extraction_method", "")
        for evidence_id in entity.get("evidence_ids", [])
        if evidence_id in evidence
    }


def _canonical_identity(
    source_uri: str,
    entities: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    aggregate_families: set[tuple[str, str, str]],
) -> tuple[str, str, str]:
    template_kind, template_name, page_role = page_template(source_uri)
    layer, framework = source_context(source_uri)
    if template_kind == "function" and (
        page_role == "aggregate" or (layer, framework, template_name) in aggregate_families
    ):
        return "function", template_name, page_role
    candidates = [entity for entity in entities if entity.get("kind") in API_PAGE_KINDS]
    if not candidates:
        return template_kind, template_name, page_role

    def rank(entity: dict[str, Any]) -> tuple[int, int, int, int, str]:
        methods = _evidence_methods(entity, evidence)
        return (
            int(bool(entity.get("signature"))),
            int("toc-symbol" in methods or "js-link-index" in methods),
            int(entity.get("name") == template_name),
            int(entity.get("kind") == template_kind),
            entity.get("id", ""),
        )

    canonical = max(candidates, key=rank)
    return (
        canonical.get("kind", template_kind),
        html.unescape(canonical.get("name", template_name)),
        page_role,
    )


def build_projection(
    store: Any,
    catalog: dict[str, Any],
    caadoc_root: Path | None = None,
) -> Projection:
    entities = list(store.entities.values())
    evidence = store.evidence
    page_entities: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entity in entities:
        source_uri = entity.get("source_path", "")
        layer, _ = source_context(source_uri)
        if not layer or not source_uri.lower().endswith(".htm"):
            continue
        kind = entity.get("kind", "")
        if kind in API_PAGE_KINDS:
            page_entities[source_uri].append(entity)
        elif kind == "document" and PureUriPath(source_uri).name.startswith("_function_"):
            page_entities[source_uri].append(entity)

    page_specs: list[dict[str, Any]] = []
    logical_specs: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    aggregate_families = {
        (*source_context(source_uri), page_template(source_uri)[1])
        for source_uri in page_entities
        if page_template(source_uri)[2] == "aggregate"
    }
    for source_uri, raw_entities in sorted(page_entities.items()):
        layer, framework = source_context(source_uri)
        api_kind, name, page_role = _canonical_identity(
            source_uri, raw_entities, evidence, aggregate_families
        )
        logical_key = (layer, framework, api_kind, name)
        api_node_id = projection_id("api-node", *logical_key)
        evidence_ids = {
            evidence_id for entity in raw_entities for evidence_id in entity.get("evidence_ids", [])
        }
        page_specs.append({
            "page_id": projection_id("api-page", source_uri),
            "api_node_id": api_node_id,
            "page_role": page_role,
            "page_template_kind": page_template(source_uri)[0],
            "signature": next(
                (entity.get("signature", "") for entity in raw_entities if entity.get("signature")),
                "",
            ),
            "summary_en": _best_summary(raw_entities),
            "include_file": _include_file(raw_entities),
            "source_uri": source_uri,
            "source_available": int(resolve_source_available(source_uri, caadoc_root)),
            "tags_json": _json_list(tag for entity in raw_entities for tag in entity.get("tags", [])),
            "raw_entity_ids_json": _json_list(entity["id"] for entity in raw_entities),
            "evidence_ids_json": _json_list(evidence_ids),
        })
        logical_specs.setdefault(logical_key, {
            "node_id": api_node_id,
            "node_type": "function-family" if api_kind == "function" else "api",
            "english_key": name,
            "name_en": name,
            "name_zh": "",
            "summary_zh": "",
            "layer": layer,
            "framework": framework,
            "api_kind": api_kind,
        })

    pages_by_uri = {page["source_uri"]: page for page in page_specs}
    member_specs: list[dict[str, Any]] = []
    seen_member_locations: set[tuple[str, str]] = set()
    for entity in entities:
        if entity.get("kind") not in MEMBER_KINDS:
            continue
        page = pages_by_uri.get(entity.get("source_path", ""))
        if not page:
            continue
        location = (page["page_id"], entity.get("anchor", ""))
        if location in seen_member_locations:
            raise ValueError(
                "Duplicate member page/anchor after parsing: "
                f"{entity.get('source_path')}#{entity.get('anchor')}"
            )
        seen_member_locations.add(location)
        member_specs.append({
            "member_id": entity["id"],
            "page_id": page["page_id"],
            "member_group_key": projection_id(
                "member-group", page["api_node_id"], entity.get("kind", ""), entity.get("name", "")
            ),
            "kind": entity.get("kind", ""),
            "name": entity.get("name", ""),
            "signature": entity.get("signature", ""),
            "summary_en": entity.get("summary", ""),
            "anchor": entity.get("anchor", ""),
            "raw_entity_id": entity["id"],
            "evidence_ids_json": _json_list(entity.get("evidence_ids", [])),
        })

    nodes: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    alias_keys: set[tuple[str, str, str]] = set()

    def add_alias(
        alias: str,
        target_type: str,
        target_id: str,
        english_key: str,
        scope: str,
        summary_zh: str = "",
        basis: str = "literal",
    ) -> None:
        normalized = normalize_term(alias)
        key = (normalized, target_type, target_id)
        if not normalized or key in alias_keys:
            return
        if not english_key:
            raise ValueError(f"Chinese alias {alias!r} has no English key")
        if basis not in {"literal", "api-content"}:
            raise ValueError(f"Unsupported alias basis {basis!r}")
        alias_keys.add(key)
        aliases.append({
            "alias_id": projection_id("alias", normalized, target_type, target_id),
            "alias": alias,
            "normalized_alias": normalized,
            "language": "zh",
            "target_type": target_type,
            "target_id": target_id,
            "english_key": english_key,
            "scope": scope,
            "summary_zh": summary_zh,
            "basis": basis,
        })

    root_id = "catalog:root"
    capability_root_id = "catalog:capabilities"
    nodes.extend([
        {
            "node_id": root_id, "parent_id": "", "node_type": "root", "english_key": "CAA",
            "name_en": "CAA API Catalog", "name_zh": "CAA API 目录",
            "summary_zh": "按文档层、Framework 和能力键浏览本地 CAA API。",
            "layer": "", "framework": "", "api_kind": "", "sort_order": 0,
        },
        {
            "node_id": capability_root_id, "parent_id": root_id, "node_type": "capability-root",
            "english_key": "capabilities", "name_en": "Capabilities", "name_zh": "能力检索",
            "summary_zh": "维护者定义的检索标签，不等同于官方 API 分类。",
            "layer": "", "framework": "", "api_kind": "", "sort_order": 90,
        },
    ])

    layer_catalog = catalog.get("layers", {})
    layers = sorted({
        entity.get("layer", "") for entity in entities
        if entity.get("layer") in {"CAA-refman", "Automation", "Online-concept", "CAA-example"}
    })
    layer_order = {"CAA-refman": 10, "Automation": 20, "Online-concept": 30, "CAA-example": 40}
    for layer in layers:
        config = layer_catalog.get(layer, {})
        node_id = projection_id("catalog-layer", layer)
        nodes.append({
            "node_id": node_id, "parent_id": root_id, "node_type": "layer", "english_key": layer,
            "name_en": layer, "name_zh": config.get("label_zh", ""),
            "summary_zh": config.get("description_zh", ""), "layer": layer, "framework": "",
            "api_kind": "", "sort_order": layer_order.get(layer, 80),
        })
        for term in [config.get("label_zh", ""), *config.get("query_terms_zh", [])]:
            add_alias(term, "catalog_node", node_id, layer, "layer", config.get("description_zh", ""))

    framework_catalog = catalog.get("frameworks", {})
    formal_frameworks = sorted({
        (entity.get("layer", ""), entity.get("name", ""))
        for entity in entities if entity.get("kind") == "framework"
    })
    framework_node_ids: dict[tuple[str, str], str] = {}
    for layer, framework in formal_frameworks:
        if not layer or not framework:
            continue
        config = framework_catalog.get(framework, {})
        node_id = projection_id("catalog-framework", layer, framework)
        framework_node_ids[(layer, framework)] = node_id
        nodes.append({
            "node_id": node_id, "parent_id": projection_id("catalog-layer", layer),
            "node_type": "framework", "english_key": framework, "name_en": framework,
            "name_zh": config.get("label_zh", ""), "summary_zh": config.get("description_zh", ""),
            "layer": layer, "framework": framework, "api_kind": "", "sort_order": 100,
        })
        basis = config.get("basis", "literal")
        for term in [config.get("label_zh", ""), *config.get("query_terms_zh", [])]:
            add_alias(term, "catalog_node", node_id, framework, "framework", config.get("description_zh", ""), basis)

    for logical in logical_specs.values():
        nodes.append({
            **logical,
            "parent_id": framework_node_ids.get(
                (logical["layer"], logical["framework"]),
                projection_id("catalog-layer", logical["layer"]),
            ),
            "sort_order": 200,
        })

    capability_catalog = catalog.get("capabilities", {})
    capability_keys = sorted({
        tag for entity in entities for tag in entity.get("tags", [])
    } | {
        tag for chunk in store.chunks.values() for tag in chunk.get("capabilities", [])
    })
    for capability in capability_keys:
        config = capability_catalog.get(capability, {})
        node_id = projection_id("catalog-capability", capability)
        nodes.append({
            "node_id": node_id, "parent_id": capability_root_id, "node_type": "capability",
            "english_key": capability, "name_en": capability, "name_zh": config.get("label_zh", ""),
            "summary_zh": config.get("description_zh", ""), "layer": "", "framework": "",
            "api_kind": "", "sort_order": 100,
        })
        for term in [config.get("label_zh", ""), *config.get("query_terms_zh", [])]:
            add_alias(term, "catalog_node", node_id, capability, "capability", config.get("description_zh", ""))

    api_nodes_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for logical in logical_specs.values():
        api_nodes_by_name[logical["name_en"]].append(logical)
    for config in catalog.get("api_aliases", []):
        target = config.get("target", "")
        candidates = api_nodes_by_name.get(target, [])
        if config.get("framework"):
            candidates = [item for item in candidates if item["framework"] == config["framework"]]
        if config.get("layer"):
            candidates = [item for item in candidates if item["layer"] == config["layer"]]
        if not candidates:
            raise ValueError(f"API alias target does not resolve: {target!r}")
        for logical in candidates:
            logical["name_zh"] = logical["name_zh"] or config.get("label_zh", "")
            logical["summary_zh"] = logical["summary_zh"] or config.get("description_zh", "")
            for term in config.get("query_terms_zh", []):
                add_alias(
                    term, "api_node", logical["node_id"], target, "api",
                    config.get("description_zh", ""), config.get("basis", "api-content"),
                )

    node_index = {node["node_id"]: node for node in nodes}
    for logical in logical_specs.values():
        node_index[logical["node_id"]]["name_zh"] = logical["name_zh"]
        node_index[logical["node_id"]]["summary_zh"] = logical["summary_zh"]
    for alias in aliases:
        if alias["target_id"] not in node_index:
            raise ValueError(f"Alias target does not exist: {alias['target_id']}")

    return Projection(
        catalog_nodes=sorted(nodes, key=lambda row: (row["parent_id"], row["sort_order"], row["name_en"])),
        api_pages=sorted(page_specs, key=lambda row: (row["api_node_id"], row["page_role"], row["source_uri"])),
        api_members=sorted(member_specs, key=lambda row: (row["page_id"], row["member_group_key"], row["signature"])),
        api_aliases=sorted(aliases, key=lambda row: (row["normalized_alias"], row["scope"], row["english_key"])),
    )


def create_projection_tables(cur: sqlite3.Cursor, projection: Projection) -> None:
    cur.executescript(
        """
        CREATE TABLE catalog_nodes (
          node_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, node_type TEXT NOT NULL,
          english_key TEXT NOT NULL, name_en TEXT NOT NULL, name_zh TEXT NOT NULL,
          summary_zh TEXT NOT NULL, layer TEXT NOT NULL, framework TEXT NOT NULL,
          api_kind TEXT NOT NULL, sort_order INTEGER NOT NULL
        );
        CREATE TABLE api_pages (
          page_id TEXT PRIMARY KEY, api_node_id TEXT NOT NULL, page_role TEXT NOT NULL,
          page_template_kind TEXT NOT NULL, signature TEXT NOT NULL, summary_en TEXT NOT NULL,
          include_file TEXT NOT NULL, source_uri TEXT NOT NULL UNIQUE, source_available INTEGER NOT NULL,
          tags_json TEXT NOT NULL, raw_entity_ids_json TEXT NOT NULL, evidence_ids_json TEXT NOT NULL,
          FOREIGN KEY(api_node_id) REFERENCES catalog_nodes(node_id)
        );
        CREATE TABLE api_members (
          member_id TEXT PRIMARY KEY, page_id TEXT NOT NULL, member_group_key TEXT NOT NULL,
          kind TEXT NOT NULL, name TEXT NOT NULL, signature TEXT NOT NULL, summary_en TEXT NOT NULL,
          anchor TEXT NOT NULL, raw_entity_id TEXT NOT NULL, evidence_ids_json TEXT NOT NULL,
          FOREIGN KEY(page_id) REFERENCES api_pages(page_id), UNIQUE(page_id, anchor)
        );
        CREATE TABLE api_aliases (
          alias_id TEXT PRIMARY KEY, alias TEXT NOT NULL, normalized_alias TEXT NOT NULL,
          language TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
          english_key TEXT NOT NULL, scope TEXT NOT NULL, summary_zh TEXT NOT NULL, basis TEXT NOT NULL
        );
        CREATE TABLE projection_metadata (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE INDEX idx_entities_name ON entities(name);
        CREATE INDEX idx_relations_src_type ON relations(src_id, type);
        CREATE INDEX idx_relations_dst_type ON relations(dst_id, type);
        CREATE INDEX idx_catalog_nodes_parent ON catalog_nodes(parent_id, sort_order, name_en);
        CREATE INDEX idx_catalog_nodes_key ON catalog_nodes(english_key, node_type);
        CREATE INDEX idx_api_pages_node ON api_pages(api_node_id, page_role, source_uri);
        CREATE INDEX idx_api_members_page ON api_members(page_id, member_group_key, signature);
        CREATE INDEX idx_api_members_name ON api_members(name);
        CREATE INDEX idx_api_aliases_term ON api_aliases(normalized_alias, scope);
        """
    )
    cur.executemany(
        "INSERT INTO catalog_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [tuple(row[key] for key in (
            "node_id", "parent_id", "node_type", "english_key", "name_en", "name_zh",
            "summary_zh", "layer", "framework", "api_kind", "sort_order",
        )) for row in projection.catalog_nodes],
    )
    cur.executemany(
        "INSERT INTO api_pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [tuple(row[key] for key in (
            "page_id", "api_node_id", "page_role", "page_template_kind", "signature",
            "summary_en", "include_file", "source_uri", "source_available", "tags_json",
            "raw_entity_ids_json", "evidence_ids_json",
        )) for row in projection.api_pages],
    )
    cur.executemany(
        "INSERT INTO api_members VALUES (?,?,?,?,?,?,?,?,?,?)",
        [tuple(row[key] for key in (
            "member_id", "page_id", "member_group_key", "kind", "name", "signature",
            "summary_en", "anchor", "raw_entity_id", "evidence_ids_json",
        )) for row in projection.api_members],
    )
    cur.executemany(
        "INSERT INTO api_aliases VALUES (?,?,?,?,?,?,?,?,?,?)",
        [tuple(row[key] for key in (
            "alias_id", "alias", "normalized_alias", "language", "target_type",
            "target_id", "english_key", "scope", "summary_zh", "basis",
        )) for row in projection.api_aliases],
    )
    cur.executemany(
        "INSERT INTO projection_metadata VALUES (?,?)",
        [
            ("projection_schema_version", str(PROJECTION_SCHEMA_VERSION)),
            ("source_baseline", SOURCE_BASELINE),
            ("version_policy", VERSION_POLICY),
            ("api_search_fts_available", "false"),
        ],
    )

    try:
        cur.execute(
            """
            CREATE VIRTUAL TABLE api_search_fts USING fts5(
              api_node_id UNINDEXED, page_id UNINDEXED, member_id UNINDEXED,
              record_type UNINDEXED, name, owner_name, framework, summary,
              signature, tags, aliases
            )
            """
        )
    except sqlite3.OperationalError as exc:
        if "no such module: fts5" in str(exc).casefold():
            return
        raise
    cur.execute(
        "UPDATE projection_metadata SET value='true' WHERE key='api_search_fts_available'"
    )

    pages_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for page in projection.api_pages:
        pages_by_node[page["api_node_id"]].append(page)
    aliases_by_node: dict[str, list[str]] = defaultdict(list)
    for alias in projection.api_aliases:
        if alias["target_type"] == "api_node":
            aliases_by_node[alias["target_id"]].append(alias["alias"])
    api_nodes = {
        node["node_id"]: node for node in projection.catalog_nodes
        if node["node_type"] in {"api", "function-family"}
    }
    cur.executemany(
        "INSERT INTO api_search_fts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(
            node_id, "", "", "api_node", node["name_en"], "", node["framework"],
            " ".join(page["summary_en"] for page in pages_by_node[node_id]),
            " ".join(page["signature"] for page in pages_by_node[node_id]),
            " ".join(sorted({
                tag for page in pages_by_node[node_id] for tag in json.loads(page["tags_json"])
            })),
            " ".join(aliases_by_node.get(node_id, [])),
        ) for node_id, node in api_nodes.items()],
    )
    page_index = {page["page_id"]: page for page in projection.api_pages}
    cur.executemany(
        "INSERT INTO api_search_fts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(
            page_index[member["page_id"]]["api_node_id"], member["page_id"], member["member_id"],
            "api_member", member["name"],
            api_nodes[page_index[member["page_id"]]["api_node_id"]]["name_en"],
            api_nodes[page_index[member["page_id"]]["api_node_id"]]["framework"],
            member["summary_en"], member["signature"], "", "",
        ) for member in projection.api_members],
    )
