"""Run: uv run python -m app.core.manage_template path/to/template.json"""

import argparse
import asyncio
from pathlib import Path

from app.business.templates import upsert_template
from app.db.session import SessionFactory, engine
from app.types import TemplateDefinition


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


