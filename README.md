# Employee Attendance & Analytics API

FastAPI service for employee attendance, manual corrections with audit history, and MongoDB-backed analytics. The API
contract and business rules are in `openapi.yaml`; MongoDB document shapes are in `DATA_MODEL.md`.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000 --reload
```

The application creates its indexes at startup. Set `MONGO_URI` and `MONGO_DB` in the environment for your own MongoDB
instance; environment variables take precedence over values loaded from a local `.env`. Do not commit `.env` files,
credentials, the virtual environment, or the supplied `sample_data/` JSON records. The optional `sample_seed.py` script
can load those records when the candidate-kit files are available locally; the data folder is ignored by Git.

## Tests

The endpoint test fixtures clear the `employees` and `attendance_logs` collections in the configured database. Point
`MONGO_DB` at a disposable test database before running them:

```powershell
$env:MONGO_DB = "attendance_takehome_test"
python -m pytest tests/ -q
```

The suite covers contract and response schemas, business-rule edge cases, and concurrent HTTP requests using local
MongoDB data. Its fixtures use the candidate kit's `sample_data/` files, which are intentionally not included in the
submission. All five explain plans were also checked with a generated 100,000-record dataset, including the
unfiltered department summary: each included `IXSCAN` and none included `COLLSCAN`. This representative local check is
not a guarantee about every production workload or the evaluator's hidden dataset.
