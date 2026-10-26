"""Environment handling. Secrets only ever come from environment variables / a local, uncommitted .env."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Read .env from the project folder, then scripts/, then the current folder (the loader script accepts the same places)."""
    for candidate in ([path] if path else [ROOT / ".env", ROOT / "scripts" / ".env", Path.cwd() / ".env"]):
        if candidate.exists():
            _read(candidate)


def _read(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:]
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def get_float(name: str, default: float) -> float:
    try:
        return float(get(name, str(default)))
    except ValueError:
        return default


def sample_mode() -> bool:
    return (get("SAMPLE_MODE", "false") or "false").lower() == "true"
