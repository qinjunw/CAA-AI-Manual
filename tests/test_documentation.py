import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPLANATORY_FILES = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "reports" / "summary.md",
    PROJECT_ROOT / "reports" / "retrieval-iteration.md",
    PROJECT_ROOT / "reports" / "agent-retrieval-metrics.json",
    PROJECT_ROOT / "reports" / "agent-stability.md",
    PROJECT_ROOT / "reports" / "agent-stability-metrics.json",
    PROJECT_ROOT / "reports" / "retrieval-methods-research.md",
    PROJECT_ROOT / "reports" / "reference-transfer-plan.md",
    PROJECT_ROOT / "reports" / "reference-transfer-results.md",
    PROJECT_ROOT / "reports" / "reference-transfer-metrics.json",
    PROJECT_ROOT / "docs" / "AGENT_QUERY_GUIDE.md",
    PROJECT_ROOT / "docs" / "MAINTAINER_GUIDE.md",
    PROJECT_ROOT / "docs" / "agent-system-prompt.txt",
)
ENV_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:CAA_AI_MANUAL_ROOT|CAA_AI_MANUAL_DB|CAA_CAADOC_ROOT)\s*=",
    re.MULTILINE,
)
WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")


class DocumentationTests(unittest.TestCase):
    def test_explanatory_files_do_not_contain_windows_absolute_paths(self):
        for path in EXPLANATORY_FILES:
            text = path.read_text(encoding="utf-8-sig")
            self.assertIsNone(
                WINDOWS_ABSOLUTE_PATH_RE.search(text),
                f"Local absolute path found in {path}",
            )

    def test_environment_assignments_live_in_configuration_templates(self):
        for path in EXPLANATORY_FILES:
            text = path.read_text(encoding="utf-8-sig")
            self.assertIsNone(ENV_ASSIGNMENT_RE.search(text), str(path))

    def test_onboarding_relative_file_links_resolve(self):
        for path in (PROJECT_ROOT / "README.md", PROJECT_ROOT / "docs" / "MAINTAINER_GUIDE.md"):
            text = path.read_text(encoding="utf-8-sig")
            for target in re.findall(r"\[[^\]]+\]\(([^)\s]+)\)", text):
                if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target):
                    continue
                relative = target.split("#", 1)[0]
                if relative:
                    self.assertTrue((path.parent / relative).is_file(), f"{path}: {target}")

    def test_testing_api_template_matches_nested_credentials_format(self):
        template = PROJECT_ROOT / "config" / "testing-api.example.json"
        config = json.loads(template.read_text(encoding="utf-8"))
        keys = [value["testingAPIKey"] for value in config.values()
                if isinstance(value, dict) and value.get("testingAPIKey")]
        self.assertEqual(keys, ["<your-test-api-key>"])

    def test_distributed_database_is_index_only_and_within_git_file_limit(self):
        database = PROJECT_ROOT / "data" / "manual.sqlite"
        self.assertLess(database.stat().st_size, 100 * 1024 * 1024)
        with closing(sqlite3.connect(database)) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM projection_metadata"))
            self.assertEqual(metadata["content_mode"], "index-only")
            self.assertEqual(metadata["embedded_official_text"], "false")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)
            for table, column in (
                ("evidence", "quote_or_span"),
                ("api_pages", "summary_en"),
                ("api_members", "summary_en"),
            ):
                count = connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} <> ''"
                ).fetchone()[0]
                self.assertEqual(count, 0, f"Embedded text found in {table}.{column}")


if __name__ == "__main__":
    unittest.main()
