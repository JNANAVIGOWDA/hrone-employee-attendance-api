# Employee Attendance & Analytics API

A REST API built with **Python, FastAPI, and MongoDB** to manage employee attendance, record punch-in and punch-out events, support manual attendance corrections with an audit trail, and provide attendance analytics.

## Tech Stack

* Python 3.11+
* FastAPI
* MongoDB 6+
* PyMongo
* Pytest

## Features

* Employee management and employee listing.
* Employee attendance punch-in and punch-out.
* Attendance business-rule validation.
* Manual attendance corrections with audit history.
* Attendance analytics powered by MongoDB aggregation pipelines.
* Query explain endpoints for inspecting database query execution plans.
* API documentation through Swagger UI.
* Automated tests for API contracts, business rules, analytics, and concurrent requests.

## Project Structure

```text
hrone-employee-attendance-api/
├── app/                  # FastAPI application
├── tests/                # Automated tests
├── .gitignore
├── DATA_MODEL.md         # MongoDB document structures
├── DECISIONS.md          # Technical decisions and trade-offs
├── REVIEW.md             # Starter-code review
├── openapi.yaml          # API specification
├── requirements.txt      # Python dependencies
├── sample_seed.py        # Optional sample-data seeding script
└── README.md
```

## Prerequisites

Install the following before running the application:

* Python 3.11 or newer
* MongoDB 6 or newer
* Git

## Installation and Setup

### 1. Clone the repository

```bash
git clone https://github.com/JNANAVIGOWDA/hrone-employee-attendance-api.git
cd hrone-employee-attendance-api
```

### 2. Create and activate a virtual environment

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure MongoDB

Ensure your MongoDB server is running.

Set the MongoDB connection and database name in PowerShell:

```powershell
$env:MONGO_URI = "mongodb://127.0.0.1:27017"
$env:MONGO_DB = "attendance_db"
```

Use your own MongoDB connection URI if you are connecting to a different instance.

### 5. Start the API

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The API will be available at:

* **API base URL:** `http://127.0.0.1:8000`
* **Swagger UI:** `http://127.0.0.1:8000/docs`
* **OpenAPI JSON:** `http://127.0.0.1:8000/openapi.json`

Use the Swagger UI to explore endpoints and send requests interactively.

## Running Tests

Run the test suite using:

```bash
python -m pytest tests/ -q
```

**Important:** Run tests only against a disposable test database because test fixtures may clear the `employees` and `attendance_logs` collections.

For example, in PowerShell:

```powershell
$env:MONGO_DB = "attendance_takehome_test"
python -m pytest tests/ -q
```

Ensure the required test data and MongoDB instance are available before running the suite.

The test suite was run locally, with **72 tests passing**.

## Query Performance Validation

The five query explain plans were checked locally using a generated dataset containing 100,000 records. Each checked plan included an `IXSCAN`, and none included a `COLLSCAN`.

These results reflect the tested local dataset and queries. Actual query performance may vary with different data distributions and workloads.

## Documentation

* `openapi.yaml` — API contract and endpoint specifications.
* `DATA_MODEL.md` — MongoDB document structures.
* `DECISIONS.md` — Technical decisions and trade-offs.
* `REVIEW.md` — Review of the supplied starter code.

## Security and Configuration

* Configure MongoDB connection details through environment variables.
* Do not commit credentials, secrets, `.env` files, or virtual-environment directories.
* Do not commit the supplied sample-data JSON records.
* Use a separate, disposable database for automated tests.

## Author

**Jnanavi N N**

GitHub: https://github.com/JNANAVIGOWDA
