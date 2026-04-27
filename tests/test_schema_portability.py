from __future__ import annotations

import unittest

from sqlalchemy import create_mock_engine

from syncraft.database import Base
from syncraft import models  # noqa: F401


class SchemaPortabilityTests(unittest.TestCase):
    def test_metadata_create_all_compiles_for_supported_sql_engines(self) -> None:
        urls = (
            "sqlite://",
            "postgresql+psycopg://user:pass@localhost/testdb",
            "mysql+pymysql://user:pass@localhost/testdb",
        )
        for url in urls:
            statements: list[str] = []
            engine = create_mock_engine(url, lambda sql, *args, **kwargs: statements.append(str(sql)))
            Base.metadata.create_all(bind=engine)
            self.assertTrue(statements, url)
            self.assertTrue(any("CREATE TABLE" in statement for statement in statements), url)


if __name__ == "__main__":
    unittest.main()
