import argparse
import json
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINE_BASELINE = Fraction(1253, 1311)
BRANCH_BASELINE = Fraction(343, 388)


def check_report(report):
    failures = []
    files = report.get("files", {})
    expected = {str(path.relative_to(ROOT)) for path in (ROOT / "src").rglob("*.py")}
    actual = {
        str(Path(name).resolve().relative_to(ROOT))
        if Path(name).is_absolute() and Path(name).is_relative_to(ROOT)
        else name
        for name in files
    }
    if actual != expected:
        failures.append(
            "report must include every runtime module and only runtime code"
        )
    if not report.get("meta", {}).get("branch_coverage"):
        failures.append("branch collection is missing")
    totals = report.get("totals", {})
    for field in (
        "covered_lines",
        "num_statements",
        "covered_branches",
        "num_branches",
    ):
        values = [entry.get("summary", {}).get(field) for entry in files.values()]
        if not values or any(not isinstance(value, int) for value in values):
            failures.append(f"per-module {field} is missing")
        elif sum(values) != totals.get(field):
            failures.append(f"per-module {field} disagrees with totals")
    for label, covered, total, baseline in (
        ("line", "covered_lines", "num_statements", LINE_BASELINE),
        ("branch", "covered_branches", "num_branches", BRANCH_BASELINE),
    ):
        count, size = totals.get(covered), totals.get(total)
        if (
            not isinstance(count, int)
            or not isinstance(size, int)
            or not 0 <= count <= size
            or size == 0
        ):
            failures.append(f"{label} coverage data is missing or invalid")
        elif Fraction(count, size) < baseline:
            failures.append(f"{label} coverage {count}/{size} is below {baseline}")
    return failures


def collect(output):
    output.mkdir(parents=True, exist_ok=True)
    env = dict(
        os.environ,
        COVERAGE_FILE=str(output / ".coverage"),
        COVERAGE_RCFILE=str(ROOT / "pyproject.toml"),
    )
    commands = [
        ["erase"],
        ["run", "-m", "pytest"],
        ["combine"],
        ["report"],
        ["json", "-o", str(output / "coverage.json")],
        ["html", "-d", str(output / "html")],
    ]
    for command in commands:
        subprocess.run(
            [sys.executable, "-m", "coverage", *command], cwd=ROOT, env=env, check=True
        )
    return output / "coverage.json"


def main():
    parser = argparse.ArgumentParser(description="Check separate line/branch coverage")
    parser.add_argument(
        "--output", type=Path, default=Path.home() / ".cache/llm-ocs-bridge/coverage"
    )
    parser.add_argument("--check-report", type=Path)
    args = parser.parse_args()
    try:
        path = args.check_report or collect(args.output.resolve())
        report = json.loads(path.read_text())
        failures = check_report(report)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Coverage verification failed: {exc}", file=sys.stderr)
        return 1
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    totals = report["totals"]
    print(
        f"Line: {totals['covered_lines']}/{totals['num_statements']}; "
        f"branch: {totals['covered_branches']}/{totals['num_branches']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
