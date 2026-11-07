import argparse
import json
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINE_BASELINE = Fraction(1558, 1601)
BRANCH_BASELINE = Fraction(483, 522)
FILE_BASELINES = {
    "src/llm_ocs_bridge/__init__.py": (Fraction(0), Fraction(0)),
    "src/llm_ocs_bridge/answers.py": (Fraction(162, 164), Fraction(80, 82)),
    "src/llm_ocs_bridge/app.py": (Fraction(108, 109), Fraction(25, 26)),
    "src/llm_ocs_bridge/cache.py": (Fraction(88, 92), Fraction(14, 18)),
    "src/llm_ocs_bridge/config.py": (Fraction(132, 139), Fraction(32, 38)),
    "src/llm_ocs_bridge/images.py": (Fraction(28, 29), Fraction(11, 12)),
    "src/llm_ocs_bridge/llm.py": (Fraction(89, 93), Fraction(28, 30)),
    "src/llm_ocs_bridge/main.py": (Fraction(227, 237), Fraction(82, 92)),
    "src/llm_ocs_bridge/metrics.py": (Fraction(33, 33), Fraction(2, 2)),
    "src/llm_ocs_bridge/models.py": (Fraction(31, 32), Fraction(1, 2)),
    "src/llm_ocs_bridge/questions.py": (Fraction(167, 169), Fraction(74, 76)),
    "src/llm_ocs_bridge/search.py": (Fraction(127, 130), Fraction(47, 48)),
    "src/llm_ocs_bridge/service.py": (Fraction(181, 183), Fraction(53, 56)),
    "src/llm_ocs_bridge/storage.py": (Fraction(167, 173), Fraction(32, 38)),
    "src/llm_ocs_bridge/utils.py": (Fraction(18, 18), Fraction(2, 2)),
}


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
    if set(FILE_BASELINES) != expected:
        failures.append("frozen per-module baselines do not match runtime modules")
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
    for name, entry in files.items():
        summary = entry.get("summary", {})
        line_baseline, branch_baseline = FILE_BASELINES.get(
            name, (Fraction(0), Fraction(0))
        )
        for label, covered, total, baseline in (
            ("line", "covered_lines", "num_statements", line_baseline),
            ("branch", "covered_branches", "num_branches", branch_baseline),
        ):
            count, size = summary.get(covered), summary.get(total)
            if (
                not isinstance(count, int)
                or not isinstance(size, int)
                or not 0 <= count <= size
            ):
                failures.append(
                    f"{name} {label} coverage data is missing or invalid"
                )
            elif size and Fraction(count, size) < baseline:
                failures.append(
                    f"{name} {label} coverage {count}/{size} is below {baseline}"
                )
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
