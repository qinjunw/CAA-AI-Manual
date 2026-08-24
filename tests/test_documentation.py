import re
import sqlite3
from contextlib import closing
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPLANATORY_FILES = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "reports" / "summary.md",
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
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8-sig")
        self.assertIsNone(ENV_ASSIGNMENT_RE.search(readme))

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
