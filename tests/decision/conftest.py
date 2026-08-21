from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from healthmes.store import Base, create_db_engine


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as value:
        yield value
        value.rollback()
    engine.dispose()
