"""CI's packaging check: install the built wheel into empty virtual environments and check what comes with it.

    python scripts/check_install.py dist

The plain install must bring urllib3 and truststore and nothing else; atgeo[async] must add exactly what
aiohttp's own metadata requires on this Python: computed, by resolving aiohttp alone into another empty
environment, rather than listed, since it differs between Python versions.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*args: str | Path) -> str:
    return subprocess.run([str(a) for a in args], check=True, capture_output=True, text=True).stdout


def installed(python: Path) -> set[str]:
    out = run("uv", "pip", "list", "--python", python, "--format", "json")
    return {p["name"].lower().replace("_", "-") for p in json.loads(out)}


def main(dist: Path) -> None:
    (wheel,) = dist.glob("atgeo-*.whl")
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        run("uv", "venv", "--python", sys.executable, venv)
        python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

        run("uv", "pip", "install", "--python", python, wheel)
        plain = installed(python) - {"atgeo"}
        if plain != {"urllib3", "truststore"}:
            sys.exit(f"the plain install brought {sorted(plain)}, not urllib3 and truststore alone")
        run(python, "-c", "import atgeo; atgeo.AtgeoClient().close()")
        print("plain install:", sorted(plain))

        run("uv", "pip", "install", "--python", python, f"atgeo[async] @ {wheel.resolve().as_uri()}")
        added = installed(python) - plain - {"atgeo"}
        # What aiohttp needs here, by its own metadata: aiohttp alone, resolved into another empty environment.
        bare = Path(tmp) / "bare"
        run("uv", "venv", "--python", sys.executable, bare)
        bare_python = bare / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        run("uv", "pip", "install", "--python", bare_python, "aiohttp")
        expected = installed(bare_python) - plain
        if added != expected:
            sys.exit(f"atgeo[async] added {sorted(added)}; aiohttp alone brings {sorted(expected)}")
        run(python, "-c", "import atgeo, asyncio; asyncio.run(atgeo.AsyncAtgeoClient().aclose())")
        print("async extra added:", sorted(added))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
