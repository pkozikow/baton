"""Reusable direct-scan corpus and measurements, without latency assertions.

Run the representative benchmark with:
BATON_SEARCH_BENCHMARK=1 uv run --locked python tests/test_project_search_performance.py
The test prints a JSON resource report; the runner buffers successful test output.
"""

import contextlib
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from baton.manager import Manager
import test_project_manager as fixtures


QUERIES = ("corpus-project", "typical-hit", "absent-query", 'cross-file second-source', '"refresh rotation"', "common-evidence")
CATEGORIES = {"metadata": (), "ordinary": ("ordinary",),
              "overview": ("coordination_overview",), "history": ("coordination_history",),
              "ordinary_overview": ("ordinary", "coordination_overview"),
              "all_artifacts": ("ordinary", "coordination_overview", "coordination_history")}


def generate_corpus(layout: Path, artifacts: Path, *, projects: int, ordinary_bytes: int,
                    history_bytes: int, oversized_bytes: int) -> dict[str, int]:
    totals = {"projects": projects, "ordinary_bytes": 0, "overview_bytes": 0, "history_bytes": 0}
    for index in range(projects):
        project_id = f"{index + 1:032x}"
        directory = artifacts / f"corpus-{index}"
        coordination = directory / "coordination"
        coordination.mkdir(parents=True)
        record = {"uuid": project_id, "name": f"corpus-project-{index}", "slot": "p3", "state": "archived",
                  "artifacts_path": str(directory), "repos": [{"id": "corpus", "branch": f"branch-{index}"}]}
        (layout / "archive" / f"{index:06d}-{project_id}.json").write_text(json.dumps(record))
        plan = "common-evidence cross-file\nrefresh\n rotation\n" + "ordinary saved project text\n" * (ordinary_bytes // 28 + 1)
        if index == 0:
            plan += "typical-hit\n" + "oversized source content\n" * (oversized_bytes // 25)
        note = "second-source\n"
        overview = "common-evidence saved overview\n" * 64
        history = "coordination history evidence\n" * (history_bytes // 30 + 1)
        for path, text, category in ((directory / "plan.md", plan, "ordinary_bytes"),
                                     (directory / "notes.txt", note, "ordinary_bytes"),
                                     (coordination / "overview.md", overview, "overview_bytes"),
                                     (coordination / "history.md", history, "history_bytes")):
            path.write_text(text)
            totals[category] += len(text.encode())
    return totals


def measure(query: str, category: str) -> dict:
    start = time.perf_counter()
    result = Manager().search_projects(query, categories=CATEGORIES[category])
    return {"seconds": time.perf_counter() - start, "matches": result["total_matches"],
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)}


class ProjectSearchPerformanceTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli

    def test_generated_corpus_and_resource_report(self) -> None:
        benchmark = os.environ.get("BATON_SEARCH_BENCHMARK") == "1"
        projects = 500 if benchmark else 3
        layout = self.projects / ".git" / "baton" / "v2"
        totals = generate_corpus(layout, self.artifacts, projects=projects,
                                 ordinary_bytes=230 * 1024 if benchmark else 1024,
                                 history_bytes=256 * 1024 if benchmark else 2048,
                                 oversized_bytes=16 * 1024 * 1024 if benchmark else 4096)
        with patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.root):
            for category in CATEGORIES:
                for query in QUERIES:
                    result = Manager().search_projects(query, categories=CATEGORIES[category])
                    expected = (projects if query == "corpus-project" or ("ordinary" in CATEGORIES[category] and query in QUERIES[3:])
                                or ("coordination_overview" in CATEGORIES[category] and query == "common-evidence") else
                                1 if "ordinary" in CATEGORIES[category] and query == "typical-hit" else 0)
                    self.assertEqual(result["total_matches"], expected, (query, category))
            if benchmark:
                self.assertGreaterEqual(totals["ordinary_bytes"], 100 * 1024 * 1024)
                report = {"corpus": totals, "measurements": []}
                # Each child is a complete fresh interpreter/registry scan; first and repeats remain separate.
                for category in CATEGORIES:
                    for query in QUERIES:
                        samples = []
                        for _ in range(6):
                            child_start = time.perf_counter()
                            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--measure", category, query],
                                                   env=self.env, text=True, capture_output=True, check=True)
                            sample = json.loads(child.stdout)
                            sample["invocation_seconds"] = time.perf_counter() - child_start
                            samples.append(sample)
                        repeats = [sample["seconds"] for sample in samples[1:]]
                        report["measurements"].append({"category": category, "query": query,
                                                       "first": samples[0], "repeats": samples[1:],
                                                       "repeat_p95_seconds": sorted(repeats)[-1],
                                                       "repeat_median_seconds": statistics.median(repeats),
                                                       "repeat_p95_invocation_seconds": max(sample["invocation_seconds"] for sample in samples[1:])})
                print("BATON_SEARCH_BENCHMARK_REPORT=" + json.dumps(report, sort_keys=True), flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--measure":
        print(json.dumps(measure(sys.argv[3], sys.argv[2])))
    else:
        unittest.main()
