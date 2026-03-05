from __future__ import annotations

from pathlib import Path

from app.db.session import db_session
from app.services.taxonomy_loader import load_taxonomy_from_excel


def _resolve_excel_path() -> Path:
    # Support both repository filename variants.
    candidates = [
        Path("/app/data/defra/UK DEFRA.xlsx"),
        Path("/app/data/defra/UK_DEFRA.xlsx"),
        Path("data/defra/UK DEFRA.xlsx"),
        Path("data/defra/UK_DEFRA.xlsx"),
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("Could not find DEFRA workbook in data/defra")


def main() -> None:
    excel_path = _resolve_excel_path()
    with db_session() as session:
        result = load_taxonomy_from_excel(session=session, excel_path=excel_path)
    print(f"Seed complete: inserted={result.inserted} sheet={result.sheet_name}")


if __name__ == "__main__":
    main()
