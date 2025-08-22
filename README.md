# splash_timepix

Code to stream from timepix

## Installation

Clone the repository and install dependencies (Python 3.9+ required):

```bash
git clone https://github.com/als-computing/splash_timepix.git
cd splash_timepix
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Configuration

- All tool and code style configurations are in `pyproject.toml`.
- Pre-commit hooks are configured in `.pre-commit-config.yaml`.
- To enable pre-commit hooks (recommended):

	```bash
	pre-commit install
	```

## Running as a Developer

- **Run the example server:**

	```bash
	python -m splash_timepix.example
	```

- **Run the test source (in another terminal):**

	```bash
	python -m splash_timepix.test_source
	```

- **Run all tests:**

	```bash
	pytest
	```

- **Run pre-commit checks on all files:**

	```bash
	pre-commit run --all-files
	```

- **Demo script (interactive menu):**

	```bash
	python demo.py
	```
