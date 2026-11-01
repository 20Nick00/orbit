import csv
import io
from decimal import Decimal
from numbers import Number

from fastapi.responses import Response


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Number):
        return value
    text = str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def csv_response(filename, header, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([_cell(cell) for cell in header])
    writer.writerows([_cell(cell) for cell in row] for row in rows)
    return Response(
        output.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
