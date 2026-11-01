"""Generate fake text PDFs only in the test's temporary directory."""

from pathlib import Path

from fpdf import FPDF

A_ROWS = [
    ("Gross Pay", "10,417.00", "208,340.00"),
    ("Federal Income Tax", "1,493.00", "29,860.00"),
    ("NY State Income Tax", "540.00", "10,800.00"),
    ("NYC Income Tax", "330.00", "6,600.00"),
    ("Social Security", "646.00", "12,920.00"),
    ("Medicare", "151.00", "3,020.00"),
    ("401(k) Pre-tax", "1,042.00", "20,840.00"),
    ("HSA", "165.00", "3,300.00"),
    ("Medical Premium", "142.00", "2,840.00"),
    ("Dental Premium", "18.00", "360.00"),
    ("Employer 401(k) Match", "417.00", "8,340.00"),
    ("Net Pay", "5,890.00", "117,800.00"),
]
B_ROWS = [
    ("Federal Withholding", "500.00", "6,000.00"),
    ("State Tax", "200.00", "2,400.00"),
    ("OASDI", "248.00", "2,976.00"),
    ("Medicare", "58.00", "696.00"),
    ("401K Pre-Tax", "300.00", "3,600.00"),
    ("Health", "100.00", "1,200.00"),
    ("Roth 401K", "200.00", "2,400.00"),
    ("ER HSA", "50.00", "600.00"),
]


def write_pdf(path: Path, lines: list[str], password: str | None = None) -> Path:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=9)
    for line in lines:
        pdf.cell(w=0, h=6, text=line, new_x="LMARGIN", new_y="NEXT")
    if password is not None:
        pdf.set_encryption(owner_password="synthetic-owner", user_password=password)
    pdf.output(str(path))
    return path


def stub_a(
    path: Path,
    pay_date: str = "10/30/2026",
    period: str = "10/16/2026 - 10/31/2026",
    overrides: dict[str, tuple[str, str | None]] | None = None,
    extra: list[str] | None = None,
) -> Path:
    rows = []
    for label, current, ytd in A_ROWS:
        current, ytd = (overrides or {}).get(label, (current, ytd))
        rows.append(f"{label} {current}" + (f" {ytd}" if ytd is not None else ""))
    return write_pdf(
        path,
        [
            "Northwind Test Labs Inc",
            f"Pay Date {pay_date}",
            f"Period {period}",
            "Description / Current / YTD",
            *rows,
            *(extra or []),
        ],
    )


def stub_b(
    path: Path,
    pay_date: str = "06/12/2026",
    period: str = "05/30/2026 to 06/12/2026",
    overrides: dict[str, tuple[str, str]] | None = None,
) -> Path:
    pdf = FPDF(orientation="L")
    pdf.add_page()
    pdf.set_font("Helvetica", size=9)
    for line in [
        "Contoso Test Corp",
        f"Check Date {pay_date}",
        f"Pay Period {period}",
        "Description Current YTD",
    ]:
        pdf.cell(w=0, h=6, text=line, new_x="LMARGIN", new_y="NEXT")
    left = [
        "Regular 80.00 50.00 $4,000.00 $48,000.00",
        "Gross Pay $4,000.00 $48,000.00",
        "Net Pay $2,394.00 $28,728.00",
    ]
    for i, (label, current, ytd) in enumerate(B_ROWS):
        current, ytd = (overrides or {}).get(label, (current, ytd))
        pdf.cell(w=130, h=6, text=left[i] if i < len(left) else "")
        # Parentheses indicate deductions here. Employer contributions are positive;
        # a negative employer amount is a reversal, tested separately.
        amounts = f"${current} ${ytd}" if label == "ER HSA" else f"(${current}) (${ytd})"
        pdf.cell(w=0, h=6, text=f"{label} {amounts}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(w=0, h=6, text="Net Pay $2,394.00 $28,728.00", border=1)
    pdf.output(str(path))
    return path
