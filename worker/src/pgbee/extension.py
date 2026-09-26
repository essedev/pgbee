"""Build the files for `CREATE EXTENSION pgbee` from the same versioned SQL files the installer
applies, so the two ways of installing cannot drift apart.

Version 0.N is file NNNN: `pgbee--0.1.sql` is file 0001 and every later file becomes an update
script `pgbee--0.(N-1)--0.N.sql`. Postgres chains them, both on a fresh CREATE EXTENSION and on
ALTER EXTENSION pgbee UPDATE.
"""

from __future__ import annotations

from pathlib import Path

from pgbee.installer import SqlFile, sql_files

NAME = "pgbee"
COMMENT = "Derived columns computed by models: queue, versions, lineage, human overrides, budgets"

GUARD = (
    '\\echo Use "CREATE EXTENSION pgbee" or "ALTER EXTENSION pgbee UPDATE" to load this file.'
    " \\quit\n"
)

# Tables and sequences of the extension hold user data (definitions, jobs, lineage, spend):
# pg_dump must dump their rows. schema_version is left out because the scripts fill it.
CONFIG_DUMP = """
DO $pgbee$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT c.oid::regclass AS rel
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_depend d ON d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid
                               AND d.deptype = 'e'
    JOIN pg_catalog.pg_extension e ON e.oid = d.refobjid AND e.extname = 'pgbee'
    WHERE n.nspname = 'bee' AND c.relkind IN ('r', 'S')
      AND c.relname <> 'schema_version'
      AND NOT (c.oid = ANY (coalesce(e.extconfig, '{}')))
  LOOP
    PERFORM pg_catalog.pg_extension_config_dump(r.rel, '');
  END LOOP;
END $pgbee$;
"""


def version_of(sql_file: SqlFile) -> str:
    return f"0.{sql_file.version}"


def script(sql_file: SqlFile) -> str:
    body = sql_file.path.read_text(encoding="utf-8")
    return (
        f"{GUARD}\n{body.rstrip()}\n{CONFIG_DUMP}\n"
        f"INSERT INTO bee.schema_version (version) VALUES ({sql_file.version});\n"
    )


def control(default_version: str) -> str:
    return (
        f"comment = '{COMMENT}'\n"
        f"default_version = '{default_version}'\n"
        "relocatable = false\n"
        "superuser = true\n"
    )


def build(out_dir: Path, directory: Path | None = None) -> list[Path]:
    """Write the control file and one script per SQL file into out_dir. Returns the paths."""
    files = sql_files(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    previous: SqlFile | None = None
    for sql_file in files:
        if previous is None:
            name = f"{NAME}--{version_of(sql_file)}.sql"
        else:
            name = f"{NAME}--{version_of(previous)}--{version_of(sql_file)}.sql"
        path = out_dir / name
        path.write_text(script(sql_file), encoding="utf-8")
        written.append(path)
        previous = sql_file
    assert previous is not None
    control_path = out_dir / f"{NAME}.control"
    control_path.write_text(control(version_of(previous)), encoding="utf-8")
    written.append(control_path)
    return written
