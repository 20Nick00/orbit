"""Explicit annual IRS limits; unknown years stay unknown."""

from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from pathlib import Path

import yaml


@dataclass(frozen=True)
class YearLimits:
    year: int
    deferral_401k: Decimal
    catchup_50: Decimal
    catchup_60_63: Decimal | None
    hsa_self: Decimal
    hsa_family: Decimal
    hsa_catchup_55: Decimal


@cache
def limits_for(year: int) -> YearLimits | None:
    table = yaml.safe_load(Path(__file__).with_name("irs_limits.yaml").read_text(encoding="utf-8"))
    row = table.get(year)
    if row is None:
        return None
    return YearLimits(
        year=year,
        **{key: Decimal(str(value)) if value is not None else None for key, value in row.items()},
    )
