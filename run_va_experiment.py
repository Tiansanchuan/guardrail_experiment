"""Standalone entry point for the VA experiment; no legacy runner import."""

from va_experiment.experiment import main


if __name__ == "__main__":
    raise SystemExit(main())
