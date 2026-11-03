"""A test-only provider registered without any other code change, plus a CLI launcher that registers it."""

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import sys

from baton.providers import PROMPT_ARGV, PROMPT_STDIN, PROVIDERS, Provider, UnsupportedProviderOperation


ROOT = Path(__file__).resolve().parents[1]


class ExtraProvider(Provider):
    """Cannot fork; its unattended mode is chosen by EXTRA_UNATTENDED so tests cover every path."""

    def resume(self, session_id: str, directory: Path) -> list[str]:
        return [self.executable(), "continue", session_id]

    def start(self, directory: Path, prompt: str) -> list[str]:
        return [self.executable(), "new", prompt]

    def exec_unattended(self, session_id: str | None, workspace: Path, prompt: str) -> tuple[list[str], str]:
        self.require_fresh(session_id)
        mode = os.environ.get("EXTRA_UNATTENDED", PROMPT_STDIN)
        if mode == "unsupported":
            raise UnsupportedProviderOperation("extra has no headless mode that blocks writes")
        if mode == PROMPT_ARGV:
            return [self.executable(), "headless", "--read-only", prompt], PROMPT_ARGV
        return [self.executable(), "headless", "--read-only"], PROMPT_STDIN


EXTRA = ExtraProvider(
    "extra", "extra-agent", default_enabled=False, session_env="EXTRA_SESSION_ID",
    skill_dir=".extra/skills", skill_reads=(".extra/skills",),
    session_id_source="EXTRA_SESSION_ID holds the current session ID.",
    subagent_guidance="Use the extra fresh-subagent tool.")


def register() -> None:
    PROVIDERS.setdefault(EXTRA.name, EXTRA)


@contextmanager
def registered() -> Iterator[None]:
    register()
    try:
        yield
    finally:
        PROVIDERS.pop(EXTRA.name, None)


# Runs the WIP CLI with the extra provider registered first, as a plugin-free stand-in for a new registry entry.
LAUNCHER = [sys.executable, "-c",
            "import sys; sys.path[:0] = sys.argv[1:3]; del sys.argv[1:3]; import provider_fixtures; "
            "provider_fixtures.register(); from baton.cli import main; raise SystemExit(main())",
            str(ROOT), str(ROOT / "tests")]
