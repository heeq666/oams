from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from workhub.config import settings


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        yield conn


@contextmanager
def migration_connection() -> Iterator[psycopg.Connection]:
    """Schema changes run as the migrator role when one is configured.

    The web process must never be able to restructure the database, so the role
    it runs as has no DDL rights -- which means migrations cannot run as it
    either. Same credentials, different role.
    """
    with psycopg.connect(settings.migrator_url or settings.database_url,
                         row_factory=dict_row) as conn:
        yield conn
