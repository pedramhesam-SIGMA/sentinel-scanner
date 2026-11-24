"""Checks on the repo itself: text style, workflow files, standard library only."""
import ast
import json
import os
import sys
import unittest

from tests.helpers import CFG, ROOT

SKIP_DIRS = {".git", "__pycache__", "state"}
EM_DASH = chr(0x2014)
BANNED_WORD = "kind" + "ly"


def repo_files():
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if not name.endswith(".pyc"):
                yield os.path.join(folder, name)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class RepoTests(unittest.TestCase):
    def test_text_style(self):
        for path in repo_files():
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertNotIn(EM_DASH, text, path)
            self.assertNotIn(BANNED_WORD, text.lower(), path)

    def test_layout(self):
        for parts in (("scanner", "config.json"), ("scanner", "rules.py"), ("scanner", "data.py"),
                      ("scanner", "fire.py"), ("scanner", "scan.py"), ("HALT.example",),
                      ("README.md",), (".github", "workflows", "scan.yml"),
                      (".github", "workflows", "tests.yml"), ("scripts", "commit_state.sh")):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, *parts)), parts)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "HALT")))

    def test_config_matches_the_spec(self):
        self.assertEqual(CFG["tickers"], ["SPY", "QQQ", "IWM", "DIA"])
        self.assertEqual(CFG["timezone"], "America/New_York")
        self.assertEqual(CFG["calendar_end"], "2027-12-31")
        self.assertEqual(len(CFG["holidays"]), 12)
        self.assertEqual(CFG["half_days"], ["2026-11-27", "2026-12-24", "2027-11-26"])
        self.assertEqual(CFG["s2e"], {
            "checkpoints": ["11:00", "12:00", "13:00", "14:00", "15:00"],
            "checkpoint_window_seconds": 599, "r_entry_below": "0.20", "range_floor": "0.005",
            "ibs_exit_above": "0.50", "max_hold_days": 5, "notional": "100.00", "cost_bp": "5",
            "exit_window": ["15:40:00", "15:49:59"]})
        self.assertEqual(CFG["fire"], {"enabled": True, "window": ["15:25:00", "15:42:59"],
                                       "max_attempts_per_day": 3,
                                       "marker": "SCANNER-BACKUP-START"})
        self.assertEqual(CFG["data"], {"bar_freshness_seconds": 900})

    def test_no_secret_or_fire_url_in_the_repo(self):
        for path in repo_files():
            if os.sep + "tests" + os.sep in path:
                continue   # tests hold fake values only
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertNotIn("sk-ant-", text, path)
            self.assertNotIn("routines/trig_", text, path)

    def test_standard_library_only(self):
        allowed = set(sys.stdlib_module_names) | {"scanner", "tests"}
        for path in repo_files():
            if not path.endswith(".py"):
                continue
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module]
                for name in names:
                    self.assertIn(name.split(".")[0], allowed, "%s imports %s" % (path, name))

    def test_no_eval_or_exec_or_shell(self):
        for name in ("rules.py", "data.py", "fire.py", "scan.py"):
            tree = ast.parse(read("scanner", name))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, ("eval", "exec", "compile", "__import__"), name)
                if isinstance(node, ast.keyword) and node.arg == "shell":
                    self.fail("shell= used in " + name)

    def test_rules_module_reads_no_clock_and_does_no_io(self):
        text = read("scanner", "rules.py")
        for banned in (".now(", "today()", "open(", "import os", "import time", "urllib",
                       "print("):
            self.assertNotIn(banned, text)

    def test_scan_workflow(self):
        text = read(".github", "workflows", "scan.yml")
        for needed in ('name: scan', 'cron: "*/5 13-21 * * 1-5"', "workflow_dispatch:",
                       "contents: write", "group: scan", "cancel-in-progress: false",
                       "runs-on: ubuntu-latest", "timeout-minutes: 6", "actions/checkout@v4",
                       "actions/setup-python@v5", 'python-version: "3.12"',
                       "python -m scanner.scan", "secrets.ROUTINE_FIRE_URL",
                       "secrets.ROUTINE_FIRE_TOKEN", "secrets.ALPACA_KEY_ID",
                       "secrets.ALPACA_SECRET_KEY", "if: always()",
                       "STATE_PERSIST_CMD: bash scripts/commit_state.sh strict"):
            self.assertIn(needed, text)
        self.assertNotIn("force", text.lower())

    def test_commit_script(self):
        text = read("scripts", "commit_state.sh")
        self.assertIn("git add state", text)
        self.assertIn('git commit -q -m "scan $(date -u', text)
        self.assertIn("github-actions[bot]", text)
        self.assertIn("pull -q --rebase", text)
        self.assertIn("for try in 1 2 3", text)
        code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
        self.assertNotIn("--force", code)
        self.assertNotIn(" -f", code)

    def test_tests_workflow(self):
        text = read(".github", "workflows", "tests.yml")
        self.assertIn("push:", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("python -m unittest discover -s tests -v", text)

    def test_fixtures_are_valid_json(self):
        folder = os.path.join(ROOT, "tests", "fixtures")
        count = 0
        for sub, _, files in os.walk(folder):
            for name in files:
                with open(os.path.join(sub, name), encoding="utf-8") as handle:
                    json.load(handle)
                count += 1
        self.assertEqual(count, 8)

    def test_readme_points_to_the_frozen_rule(self):
        text = read("README.md")
        self.assertIn("claude/108a_PREREG_S2E.md", text)
        self.assertIn("HALT", text)
        self.assertIn("--dry-run", text)


if __name__ == "__main__":
    unittest.main()
