"""Install the serve extra at its declared floors, then prove that happened.

    python scripts/serve_floors.py constraints > floors.txt   # name==floor lines
    python -m pip install ".[serve]" pytest httpx -c floors.txt
    python scripts/serve_floors.py verify                      # installed == floors

This is the lowest-direct strategy for the serve extra: its direct dependencies
at their declared floors, everything else resolved normally. Every serve
requirement must be a bare ``name>=version`` floor. A requirement without a
floor, or with any other specifier, extra or marker, fails closed, so a floor
that nothing installs cannot be declared. Standard library only. Python 3.10
has no tomllib, so the extra is read the way tests/test_dependency_contract.py
reads it and cross-checked with tomllib where tomllib exists.
"""
from __future__ import annotations

import ast
import re
import sys
from importlib import metadata
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
FLOOR = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*>=\s*([0-9]+(?:\.[0-9]+)*)")


def serve_requirements(text: str) -> list[str]:
    """String array of [project.optional-dependencies].serve, without tomllib."""
    table = ""
    collecting = False
    buffer = ""
    for line in text.splitlines():
        header = re.match(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$", line)
        if header and not collecting:
            table = header.group(1).strip()
            continue
        if not collecting:
            if table != "project.optional-dependencies":
                continue
            started = re.match(r"^\s*serve\s*=\s*(\[.*)$", line)
            if started is None:
                continue
            collecting = True
            buffer = started.group(1)
        else:
            buffer += "\n" + line
        try:
            value = ast.literal_eval(buffer)
        except SyntaxError:
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("serve extra is not an array of strings")
        return value
    if collecting:
        raise ValueError("serve extra array is not closed")
    return []


def floors(text: str) -> dict[str, str]:
    """Distribution name -> declared floor for every serve requirement."""
    requirements = serve_requirements(text)
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        pass
    else:
        project = tomllib.loads(text).get("project", {})
        declared = project.get("optional-dependencies", {}).get("serve", [])
        if declared != requirements:
            raise ValueError(f"serve extra read as {requirements!r}; tomllib reads {declared!r}")
    if not requirements:
        raise ValueError("pyproject.toml declares no serve extra")
    found: dict[str, str] = {}
    for requirement in requirements:
        match = FLOOR.fullmatch(requirement.strip())
        if match is None:
            raise ValueError(f"serve requirement {requirement!r} is not a bare name>=version floor")
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        if name in found:
            raise ValueError(f"serve extra names {name} twice")
        found[name] = match.group(2)
    return found


def release(version: str) -> tuple[int, ...]:
    """Release tuple without trailing zeros; pre, post, dev and local versions are refused."""
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version) is None:
        raise ValueError(f"{version!r} is not a final release version")
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def mismatches(declared: dict[str, str], installed=metadata.version) -> list[str]:
    """Every declared floor whose installed version is absent or different."""
    problems = []
    for name, floor in declared.items():
        try:
            have = installed(name)
        except metadata.PackageNotFoundError:
            problems.append(f"{name}: not installed (declared floor {floor})")
            continue
        try:
            same = release(have) == release(floor)
        except ValueError as exc:
            problems.append(f"{name}: {exc}")
            continue
        if not same:
            problems.append(f"{name}: installed {have}, declared floor {floor}")
    return problems


def main(argv: list[str]) -> int:
    if argv not in (["constraints"], ["verify"]):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        declared = floors(PYPROJECT.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if argv == ["constraints"]:
        for name, floor in declared.items():
            print(f"{name}=={floor}")
        return 0
    problems = mismatches(declared)
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if problems:
        return 1
    pinned = ", ".join(f"{name} {floor}" for name, floor in declared.items())
    print(f"OK: serve extra installed at its declared floors ({pinned})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
