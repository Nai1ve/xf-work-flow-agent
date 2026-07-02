from __future__ import annotations

from pathlib import Path

ALLOWED_REQUIREMENTS = {
    "google-generativeai",
    "cohere",
    "together",
    "autogen",
    "crewai",
    "jieba",
    "pypinyin",
    "pandas",
    "numpy",
    "tenacity",
    "tiktoken",
}


def _strip_inline_comment(line: str) -> str:
    if "#" not in line:
        return line
    return line.split("#", 1)[0].strip()


def validate_requirements_lines(lines: list[str]) -> list[str]:
    errors: list[str] = []
    for raw_line in lines:
        line = _strip_inline_comment(raw_line.strip())
        if not line:
            continue
        if line.startswith("-r ") or line.startswith("--requirement "):
            errors.append(f"nested requirements files are not allowed: {raw_line.strip()}")
            continue
        if "==" not in line:
            errors.append(f"requirement must pin exact version with '==': {raw_line.strip()}")
            continue
        name, version = line.split("==", 1)
        name = name.strip().lower().replace("_", "-")
        version = version.strip()
        if not name or not version:
            errors.append(f"invalid requirement line: {raw_line.strip()}")
            continue
        if name not in ALLOWED_REQUIREMENTS:
            errors.append(f"requirement is not on the whitelist: {raw_line.strip()}")
    return errors


def validate_requirements_file(path: str | Path) -> list[str]:
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return validate_requirements_lines(f.readlines())
