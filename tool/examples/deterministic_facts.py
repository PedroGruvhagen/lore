#!/usr/bin/env python3
"""Python equivalent of quickstart.sh's fact lookup: bootstrap a scratch lore
directory, add the example page and its extract, then read a value back with
`lore.py fact` via subprocess, the same way any Python-based tool would.
Uses only the standard library; writes only under a temp directory."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LORE_PY = REPO_ROOT / "skill" / "scripts" / "lore.py"
EXAMPLE_PAGE = Path(__file__).resolve().parent / "pages" / "example-api.md"

EXTRACT = {
    "endpoint": "https://api.example.com/v1/complete",
    "models": {"large": "example-model-large", "mid": "example-model-mid"},
}


def run(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(LORE_PY), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        lore_dir = Path(tmp) / "lore"
        print("== bootstrap ==")
        print(run("bootstrap", "--lore-dir", str(lore_dir)))

        pages_dir = lore_dir / "pages"
        extracts_dir = lore_dir / "extracts"
        shutil.copy(EXAMPLE_PAGE, pages_dir / "example-api.md")
        extracts_dir.mkdir(parents=True, exist_ok=True)
        (extracts_dir / "example-api.json").write_text(
            json.dumps(EXTRACT, indent=2), encoding="utf-8"
        )

        print("== index ==")
        print(run("index", "--lore-dir", str(lore_dir)))

        print("== fact ==")
        endpoint = run("fact", "example-api", "endpoint", "--lore-dir", str(lore_dir))
        print(f"endpoint fact: {endpoint}")

        model = run(
            "fact", "example-api", "models.large", "--lore-dir", str(lore_dir)
        )
        print(f"large model fact: {model}")

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
