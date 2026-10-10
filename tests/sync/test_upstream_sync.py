"""Exercise real Git merges in temporary repositories, without GitHub."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2] / ".github/scripts/prepare_upstream_sync.py"
)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Sync test")
        self.git("config", "user.email", "sync@example.invalid")
        self.commit("provider.txt", "base\n", "base")
        self.git("branch", "upstream")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True).strip()

    def commit(self, filename, content, message):
        (self.repo / filename).write_text(content, encoding="utf-8")
        self.git("add", "--", filename)
        self.git("commit", "-m", message)

    def sync(self, check=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "main", "upstream"],
            cwd=self.repo,
            text=True,
            capture_output=True,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        return result

    def test_no_new_upstream_commit_leaves_fork_untouched(self):
        self.commit("locale.txt", "日本語\n", "fork locale")
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.sync()["status"], "unchanged")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("branch", "--show-current"), "main")

    def test_new_upstream_commit_merges_and_preserves_fork_patch(self):
        self.commit("locale.txt", "日本語\n", "fork locale")
        self.git("switch", "upstream")
        self.commit("fix.txt", "upstream fix\n", "upstream fix")
        upstream = self.git("rev-parse", "HEAD")
        self.git("switch", "main")
        result = self.sync()
        self.assertEqual(result["status"], "merged")
        self.assertEqual((self.repo / "locale.txt").read_text(), "日本語\n")
        self.assertEqual((self.repo / "fix.txt").read_text(), "upstream fix\n")
        self.git("merge-base", "--is-ancestor", upstream, "HEAD")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_conflict_aborts_merge_and_creates_report_only(self):
        self.commit("provider.txt", "日本語パッチ\n", "fork locale")
        fork = self.git("rev-parse", "HEAD")
        self.git("switch", "upstream")
        self.commit("provider.txt", "upstream rewrite\n", "upstream change")
        upstream = self.git("rev-parse", "HEAD")
        self.git("switch", "main")
        result = self.sync()
        self.assertEqual(result["status"], "conflict")
        self.assertEqual((self.repo / "provider.txt").read_text(), "日本語パッチ\n")
        changed = self.git("diff", "--name-only", fork, "HEAD")
        self.assertEqual(changed, f".github/upstream-conflicts/{upstream}.md")
        self.assertIn("provider.txt", (self.repo / changed).read_text())
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertNotEqual(
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", upstream, "HEAD"], cwd=self.repo
            ).returncode,
            0,
        )

    def test_local_changes_are_never_overwritten(self):
        (self.repo / "provider.txt").write_text("uncommitted\n")
        result = self.sync(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local changes", result.stderr)
        self.assertEqual((self.repo / "provider.txt").read_text(), "uncommitted\n")

    def test_existing_sync_branch_is_not_reset(self):
        self.git("switch", "upstream")
        self.commit("fix.txt", "fix\n", "upstream fix")
        self.git("switch", "main")
        result = self.sync()
        synced_head = self.git("rev-parse", result["branch"])
        self.git("switch", "main")
        self.assertNotEqual(self.sync(check=False).returncode, 0)
        self.assertEqual(self.git("rev-parse", result["branch"]), synced_head)


if __name__ == "__main__":
    unittest.main()
