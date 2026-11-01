"""Extract text and money without guessing a payroll provider's layout."""

import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException

NUMBER = r"\$?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2,4}"
MONEY_RE = re.compile(rf"(?<![\w.,$+/-])(?:\({NUMBER}\)|-{NUMBER}|{NUMBER}-?)(?![\w.,/)\-])")
DATE_PATTERN = (
    r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})|[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})"
)
DATE_RE = re.compile(rf"\b{DATE_PATTERN}\b")


@dataclass
class ParsedLine:
    label: str
    amount: Decimal | None
    ytd: Decimal | None
    repeats: int = 0
    merged: bool = False


@dataclass
class ParsedStub:
    lines: list[ParsedLine] = field(default_factory=list)
    pay_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    header_candidates: list[str] = field(default_factory=list, repr=False)
    text_for_matching: str = field(default="", repr=False)


class UnreadablePdf(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@contextmanager
def _private_pdf_logs() -> Iterator[None]:
    # pdfminer debug logs contain raw PDF tokens, including payroll text.
    thread_id = threading.get_ident()

    class OtherThreadsOnly(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return record.thread != thread_id

    blocker = OtherThreadsOnly()
    names = list(logging.Logger.manager.loggerDict)
    loggers = [
        logging.getLogger(name)
        for name in names
        if name == "pdfminer"
        or name.startswith("pdfminer.")
        or name == "pdfplumber"
        or name.startswith("pdfplumber.")
    ]
    for logger in loggers:
        logger.addFilter(blocker)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(blocker)


def normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def clean_label(text: str) -> str:
    text = DATE_RE.sub(" ", text)
    # One-decimal hours ("80.0") aren't money tokens but must not make the label vary per stub.
    text = re.sub(r"(?<![\w.,])\d+\.\d(?![\w.,])", " ", text)
    return " ".join(text.strip(" \t*$:").split())


def _date(value: str) -> date | None:
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(" ".join(value.split()), fmt).date()
        except ValueError:
            pass
    return None


def _dates(text: str) -> tuple[date | None, date | None, date | None]:
    found: list[list[date | None]] = [[], [], []]
    labels = (
        r"(?:Pay|Check|Advice|Payment)\s+Date",
        r"Period\s+(?:Begin(?:ning)?|Start)",
        r"Period\s+End(?:ing)?",
    )
    for i, label in enumerate(labels):
        for match in re.finditer(rf"\b{label}\s*:?\s*({DATE_PATTERN})\b", text, re.I):
            found[i].append(_date(match[1]))
    for match in re.finditer(
        rf"\b(?:Pay\s+)?Period\s*:?\s*({DATE_PATTERN})\s*(?:-|to)\s*({DATE_PATTERN})\b", text, re.I
    ):
        found[1].append(_date(match[1]))
        found[2].append(_date(match[2]))
    result = [
        values[0] if values and None not in values and len(set(values)) == 1 else None
        for values in found
    ]
    if result[1] and result[2] and result[1] > result[2]:
        result[1] = result[2] = None
    return result[0], result[1], result[2]


def parse_lines(text: str) -> list[ParsedLine]:
    """Public text-level parser for testing and future extraction adapters."""
    rows: dict[str, ParsedLine] = {}
    seen: set[tuple[str, Decimal | None, Decimal | None]] = set()

    def add(label: str, tokens: list[Decimal]) -> None:
        label = clean_label(label)
        if not label or not tokens:
            return
        amount, ytd = (tokens[-2], tokens[-1]) if len(tokens) > 1 else (tokens[0], None)
        key = normalize(label)
        signature = (key, amount, ytd)
        if signature in seen:
            rows[key].repeats += 1
            return
        seen.add(signature)
        if key in rows:
            prior = rows[key]
            prior.merged = True
            prior.amount = prior.amount + amount if prior.amount is not None else None
            prior.ytd = prior.ytd + ytd if prior.ytd is not None and ytd is not None else None
        else:
            rows[key] = ParsedLine(label, amount, ytd)

    for line in text.splitlines():
        label, tokens, end = "", [], 0
        for match in MONEY_RE.finditer(line):
            between = line[end : match.start()]
            if between.strip(" \t/|$"):
                add(label, tokens)
                label, tokens = between, []
            token = re.sub(r"[$,()\-]", "", match[0])
            sign = -1 if "-" in match[0] or "(" in match[0] else 1
            tokens.append(Decimal(token) * sign)
            end = match.end()
        add(label, tokens)
    return list(rows.values())


def parse_pdf(path: str | Path) -> ParsedStub:
    try:
        with _private_pdf_logs(), Path(path).open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise UnreadablePdf("This file is not a PDF.")
            stream.seek(0)
            # Owner-only encryption opens with an empty password; a real password raises below.
            with pdfplumber.open(stream, strict_metadata=True) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
    except UnreadablePdf:
        raise
    except PDFPasswordIncorrect:
        raise UnreadablePdf("This PDF is password-protected.") from None
    except PdfminerException as exc:
        if exc.args and isinstance(exc.args[0], PDFPasswordIncorrect):
            raise UnreadablePdf("This PDF is password-protected.") from None
        raise UnreadablePdf(
            "This PDF could not be read; it may be damaged or inaccessible."
        ) from None
    except Exception:
        raise UnreadablePdf(
            "This PDF could not be read; it may be damaged or inaccessible."
        ) from None
    text = "\n".join(pages)
    if not text.strip():
        raise UnreadablePdf("No extractable text. A scan needs OCR, which is not supported.")
    pay_date, start, end = _dates(text)
    headers = [
        line.strip() for line in pages[0].splitlines() if line.strip() and not MONEY_RE.search(line)
    ][:5]
    return ParsedStub(parse_lines(text), pay_date, start, end, headers, " ".join(text.split()))
