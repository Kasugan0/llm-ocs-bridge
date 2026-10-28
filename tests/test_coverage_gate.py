import json
import subprocess
import sys

import pytest
import run_coverage


@pytest.fixture
def report():
    paths = list((run_coverage.ROOT / "src").rglob("*.py"))
    summary = {
        "covered_lines": 1,
        "num_statements": 1,
        "covered_branches": 1,
        "num_branches": 1,
    }
    return {
        "meta": {"branch_coverage": True},
        "files": {
            str(path.relative_to(run_coverage.ROOT)): {"summary": dict(summary)}
            for path in paths
        },
        "totals": {name: value * len(paths) for name, value in summary.items()},
    }


def test_coverage_gate_accepts_complete_report(report):
    assert run_coverage.check_report(report) == []


@pytest.mark.parametrize("field", ["covered_lines", "covered_branches"])
def test_coverage_gate_rejects_each_regression(report, field):
    report["totals"][field] = 1
    for index, entry in enumerate(report["files"].values()):
        entry["summary"][field] = int(index == 0)
    assert run_coverage.check_report(report)


def test_coverage_gate_rejects_module_below_floor(report):
    name = next(
        name
        for name, baselines in run_coverage.FILE_BASELINES.items()
        if any(baselines)
    )
    summaries = [entry["summary"] for entry in report["files"].values()]
    for summary in summaries:
        summary.update(
            covered_lines=1000,
            num_statements=1000,
            covered_branches=1000,
            num_branches=1000,
        )
    report["files"][name]["summary"].update(
        covered_lines=0,
        num_statements=10,
        covered_branches=0,
        num_branches=10,
    )
    for field in report["totals"]:
        report["totals"][field] = sum(summary[field] for summary in summaries)
    failures = run_coverage.check_report(report)
    assert failures and all(name in failure for failure in failures)


def test_coverage_gate_rejects_forged_totals(report):
    report["totals"]["covered_lines"] += 1
    assert run_coverage.check_report(report)


def test_coverage_gate_rejects_missing_module_summary(report):
    next(iter(report["files"].values())).clear()
    assert run_coverage.check_report(report)


@pytest.mark.parametrize("missing", ["files", "meta", "totals"])
def test_coverage_gate_rejects_missing_data(report, missing):
    del report[missing]
    assert run_coverage.check_report(report)


@pytest.mark.parametrize("passed", [False, True])
def test_coverage_command_returns_failure_status(tmp_path, report, passed):
    if not passed:
        report["totals"]["covered_branches"] = 1
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    result = subprocess.run(
        [
            sys.executable,
            str(run_coverage.ROOT / "tests/run_coverage.py"),
            "--check-report",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == int(not passed), result.stderr


def test_coverage_command_rejects_missing_report(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(run_coverage.ROOT / "tests/run_coverage.py"),
            "--check-report",
            str(tmp_path / "absent.json"),
        ],
        capture_output=True,
    )
    assert result.returncode == 1
