"""ASGI entry point: `uvicorn meetmind.main:app`."""
from . import create_app

app = create_app()
