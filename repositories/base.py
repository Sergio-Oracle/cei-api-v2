"""Base repository — generic session lifecycle helpers."""
from __future__ import annotations
from contextlib import contextmanager
from models import get_session as _get_session


@contextmanager
def db_session():
    """Context manager that auto-commits on success and rollbacks on error."""
    session = _get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
