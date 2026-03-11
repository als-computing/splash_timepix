"""Nox sessions for testing and linting."""

import nox

# Python versions to test against
PYTHON_VERSIONS = ["3.9", "3.10", "3.11", "3.12", "3.13"]

# Source and test paths
SOURCE_PATHS = ["src", "tests", "noxfile.py"]


@nox.session(python=PYTHON_VERSIONS)
def tests(session):
    """Run the test suite with pytest."""
    session.install(".[test]")
    session.run("pytest", *session.posargs)


@nox.session
def lint(session):
    """Run linters: black, isort, and flake8."""
    session.install(".[dev]")

    # Check code formatting with black
    session.run("black", "--check", *SOURCE_PATHS)

    # Check import sorting with isort
    session.run("isort", "--check-only", *SOURCE_PATHS)

    # Check code style with flake8
    session.run("flake8", *SOURCE_PATHS)


@nox.session
def format(session):
    """Format code with black and isort."""
    session.install(".[dev]")

    # Format code with black
    session.run("black", *SOURCE_PATHS)

    # Sort imports with isort
    session.run("isort", *SOURCE_PATHS)
