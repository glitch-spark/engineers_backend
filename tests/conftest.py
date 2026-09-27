"""Integration test fixtures for engineers_backend.

Boots the real FastAPI app via its lifespan against the database pointed to by
MONGODB_URI_TEST, then clears all 5 collections between tests so each test
starts from a known-empty state. NOT mocks — see tests/README.md.
"""
import asyncio
import os

# Env must be set BEFORE importing app modules — pydantic-settings caches the
# Settings instance on first call to get_settings().
TEST_URI = os.environ.get("MONGODB_URI_TEST")
if not TEST_URI:
    raise RuntimeError(
        "MONGODB_URI_TEST is required for the integration test suite. "
        "Point it at a throwaway database (different db name from prod). "
        "See tests/README.md."
    )

os.environ["MONGODB_URI"] = TEST_URI
os.environ.setdefault("AUTH_SECRET", "test-secret-for-pytest-only")
os.environ["SLACK_WEBHOOK_URL"] = ""  # ensure slack notify is a no-op
os.environ["SLACK_DAILY_PLAN_CHANNEL_ID"] = ""  # never post test plans to Slack
os.environ.setdefault("FRONTEND_ORIGIN", "http://test.localhost")
os.environ.setdefault("APP_BASE_URL", "http://test.localhost")
os.environ.setdefault("APP_TIMEZONE", "UTC")
os.environ.setdefault("JWT_EXPIRES_MIN", "60")

from typing import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402
from app.models import Account, DailyPlan, Interview, Notification, RecurringCharge, Transaction, UserExtra, WeeklyPlan  # noqa: E402
from app.security import create_access_token, hash_password  # noqa: E402


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def app() -> AsyncIterator:
    async with LifespanManager(fastapi_app) as manager:
        yield manager.app


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(autouse=True)
async def db_clean(app) -> AsyncIterator[None]:
    for model in (UserExtra, Transaction, Account, WeeklyPlan, DailyPlan, Interview, Notification, RecurringCharge):
        await model.delete_all()
    yield


@pytest.fixture
def default_password() -> str:
    return "TestPassword123!"


@pytest.fixture
def make_user(default_password):
    async def _make(
        *,
        name: str = "Test User",
        email: str = "test@example.com",
        role: str = "staff",
        password: str = None,
    ) -> UserExtra:
        user = UserExtra(
            name=name,
            email=email,
            role=role,
            hashedPassword=hash_password(password or default_password),
        )
        await user.insert()
        return user

    return _make


@pytest.fixture
def auth_header():
    def _make(user: UserExtra) -> dict:
        token = create_access_token(sub=str(user.id), role=user.role)
        return {"Authorization": f"Bearer {token}"}

    return _make


@pytest_asyncio.fixture
async def staff_user(make_user) -> UserExtra:
    return await make_user(name="Staff One", email="staff@example.com", role="staff")


@pytest_asyncio.fixture
async def staff_user_2(make_user) -> UserExtra:
    return await make_user(name="Staff Two", email="staff2@example.com", role="staff")


@pytest_asyncio.fixture
async def admin_user(make_user) -> UserExtra:
    return await make_user(name="Admin One", email="admin@example.com", role="admin")
