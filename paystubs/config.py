"""Environment configuration; loading settings has no filesystem side effects."""

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "data"
    rules_dir: Path | None = None
    watch_polling: bool = False
    background: bool = True
    hsa_coverage: str = "self"
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = field(default="", repr=False)
    imap_sender: tuple[str, ...] = ()
    imap_label: str = ""
    imap_lookback_days: int = 45
    imap_day_of_month: int = 1

    def __post_init__(self) -> None:
        if self.rules_dir is None:
            object.__setattr__(self, "rules_dir", self.data_dir / "paystubs" / "rules")
        if self.hsa_coverage not in {"self", "family"}:
            logging.getLogger(__name__).warning("Invalid PAYSTUB_HSA_COVERAGE; using self")
            object.__setattr__(self, "hsa_coverage", "self")
        if not 1 <= self.imap_port <= 65535:
            raise ValueError("IMAP port must be between 1 and 65535")
        if self.imap_lookback_days < 0:
            raise ValueError("IMAP lookback days cannot be negative")
        if not 1 <= self.imap_day_of_month <= 31:
            raise ValueError("IMAP day of month must be between 1 and 31")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        return cls(
            data_dir=Path(env.get("ORBIT_DATA_DIR", str(ROOT / "data"))),
            rules_dir=Path(env["PAYSTUB_RULES_DIR"]) if env.get("PAYSTUB_RULES_DIR") else None,
            watch_polling=env.get("PAYSTUB_WATCH_POLLING") == "1",
            background=env.get("PAYSTUB_BACKGROUND", "1") != "0",
            hsa_coverage=env.get("PAYSTUB_HSA_COVERAGE", "self"),
            imap_host=env.get("PAYSTUB_IMAP_HOST", ""),
            imap_port=int(env.get("PAYSTUB_IMAP_PORT", "").strip() or "993"),
            imap_user=env.get("PAYSTUB_IMAP_USER", ""),
            imap_password=env.get("PAYSTUB_IMAP_PASSWORD", ""),
            imap_sender=tuple(
                s.strip() for s in env.get("PAYSTUB_IMAP_SENDER", "").split(",") if s.strip()
            ),
            imap_label=env.get("PAYSTUB_IMAP_LABEL", ""),
            imap_lookback_days=int(env.get("PAYSTUB_IMAP_LOOKBACK_DAYS", "").strip() or "45"),
            imap_day_of_month=int(env.get("PAYSTUB_IMAP_DAY_OF_MONTH", "").strip() or "1"),
        )

    @property
    def imap_enabled(self) -> bool:
        return bool(
            self.imap_host
            and self.imap_user
            and self.imap_password
            and (self.imap_sender or self.imap_label)
        )

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox" / "paystubs"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "paystubs" / "archive"

    @property
    def pending_dir(self) -> Path:
        return self.data_dir / "paystubs" / "pending"

    @property
    def failed_dir(self) -> Path:
        return self.data_dir / "paystubs" / "failed"

    @property
    def duplicates_dir(self) -> Path:
        return self.data_dir / "paystubs" / "duplicates"


def load_env_file(path: str | Path) -> None:
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if value.startswith(("'", '"')):
            closing = value.find(value[0], 1)
            remainder = value[closing + 1 :].strip() if closing >= 0 else ""
            if closing < 0 or (remainder and not remainder.startswith("#")):
                continue
            value = value[1:closing]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        os.environ.setdefault(key, value)
