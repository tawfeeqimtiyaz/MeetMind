"""Where LangGraph saves pipeline progress (needed to pause for review and resume later).

Postgres in Docker/Kubernetes: the worker pauses the graph, and the API resumes it later
in a different process, so the checkpoint must live in a shared database.
In-memory for tests (SQLite).
"""
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

_memory_saver = InMemorySaver()
_setup_lock = threading.Lock()
_setup_done = False


@contextmanager
def open_checkpointer(database_url: str) -> Iterator[BaseCheckpointSaver]:
    if not database_url.startswith("postgresql"):
        yield _memory_saver
        return

    from langgraph.checkpoint.postgres import PostgresSaver

    global _setup_done
    # SQLAlchemy URL "postgresql+psycopg://..." -> plain libpq URL "postgresql://..."
    conn_string = re.sub(r"^postgresql\+\w+://", "postgresql://", database_url)
    with PostgresSaver.from_conn_string(conn_string) as saver:
        with _setup_lock:
            if not _setup_done:
                saver.setup()  # creates/migrates LangGraph's own tables; safe to repeat
                _setup_done = True
        yield saver
