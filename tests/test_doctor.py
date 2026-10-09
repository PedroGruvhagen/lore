"""Regression test for the Python floor doctor check (skill/scripts/lore.py
cmd_doctor, the "python" check around line 2799). Confirms the PASS line
names the current 3.12 floor, not the project's earlier 3.10 floor, on
whichever interpreter is running the suite (matches the completion
criteria of passing under both system python3 and python3.12).
"""
from __future__ import annotations

import unittest

from tests.helpers import bootstrap_tmp, run_lore


class DoctorPythonFloorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = bootstrap_tmp()
        self.lore_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_python_check_passes_with_312_floor_text(self):
        result = run_lore("doctor", "--offline", "--lore-dir", self.lore_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

        python_lines = [
            line for line in result.stdout.splitlines() if "] python:" in line
        ]
        self.assertEqual(len(python_lines), 1, result.stdout)
        self.assertIn("[PASS]", python_lines[0])
        self.assertIn("(>= 3.12)", python_lines[0])
        self.assertNotIn("3.10", python_lines[0])


if __name__ == "__main__":
    unittest.main()
