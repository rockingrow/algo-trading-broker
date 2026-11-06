"""
broker/db/repository/_session.py — the one place repositories open a session.

Every repository module calls ``_session.get_session()`` through this module
attribute rather than importing the function, so a test can swap the session
factory for all repositories with a single ``monkeypatch.setattr``.
"""

from broker.db.engine import get_session

__all__ = ["get_session"]
