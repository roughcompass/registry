# Migrations

One Alembic revision per schema generation. Numbering: `0001_phase0_baseline.py`,
`0002_phase1_schema_registry.py`, etc.

## Running

`DATABASE_URL` must be exported (asyncpg DSN, e.g.
`postgresql+asyncpg://postgres:password@localhost:5432/registry`).

```bash
cd capability-fabric
alembic upgrade head      # apply all migrations
alembic current           # print the current revision
alembic downgrade -1      # roll back one revision
alembic downgrade base    # roll back everything
```

## Authoring

* Every revision is reversible. `downgrade()` must restore the previous schema.
  The partition migration (0006_phase5_partitions.py) is the only documented
  exception — see that file's docstring and the partition_migrate.py script.
* DDL is written verbatim in the migration file. Indexes and PARTITION DDL live
  in the migration, not in `storage/models.py`.
