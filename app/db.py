import os

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.models import (
    Account,
    AiReviewRun,
    CardLink,
    GlobalPrompt,
    Interview,
    ResumeApplication,
    Skill,
    Transaction,
    UserExtra,
    WeeklyPlan,
)

DOCUMENT_MODELS = [
    UserExtra,
    Transaction,
    Account,
    CardLink,
    WeeklyPlan,
    Interview,
    Skill,
    AiReviewRun,
    GlobalPrompt,
    ResumeApplication,
]


async def init_db() -> None:
    uri = os.environ["MONGODB_URI"]
    client = AsyncIOMotorClient(uri)
    database = client.get_default_database()
    await init_beanie(database=database, document_models=DOCUMENT_MODELS)
