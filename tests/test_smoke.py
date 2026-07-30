"""Smoke tests proving pytest discovery and package imports work."""

import importlib

PACKAGES = ("runner", "judge", "scoring", "report")


def test_packages_importable() -> None:
    """All four responsibility packages import without error."""
    for name in PACKAGES:
        module = importlib.import_module(name)
        assert module.__name__ == name


def test_runner_module_entry_exposes_main() -> None:
    """The ``runner.benchmark_runner`` module entry exposes a callable main()."""
    module = importlib.import_module("runner.benchmark_runner")
    assert callable(module.main)
