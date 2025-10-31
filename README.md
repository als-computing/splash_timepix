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

- **Run the server that will consume incoming TCP packets:**

	For any high count rates
	```bash
	python -m splash_timepix.example
	```
	For low count rates (using the test_source)
	This will print a line for every incoming event)
	```bash
	python -m splash_timepix.example --verbose
	```

- **Run the client that streams data (in another terminal):**
	
	Use the test source
	```bash
	python -m splash_timepix.test_source
	```
	Replay a previously recorded TimePix file
	```bash
	./ASI/live-cli_alpha-1/live-cli --source-files path/to/file.tpx3
	```
	Stream data from TimePix in real time
	```bash
	./ASI/live-cli_alpha-1/live-cli
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
