"""Check required runtime imports and local project configuration."""
from __future__ import annotations
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODULES = [
    "fastapi", "uvicorn", "pydantic", "pydantic_settings", "sqlalchemy",
    "pandas", "numpy", "openpyxl", "reportlab", "matplotlib", "langgraph",
    "httpx", "streamlit", "pypdf", "docx", "multipart",
]

def main() -> int:
    missing = []
    for name in MODULES:
        try:
            importlib.import_module(name)
            print(f"[OK] {name}")
        except Exception as exc:
            missing.append(name)
            print(f"[MISSING] {name}: {type(exc).__name__}: {exc}")
    for folder in (ROOT / "data", ROOT / "outputs"):
        folder.mkdir(parents=True, exist_ok=True)
    if not (ROOT / ".env").exists():
        print("[INFO] .env not found. Copy .env.example to .env to customize settings.")
    if missing:
        print("\nInstall dependencies with: python -m pip install -r requirements.txt")
        return 1
    print("\nEnvironment check passed.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
