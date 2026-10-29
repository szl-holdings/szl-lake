"""Dependency contract for the szl-lake server. Standard library only.

szl_lake_server.py may import a third-party module only when the serve extra
names that module's distribution. A planted import, or a serve extra that
drops a name the server imports, fails this test. The test does not import
the server and does not install anything.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER = REPO_ROOT / "szl_lake_server.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Top-level import name -> distribution the serve extra must list.
SERVE_IMPORTS = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
}
FIRST_PARTY = frozenset({
    "szl_lake_client",
    "szl_lake_query",
    "szl_lake_receipt",
    "szl_lake_server",
    "szl_lake_store",
})


def _dist_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    if match is None:
        raise ValueError(f"unparseable requirement: {requirement!r}")
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _top_level(node: ast.AST) -> str | None:
    if isinstance(node, ast.Import) and node.names:
        return node.names[0].name.split(".")[0]
    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        return node.module.split(".")[0]
    return None


def server_imports(source: str) -> list[tuple[str, int]]:
    """Every top-level module imported by the server, including inside __main__."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            name = _top_level(node)
            if name and name != "__future__":
                found.append((name, node.lineno))
    return found


def third_party(imports: list[tuple[str, int]]) -> list[tuple[str, int]]:
    return [
        (name, line)
        for name, line in imports
        if name not in sys.stdlib_module_names and name not in FIRST_PARTY
    ]


def serve_requirements(pyproject_text: str) -> list[str]:
    """String array of [project.optional-dependencies].serve. No tomllib, so 3.10 works."""
    table = ""
    collecting = False
    buffer = ""
    for line in pyproject_text.splitlines():
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
    return []


def contract_problems(source: str, pyproject_text: str) -> list[str]:
    named = {_dist_name(item) for item in serve_requirements(pyproject_text)}
    problems = []
    for name, line in third_party(server_imports(source)):
        dist = SERVE_IMPORTS.get(name, name.replace("_", "-"))
        if _dist_name(dist) not in named:
            problems.append(
                f"szl_lake_server.py:{line} imports {name!r}, "
                f"which the serve extra does not name ({dist})"
            )
    return problems


def test_server_imports_are_named_by_the_serve_extra():
    problems = contract_problems(SERVER.read_text(encoding="utf-8"), PYPROJECT.read_text(encoding="utf-8"))
    assert problems == []


def test_serve_extra_names_fastapi_and_uvicorn():
    named = {_dist_name(item) for item in serve_requirements(PYPROJECT.read_text(encoding="utf-8"))}
    assert named == {"fastapi", "uvicorn"}


def test_negative_control_dropped_extra_fails():
    source = SERVER.read_text(encoding="utf-8")
    pyproject = '[project.optional-dependencies]\nserve = [\n  "fastapi>=0.110",\n]\n'
    problems = contract_problems(source, pyproject)
    assert any("uvicorn" in problem for problem in problems)


def test_negative_control_planted_import_fails():
    source = SERVER.read_text(encoding="utf-8") + "\nimport requests\n"
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    problems = contract_problems(source, pyproject)
    assert any("requests" in problem for problem in problems)


def test_negative_control_current_server_is_not_stdlib_only():
    names = {name for name, _line in third_party(server_imports(SERVER.read_text(encoding="utf-8")))}
    assert names == {"fastapi", "uvicorn"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
