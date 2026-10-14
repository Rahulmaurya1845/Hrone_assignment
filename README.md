<!-- # Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile. -->


# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv venv
venv\Scripts\activate            # Windows (Mac/Linux: source venv/bin/activate)
pip install -r requirements.txt
```
Set `MONGO_URI` and `MONGO_DB` as environment variables (a local `.env` file is also read, and real environment variables take priority), then:
```bash
uvicorn app.main:app --port 8000
```
Indexes are created at startup, so no manual steps are needed. `GET /health` returns 200 when MongoDB answers.

## Status
All endpoints in the contract are implemented: employees, punch-in, punch-out, regularization (PATCH) with history, attendance list, the four analytics endpoints and `/admin/explain/{endpoint}`. Nothing is knowingly missing.

## Things to know
- Instants are stored as UTC datetimes and exchanged as epoch milliseconds; dates and shift times are IST strings.
- Race conditions (punch-in, punch-out, PATCH) rely on a unique index and conditional updates. I reasoned about them and tested the sequential cases, but did not run a parallel load test.
- I tested the analytics on MongoDB Atlas (version 8.0) with the sample data plus my own cases (ties, a weekend, an empty range, an overnight shift). I did not test with 100,000 records; `/admin/explain` shows `IXSCAN` and no `COLLSCAN` on my small data.
- The queries use index hints so MongoDB keeps using the intended index on wide filters.
- For the trend, the 7-day moving average uses the reported (rounded to 4 decimals) `attendance_rate` values.