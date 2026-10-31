from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from oams.config import settings


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        yield conn
