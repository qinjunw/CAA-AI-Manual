"""Exercise the installed entry points from outside the project directory."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReleaseLayoutTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.project = self.work / "manual project"
        shutil.copytree(ROOT / "tools", self.project / "tools", ignore=shutil.ignore_patterns("__pycache__"))
        (self.project / "config").mkdir()
        (self.project / "config/releases.json").write_text(json.dumps({
            "default_release": "R21",
            "releases": {
                name: {"source_baseline": f"CATIA V5{name} CAADoc", "version_policy": "Fixture only."}
                for name in ("R21", "R98")
            },
        }), encoding="utf-8")
        (self.project / "config/catalog_zh.yaml").write_text(
            '{"schema_version":1,"source_baseline":"CATIA V5R21 CAADoc"}', encoding="utf-8")
        self.source = self.work / "B98/CAADoc"
        refman = self.source / "Doc/generated/refman"
        (refman / "_index").mkdir(parents=True)
        (refman / "_index/jsTree.js").write_text("// fixture", encoding="utf-8")
        (self.source / "Doc/generated/interfaces").mkdir()
        (refman / "Test").mkdir()
        (refman / "Test/class_CATWidget_1.htm").write_text(
            "<h1>CATWidget</h1><p>Widget fixture contents.</p>", encoding="utf-8")
        (self.project / ".env").write_text("CAA_CAADOC_ROOT=unused-B21-source\n", encoding="utf-8")
        self.profile = self.project / ".env.r98"
        self.profile.write_text(f"CAA_CAADOC_ROOT={self.source}\n", encoding="utf-8")
        self.full = self.work / "private build"

    def script(self, script, *args, success=True, stdin=None, env=None):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("CAA_")}
        process = subprocess.run(
            [sys.executable, "-X", "utf8", str(self.project / "tools" / script), *map(str, args)],
            cwd=self.work, env={**environment, **(env or {})}, input=stdin,
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        if success:
            self.assertEqual(process.returncode, 0, process.stderr)
        else:
            self.assertNotEqual(process.returncode, 0, process.stdout)
        return process

    def test_new_registry_entry_builds_exports_and_queries_without_a_python_edit(self):
        self.script("build_caa_ai_manual.py", "build", "--release", "R98", "--out", self.full)
        source_db = self.full / "data/manual.sqlite"
        self.script("export_public_index.py", "--release", "R98", "--source-db", source_db)
        database = self.project / "data/r98/manual.sqlite"
        self.assertTrue(database.is_file())
        manifest = json.loads(database.with_name("manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_release"], "R98")
        self.assertEqual(manifest["source_counts"]["htm"], 1)

        def cli(*args):
            # An ambient R21 client must not override an explicitly selected release.
            result = self.script("caa_manual_cli.py", "--release", "R98", *args,
                                 env={"CAA_AI_MANUAL_DB": "wrong.sqlite", "CAA_CAADOC_ROOT": "wrong-source"})
            return json.loads(result.stdout)

        self.assertEqual(Path(cli("status")["db_path"]), database)
        self.assertEqual(cli("read-source", "CATWidget")["status"], "ok")
        self.assertEqual(Path(cli("index-sources")["cache_path"]), self.project / "cache/r98/source-search.sqlite")
        self.assertGreater(cli("search-source", "Widget")["total_count"], 0)
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "caa_status", "arguments": {}}}
        mcp = self.script("caa_manual_mcp_server.py", "--release", "R98", stdin=json.dumps(call) + "\n")
        self.assertEqual(json.loads(mcp.stdout)["result"]["structuredContent"]["metadata"]["source_release"], "R98")
        self.profile.unlink()
        self.assertEqual(cli("status")["caadoc_root"], "")
        self.assertTrue(cli("search", "CATWidget")["candidate_groups"])

        # Both shorthand and explicit destinations must protect another release.
        r21 = self.project / "data/r21/manual.sqlite"
        r21.parent.mkdir()
        r21.write_bytes(b"preserve this other release")
        for destination in (("--release", "R21"), ("--output-db", r21)):
            self.script("export_public_index.py", "--source-db", source_db, *destination, success=False)
            self.assertEqual(r21.read_bytes(), b"preserve this other release")
            self.assertFalse(r21.with_name("manifest.json").exists())

    def test_missing_legacy_distribution_path_resolves_only_to_r21(self):
        old = self.project / "data/manual.sqlite"
        canonical = self.project / "data/r21"
        canonical.mkdir(parents=True)
        # An earlier MCP config can still point to the retired file path.
        shipped = ROOT / "data/r21/manual.sqlite"
        shutil.copyfile(shipped, canonical / "manual.sqlite")
        result = self.script("caa_manual_cli.py", "--db", old, "status")
        self.assertEqual(Path(json.loads(result.stdout)["db_path"]), canonical / "manual.sqlite")
        self.assertFalse(old.exists())
        self.script("caa_manual_cli.py", "--db", self.work / "other/data/manual.sqlite", "status", success=False)
        self.script("caa_manual_cli.py", "--env-file", self.work / "absent-profile", "status", success=False)

        # A default query should use the same local source as an explicit R21 query.
        (self.project / ".env.r21").write_text("CAA_CAADOC_ROOT=source for default\n", encoding="utf-8")
        for selection in ((), ("--release", "R21")):
            result = self.script("caa_manual_cli.py", *selection, "status")
            self.assertEqual(Path(json.loads(result.stdout)["caadoc_root"]), self.project / "source for default")
