"""The serve extra's floors are bare, installable, installed exactly and checked in CI.

Standard library only. Exercises scripts/serve_floors.py, which the serve-floors
workflow runs, with negative controls, and pins that workflow's wiring so the
floors job cannot be removed, skipped, made non-blocking or path-filtered.
fastapi 0.110.0 resolves starlette 0.36.3, whose TestClient passes the `app`
argument that httpx 0.28 removed; starlette 0.37.2 (first allowed by fastapi
0.110.1) dropped it, so the declared fastapi floor must stay at 0.110.1 or later.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "serve_floors.py"
WORKFLOW = ROOT / ".github" / "workflows" / "serve-floors.yml"
INSTALL_STEP = (
    "      - name: Install the serve extra at its declared floors\n"
    "        run: |\n"
    '          python scripts/serve_floors.py constraints > "$RUNNER_TEMP/serve-floors.txt"\n'
    '          cat "$RUNNER_TEMP/serve-floors.txt"\n'
    '          python -m pip install ".[serve]" pytest httpx -c "$RUNNER_TEMP/serve-floors.txt"\n'
    "          python -m pip check\n"
    "          python scripts/serve_floors.py verify\n"
    "      - name: Exercise the existing suite at the floors\n"
    "        run: python -m pytest -q tests\n"
    "      - name: Verify the installed server at the floors outside the checkout\n"
    "        working-directory: ${{ runner.temp }}\n"
    '        run: python -I "$GITHUB_WORKSPACE/scripts/check_installed_server.py"\n'
)


def _load_helper():
    spec = importlib.util.spec_from_file_location("serve_floors", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


serve_floors = _load_helper()


def test_current_pyproject_declares_bare_floors_above_the_testclient_break():
    declared = serve_floors.floors((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert set(declared) == {"fastapi", "uvicorn"}
    assert serve_floors.release(declared["fastapi"]) >= (0, 110, 1)


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ('serve = ["fastapi"]', "is not a bare name>=version floor"),
        ('serve = ["fastapi>=0.110.1,<1"]', "is not a bare name>=version floor"),
        ('serve = ["fastapi[all]>=0.110.1"]', "is not a bare name>=version floor"),
        ("serve = [\"fastapi>=0.110.1; python_version < '3.13'\"]", "is not a bare"),
        ("serve = []", "declares no serve extra"),
        ('serve = ["fastapi>=0.110.1", "FastAPI>=0.111"]', "names fastapi twice"),
    ],
    ids=["no-floor", "upper-bound", "extras", "marker", "empty", "duplicate"],
)
def test_floors_refuse_requirements_they_cannot_pin(extra, reason):
    with pytest.raises(ValueError, match=re.escape(reason)):
        serve_floors.floors("[project.optional-dependencies]\n" + extra + "\n")


def test_floors_refuse_a_missing_serve_extra():
    with pytest.raises(ValueError, match="declares no serve extra"):
        serve_floors.floors('[project]\nname = "x"\n')


@pytest.mark.parametrize(
    ("installed", "ok"),
    [("0.110.1", True), ("0.110.1.0", True), ("0.110.0", False), ("0.162.0", False),
     ("0.110.1rc1", False), ("0.110.1+local", False)],
)
def test_verify_reports_any_installed_version_other_than_the_floor(installed, ok):
    problems = serve_floors.mismatches({"fastapi": "0.110.1"}, installed=lambda name: installed)
    assert (problems == []) is ok


def test_verify_reports_a_missing_distribution():
    def absent(name):
        raise serve_floors.metadata.PackageNotFoundError(name)

    assert serve_floors.mismatches({"fastapi": "0.110.1"}, installed=absent) == [
        "fastapi: not installed (declared floor 0.110.1)"
    ]


def test_floors_workflow_installs_verifies_and_tests_the_floors_on_every_event():
    text = WORKFLOW.read_text(encoding="utf-8")
    on = re.search(r"^on:\n((?:[ \t].*\n|\n)*)", text, re.MULTILINE)
    assert on, "serve-floors.yml has no on: block"
    assert re.search(r"^  pull_request:\s*$", on.group(1), re.MULTILINE)
    assert re.search(r"^  push:\s*\n    branches: \[main\]\s*$", on.group(1), re.MULTILINE)
    assert re.search(r"^  merge_group:\s*$", on.group(1), re.MULTILINE)
    assert not re.search(r"paths|ignore|tags", on.group(1))
    assert text.count(INSTALL_STEP) == 1
    assert re.findall(r"^\s*(?:-\s+)?(if|continue-on-error)\s*:", text, re.MULTILINE) == []
