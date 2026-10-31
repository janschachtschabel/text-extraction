"""The UI's Jinja2 templates, with autoescape on for every template whatever its name
(ruling P10-S12)."""

from pathlib import Path
from typing import Final

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR: Final = Path(__file__).parent / "templates"

templates: Final = Jinja2Templates(
    env=Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
)
