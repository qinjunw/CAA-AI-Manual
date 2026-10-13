import hashlib
import io
import json
import sqlite3
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools import build_caa_ai_manual as builder
from tools.caa_manual_projection import SOURCE_BASELINE, release_metadata


class BuildReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.project = self.root / "project"
        (self.project / "config").mkdir(parents=True)
        (self.project / "config/catalog_zh.yaml").write_text(
            json.dumps({"schema_version": 1, "source_baseline": SOURCE_BASELINE}),
            encoding="utf-8",
        )
        for name, value in (("PROJECT_ROOT", self.project), ("LOGICAL_ROOTS", [])):
            patcher = patch.object(builder, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_source(self, release="R33"):
        source = self.root / ("B" + release[1:]) / "CAADoc"
        refman = source / "Doc/generated/refman"
        (refman / "_index").mkdir(parents=True)
        (source / "Doc/generated/interfaces").mkdir(parents=True)
        tree_content = b'fatherLink["interface_CATWidget_1"]="";'
        (refman / "_index/jsTree.js").write_bytes(tree_content)
        (refman / "TestFramework").mkdir()
        (refman / "TestFramework/interface_CATWidget_1.htm").write_text(
            """<html><h1>CATWidget interface</h1>
            <p>Synthetic API documentation for build tests.</p>
            <h2>Method Index</h2><dl>
            <dt>o <a href="#Run()"><b>Run</b></a>()
            <dd>Runs the synthetic widget.</dl>
            <h2>Methods</h2><a name="Run()"></a><p>int Run()</p></html>""",
            encoding="utf-8",
        )
        return source, tree_content

    def test_cli_release_mismatch_is_rejected_before_parsing_or_output(self):
        for installed, requested in (("R33", "R21"), ("R21", "R33")):
            with self.subTest(installed=installed, requested=requested):
                source, _ = self.make_source(installed)
                output = self.root / ("rejected-" + installed)
                errors = io.StringIO()
                with patch.object(builder, "parse_toc_files") as parse, redirect_stderr(errors):
                    result = builder.main([
                        "build", "--caadoc", str(source), "--out", str(output),
                        "--release", requested,
                    ])
                self.assertEqual(result, 2)
                self.assertIn("does not match the installation directory", errors.getvalue())
                parse.assert_not_called()
                self.assertFalse(output.exists())

    def test_missing_required_structure_is_rejected_before_output(self):
        for missing in ("refman", "interfaces", "jsTree.js"):
            with self.subTest(missing=missing):
                source = self.root / missing / "B33/CAADoc"
                source.mkdir(parents=True)
                if missing != "refman":
                    refman = source / "Doc/generated/refman"
                    refman.mkdir(parents=True)
                    if missing != "jsTree.js":
                        (refman / "_index").mkdir()
                        (refman / "_index/jsTree.js").write_text("", encoding="utf-8")
                if missing != "interfaces":
                    (source / "Doc/generated/interfaces").mkdir(parents=True)
                output = self.root / ("missing-" + missing)
                errors = io.StringIO()
                with patch.object(builder, "parse_toc_files") as parse, redirect_stderr(errors):
                    result = builder.main([
                        "build", "--caadoc", str(source), "--out", str(output), "--release", "R33",
                    ])
                self.assertEqual(result, 2)
                self.assertIn("missing required inputs", errors.getvalue())
                self.assertIn(missing, errors.getvalue())
                parse.assert_not_called()
                self.assertFalse(output.exists())

    def test_r33_default_and_explicit_repository_outputs_are_rejected(self):
        source, _ = self.make_source()
        for name, output_args, output in (
            ("default", [], self.project),
            ("root", ["--out", str(self.project)], self.project),
            ("nested", ["--out", str(self.project / "private")], self.project / "private"),
        ):
            with self.subTest(output=name):
                database = output / "data/manual.sqlite"
                database.parent.mkdir(parents=True, exist_ok=True)
                if not database.exists():
                    with closing(sqlite3.connect(database)) as connection, connection:
                        connection.execute("CREATE TABLE preserved (value TEXT)")
                        connection.execute("INSERT INTO preserved VALUES ('public index')")
                original = database.read_bytes()
                errors = io.StringIO()
                with patch.object(builder, "DEFAULT_OUT", self.project), \
                        patch.object(builder, "parse_toc_files") as parse, redirect_stderr(errors):
                    result = builder.main([
                        "build", "--caadoc", str(source), "--release", "R33", *output_args,
                    ])
                self.assertEqual(result, 2)
                self.assertIn("outside the project", errors.getvalue())
                parse.assert_not_called()
                self.assertEqual(database.read_bytes(), original)
                self.assertFalse((output / "reports").exists())

    def test_existing_different_or_unknown_release_database_is_not_overwritten(self):
        sources = {release: self.make_source(release)[0] for release in ("R21", "R33")}
        for name, requested, metadata in (
            ("explicit-r21", "R33", release_metadata("R21")),
            ("legacy-r21", "R33", {"source_baseline": SOURCE_BASELINE}),
            ("unknown", "R33", {}),
            ("explicit-r33", "R21", release_metadata("R33")),
        ):
            with self.subTest(existing=name, requested=requested):
                output = self.root / ("existing-" + name)
                database = output / "data/manual.sqlite"
                database.parent.mkdir(parents=True)
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute("CREATE TABLE projection_metadata (key TEXT PRIMARY KEY, value TEXT)")
                    connection.executemany("INSERT INTO projection_metadata VALUES (?,?)", metadata.items())
                original = database.read_bytes()
                errors = io.StringIO()
                with patch.object(builder, "parse_toc_files") as parse, redirect_stderr(errors):
                    result = builder.main([
                        "build", "--caadoc", str(sources[requested]), "--out", str(output),
                        "--release", requested,
                    ])
                self.assertEqual(result, 2)
                self.assertIn("choose a separate --out", errors.getvalue())
                if name == "unknown":
                    self.assertIn("belongs to unknown", errors.getvalue())
                parse.assert_not_called()
                self.assertEqual(database.read_bytes(), original)
                self.assertFalse((output / "reports").exists())

    def test_build_keeps_release_metadata_consistent_across_artifacts(self):
        for release in ("R21", "R33"):
            with self.subTest(release=release):
                source, tree_content = self.make_source(release)
                output = self.root / ("output-" + release)
                args = ["build", "--caadoc", str(source), "--out", str(output)]
                if release != "R21":
                    args.extend(["--release", release])
                with redirect_stdout(io.StringIO()):
                    result = builder.main(args)
                self.assertEqual(result, 0)
                manifest = json.loads((output / "data/manifest.json").read_text(encoding="utf-8"))
                expected = {
                    **release_metadata(release),
                    "catalog_source_baseline": SOURCE_BASELINE,
                    "source_tree_sha256": hashlib.sha256(tree_content).hexdigest(),
                }
                with closing(sqlite3.connect(output / "data/manual.sqlite")) as connection:
                    metadata = dict(connection.execute("SELECT key, value FROM projection_metadata"))
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM api_pages").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM api_members").fetchone()[0], 1)
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                for key, value in expected.items():
                    self.assertEqual(metadata[key], value, key)
                    self.assertEqual(manifest[key], value, key)
                self.assertEqual(manifest["source"], "caadoc://")
                self.assertEqual(manifest["output"], "manual://")
                self.assertEqual(manifest["artifact_counts"]["api_pages"], 1)
                report = (output / "reports/summary.md").read_text(encoding="utf-8")
                self.assertIn(expected["source_baseline"], report)
                self.assertIn(expected["source_tree_sha256"], report)


if __name__ == "__main__":
    unittest.main()
