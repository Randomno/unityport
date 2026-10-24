import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unityport import report


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.out = self.root / "Game-linux"
        data = self.out / "Game_Data"
        data.mkdir(parents=True)
        (data / "app.info").write_text("Some Company\nSome Game")
        (data / "globalgamemanagers").write_bytes(b"")
        self.log_dir = self.home / ".config" / "unity3d" / "Some Company" / "Some Game"
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    @unittest.skipIf(os.name == "nt", "from Windows, Player.log is looked for in WSL's home folders")
    def test_finds_player_log_and_hides_home(self):
        self.log_dir.mkdir(parents=True)
        (self.log_dir / "Player.log").write_text(f"Mono path[0] = '{self.home}/games/Game_Data/Managed'\n")
        text = report.build("Linux build written to " + str(self.home / "Game-linux"), None, self.out)
        self.assertIn("- Name: Some Game", text)
        self.assertIn("Mono path[0] = '~/games/Game_Data/Managed'", text)
        self.assertIn("Linux build written to ~/Game-linux", text)
        self.assertNotIn(str(self.home), text)

    def test_placeholders_without_log_or_output(self):
        text = report.build("", None, self.out)
        self.assertIn("<!-- Paste what unityport printed", text)
        self.assertIn("<!-- If you ran the ported game", text)

    def test_long_log_trimmed_and_extensions_folded(self):
        lines = [f"line {i}" for i in range(2000)]
        lines[3] = " " + " ".join(f"GL_EXT_{i}" for i in range(50))
        out = report._trim(lines)
        self.assertEqual(out[3], "[OpenGL extension list left out]")
        self.assertEqual(len(out), report.LOG_HEAD + report.LOG_TAIL + 1)
        self.assertEqual(out[-1], "line 1999")

    def test_windows_11_named_from_build(self):
        for version, release, name in (("10.0.26200", "10", "Windows 11"), ("10.0.19045", "10", "Windows 10")):
            with mock.patch.object(os, "name", "nt"), mock.patch("platform.version", return_value=version), \
                    mock.patch("platform.release", return_value=release):
                self.assertEqual(report._system(), f"{name} ({version})")

    def test_long_report_goes_by_clipboard(self):
        url, inline = report.issue_url("short", "Game: ")
        self.assertTrue(inline)
        self.assertIn("body=short", url)
        url, inline = report.issue_url("x" * 20000)
        self.assertFalse(inline)
        self.assertLessEqual(len(url), report.MAX_URL)
        self.assertIn("clipboard", url)


if __name__ == "__main__":
    unittest.main()
