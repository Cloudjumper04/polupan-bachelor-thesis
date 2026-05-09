from __future__ import annotations

from pathlib import Path

from sqlmodel import SQLModel, create_engine
from sqlalchemy.engine import Engine


DEFAULT_DATABASE_PATH = Path(__file__).resolve().parents[2] / "data" / "smartenergy.db"


def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or f"sqlite:///{DEFAULT_DATABASE_PATH}"
    if url.startswith("sqlite:///"):
        db_path = Path(url.removeprefix("sqlite:///"))
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url)


def create_db_and_tables(engine: Engine | None = None) -> None:
    if engine is None:
        engine = get_engine()
    SQLModel.metadata.create_all(engine)
