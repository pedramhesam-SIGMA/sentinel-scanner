"""Runs scripts/commit_state.sh against throwaway local git repos (no network)."""
import os
import shutil
import subprocess
import tempfile
import unittest

from tests.helpers import ROOT

SCRIPT = os.path.join(ROOT, "scripts", "commit_state.sh")
GIT = shutil.which("git")
BASH = shutil.which("bash")


@unittest.skipUnless(GIT and BASH, "git and bash are needed for this test")
class CommitScriptTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="scanner-git-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("GIT_") and k != "GITHUB_REF_NAME"}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_TERMINAL_PROMPT="0")
        self.remote = os.path.join(self.base, "remote.git")
        self.git(self.base, "init", "-q", "--bare", self.remote)
        seed = self.clone("seed", fresh=True)
        self.write(seed, "state/state.json", "0")
        self.git(seed, "add", ".")
        self.commit(seed, "init")
        self.git(seed, "push", "-q", "origin", "HEAD:main")
        self.git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        self.seed = seed

    def git(self, cwd, *args, check=True):
        done = subprocess.run([GIT] + list(args), cwd=cwd, env=self.env,
                              capture_output=True, text=True)
        if check and done.returncode:
            self.fail("git %s failed: %s" % (" ".join(args), done.stderr))
        return done

    def commit(self, cwd, message):
        self.git(cwd, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
                 "-m", message)

    def clone(self, name, fresh=False):
        path = os.path.join(self.base, name)
        if fresh:
            self.git(self.base, "init", "-q", path)
            self.git(path, "checkout", "-q", "-b", "main")
            self.git(path, "remote", "add", "origin", self.remote)
        else:
            self.git(self.base, "clone", "-q", "-b", "main", self.remote, path)
        return path

    def write(self, repo, rel, text):
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)

    def run_script(self, repo, *args):
        return subprocess.run([BASH, SCRIPT] + list(args), cwd=repo, env=self.env,
                              capture_output=True, text=True)

    def remote_file(self, rel):
        return self.git(self.remote, "show", "main:" + rel).stdout

    def move_remote(self):
        self.git(self.seed, "pull", "-q", "origin", "main")
        self.write(self.seed, "README.md", "edit")
        self.git(self.seed, "add", ".")
        self.commit(self.seed, "other edit")
        self.git(self.seed, "push", "-q", "origin", "HEAD:main")

    def test_normal_mode_commits_and_pushes_state_only(self):
        work = self.clone("work")
        self.write(work, "state/state.json", "1")
        self.write(work, "notes.txt", "not state")
        done = self.run_script(work)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.remote_file("state/state.json"), "1")
        self.assertNotEqual(self.git(self.remote, "show", "main:notes.txt", check=False)
                            .returncode, 0)
        subject = self.git(work, "log", "-1", "--format=%s|%an").stdout.strip()
        self.assertRegex(subject, r"^scan \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\|github-actions\[bot\]$")

    def test_normal_mode_with_no_change_does_nothing(self):
        work = self.clone("work")
        before = self.git(self.remote, "rev-parse", "main").stdout
        done = self.run_script(work)
        self.assertEqual(done.returncode, 0)
        self.assertIn("nothing to push", done.stdout)
        self.assertEqual(self.git(self.remote, "rev-parse", "main").stdout, before)

    def test_normal_mode_rebases_when_the_remote_moved(self):
        work = self.clone("work")
        self.move_remote()
        self.write(work, "state/state.json", "2")
        done = self.run_script(work)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.remote_file("state/state.json"), "2")
        self.assertEqual(self.remote_file("README.md"), "edit")   # nothing was overwritten

    def test_strict_mode_pushes_when_up_to_date(self):
        work = self.clone("work")
        self.write(work, "state/state.json", "3")
        self.assertEqual(self.run_script(work, "strict").returncode, 0)
        self.assertEqual(self.remote_file("state/state.json"), "3")

    def test_strict_mode_fails_when_the_remote_moved(self):
        work = self.clone("work")
        self.move_remote()
        self.write(work, "state/state.json", "4")
        done = self.run_script(work, "strict")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.remote_file("state/state.json"), "0")   # claim not on the remote

    def test_f6_normal_mode_fails_if_the_remote_branch_ref_is_missing(self):
        work = self.clone("work")
        self.write(work, "state/state.json", "5")
        self.git(work, "update-ref", "-d", "refs/remotes/origin/main")
        done = self.run_script(work)
        self.assertEqual(done.returncode, 1)
        self.assertIn("origin/main not found", done.stderr)
        self.assertEqual(self.remote_file("state/state.json"), "0")


if __name__ == "__main__":
    unittest.main()
