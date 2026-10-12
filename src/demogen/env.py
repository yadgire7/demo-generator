from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def load_env() -> None:
    """Load API keys and demo secrets from `.env` (working directory, then the package folder).

    Variables already set in the environment win.
    """
    for path in (Path.cwd() / ".env", Path(__file__).parent / ".env"):
        if path.is_file():
            load_dotenv(path, override=False)
