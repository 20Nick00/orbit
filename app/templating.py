from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.formatting import FILTERS

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
templates.env.filters.update(FILTERS)
