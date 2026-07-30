"""Command-line entry point for the GraphBenchmark runner.

Exposes both the ``graphbenchmark`` console script (declared via
``[project.scripts]``) and the ``python -m runner.benchmark_runner``
module entry. v1 ships a minimal argument parser only; concrete execution,
artifact validation and policy checks are added by later tasks (AIS-003+).
"""

import argparse


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level benchmark argument parser."""
    parser = argparse.ArgumentParser(
        prog="graphbenchmark",
        description=(
            "GraphBenchmark AI scoring runner (semantic_outcome_v1). "
            "Execution, judging and reporting are added by later tasks."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark CLI and return an exit code."""
    parser = build_parser()
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
