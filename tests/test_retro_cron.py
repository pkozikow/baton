import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_project_manager as fixtures

from baton.retro_cron import BEGIN, END, WEEKLY_BEGIN, WEEKLY_END, cron_entry, install_cron, update_crontab, validate_schedule


class RetroCronTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.table = self.root / "crontab.txt"
        self.capture = self.root / "captured.json"
        self.env = os.environ | {"HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state"),
                                 "BATON_CONFIG_FILE": str(self.root / "configuration.json"),
                                 "CRON_FIXTURE_TABLE": str(self.table), "CRON_CAPTURE": str(self.capture),
                                 "CRON_FIXTURE_MODE": "normal"}
        self.binary = self.root / "crontab"
        self.binary.write_text("#!" + sys.executable + "\n" + '''import fcntl,os
from pathlib import Path
import sys
p=Path(os.environ["CRON_FIXTURE_TABLE"])
mode=os.environ["CRON_FIXTURE_MODE"]
if "CRON_ASSERT_LOCK" in os.environ:
    with open(os.environ["CRON_ASSERT_LOCK"], "a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise RuntimeError("Cron installation lock is not held")
if sys.argv[1]=="-l":
    if mode=="denied":
        print("crontab: operation not permitted",file=sys.stderr)
        raise SystemExit(1)
    if not p.exists():
        print("no crontab for test-user",file=sys.stderr)
        raise SystemExit(1)
    previous=p.read_text()
    print(previous,end="")
    if mode=="concurrent":
        p.write_text(previous+"# another process edited it\\n")
else:
    if mode=="write-fails":
        print("crontab: cannot install",file=sys.stderr)
        raise SystemExit(1)
    p.write_text(sys.stdin.read())
''')
        self.binary.chmod(0o755)

    def install(self, schedule: str = "0 10 * * *") -> str:
        return install_cron(schedule, environ=self.env, executable=Path(sys.executable), crontab=str(self.binary))

    def test_installs_once_and_updates_only_owned_block(self) -> None:
        original = "MAILTO=me@example.test\n15 8 * * * echo keep-me\n"
        self.table.write_text(original)
        self.install()
        first = self.table.read_text()
        self.assertTrue(first.startswith(original))
        self.assertEqual(first.count(BEGIN), 1)
        self.install()
        self.assertEqual(self.table.read_text(), first)
        self.install("30 10 * * 1-5")
        changed = self.table.read_text()
        self.assertTrue(changed.startswith(original))
        self.assertIn("30 10 * * 1-5 /bin/sh", changed)
        self.assertNotIn("0 10 * * * /bin/sh", changed)
        self.assertEqual(changed.count(END), 1)

    def test_weekly_and_daily_installations_preserve_each_other(self) -> None:
        self.table.write_text("# unrelated\n15 8 * * * echo keep\n")
        self.install()
        daily = self.table.read_text()
        weekly_entry = install_cron(weekly_provider="codex", environ=self.env,
                                    executable=Path(sys.executable), crontab=str(self.binary))
        combined = self.table.read_text()
        self.assertTrue(combined.startswith(daily))
        self.assertTrue(weekly_entry.startswith("0 11 * * 1 /bin/sh"))
        self.assertIn("run-weekly --provider codex", weekly_entry)
        self.assertIn("retro-weekly-cron.log", weekly_entry)
        install_cron(weekly_provider="codex", environ=self.env,
                     executable=Path(sys.executable), crontab=str(self.binary))
        self.assertEqual(self.table.read_text(), combined)
        self.install("30 9 * * *")
        self.assertIn(WEEKLY_BEGIN + "\n" + weekly_entry + "\n" + WEEKLY_END, self.table.read_text())
        install_cron("15 11 * * 2", weekly_provider="claude", environ=self.env,
                     executable=Path(sys.executable), crontab=str(self.binary))
        final = self.table.read_text()
        self.assertIn("30 9 * * * /bin/sh", final)
        self.assertIn("15 11 * * 2 /bin/sh", final)
        self.assertIn("run-weekly --provider claude", final)
        self.assertEqual(final.count(BEGIN), 1)
        self.assertEqual(final.count(WEEKLY_BEGIN), 1)
        self.assertTrue(final.startswith("# unrelated\n15 8 * * * echo keep\n"))

    def test_weekly_installation_holds_shared_lock_and_checks_concurrent_edits(self) -> None:
        self.env["CRON_ASSERT_LOCK"] = str(self.root / "state/baton/retro-cron-install.lock")
        install_cron(weekly_provider="agy", environ=self.env, crontab=str(self.binary))
        self.env["CRON_FIXTURE_MODE"] = "concurrent"
        with self.assertRaisesRegex(ValueError, "changed during"):
            install_cron("0 8 * * 1", weekly_provider="agy", environ=self.env, crontab=str(self.binary))
        self.assertNotIn("0 8 * * 1 /bin/sh", self.table.read_text())

    def test_both_managed_blocks_are_validated_for_either_installation(self) -> None:
        malformed = (WEEKLY_BEGIN + "\n", WEEKLY_END + "\n",
                     (WEEKLY_BEGIN + "\n" + WEEKLY_END + "\n") * 2,
                     BEGIN + "\n" + WEEKLY_BEGIN + "\n" + END + "\n" + WEEKLY_END + "\n",
                     WEEKLY_BEGIN + "\n" + BEGIN + "\n" + END + "\n" + WEEKLY_END + "\n")
        for previous in malformed:
            for weekly in (False, True):
                with self.subTest(previous=previous, weekly=weekly):
                    with self.assertRaisesRegex(ValueError, "Malformed"):
                        update_crontab(previous, "0 11 * * 1 command", weekly=weekly)

    def test_invalid_weekly_provider_and_schedule_leave_crontab_untouched(self) -> None:
        for provider, schedule in (("unknown", None), ("", None), ("codex", ""), ("codex", "0 25 * * 1")):
            with self.subTest(provider=provider, schedule=schedule):
                with self.assertRaises(ValueError):
                    install_cron(schedule, weekly_provider=provider, environ=self.env, crontab=str(self.binary))
                self.assertFalse(self.table.exists())

    def test_weekly_preflight_validates_without_trust_or_crontab(self) -> None:
        for schedule, expected in (("0 11 * * 1", 0), ("0 25 * * 1", 1)):
            result = subprocess.run([sys.executable, "-m", "baton.retro_cron", "install-weekly", "--check",
                                     "--provider", "codex", "--schedule", schedule], env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertFalse(self.table.exists())
            self.assertFalse((self.root / "state").exists())

    def test_weekly_wrapper_requires_provider_before_touching_crontab(self) -> None:
        for extra in ([], ["--provider", "unknown"]):
            result = subprocess.run([sys.executable, "-m", "baton.retro_cron", "install-weekly", *extra],
                                    capture_output=True, text=True, env=self.env)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("--provider", result.stderr)
            self.assertFalse(self.table.exists())

    def test_new_user_crontab_is_supported(self) -> None:
        entry = install_cron(environ=self.env, executable=Path(sys.executable), crontab=str(self.binary))
        self.assertTrue(entry.startswith("0 10 * * * /bin/sh"))
        self.assertTrue(self.table.read_text().startswith(BEGIN))


    def test_installation_holds_its_state_lock(self) -> None:
        self.env["CRON_ASSERT_LOCK"] = str(self.root / "state/baton/retro-cron-install.lock")
        self.install()

    def test_read_errors_never_overwrite_crontab(self) -> None:
        self.table.write_text("# original\n")
        self.env["CRON_FIXTURE_MODE"] = "denied"
        with self.assertRaisesRegex(ValueError, "Cannot read existing"):
            self.install()
        self.assertEqual(self.table.read_text(), "# original\n")

    def test_concurrent_edit_is_preserved_and_install_refused(self) -> None:
        self.table.write_text("# original\n")
        self.env["CRON_FIXTURE_MODE"] = "concurrent"
        with self.assertRaisesRegex(ValueError, "changed during"):
            self.install()
        self.assertTrue(self.table.read_text().startswith("# original\n"))
        self.assertNotIn(BEGIN, self.table.read_text())

    def test_install_failure_is_reported(self) -> None:
        self.table.write_text("# original\n")
        self.env["CRON_FIXTURE_MODE"] = "write-fails"
        with self.assertRaisesRegex(ValueError, "Cannot install"):
            self.install()
        self.assertEqual(self.table.read_text(), "# original\n")

    def test_malformed_owned_blocks_are_not_overwritten(self) -> None:
        for text in (BEGIN + "\n", END + "\n", END + "\n" + BEGIN + "\n",
                     (BEGIN + "\n" + END + "\n") * 2):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "Malformed"):
                    update_crontab(text, "0 10 * * * command")


    def test_entry_quotes_paths_and_percent_without_cron_metacharacters(self) -> None:
        directory = self.root / "space ' % \\ path"
        directory.mkdir()
        executable = directory / "python shim%"
        executable.write_text("#!" + sys.executable + "\n" + '''import json,os,sys
from pathlib import Path
Path(os.environ["CRON_CAPTURE"]).write_text(json.dumps({"args":sys.argv[1:],"config":os.environ["BATON_CONFIG_FILE"],"bin":os.environ["CODEX_BIN"],"agy":os.environ["AGY_BIN"],"user":os.environ["USER"],"state":os.environ["XDG_STATE_HOME"],"paths":{key:value for key,value in os.environ.items() if key.startswith("BATON_")}}))
''')
        executable.chmod(0o755)
        config = directory / "config%.json"
        log = directory / "log%.txt"
        self.env["CODEX_BIN"] = str(directory / "codex%")
        self.env["AGY_BIN"] = str(directory / "agy")
        self.env["USER"] = "keychain user"
        overrides = {name: str(directory / name) for name in (
            "BATON_REPO", "BATON_SLOTS_ROOT", "BATON_WORKTREES_ROOT", "BATON_REPOS_ROOT",
            "BATON_ARTIFACTS_ROOT")}
        self.env.update(overrides)
        for provider, expected in ((None, ["run", "--since", "3d"]), ("codex", ["run-weekly", "--provider", "codex"])):
            with self.subTest(provider=provider):
                entry = cron_entry("0 10 * * *", executable=executable, environment=self.env,
                                   configuration=config, log=log, provider=provider)
                self.assertNotIn("%", entry)
                command = entry.split(None, 5)[5]
                result = subprocess.run(["/bin/sh", "-c", command], env={"CRON_CAPTURE": str(self.capture)},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                captured = json.loads(self.capture.read_text())
                self.assertEqual(captured["args"], ["-I", "-m", "baton.retro_cron", *expected])
        self.assertEqual(captured["config"], str(config))
        self.assertEqual(captured["bin"], self.env["CODEX_BIN"])
        self.assertEqual(captured["agy"], self.env["AGY_BIN"])
        # Claude's keychain login fails under cron without USER.
        self.assertEqual(captured["user"], "keychain user")
        self.assertEqual(captured["state"], self.env["XDG_STATE_HOME"])
        self.assertEqual(captured["paths"], overrides | {"BATON_CONFIG_FILE": str(config)})
        self.assertTrue(log.is_file())

    def test_schedule_and_environment_are_validated_before_install(self) -> None:
        for schedule in ("* * * *", "0 24 * * *", "60 10 * * *", "0 9 0 * *", "0 9 * 13 *", "0 9 * * 8",
                         "0 10 * * *\necho surprise", "0 10 * * * #oops", "*/0 10 * * *", "10-5 9 * * *"):
            with self.subTest(schedule=schedule):
                with self.assertRaises(ValueError):
                    self.install(schedule)
        self.assertFalse(self.table.exists())
        self.env["PATH"] += "\nBAD"
        with self.assertRaisesRegex(ValueError, "control characters"):
            self.install()
        self.assertFalse(self.table.exists())
        self.assertEqual(validate_schedule("*/15 1,4-6 * * 0-7"), "*/15 1,4-6 * * 0-7")

    def test_window_is_configurable_and_invalid_windows_do_not_install(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive duration"):
            install_cron(environ=self.env, crontab=str(self.binary), since="0d")
        self.assertFalse(self.table.exists())
        entry = install_cron(environ=self.env, executable=Path(sys.executable), crontab=str(self.binary), since="12h")
        self.assertIn("--since 12h", entry)

    def test_weekly_routes_forward_to_summary(self) -> None:
        for module, action in (("baton.cli", ["retro", "summarize"]), ("baton.retro_cron", ["run-weekly"])):
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, "-c",
                                         f"from {module} import main; raise SystemExit(main())", *action, "--help"],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("baton retro summarize", result.stdout)
                self.assertIn("--provider", result.stdout)
                self.assertIn("--dry-run", result.stdout)

    def test_wrapper_forwards_to_pending_scan(self) -> None:
        result = subprocess.run([sys.executable, "-m", "baton.retro_cron", "run", "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--since", result.stdout)


class SummaryRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ProjectManagerTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.root / "private-state"
        self.fixture.env.update(HOME=str(self.fixture.root / "home"), XDG_STATE_HOME=str(self.state))
        shutil.rmtree(self.fixture.projects)
        self.artifacts = self.fixture.artifacts / "current-root"
        self.artifacts.mkdir()
        self.fixture.env["BATON_ARTIFACTS_ROOT"] = str(self.artifacts)

    def wrapper(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.fixture.run_command([sys.executable, "-m", "baton.retro_cron", "run-weekly", *args], self.fixture.root)

    def test_dry_run_routes_need_no_provider_registry_or_write_trust(self) -> None:
        self.fixture.env.pop("BATON_ALLOW_UNTRUSTED_WRITES")
        for invoke in (lambda: self.fixture.cli("retro", "summarize", "--dry-run", "--json"),
                       lambda: self.wrapper("--dry-run", "--json")):
            result = json.loads(invoke().stdout)
            self.assertEqual(result["sources"], [])
            self.assertTrue(Path(result["report"]).is_relative_to(self.artifacts))
            self.assertFalse(self.state.exists())
            self.assertFalse((self.artifacts / "retros").exists())
            self.assertFalse(self.fixture.projects.exists())

    def test_real_empty_week_and_repeat_wrapper_share_one_artifact(self) -> None:
        first = json.loads(self.fixture.cli("retro", "summarize", "--provider", "codex", "--json").stdout)
        commit = self.fixture.git(self.fixture.artifacts, "rev-parse", "HEAD")
        again = json.loads(self.wrapper("--provider", "codex", "--json").stdout)
        self.assertEqual(first["report"], again["report"])
        self.assertEqual(first["window_end"], again["window_end"])
        self.assertTrue(Path(first["report"]).is_relative_to(self.artifacts))
        self.assertTrue(first["saved"])
        self.assertTrue(first["committed"])
        self.assertFalse(first["published"])
        self.assertEqual(self.fixture.git(self.fixture.artifacts, "rev-parse", "HEAD"), commit)
        self.assertIn("No retros in this window", Path(first["report"]).read_text())
        self.assertFalse(self.fixture.projects.exists())


if __name__ == "__main__":
    unittest.main()
