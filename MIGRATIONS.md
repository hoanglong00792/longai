# Database Migrations

Hand-written `ALTER TABLE` snippets. v1 has no migrations framework
(per `INVARIANTS.md` § "On adding new invariants" rationale: keep schema
flat until 3 migrations exist).

## v0 → v1 (initial schema)

Created by `persistence.py:Persistence._init_schema()` on first run.

## (no migrations yet)

When you need to migrate:
1. Add a section here with the SQL.
2. Bump a `schema_version` row in a `meta` table (add the table when needed).
3. Run the SQL in `Persistence._init_schema()` guarded by version check.
