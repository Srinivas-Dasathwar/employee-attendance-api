# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out, corrections with an audit trail, and analytics.

## Run
1. python -m venv .venv and activate it
2. pip install -r requirements.txt
3. Set MONGO_URI and MONGO_DB as environment variables (or in a local .env)
4. uvicorn app.main:app --port 8000

Indexes are created automatically at startup.

## Status
Done: all endpoints in the contract (health, employees, punch-in, punch-out, attendance list, PATCH regularize, the four analytics endpoints, admin explain). Review of the starter is in REVIEW.md, design decisions in DECISIONS.md.
Missing: nothing known.

## Notes
All code is in app/main.py. Tested locally with a generated dataset of about 100k attendance records: explain plans show IXSCAN with no COLLSCAN, and parallel punch-in / punch-out requests give exactly one success.