#!/usr/bin/env python3
"""
Build an AI-first static index for the local CATIA CAA documentation.

The builder is intentionally deterministic: official symbols, signatures,
relationships, chunks, and source paths are extracted from local CAADoc files
without calling an LLM. Lightweight capability tags and sparse hash vectors are
derived locally so the output can be rebuilt offline.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

try:
    from .caa_manual_projection import (
        Projection,
        build_projection,
        create_projection_tables,
        load_catalog,
    )
except ImportError:
    from caa_manual_projection import (
        Projection,
        build_projection,
        create_projection_tables,
        load_catalog,
    )

PROJECT_ROOT = Path(__file__).resolve().parents[1]
URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
LOGICAL_ROOTS: list[tuple[Path, str]] = []


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


ENV_FILE_VALUES = load_env_file(PROJECT_ROOT / ".env")


def configured_value(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key) or ENV_FILE_VALUES.get(key) or default


DEFAULT_OUT = Path(configured_value("CAA_AI_MANUAL_ROOT", str(PROJECT_ROOT)))
DEFAULT_CAADOC = configured_value("CAA_CAADOC_ROOT")
DEFAULT_DB = Path(configured_value("CAA_AI_MANUAL_DB", str(DEFAULT_OUT / "data" / "manual.sqlite")))

TAG_RULES = {
    "geometry": ["geometry", "geometric", "curve", "surface", "point", "nurbs", "pcurve"],
    "topology": ["topology", "topological", "body", "cell", "domain", "face", "edge", "vertex"],
    "operator": ["operator", "run()", "getresult", "create operator", "catcgmoperator"],
    "factory": ["factory", "create", "catgeofactory", "hybridshapefactory"],
    "automation": ["automation", "vb", "object)", "property", "hybridshape"],
    "feature": ["feature", "spec", "catigs", "gsm", "part"],
    "journal": ["journal", "catcgmjournal", "findlasts", "findorigins"],
    "generic-naming": ["generic naming", "brep", "reference", "createreference"],
    "validation": ["checker", "check", "diagnosis", "validity", "invalid", "tolerance"],
    "license": ["license", "granted license", "gsd", "gso", "dl1"],
    "container": ["container", "current container", "caticgmcontainer"],
    "nurbs": ["nurbs", "knot", "control point"],
    "boolean": ["boolean", "add", "subtract", "intersect"],
    "fillet": ["fillet", "radius", "ribbon"],
    "sweep": ["sweep", "prism", "revol", "extrude"],
}

LAYER_RULES = [
    ("Automation", ["generated\\interfaces", "HybridShape", "Factory (Object)", "object)"]),
    ("CAA-refman", ["generated\\refman"]),
    ("Online-concept", ["Doc\\online"]),
    ("CAA-example", [".edu"]),
]


def configure_logical_roots(caadoc: Path | None, manual_root: Path | None) -> None:
    LOGICAL_ROOTS.clear()
    if caadoc:
        LOGICAL_ROOTS.append((caadoc.resolve(), "caadoc"))
    if manual_root:
        LOGICAL_ROOTS.append((manual_root.resolve(), "manual"))


def norm_path(p: Path | str) -> str:
    if not p:
        return ""
    if isinstance(p, str) and URI_RE.match(p):
        return p.replace("\\", "/")
    resolved = Path(p).resolve()
    for root, scheme in LOGICAL_ROOTS:
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        rel_text = "/".join(rel.parts)
        return f"{scheme}://{rel_text}" if rel_text else f"{scheme}://"
    return str(resolved)


def stable_id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return f"{prefix}:{hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()[:16]}"


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "iso-8859-1", "cp1252", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def strip_tags(value: str) -> str:
    value = re.sub(r"(?is)<script.*?</script>", " ", value)
    value = re.sub(r"(?is)<style.*?</style>", " ", value)
    value = re.sub(r"(?is)<br\s*/?>", "\n", value)
    value = re.sub(r"(?is)</(p|div|li|dt|dd|h[1-6]|tr)>", "\n", value)
    value = re.sub(r"(?is)<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s+", "\n", value)
    return value.strip()


def compact(value: str, limit: int = 900) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def infer_tags(*texts: str) -> list[str]:
    hay = " ".join(t for t in texts if t).lower()
    tags = []
    for tag, words in TAG_RULES.items():
        if any(w.lower() in hay for w in words):
            tags.append(tag)
    return sorted(tags)


def infer_layer(path: Path, text: str = "") -> str:
    normalized_path = str(path).replace("/", "\\").lower()
    path_rules = [
        ("Automation", "\\generated\\interfaces\\"),
        ("CAA-refman", "\\generated\\refman\\"),
        ("Online-concept", "\\doc\\online\\"),
    ]
    for layer, needle in path_rules:
        if needle in normalized_path:
            return layer
    if ".edu\\" in normalized_path:
        return "CAA-example"

    combined = f"{path} {text}"
    for layer, needles in LAYER_RULES:
        if any(n.lower() in combined.lower() for n in needles):
            return layer
    return "CAA-doc"


def words_for_vector(text: str) -> Counter:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text)
    c = Counter(t.lower() for t in tokens)
    return c


def sparse_hash_vector(text: str, dims: int = 1024, max_terms: int = 80) -> dict:
    counts = words_for_vector(text)
    if not counts:
        return {"dims": dims, "indices": [], "values": []}
    buckets = defaultdict(float)
    for token, count in counts.most_common(max_terms * 4):
        h = int(hashlib.md5(token.encode("utf-8", "ignore")).hexdigest(), 16)
        idx = h % dims
        sign = -1.0 if (h >> 8) & 1 else 1.0
        buckets[idx] += sign * float(count)
    top = sorted(buckets.items(), key=lambda kv: abs(kv[1]), reverse=True)[:max_terms]
    norm = sum(v * v for _, v in top) ** 0.5 or 1.0
    return {
        "dims": dims,
        "indices": [i for i, _ in top],
        "values": [round(v / norm, 6) for _, v in top],
    }


@dataclass
class Store:
    out: Path
    entities: dict[str, dict] = field(default_factory=dict)
    relations: list[dict] = field(default_factory=list)
    chunks: dict[str, dict] = field(default_factory=dict)
    evidence: dict[str, dict] = field(default_factory=dict)
    vectors: dict[str, dict] = field(default_factory=dict)
    symbol_names: set[str] = field(default_factory=set)
    source_manifest: list[dict] = field(default_factory=list)

    def add_entity(self, entity: dict) -> str:
        entity.setdefault("tags", [])
        entity.setdefault("evidence_ids", [])
        if entity["id"] in self.entities:
            old = self.entities[entity["id"]]
            for key, value in entity.items():
                if value and not old.get(key):
                    old[key] = value
            old["tags"] = sorted(set(old.get("tags", [])) | set(entity.get("tags", [])))
            old["evidence_ids"] = sorted(set(old.get("evidence_ids", [])) | set(entity.get("evidence_ids", [])))
        else:
            self.entities[entity["id"]] = entity
        name = entity.get("name")
        if name and re.match(r"^[A-Za-z_][A-Za-z0-9_()]*$", name):
            self.symbol_names.add(name.replace("()", ""))
        return entity["id"]

    def add_relation(self, src: str, rel_type: str, dst: str, source_path: str = "", evidence_id: str = "", confidence: float = 1.0):
        if not src or not dst or src == dst:
            return
        self.relations.append({
            "src_id": src,
            "type": rel_type,
            "dst_id": dst,
            "confidence": confidence,
            "source_path": norm_path(source_path),
            "evidence_id": evidence_id,
        })

    def add_evidence(self, source_path: Path | str, anchor: str, text: str, method: str, trust: str = "official-doc") -> str:
        source_uri = norm_path(source_path)
        sid = stable_id("ev", source_uri, anchor, compact(text, 200))
        self.evidence[sid] = {
            "id": sid,
            "source_path": source_uri,
            "anchor": anchor or "",
            "quote_or_span": compact(text, 600),
            "extraction_method": method,
            "trust_level": trust,
        }
        return sid

    def add_chunk(self, source_path: Path | str, doc_kind: str, heading_path: list[str], text: str, symbols: list[str] | None = None, evidence_rank: str = "official"):
        text = compact(text, 5000)
        if len(text) < 20:
            return ""
        source_uri = norm_path(source_path)
        cid = stable_id("chunk", source_uri, " > ".join(heading_path), text[:200])
        tags = infer_tags(text, " ".join(heading_path))
        self.chunks[cid] = {
            "id": cid,
            "source_path": source_uri,
            "doc_kind": doc_kind,
            "heading_path": heading_path,
            "text": text,
            "symbols": sorted(set(symbols or [])),
            "capabilities": tags,
            "evidence_rank": evidence_rank,
        }
        self.vectors[cid] = {"id": cid, "kind": "chunk", **sparse_hash_vector(" ".join(heading_path) + " " + text)}
        return cid

    def write_jsonl(self, name: str, rows: Iterable[dict]):
        path = self.out / "data" / name
        with path.open("w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def parse_toc_files(caadoc: Path, store: Store):
    roots = [
        (caadoc / "Doc" / "generated" / "refman", "CAA-refman"),
        (caadoc / "Doc" / "generated" / "interfaces", "Automation"),
    ]
    for root, layer in roots:
        if not root.exists():
            continue
        for toc in root.rglob("*Toc.xml"):
            framework = toc.parent.name
            try:
                tree = ET.parse(toc)
            except ET.ParseError:
                continue
            fw_id = f"framework:{layer}:{framework}"
            ev_id = store.add_evidence(toc, "", f"{framework} symbol table", "toc-xml", "official-index")
            store.add_entity({
                "id": fw_id,
                "kind": "framework",
                "name": framework,
                "framework": framework,
                "layer": layer,
                "source_path": norm_path(toc),
                "anchor": "",
                "signature": "",
                "summary": f"{framework} framework symbol table extracted from {toc.name}.",
                "tags": infer_tags(framework),
                "evidence_ids": [ev_id],
            })
            for item in tree.iter("ITEM"):
                kind = item.attrib.get("type", "")
                name = item.attrib.get("name", "")
                href = item.attrib.get("href", "")
                if not kind or not name or not href:
                    continue
                entity_path = (toc.parent / href).resolve()
                ent_id = f"symbol:{layer}:{framework}:{kind}:{name}"
                ev = store.add_evidence(entity_path, "", f"{kind} {name}", "toc-symbol", "official-index")
                store.add_entity({
                    "id": ent_id,
                    "kind": kind,
                    "name": name,
                    "framework": framework,
                    "layer": layer,
                    "source_path": norm_path(entity_path),
                    "anchor": "",
                    "signature": "",
                    "summary": "",
                    "tags": infer_tags(framework, kind, name),
                    "evidence_ids": [ev],
                })
                store.add_relation(fw_id, "DECLARES", ent_id, norm_path(toc), ev, 1.0)


def parse_js_links(caadoc: Path, store: Store):
    for js in (caadoc / "Doc" / "generated").rglob("jsFwLink.js"):
        framework = js.parent.name
        layer = infer_layer(js)
        text = read_text(js)
        for m in re.finditer(r'objet\["(?P<kind>[^"]+)"\]\["(?P<name>[^"]+)"\]=(?P<idx>\d+);\s*object\["(?P=kind)"\]\[(?P=idx)\]="(?P<href>[^"]+)";', text):
            kind, name, href = m.group("kind"), m.group("name"), m.group("href")
            path = (js.parent / href).resolve()
            ent_id = f"symbol:{layer}:{framework}:{kind}:{name}"
            ev = store.add_evidence(path, "", f"{kind} {name}", "js-link-index", "official-index")
            store.add_entity({
                "id": ent_id,
                "kind": kind,
                "name": name,
                "framework": framework,
                "layer": layer,
                "source_path": norm_path(path),
                "anchor": "",
                "signature": "",
                "summary": "",
                "tags": infer_tags(framework, kind, name),
                "evidence_ids": [ev],
            })


def h1_title(text: str) -> str:
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", text)
    return strip_tags(m.group(1)) if m else ""


def page_summary(raw: str, plain: str) -> str:
    m = re.search(r"(?is)<b>\s*<i>\s*(.*?)\s*</i>\s*</b>", raw)
    if m:
        summary = compact(strip_tags(m.group(1)), 900)
        if summary:
            return summary
    cut = len(plain)
    for marker in ("Method Index", "Property Index", "Data Member Index"):
        pos = plain.find(marker)
        if pos >= 0:
            cut = min(cut, pos)
    pre_index = plain[:cut]
    m = re.search(r"(?is)\bRole\s*:\s*(.*?)(?:\bLifecycle\b|\bLifecyle\b|This object is included|$)", pre_index)
    if m:
        return compact(m.group(1), 900)
    lines = [ln.strip() for ln in pre_index.splitlines() if ln.strip()]
    return compact(" ".join(lines[:5]), 900)


def extract_include_file(raw: str) -> str:
    m = re.search(r"(?is)included in the file:\s*<b>(.*?)</b>", raw)
    return strip_tags(m.group(1)) if m else ""


def source_entity_id_from_path(path: Path, layer: str, framework: str, raw: str) -> str:
    name = ""
    kind = "document"
    base = path.name
    m = re.match(r"(interface|class|function|enum|typedef|struct|macro|collection|exception|notification)_(.+?)_\d+\.htm$", base)
    if m:
        kind = m.group(1)
        name = m.group(2)
    if not name:
        title = h1_title(raw)
        name = title.split()[0] if title else path.stem
    return f"symbol:{layer}:{framework}:{kind}:{name}", kind, name


def parse_member_index(raw: str, owner_id: str, owner_name: str, source_path: Path, store: Store):
    sections = [
        ("method", "Method Index"),
        ("property", "Property Index"),
        ("data-member", "Data Member Index"),
    ]
    section_starts = []
    raw_lower = raw.lower()
    for member_kind, title in sections:
        pos = raw_lower.find(title.lower())
        if pos < 0:
            continue
        section_starts.append((pos, member_kind, title))

    section_starts.sort()
    for index, (pos, member_kind, title) in enumerate(section_starts):
        next_pos = section_starts[index + 1][0] if index + 1 < len(section_starts) else len(raw)
        section = raw[pos: min(pos + 120000, next_pos)]
        for m in re.finditer(
            r'(?is)<a\s+href="#(?P<anchor>[^"]+)"[^>]*>\s*<b>(?P<name>[^<]+)</b>\s*</a>\s*(?P<sig>.*?)\s*(?:<dd>\s*(?P<desc>.*?))?(?=\s*<dt|\s*<h2|\s*<hr|\s*</dl>)',
            section,
        ):
            name = strip_tags(m.group("name"))
            if not name or len(name) > 160:
                continue
            sig = compact(strip_tags(m.group("sig")), 500)
            desc = compact(strip_tags(m.group("desc") or ""), 800)
            if member_kind == "method" and not sig.startswith("(") and "(" not in sig and title.lower() == "method index":
                pass
            mem_id = stable_id("member", owner_id, member_kind, name, sig, m.group("anchor"))
            ev = store.add_evidence(source_path, m.group("anchor"), f"{name} {sig} {desc}", f"{member_kind}-index", "official-refman")
            store.add_entity({
                "id": mem_id,
                "kind": member_kind,
                "name": name,
                "framework": store.entities.get(owner_id, {}).get("framework", ""),
                "layer": store.entities.get(owner_id, {}).get("layer", ""),
                "source_path": norm_path(source_path),
                "anchor": m.group("anchor"),
                "signature": sig,
                "summary": desc,
                "tags": infer_tags(owner_name, name, sig, desc),
                "evidence_ids": [ev],
            })
            store.add_relation(owner_id, "HAS_MEMBER", mem_id, norm_path(source_path), ev, 1.0)
            for typ in re.findall(r"\bCAT[A-Za-z0-9_]+\b|\bCATICGM[A-Za-z0-9_]+\b|\bHybridShape[A-Za-z0-9_]+\b", f"{sig} {desc}"):
                # Type-reference edges are resolved after all symbols are loaded.
                pass


def parse_refman_pages(caadoc: Path, store: Store):
    for root in [caadoc / "Doc" / "generated" / "refman", caadoc / "Doc" / "generated" / "interfaces"]:
        if not root.exists():
            continue
        for path in root.rglob("*.htm"):
            if "_index" in path.parts:
                continue
            raw = read_text(path)
            plain = strip_tags(raw)
            framework = path.parent.name
            layer = infer_layer(path, raw)
            ent_id, kind, name = source_entity_id_from_path(path, layer, framework, raw)
            summary = page_summary(raw, plain)
            include_file = extract_include_file(raw)
            ev = store.add_evidence(path, "", summary or name, "refman-html", "official-refman")
            tags = infer_tags(framework, kind, name, summary, include_file, h1_title(raw))
            entity = {
                "id": ent_id,
                "kind": kind,
                "name": name,
                "framework": framework,
                "layer": layer,
                "source_path": norm_path(path),
                "anchor": "",
                "signature": "",
                "summary": summary,
                "tags": tags,
                "evidence_ids": [ev],
            }
            if include_file:
                entity["include_file"] = include_file
            store.add_entity(entity)
            parse_member_index(raw, ent_id, name, path, store)
            chunk_text = compact(plain, 3000)
            if chunk_text:
                store.add_chunk(path, "refman", [framework, name], chunk_text, symbols=[name], evidence_rank="official-refman")


def parse_global_function_indexes(caadoc: Path, store: Store):
    candidates = [
        caadoc / "Doc" / "generated" / "refman" / "_index" / "GlobalFunctionIdx.htm",
        caadoc / "Doc" / "generated" / "interfaces" / "_index" / "CAAMethodIdx.htm",
        caadoc / "Doc" / "generated" / "interfaces" / "_index" / "CAAPropertyIdx.htm",
    ]
    pattern = re.compile(
        r'(?is)<dt><a href="(?P<href>[^"]+)"(?:\s+onmouseover="self.status=\'(?P<sig>[^\']*)\';return true;")?[^>]*>\s*<b>(?P<name>[^<]+)</b></a>\s*(?P<label>[^<]*)(?:<dd>(?P<desc>.*?))?(?=\s*<dt>|$)'
    )
    for idx in candidates:
        if not idx.exists():
            continue
        raw = read_text(idx)
        layer = infer_layer(idx)
        for m in pattern.finditer(raw):
            href = html.unescape(m.group("href"))
            target = (idx.parent / href).resolve()
            framework = target.parent.name
            name = strip_tags(m.group("name"))
            sig = compact(strip_tags(m.group("sig") or ""), 500)
            desc = compact(strip_tags(m.group("desc") or ""), 700)
            label = strip_tags(m.group("label") or "")
            kind = "function" if "function" in label.lower() or "function_" in target.name else "indexed-member"
            ent_id = stable_id("index-symbol", layer, framework, kind, name, sig, norm_path(target))
            ev = store.add_evidence(target, "", f"{name} {sig} {desc}", idx.name, "official-index")
            store.add_entity({
                "id": ent_id,
                "kind": kind,
                "name": name,
                "framework": framework,
                "layer": layer,
                "source_path": norm_path(target),
                "anchor": "",
                "signature": sig,
                "summary": desc,
                "tags": infer_tags(framework, name, sig, desc),
                "evidence_ids": [ev],
            })


def extract_headed_sections(raw: str) -> list[tuple[list[str], str, str]]:
    matches = list(re.finditer(r"(?is)<h([1-4])[^>]*>(.*?)</h\1>", raw))
    if not matches:
        return [([h1_title(raw) or "Document"], "", strip_tags(raw))]
    sections = []
    stack: list[tuple[int, str]] = []
    for i, m in enumerate(matches):
        level = int(m.group(1))
        title = strip_tags(m.group(2)) or "Untitled"
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = strip_tags(raw[start:end])
        sections.append(([t for _, t in stack], title, body))
    return sections


def parse_online_docs(caadoc: Path, store: Store):
    root = caadoc / "Doc" / "online"
    if not root.exists():
        return
    for path in root.rglob("*.htm"):
        raw = read_text(path)
        plain = strip_tags(raw)
        rel_parts = path.relative_to(root).parts
        doc_kind = "use_case" if any("UseCases" in p for p in rel_parts) else "concept"
        title = h1_title(raw) or path.stem
        doc_id = stable_id("doc", doc_kind, norm_path(path))
        ev = store.add_evidence(path, "", title, "online-html", "official-online")
        store.add_entity({
            "id": doc_id,
            "kind": doc_kind,
            "name": title,
            "framework": rel_parts[0] if rel_parts else "",
            "layer": "Online-concept",
            "source_path": norm_path(path),
            "anchor": "",
            "signature": "",
            "summary": compact(plain, 900),
            "tags": infer_tags(title, plain[:2000]),
            "evidence_ids": [ev],
        })
        for heading_path, heading, body in extract_headed_sections(raw):
            if len(body) < 40:
                continue
            symbols = sorted(set(re.findall(r"\bCAT[A-Za-z0-9_]+\b|\bCATICGM[A-Za-z0-9_]+\b|\bHybridShape[A-Za-z0-9_]+\b|\bAddNew[A-Za-z0-9_]+\b", body)))
            cid = store.add_chunk(path, doc_kind, heading_path, body, symbols=symbols, evidence_rank="official-online")
            if cid:
                store.add_relation(doc_id, "HAS_EVIDENCE", cid, norm_path(path), ev, 0.9)


def parse_examples(caadoc: Path, store: Store):
    exts = {".cpp", ".h", ".hpp", ".cxx"}
    for edu in caadoc.glob("*.edu"):
        for path in edu.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in exts:
                continue
            text = read_text(path)
            rel = path.relative_to(caadoc)
            name = str(rel)
            symbols = sorted(set(re.findall(r"\bCAT[A-Za-z0-9_]+\b|\bCATICGM[A-Za-z0-9_]+\b|\bHybridShape[A-Za-z0-9_]+\b|\bAddNew[A-Za-z0-9_]+\b|::CATCreate[A-Za-z0-9_]+\b", text)))
            includes = re.findall(r'#include\s+[<"]([^>"]+)[>"]', text)
            ex_id = stable_id("example", norm_path(path))
            ev = store.add_evidence(path, "", f"{name} mentions {', '.join(symbols[:12])}", "edu-source", "official-example")
            store.add_entity({
                "id": ex_id,
                "kind": "example",
                "name": path.name,
                "framework": edu.name,
                "layer": "CAA-example",
                "source_path": norm_path(path),
                "anchor": "",
                "signature": "",
                "summary": compact(f"Example source {name}. Includes: {', '.join(includes[:20])}. Symbols: {', '.join(symbols[:40])}.", 900),
                "tags": infer_tags(name, text[:2000]),
                "evidence_ids": [ev],
                "includes": includes[:80],
            })
            store.add_chunk(path, "example-source", [edu.name, path.name], text[:8000], symbols=symbols[:120], evidence_rank="official-example")


def resolve_symbol_mentions(store: Store):
    by_name: dict[str, list[str]] = defaultdict(list)
    for eid, ent in store.entities.items():
        name = ent.get("name", "")
        if name and re.match(r"^[A-Za-z_][A-Za-z0-9_]+$", name):
            by_name[name].append(eid)

    for eid, ent in list(store.entities.items()):
        text = " ".join(str(ent.get(k, "")) for k in ("signature", "summary", "name"))
        names = set(re.findall(r"\bCAT[A-Za-z0-9_]+\b|\bCATICGM[A-Za-z0-9_]+\b|\bHybridShape[A-Za-z0-9_]+\b", text))
        for name in list(names)[:60]:
            for dst in by_name.get(name, [])[:4]:
                store.add_relation(eid, "TYPE_REF", dst, ent.get("source_path", ""), ent.get("evidence_ids", [""])[0] if ent.get("evidence_ids") else "", 0.65)

    for cid, chunk in list(store.chunks.items()):
        doc_kind = chunk.get("doc_kind", "")
        if doc_kind == "example-source":
            relation_type = "DEMONSTRATES"
        elif doc_kind in {"concept", "use_case"}:
            relation_type = "EXPLAINS"
        else:
            relation_type = "MENTIONS"
        for name in chunk.get("symbols", [])[:80]:
            for dst in by_name.get(name, [])[:3]:
                store.add_relation(cid, relation_type, dst, chunk.get("source_path", ""), "", 0.55)


def build_vectors_for_entities(store: Store):
    for eid, ent in store.entities.items():
        text = " ".join([
            ent.get("name", ""),
            ent.get("kind", ""),
            ent.get("framework", ""),
            ent.get("signature", ""),
            ent.get("summary", ""),
            " ".join(ent.get("tags", [])),
        ])
        store.vectors[eid] = {"id": eid, "kind": "entity", **sparse_hash_vector(text)}


def _write_sqlite_file(store: Store, projection: Projection, db_path: Path):
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE entities (
          id TEXT PRIMARY KEY,
          kind TEXT,
          name TEXT,
          framework TEXT,
          layer TEXT,
          source_path TEXT,
          anchor TEXT,
          signature TEXT,
          summary TEXT,
          tags_json TEXT,
          evidence_json TEXT,
          extra_json TEXT
        );
        CREATE TABLE relations (
          src_id TEXT,
          type TEXT,
          dst_id TEXT,
          confidence REAL,
          source_path TEXT,
          evidence_id TEXT
        );
        CREATE TABLE chunks (
          id TEXT PRIMARY KEY,
          source_path TEXT,
          doc_kind TEXT,
          heading_path_json TEXT,
          text TEXT,
          symbols_json TEXT,
          capabilities_json TEXT,
          evidence_rank TEXT
        );
        CREATE TABLE evidence (
          id TEXT PRIMARY KEY,
          source_path TEXT,
          anchor TEXT,
          quote_or_span TEXT,
          extraction_method TEXT,
          trust_level TEXT
        );
        """
    )
    for ent in store.entities.values():
        known = {"id", "kind", "name", "framework", "layer", "source_path", "anchor", "signature", "summary", "tags", "evidence_ids"}
        extra = {k: v for k, v in ent.items() if k not in known}
        cur.execute(
            "INSERT OR REPLACE INTO entities VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ent.get("id"), ent.get("kind"), ent.get("name"), ent.get("framework"),
                ent.get("layer"), ent.get("source_path"), ent.get("anchor"),
                ent.get("signature"), ent.get("summary"),
                json.dumps(ent.get("tags", []), ensure_ascii=False),
                json.dumps(ent.get("evidence_ids", []), ensure_ascii=False),
                json.dumps(extra, ensure_ascii=False),
            ),
        )
    cur.executemany(
        "INSERT INTO relations VALUES (?,?,?,?,?,?)",
        [(r["src_id"], r["type"], r["dst_id"], r["confidence"], r["source_path"], r["evidence_id"]) for r in store.relations],
    )
    for ch in store.chunks.values():
        cur.execute(
            "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?,?)",
            (
                ch["id"], ch["source_path"], ch["doc_kind"],
                json.dumps(ch["heading_path"], ensure_ascii=False), ch["text"],
                json.dumps(ch["symbols"], ensure_ascii=False),
                json.dumps(ch["capabilities"], ensure_ascii=False),
                ch["evidence_rank"],
            ),
        )
    cur.executemany(
        "INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?)",
        [(e["id"], e["source_path"], e["anchor"], e["quote_or_span"], e["extraction_method"], e["trust_level"]) for e in store.evidence.values()],
    )
    try:
        cur.executescript(
            """
            CREATE VIRTUAL TABLE entities_fts USING fts5(id UNINDEXED, name, kind, framework, summary, signature, tags);
            CREATE VIRTUAL TABLE chunks_fts USING fts5(id UNINDEXED, heading, text, symbols, capabilities);
            """
        )
        cur.executemany(
            "INSERT INTO entities_fts VALUES (?,?,?,?,?,?,?)",
            [
                (
                    e["id"], e.get("name", ""), e.get("kind", ""), e.get("framework", ""),
                    e.get("summary", ""), e.get("signature", ""), " ".join(e.get("tags", [])),
                )
                for e in store.entities.values()
            ],
        )
        cur.executemany(
            "INSERT INTO chunks_fts VALUES (?,?,?,?,?)",
            [
                (
                    c["id"], " > ".join(c.get("heading_path", [])), c.get("text", ""),
                    " ".join(c.get("symbols", [])), " ".join(c.get("capabilities", [])),
                )
                for c in store.chunks.values()
            ],
        )
    except sqlite3.OperationalError:
        # Some SQLite builds omit FTS5. The JSONL assets are still complete.
        pass
    create_projection_tables(cur, projection)
    foreign_key_errors = cur.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise ValueError(f"Projection foreign key check failed: {foreign_key_errors[:5]}")
    conn.commit()
    conn.close()


def write_sqlite(store: Store, projection: Projection):
    db_path = store.out / "data" / "manual.sqlite"
    temp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    try:
        _write_sqlite_file(store, projection, temp_path)
        os.replace(temp_path, db_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def write_report(caadoc: Path, store: Store, projection: Projection, elapsed: float):
    report = store.out / "reports" / "summary.md"
    kind_counts = Counter(e["kind"] for e in store.entities.values())
    layer_counts = Counter(e["layer"] for e in store.entities.values())
    framework_counts = Counter(e["framework"] for e in store.entities.values() if e.get("framework"))
    rel_counts = Counter(r["type"] for r in store.relations)
    lines = [
        "# CAA AI Manual Build Summary",
        "",
        f"- Source: `{norm_path(caadoc)}`",
        f"- Output: `{norm_path(store.out)}`",
        f"- Elapsed seconds: `{elapsed:.1f}`",
        f"- Entities: `{len(store.entities)}`",
        f"- Relations: `{len(store.relations)}`",
        f"- Chunks: `{len(store.chunks)}`",
        f"- Evidence records: `{len(store.evidence)}`",
        f"- Vectors: `{len(store.vectors)}`",
        f"- Catalog nodes: `{len(projection.catalog_nodes)}`",
        f"- API pages: `{len(projection.api_pages)}`",
        f"- API members: `{len(projection.api_members)}`",
        f"- Chinese aliases: `{len(projection.api_aliases)}`",
        "",
        "## Entity Kinds",
        "",
    ]
    lines += [f"- `{k}`: {v}" for k, v in kind_counts.most_common(40)]
    lines += ["", "## Layers", ""]
    lines += [f"- `{k}`: {v}" for k, v in layer_counts.most_common()]
    lines += ["", "## Top Frameworks", ""]
    lines += [f"- `{k}`: {v}" for k, v in framework_counts.most_common(30)]
    lines += ["", "## Relation Types", ""]
    lines += [f"- `{k}`: {v}" for k, v in rel_counts.most_common(30)]
    lines += [
        "",
        "## Verification Commands",
        "",
        "- `python tools/caa_manual_cli.py status`",
        "- `python tools/caa_manual_cli.py search CATGeoFactory --limit 3`",
        "- `python tools/caa_manual_cli.py get-api CATGeoFactory --include members`",
    ]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_manifest(caadoc: Path, store: Store, projection: Projection, elapsed: float):
    htm = list(caadoc.rglob("*.htm"))
    toc = list(caadoc.rglob("*Toc.xml"))
    cpp = list(caadoc.rglob("*.cpp"))
    manifest = {
        "source": norm_path(caadoc),
        "output": norm_path(store.out),
        "built_at_epoch": int(time.time()),
        "elapsed_seconds": round(elapsed, 3),
        "source_counts": {
            "htm": len(htm),
            "toc_xml": len(toc),
            "cpp": len(cpp),
        },
        "artifact_counts": {
            "entities": len(store.entities),
            "relations": len(store.relations),
            "chunks": len(store.chunks),
            "evidence": len(store.evidence),
            "vectors": len(store.vectors),
            **projection.counts(),
        },
    }
    (store.out / "data" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def build(args: argparse.Namespace) -> int:
    if not args.caadoc:
        print("CAADoc root not configured. Pass --caadoc or set CAA_CAADOC_ROOT.", file=sys.stderr)
        return 2
    caadoc = Path(args.caadoc)
    out = Path(args.out)
    if not caadoc.exists():
        print(f"CAADoc root not found: {caadoc}", file=sys.stderr)
        return 2
    configure_logical_roots(caadoc, out)
    (out / "data").mkdir(parents=True, exist_ok=True)
    (out / "reports").mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    store = Store(out=out)

    print("Scanning TOC and JS symbol indexes...")
    parse_toc_files(caadoc, store)
    parse_js_links(caadoc, store)
    print(f"  symbols after indexes: {len(store.entities)}")

    print("Parsing refman and Automation pages...")
    parse_refman_pages(caadoc, store)
    parse_global_function_indexes(caadoc, store)
    print(f"  entities after refman: {len(store.entities)}")

    print("Parsing online concept/use-case pages...")
    parse_online_docs(caadoc, store)
    print(f"  chunks after online: {len(store.chunks)}")

    print("Parsing .edu example sources...")
    parse_examples(caadoc, store)
    print(f"  entities after examples: {len(store.entities)}")

    print("Resolving symbol mentions and vectors...")
    resolve_symbol_mentions(store)
    build_vectors_for_entities(store)

    print("Building agent query projection...")
    catalog = load_catalog(out / "config" / "catalog_zh.yaml")
    projection = build_projection(store, catalog, caadoc)
    print(
        "  projection: "
        f"{len(projection.catalog_nodes)} catalog nodes, "
        f"{len(projection.api_pages)} pages, "
        f"{len(projection.api_members)} members"
    )

    print("Writing JSONL assets...")
    store.write_jsonl("entities.jsonl", store.entities.values())
    store.write_jsonl("relations.jsonl", store.relations)
    store.write_jsonl("chunks.jsonl", store.chunks.values())
    store.write_jsonl("evidence.jsonl", store.evidence.values())
    store.write_jsonl("vectors.jsonl", store.vectors.values())
    store.write_jsonl("catalog_nodes.jsonl", projection.catalog_nodes)
    store.write_jsonl("api_pages.jsonl", projection.api_pages)
    store.write_jsonl("api_members.jsonl", projection.api_members)
    store.write_jsonl("api_aliases.jsonl", projection.api_aliases)

    print("Writing SQLite database...")
    write_sqlite(store, projection)
    elapsed = time.time() - t0
    write_manifest(caadoc, store, projection, elapsed)
    write_report(caadoc, store, projection, elapsed)
    print(f"Done in {elapsed:.1f}s")
    print(f"Output: {out}")
    return 0


def query(args: argparse.Namespace) -> int:
    db = Path(args.db)
    q = args.query
    if not db.exists():
        print(f"Database not found: {db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    print(f"# Entity hits for: {q}")
    exact_rows = cur.execute(
        """
        SELECT id, kind, name, framework, layer, signature, summary, source_path
        FROM entities
        WHERE name = ? OR name LIKE ? OR signature LIKE ?
        ORDER BY
          CASE WHEN name = ? THEN 0 WHEN name LIKE ? THEN 1 ELSE 2 END,
          length(summary) DESC
        LIMIT ?
        """,
        (q, f"%{q}%", f"%{q}%", q, f"{q}%", args.limit),
    ).fetchall()
    seen = set()
    rows = list(exact_rows)
    seen.update(r["id"] for r in rows)
    try:
        fts_rows = cur.execute(
            """
            SELECT e.id, e.kind, e.name, e.framework, e.layer, e.signature, e.summary, e.source_path
            FROM entities_fts f
            JOIN entities e ON e.id = f.id
            WHERE entities_fts MATCH ?
            ORDER BY bm25(entities_fts)
            LIMIT ?
            """,
            (q, args.limit * 2),
        ).fetchall()
    except sqlite3.OperationalError:
        fts_rows = cur.execute(
            """
            SELECT id, kind, name, framework, layer, signature, summary, source_path
            FROM entities
            WHERE name LIKE ? OR summary LIKE ? OR signature LIKE ?
            LIMIT ?
            """,
            (f"%{q}%", f"%{q}%", f"%{q}%", args.limit * 2),
        ).fetchall()
    for row in fts_rows:
        if row["id"] not in seen:
            rows.append(row)
            seen.add(row["id"])
        if len(rows) >= args.limit:
            break
    for r in rows:
        print(f"- [{r['kind']}] {r['name']} ({r['framework']} / {r['layer']})")
        if r["signature"]:
            print(f"  sig: {r['signature']}")
        if r["summary"]:
            print(f"  {compact(r['summary'], 220)}")
        print(f"  src: {r['source_path']}")

    print(f"\n# Chunk hits for: {q}")
    try:
        rows = cur.execute(
            """
            SELECT c.id, c.doc_kind, c.heading_path_json, c.text, c.source_path
            FROM chunks_fts f
            JOIN chunks c ON c.id = f.id
            WHERE chunks_fts MATCH ?
            ORDER BY bm25(chunks_fts)
            LIMIT ?
            """,
            (q, args.limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = cur.execute(
            """
            SELECT id, doc_kind, heading_path_json, text, source_path
            FROM chunks
            WHERE text LIKE ?
            LIMIT ?
            """,
            (f"%{q}%", args.limit),
        ).fetchall()
    for r in rows:
        heading = " > ".join(json.loads(r["heading_path_json"]))
        print(f"- [{r['doc_kind']}] {heading}")
        print(f"  {compact(r['text'], 260)}")
        print(f"  src: {r['source_path']}")
    conn.close()
    return 0


def connect_db(db: str | Path) -> sqlite3.Connection:
    db = Path(db)
    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_entity_ids(cur: sqlite3.Cursor, term: str, limit: int = 12) -> list[str]:
    exact = cur.execute("SELECT id FROM entities WHERE id = ? OR name = ? LIMIT ?", (term, term, limit)).fetchall()
    ids = [r["id"] for r in exact]
    if ids:
        return ids[:limit]
    fuzzy = cur.execute(
        "SELECT id FROM entities WHERE name LIKE ? OR signature LIKE ? OR summary LIKE ? LIMIT ?",
        (f"%{term}%", f"%{term}%", f"%{term}%", limit * 2),
    ).fetchall()
    for r in fuzzy:
        if r["id"] not in ids:
            ids.append(r["id"])
        if len(ids) >= limit:
            break
    return ids


def print_entity_row(r: sqlite3.Row, prefix: str = "-"):
    print(f"{prefix} [{r['kind']}] {r['name']} ({r['framework']} / {r['layer']})")
    if r["signature"]:
        print(f"  sig: {r['signature']}")
    if r["summary"]:
        print(f"  {compact(r['summary'], 260)}")
    print(f"  id: {r['id']}")
    print(f"  src: {r['source_path']}")


def command_symbol(args: argparse.Namespace) -> int:
    try:
        conn = connect_db(args.db)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    cur = conn.cursor()
    ids = resolve_entity_ids(cur, args.symbol, args.limit)
    if not ids:
        print(f"No symbol found for {args.symbol!r}")
        return 1
    for eid in ids:
        row = cur.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
        print_entity_row(row)
        members = cur.execute(
            """
            SELECT e.kind, e.name, e.signature, e.summary, e.source_path
            FROM relations r JOIN entities e ON e.id = r.dst_id
            WHERE r.src_id = ? AND r.type = 'HAS_MEMBER'
            LIMIT 12
            """,
            (eid,),
        ).fetchall()
        if members:
            print("  members:")
            for m in members:
                sig = f" {m['signature']}" if m["signature"] else ""
                print(f"    - [{m['kind']}] {m['name']}{sig}: {compact(m['summary'], 120)}")
        print()
    conn.close()
    return 0


def command_neighbors(args: argparse.Namespace) -> int:
    try:
        conn = connect_db(args.db)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    cur = conn.cursor()
    ids = resolve_entity_ids(cur, args.symbol, 4)
    for eid in ids:
        row = cur.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
        print_entity_row(row)
        rels = cur.execute(
            """
            SELECT 'out' AS dir, r.type, e.kind, e.name, e.id, r.confidence
            FROM relations r JOIN entities e ON e.id = r.dst_id
            WHERE r.src_id = ?
            UNION ALL
            SELECT 'in' AS dir, r.type, e.kind, e.name, e.id, r.confidence
            FROM relations r JOIN entities e ON e.id = r.src_id
            WHERE r.dst_id = ?
            LIMIT ?
            """,
            (eid, eid, args.limit),
        ).fetchall()
        for r in rels:
            print(f"  {r['dir']} {r['type']} -> [{r['kind']}] {r['name']} ({r['confidence']:.2f})")
        print()
    conn.close()
    return 0


def command_examples(args: argparse.Namespace) -> int:
    try:
        conn = connect_db(args.db)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    cur = conn.cursor()
    ids = resolve_entity_ids(cur, args.symbol, 8)
    if not ids:
        print(f"No symbol found for {args.symbol!r}")
        return 1
    placeholders = ",".join("?" for _ in ids)
    rows = cur.execute(
        f"""
        SELECT DISTINCT c.doc_kind, c.heading_path_json, c.text, c.source_path
        FROM relations r
        JOIN chunks c ON c.id = r.src_id
        WHERE r.type = 'DEMONSTRATES' AND r.dst_id IN ({placeholders})
        LIMIT ?
        """,
        (*ids, args.limit),
    ).fetchall()
    for r in rows:
        print(f"- {' > '.join(json.loads(r['heading_path_json']))}")
        print(f"  {compact(r['text'], 220)}")
        print(f"  src: {r['source_path']}")
    conn.close()
    return 0


def command_evidence(args: argparse.Namespace) -> int:
    try:
        conn = connect_db(args.db)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    cur = conn.cursor()
    ids = resolve_entity_ids(cur, args.term, args.limit)
    evidence_ids: list[str] = []
    for eid in ids:
        row = cur.execute("SELECT evidence_json FROM entities WHERE id = ?", (eid,)).fetchone()
        if row:
            evidence_ids.extend(json.loads(row["evidence_json"] or "[]"))
    seen = set()
    for ev_id in evidence_ids[: args.limit * 3]:
        if ev_id in seen:
            continue
        seen.add(ev_id)
        r = cur.execute("SELECT * FROM evidence WHERE id = ?", (ev_id,)).fetchone()
        if r:
            print(f"- {r['id']} [{r['trust_level']} / {r['extraction_method']}]")
            print(f"  {r['quote_or_span']}")
            print(f"  src: {r['source_path']}#{r['anchor']}")
    if not seen:
        return query(argparse.Namespace(db=args.db, query=args.term, limit=args.limit))
    conn.close()
    return 0


def command_capability(args: argparse.Namespace) -> int:
    # Capability search is intentionally the same hybrid query, but the name makes
    # the intended usage explicit for agents.
    return query(argparse.Namespace(db=args.db, query=args.text, limit=args.limit))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build/query the CATIA CAA AI manual static index.")
    sub = parser.add_subparsers(dest="cmd")
    b = sub.add_parser("build", help="Build static JSONL and SQLite assets.")
    b.add_argument("--caadoc", default=DEFAULT_CAADOC)
    b.add_argument("--out", default=str(DEFAULT_OUT))
    b.set_defaults(func=build)
    q = sub.add_parser("query", help="Query the generated SQLite database.")
    q.add_argument("query")
    q.add_argument("--db", default=str(DEFAULT_DB))
    q.add_argument("--limit", type=int, default=8)
    q.set_defaults(func=query)
    s = sub.add_parser("symbol", help="Exact/fuzzy symbol lookup with member preview.")
    s.add_argument("symbol")
    s.add_argument("--db", default=str(DEFAULT_DB))
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=command_symbol)
    n = sub.add_parser("neighbors", help="Show incoming/outgoing graph neighbors for a symbol.")
    n.add_argument("symbol")
    n.add_argument("--db", default=str(DEFAULT_DB))
    n.add_argument("--limit", type=int, default=30)
    n.set_defaults(func=command_neighbors)
    ex = sub.add_parser("examples", help="Find .edu source chunks that demonstrate a symbol.")
    ex.add_argument("symbol")
    ex.add_argument("--db", default=str(DEFAULT_DB))
    ex.add_argument("--limit", type=int, default=8)
    ex.set_defaults(func=command_examples)
    ev = sub.add_parser("evidence", help="Print evidence records for a symbol or query.")
    ev.add_argument("term")
    ev.add_argument("--db", default=str(DEFAULT_DB))
    ev.add_argument("--limit", type=int, default=8)
    ev.set_defaults(func=command_evidence)
    cap = sub.add_parser("capability", help="Hybrid search for a capability or task phrase.")
    cap.add_argument("text")
    cap.add_argument("--db", default=str(DEFAULT_DB))
    cap.add_argument("--limit", type=int, default=8)
    cap.set_defaults(func=command_capability)
    args = parser.parse_args(argv)
    if not args.cmd:
        args = parser.parse_args(["build"])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
