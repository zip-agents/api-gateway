import pytest


@pytest.fixture
def anyio_backend() -> str:
    # The gateway runs on asyncio (uvicorn); the tests use asyncio primitives directly.
    return "asyncio"
