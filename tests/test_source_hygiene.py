from pathlib import Path
import unicodedata
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = [ROOT / "app", ROOT / "tests"]


class SourceHygieneTestCase(unittest.TestCase):
    def test_python_sources_are_utf8_without_bom(self):
        offenders = []
        for source_dir in SOURCE_DIRS:
            for path in source_dir.rglob("*.py"):
                if path.read_bytes().startswith(b"\xef\xbb\xbf"):
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_python_sources_do_not_contain_private_or_replacement_chars(self):
        offenders = []
        for source_dir in SOURCE_DIRS:
            for path in source_dir.rglob("*.py"):
                text = path.read_text(encoding="utf-8", errors="replace")
                for line_no, line in enumerate(text.splitlines(), 1):
                    if any(
                        char == "\ufeff"
                        or ord(char) == 0xFFFD
                        or unicodedata.category(char) == "Co"
                        for char in line
                    ):
                        offenders.append(f"{path.relative_to(ROOT)}:{line_no}")
                        break
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
