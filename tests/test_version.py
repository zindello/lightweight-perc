"""The version is written twice: in pyproject.toml, which the build reads, and in __version__."""

import os
import tomllib
import unittest

import lightweight_perc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Version(unittest.TestCase):
    def test_the_two_match(self):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            self.assertEqual(tomllib.load(f)["project"]["version"], lightweight_perc.__version__)


if __name__ == "__main__":
    unittest.main()
