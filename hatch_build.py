"""Stamp immutable build provenance without writing generated files into the checkout."""

import json
from pathlib import Path
import subprocess
import tempfile

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        info = {}
        preserved = root / "baton" / "_build_info.json"
        if preserved.is_file():
            try:
                info = json.loads(preserved.read_text())
            except (OSError, ValueError):
                pass
        try:
            result = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                                    text=True, capture_output=True, timeout=10)
            if result.returncode == 0 and Path(result.stdout.strip()).resolve() == root.resolve():
                info = {}
                revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                          text=True, capture_output=True, timeout=10)
                state = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                                       text=True, capture_output=True, timeout=10)
                branch = subprocess.run(["git", "-C", str(root), "symbolic-ref", "--quiet", "HEAD"],
                                        text=True, capture_output=True, timeout=10)
                if revision.returncode == state.returncode == 0:
                    info = {"revision": revision.stdout.strip(), "dirty": bool(state.stdout.strip()),
                            "branch": branch.stdout.strip() if branch.returncode == 0 else ""}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        handle = tempfile.NamedTemporaryFile(mode="w", prefix="baton-build-", suffix=".json", delete=False)
        with handle:
            json.dump(info, handle)
            handle.write("\n")
        self._provenance_path = handle.name
        build_data.setdefault("force_include", {})[handle.name] = "baton/_build_info.json"

    def finalize(self, version: str, build_data: dict, artifact_path: str) -> None:
        if getattr(self, "_provenance_path", None):
            Path(self._provenance_path).unlink(missing_ok=True)
