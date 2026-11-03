import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class InstallWrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.tool = self.root / "tools"
        python = self.tool / "baton/bin/python"
        python.parent.mkdir(parents=True)
        script = "#!" + sys.executable + "\n" + '''import json,os,sys
from pathlib import Path
name=Path(sys.argv[0]).name
with Path(os.environ["WRAPPER_CALLS"]).open("a") as f:
    f.write(json.dumps({"name":name,"args":sys.argv[1:]})+"\\n")
if '--check' in sys.argv and os.environ.get('WRAPPER_WEEKLY_CHECK_FAIL') == '1':
    raise SystemExit(2)
if name=="uv" and sys.argv[1:3]==["tool","dir"]:
    print(os.environ["WRAPPER_TOOL_ROOT"]+("/bin" if "--bin" in sys.argv else ""))
'''
        for executable in (python, self.bin / "uv"):
            executable.write_text(script)
            executable.chmod(0o755)
        self.calls = self.root / "calls.jsonl"
        self.env = os.environ | {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                                 "WRAPPER_CALLS": str(self.calls), "WRAPPER_TOOL_ROOT": str(self.tool)}
        self.source = self.root / "source"
        self.source.mkdir()
        self.installer = self.source / "install.sh"
        shutil.copyfile(Path(__file__).resolve().parents[1] / "install.sh", self.installer)
        self.git("init", "-b", "main")
        self.git("add", "install.sh")
        self.git("-c", "user.name=Install test", "-c", "user.email=install@example.test",
                 "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                 "commit", "-m", "Initial installer")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.source), *args], env=self.env,
                              check=True, capture_output=True, text=True).stdout.strip()

    def run_installer(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.installer), *args], env=self.env,
                              cwd=self.root, capture_output=True, text=True)

    def recorded(self) -> list[dict]:
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_normal_install_updates_cli_and_skills(self) -> None:
        target = self.root / "custom skills"
        result = self.run_installer("--skills-target", str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.recorded()
        self.assertTrue(any(row["name"] == "uv" and row["args"][:3] == ["tool", "install", "--reinstall"] for row in records))
        python = [row["args"] for row in records if row["name"] == "python"]
        self.assertEqual(python, [["-I", "-m", "baton.configuration", "init", "--preserve-review-loop"],
                                 ["-I", "-m", "baton.skills_install", "install", "--target", str(target)]])

    def test_feature_branch_refuses_before_installation(self) -> None:
        self.git("switch", "-c", "feature")
        result = self.run_installer("--install-cron")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("must be checked out on main", result.stderr)
        self.assertEqual(self.recorded(), [])

    def test_dirty_staged_and_untracked_main_refuse_before_installation(self) -> None:
        original = self.installer.read_text()
        self.installer.write_text(original + "\n# local changes\n")
        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged:
                    self.git("add", "install.sh")
                result = self.run_installer()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("must be clean", result.stderr)
                self.assertEqual(self.recorded(), [])
        self.git("reset", "--hard", "HEAD")
        (self.source / "scratch.txt").write_text("untracked\n")
        result = self.run_installer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("must be clean", result.stderr)
        self.assertEqual(self.recorded(), [])

    def test_feature_worktree_refuses_before_installation(self) -> None:
        worktree = self.root / "feature worktree"
        self.git("worktree", "add", "-b", "feature", str(worktree))
        self.installer = worktree / "install.sh"
        result = self.run_installer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("must be checked out on main", result.stderr)
        self.assertEqual(self.recorded(), [])

    def test_detached_head_refuses_before_installation(self) -> None:
        self.git("switch", "--detach", "HEAD")
        result = self.run_installer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("must be checked out on main", result.stderr)
        self.assertEqual(self.recorded(), [])

    def test_non_git_source_refuses_before_installation(self) -> None:
        shutil.rmtree(self.source / ".git")
        result = self.run_installer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("must be checked out on main", result.stderr)
        self.assertEqual(self.recorded(), [])

    def test_help_works_on_feature_branch_without_installing(self) -> None:
        self.git("switch", "-c", "feature")
        result = self.run_installer("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)
        self.assertEqual(self.recorded(), [])

    def test_cron_flag_runs_installed_module_with_defaults_or_custom_policy(self) -> None:
        for args, schedule, since in ((["--install-cron"], "0 10 * * *", "3d"),
                                      (["--install-cron", "--cron-schedule", "30 8 * * 1-5", "--cron-since", "12h"],
                                       "30 8 * * 1-5", "12h")):
            with self.subTest(args=args):
                self.calls.unlink(missing_ok=True)
                result = self.run_installer(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                python = [row["args"] for row in self.recorded() if row["name"] == "python"]
                self.assertEqual(python[-1], ["-I", "-m", "baton.retro_cron", "install",
                                             "--schedule", schedule, "--since", since])

    def test_weekly_flag_runs_only_weekly_installer_with_explicit_provider(self) -> None:
        for extra, schedule in (([], "0 11 * * 1"), (["--weekly-cron-schedule", "30 8 * * 2"], "30 8 * * 2")):
            with self.subTest(extra=extra):
                self.calls.unlink(missing_ok=True)
                result = self.run_installer("--install-weekly-cron", "--weekly-provider", "codex", *extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                cron = [row["args"] for row in self.recorded() if "baton.retro_cron" in row["args"]]
                self.assertEqual(cron, [["-I", "-m", "baton.retro_cron", "install-weekly", "--schedule", schedule, "--provider", "codex", "--check"],
                                        ["-I", "-m", "baton.retro_cron", "install-weekly", "--schedule", schedule, "--provider", "codex"]])

    def test_combined_daily_and_weekly_opt_in(self) -> None:
        result = self.run_installer("--install-cron", "--install-weekly-cron", "--weekly-provider", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        cron = [row["args"] for row in self.recorded() if "baton.retro_cron" in row["args"]]
        self.assertEqual(cron, [["-I", "-m", "baton.retro_cron", "install-weekly", "--schedule", "0 11 * * 1", "--provider", "claude", "--check"],
                                ["-I", "-m", "baton.retro_cron", "install", "--schedule", "0 10 * * *", "--since", "3d"],
                                ["-I", "-m", "baton.retro_cron", "install-weekly", "--schedule", "0 11 * * 1", "--provider", "claude"]])

    def test_weekly_validation_failure_prevents_either_cron_mutation(self) -> None:
        self.env["WRAPPER_WEEKLY_CHECK_FAIL"] = "1"
        result = self.run_installer("--install-cron", "--install-weekly-cron", "--weekly-provider", "codex")
        self.assertEqual(result.returncode, 2)
        cron = [row["args"] for row in self.recorded() if "baton.retro_cron" in row["args"]]
        self.assertEqual(len(cron), 1)
        self.assertIn("--check", cron[0])

    def test_invalid_wrapper_flags_fail_before_installation(self) -> None:
        for args in (("--cron-schedule", "0 10 * * *"), ("--cron-since", "3d"),
                     ("--install-cron", "--cron-since"), ("--install-cron", "--cron-schedule"),
                     ("--install-weekly-cron",), ("--weekly-provider", "codex"),
                     ("--weekly-cron-schedule", "0 11 * * 1"), ("--install-weekly-cron", "--weekly-provider"),
                     ("--install-weekly-cron", "--weekly-provider", "--install-cron"),
                     ("--install-weekly-cron", "--weekly-provider", "codex", "--weekly-cron-schedule")):

            with self.subTest(args=args):
                result = self.run_installer(*args)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
