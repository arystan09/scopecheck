# ADR 0001: one package and image

Status: records the implemented baseline.

Use FastAPI/Pydantic with PostgreSQL 18, async SQLAlchemy/psycopg and Alembic.
API and worker share the installed package/image/version. A small Jinja/vanilla-JS
UI suffices. This keeps deployment and provenance tractable for a synthetic tool.
The worker is a separate process so API requests do not execute evaluations.
Cost: a shared release lifecycle and one database failure domain. Microservices,
Redis/Celery and a generic agent framework are out of scope.
