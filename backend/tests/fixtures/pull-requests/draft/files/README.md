# Review App

A small application with a Python backend and a TypeScript web client. It decides which users may
change a repository (`app/auth/permissions.py`), renames repositories (`app/repositories.py`),
turns names into URL slugs (`app/text.py`), and formats item counts for the web client
(`web/src/format.ts`).

Run the Python tests with `pytest tests`.
