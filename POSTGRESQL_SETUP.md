# PostgreSQL Setup

## 1. Install PostgreSQL

Install PostgreSQL 16 or newer and make sure the `psql` command is available.

## 2. Create the database

Run:

```sql
CREATE DATABASE edawr;
CREATE USER edawr_user WITH PASSWORD 'change_me_now';
GRANT ALL PRIVILEGES ON DATABASE edawr TO edawr_user;
```

If you are already inside `psql` as the `postgres` superuser:

```bash
psql -U postgres
```

Then execute:

```sql
\c edawr
GRANT ALL ON SCHEMA public TO edawr_user;
ALTER SCHEMA public OWNER TO edawr_user;
```

## 3. Configure the backend

Create `backend/.env` from `backend/.env.example` and set:

```env
APP_HOST=0.0.0.0
APP_PORT=3000
DATABASE_URL=postgresql+psycopg://edawr_user:change_me_now@localhost:5432/edawr
CORS_ORIGINS=*
```

## 4. Install Python dependencies

From `backend/` run:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 5. Start the FastAPI server

```bash
uvicorn main:app --host 0.0.0.0 --port 3000 --reload
```

On first startup the app creates the tables and seeds:

- users
- products
- orders
- order_items
- messages

## 6. Verify the connection

Open:

```text
http://localhost:3000/
```

You should receive a JSON response confirming the API is running.
