#!/usr/bin/env python3
"""CODEX-049: the schema-migration entrypoint
(`us-stock-trading-migrate.service`).

Every other unit `Requires=` this one, so a deployment can never start a
service against a database that is behind the code. Applying migrations
is idempotent -- re-running against a current database is a no-op -- and
this is the ONLY unit whose job is to write schema.

Exits non-zero if the schema is still not at CURRENT_SCHEMA_VERSION
afterwards, which blocks every dependent unit.
"""

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from execution.secret_redaction import install_logging_redaction  # noqa: E402
from state_store import db as state_db  # noqa: E402
from state_store.migrations import CURRENT_SCHEMA_VERSION  # noqa: E402

logger = logging.getLogger("migrations")

EXIT_OK = 0
EXIT_ERROR = 1


def run_once(db_path=None):
    conn = state_db.open_db(db_path)
    try:
        version = state_db.get_schema_version(conn)
    finally:
        conn.close()
    return version


def main(argv=None):
    parser = argparse.ArgumentParser(description="Apply pending state-store schema migrations")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_logging_redaction()
    try:
        version = run_once()
    except Exception as exc:  # noqa: BLE001 -- service entrypoint
        logger.exception("migration failed: %s", exc)
        return EXIT_ERROR
    if version != CURRENT_SCHEMA_VERSION:
        logger.error("schema is at version %s, expected %s", version, CURRENT_SCHEMA_VERSION)
        return EXIT_ERROR
    logger.info("schema is at version %s", version)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
