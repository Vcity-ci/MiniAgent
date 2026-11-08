"""Pre-upload hygiene check: no runtime memory or obvious secrets in the tree.

Exit code 1 means something needs attention before committing/pushing.  The
script never prints a matched secret value.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    "data",
    "logs",
    ".run",
    "tests/.tmp",
    "_acl_recovery",
    ".venv",
    "venv",
    "node_modules",
    ".pytest_cache",
}
TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".mjs",
    ".html",
    ".css",
    ".md",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".txt",
    ".example",
    ".gitignore",
}
SECRET_PATTERNS = [
    ("openai/deepseek style key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    (
        "literal AGENT_API_KEY assignment",
        re.compile(
            r"(?m)^[ \t]*(?:export[ \t]+)?AGENT_API_KEY[ \t]*=[ \t]*"
            r"(?!(?:$|<|your|xxx|\$\{))[^\s#]+",
            re.IGNORECASE,
        ),
    ),
]


def _excluded(relative: Path) -> bool:
    text = relative.as_posix()
    for item in EXCLUDE_DIRS:
        if text == item or text.startswith(item + "/"):
            return True
    return False


def main() -> int:
    problems: list[str] = []
    warnings: list[str] = []

    gitignore = ROOT / ".gitignore"
    ignore_text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    for entry in [".env", "data/", "logs/", ".run/", "tests/.tmp/", "_acl_recovery/", "__pycache__/"]:
        if entry not in ignore_text:
            problems.append(f".gitignore missing {entry!r}")
    if "!.env.example" not in ignore_text:
        problems.append(".gitignore should explicitly keep .env.example")

    if (ROOT / ".env").exists():
        problems.append(
            "local .env exists (gitignored); remove it before zipping/uploading a working directory"
        )
    for name in ["_acl_recovery", "tests/.tmp", ".run"]:
        if (ROOT / name).exists():
            warnings.append(
                f"{name} is a local, gitignored artifact; it will not be committed, "
                "but remove it before zipping the directory"
            )

    for name in ["data", "logs"]:
        path = ROOT / name
        if path.exists() and any(path.rglob("*")):
            problems.append(f"{name}/ contains runtime files; run the logout button or delete them before upload")

    scanned = 0
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if _excluded(rel):
            continue
        if path.name == ".env":
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {".gitignore"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        for label, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{rel}:{line}: possible {label} [value redacted]")

    if warnings:
        for warning in warnings:
            print("WARNING:", warning)
    if problems:
        for problem in problems:
            print("FAIL:", problem)
        print(f"\nSecurity check failed ({len(problems)} issue(s)); scanned {scanned} text files.")
        return 1
    print(f"Security check OK: scanned {scanned} text files; no runtime data or obvious secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
