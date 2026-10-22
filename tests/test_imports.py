"""What the package imports: the counterpart of client.test.ts's source scans. No reference counterpart."""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "atgeo"
MODULES = sorted(SRC.glob("*.py"))
ALLOWED = {"urllib3", "truststore"}
ASYNC_ONLY = {"aiohttp", "yarl"}


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_reads_the_source_files() -> None:
    assert len(MODULES) > 10


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_imports_only_the_standard_library_urllib3_and_truststore(path: Path) -> None:
    allowed = ALLOWED | (ASYNC_ONLY if path.name == "_aiohttp.py" else set())
    outside = {m for m in imports_of(path) if m not in sys.stdlib_module_names and m not in allowed and m != "atgeo"}
    assert outside == set()


BLOCKED = textwrap.dedent(
    """
    import sys

    class Block:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in ("aiohttp", "yarl"):
                raise ImportError(f"blocked: {name}")
            return None

    sys.meta_path.insert(0, Block())
    import atgeo
    """
)


def run_blocked(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", BLOCKED + textwrap.dedent(code)], capture_output=True, text=True, timeout=60, check=False
    )


def test_imports_the_blocking_client_without_aiohttp() -> None:
    done = run_blocked(
        """
        atgeo.AtgeoClient().close()
        atgeo.Urllib3Transport
        assert "aiohttp" not in sys.modules
        print("ok")
        """
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def test_names_the_extra_when_aiohttp_is_missing() -> None:
    done = run_blocked(
        """
        try:
            atgeo.AiohttpTransport
        except ImportError:
            print("transport: ImportError")
        try:
            atgeo.AsyncAtgeoClient()
        except ImportError as e:
            print("client:", e)
        """
    )
    assert done.returncode == 0, done.stderr
    lines = done.stdout.strip().splitlines()
    assert lines[0] == "transport: ImportError"
    assert lines[1].startswith("client:")
    assert "atgeo[async]" in lines[1]


def test_does_not_import_aiohttp_on_import_atgeo() -> None:
    done = subprocess.run(
        [sys.executable, "-c", "import sys, atgeo; print('aiohttp' in sys.modules)"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.stdout.strip() == "False", done.stderr
