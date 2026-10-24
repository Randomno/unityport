import unittest

from unityport.wsl import normalize


class NormalizeTest(unittest.TestCase):
    def test_in_wsl(self):
        n = lambda t: normalize(t, windows=False, wsl=True)  # noqa: E731
        self.assertEqual(n(r'"C:\Program Files (x86)\Steam\steamapps\common\Game"'),
                         "/mnt/c/Program Files (x86)/Steam/steamapps/common/Game")
        self.assertEqual(n("D:/Games"), "/mnt/d/Games")
        self.assertEqual(n("C:"), "/mnt/c/")
        self.assertEqual(n(r"\\wsl.localhost\Ubuntu-24.04\home\me\Game"), "/home/me/Game")
        self.assertEqual(n(r"\\wsl$\Ubuntu\home"), "/home")
        self.assertEqual(n("file:///home/me/My%20Game"), "/home/me/My Game")
        self.assertEqual(n("  /home/me/Game  "), "/home/me/Game")

    def test_outside_wsl(self):
        self.assertEqual(normalize(r"C:\Games", windows=False, wsl=False), r"C:\Games")

    def test_on_windows(self):
        n = lambda t: normalize(t, windows=True, distro="Ubuntu")  # noqa: E731
        self.assertEqual(n("/mnt/c/Games/My Game"), r"C:\Games\My Game")
        self.assertEqual(n("/home/me/Game"), r"\\wsl.localhost\Ubuntu\home\me\Game")
        self.assertEqual(n("file:///C:/Games/My%20Game"), "C:/Games/My Game")
        self.assertEqual(n(r"'C:\Games'"), r"C:\Games")
        self.assertEqual(n("//wsl.localhost/Ubuntu/home"), "//wsl.localhost/Ubuntu/home")


if __name__ == "__main__":
    unittest.main()
