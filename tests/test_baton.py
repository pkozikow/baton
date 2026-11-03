import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from baton.configuration import config_path, effective_paths, read_config, save_config


class BatonConfigurationTest(unittest.TestCase):
    def test_defaults_and_saved_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            environment = {"HOME": str(home)}
            canonical = home / ".config/baton/config.json"
            self.assertEqual(config_path(environment), canonical)
            paths = effective_paths({}, environment)
            for key, directory in (("projects_repo", "baton-projects"),
                                   ("slots_root", "baton-wt"), ("worktrees_root", "baton-worktrees")):
                self.assertEqual(paths[key], home / "dev" / directory)
            saved = {"paths": {"slots_root": str(home / "custom-slots")}, "repo_aliases": {"alias": "real"}}
            with patch.dict(os.environ, environment, clear=True):
                save_config(saved)
                self.assertEqual(read_config(), saved)
                self.assertEqual(effective_paths(read_config())["slots_root"], home / "custom-slots")
            self.assertEqual(json.loads(canonical.read_text()), saved)
            custom = home / "custom-config.json"
            self.assertEqual(config_path(environment | {"BATON_CONFIG_FILE": str(custom)}), custom)

    def test_baton_environment_overrides_saved_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            variables = {"projects_repo": "BATON_REPO", "slots_root": "BATON_SLOTS_ROOT",
                         "worktrees_root": "BATON_WORKTREES_ROOT", "repos_root": "BATON_REPOS_ROOT",
                         "artifacts_root": "BATON_ARTIFACTS_ROOT"}
            saved = {key: str(root / "saved" / key) for key in variables}
            overrides = {name: str(root / "overrides" / key) for key, name in variables.items()}
            environment = {"HOME": str(root), **overrides}
            self.assertEqual(effective_paths({"paths": saved}, environment),
                             {key: root / "overrides" / key for key in variables})
            self.assertEqual(effective_paths({"paths": saved}, {"HOME": str(root)}),
                             {key: Path(value) for key, value in saved.items()})

    def test_console_entrypoints_and_source_command(self) -> None:
        root = Path(__file__).resolve().parents[1]
        package = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        self.assertEqual(package["name"], "baton")
        self.assertEqual(package["scripts"], {"baton": "baton.cli:main", "baton-retro-cron": "baton.retro_cron:main"})
        result = subprocess.run([str(root / "bin/baton"), "--help"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: baton", result.stdout)

    def test_review_helpers_prefer_baton_and_respect_override(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in ("baton", "override"):
                executable = directory / name
                executable.write_text("#!/bin/sh\nprintf '%s\\n' '{\"name\":\"" + name + "\"}'\n")
                executable.chmod(0o755)
            environment = dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"])
            environment.pop("BATON_CLI", None)
            command = ["bash", "-c", 'source "$1"; review_project_value name', "bash",
                       str(root / "agent-skills/review-runner-lib.sh")]
            for override, expected in ((None, "baton"), (str(directory / "override"), "override")):
                selected = environment | ({"BATON_CLI": override} if override else {})
                result = subprocess.run(command, env=selected, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
