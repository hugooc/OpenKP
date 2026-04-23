"""Kaiser Permanente portal scrapers.

Three layers:

- `auth`    Ping OAuth login via Playwright. Produces authenticated cookies.
- `session` Cookie persistence, expiry detection, auto re-auth.
- `request` Authenticated HTTP client. Every endpoint module uses this.

Endpoint modules (labs.py, medications.py, messages.py, ...) are added as we
reverse-engineer Kaiser's API surface.
"""
