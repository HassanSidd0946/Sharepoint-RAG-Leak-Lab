"""Entry point: uv run python -m leaklab.generator <plan|generate|verify|teardown>."""

from leaklab.generator.cli import app

if __name__ == "__main__":
    app()
