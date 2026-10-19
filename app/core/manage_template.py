"""Run: uv run python -m app.core.manage_template path/to/template.json"""

import argparse
import asyncio
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.business.templates import upsert_template
from app.db.session import SessionFactory, engine


class TemplateDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    system_prompt: str = Field(min_length=1, max_length=30000)
    is_active: bool = True
    service_type: Literal["conversation", "clinic"] | None = None


async def run(path):
    data = TemplateDefinition.model_validate_json(Path(path).read_text())
    try:
        async with SessionFactory() as db:
            template = await upsert_template(db, data)
            print(f"Saved template {template.slug}: {template.id}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="JSON template definition; requires operator database access")
    asyncio.run(run(parser.parse_args().path))
