from dataclasses import dataclass

from app.models.prompt_eval import EvalRole


@dataclass(frozen=True)
class SeedJd:
    role: EvalRole
    # Stable `seedKey` stored on the document; the first posting of the original roles keeps the
    # short `seed:<role>` key so existing databases are not seeded twice.
    key: str
    title: str
    company: str
    text: str


def key_for(role: str, slug: str) -> str:
    return f"seed:{role}:{slug}"
