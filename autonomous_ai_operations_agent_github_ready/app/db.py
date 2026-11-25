from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models import Base

_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def configure(url: str | None = None) -> None:
    """(Re)initialise the engine and create tables. Safe to call repeatedly (used by tests)."""
    global _engine, _session_factory
    url = url or get_settings().database_url
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(url, **kwargs)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    Base.metadata.create_all(_engine)


def session_factory() -> sessionmaker:
    if _session_factory is None:
        configure()
    return _session_factory
