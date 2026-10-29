"""
What goes into the container image. deploy.ps1 builds from the working folder, so
.dockerignore is the only thing keeping secrets and stray files out of the image.
"""

import ast
import fnmatch
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _rules() -> list[tuple[bool, str]]:
    """(is_exception, pattern) for each .dockerignore line, in order."""
    rules = []
    for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rules.append((line.startswith("!"), line.lstrip("!")))
    return rules


def _in_image(path: str) -> bool:
    """Docker's rule: the last matching line wins, and excluding a folder excludes what's in it."""
    parts = path.split("/")
    for depth in range(1, len(parts) + 1):
        candidate = "/".join(parts[:depth])
        excluded = False
        for is_exception, pattern in _rules():
            if fnmatch.fnmatchcase(candidate, pattern):
                excluded = not is_exception
        if excluded:
            return False
    return True


def _runtime_modules() -> set[str]:
    """Local modules server.py imports (and the modules they import), as file names."""
    found, queue = set(), ["server.py"]
    while queue:
        name = queue.pop()
        if name in found:
            continue
        found.add(name)
        tree = ast.parse((REPO / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for module in names:
                if (REPO / f"{module}.py").exists():
                    queue.append(f"{module}.py")
    return found


def test_image_gets_every_runtime_file():
    import server

    fonts = {f"fonts/{name}" for name in server.FONT_FILES} | {"fonts/OFL-PublicSans.txt", "fonts/OFL-IBMPlexMono.txt"}
    needed = _runtime_modules() | {"aces_facts.json", "web_ui.html", "requirements.txt"} | fonts
    missing = sorted(f for f in needed if not _in_image(f))
    assert not missing, f"left out of the image: {missing}"


def test_image_never_gets_secrets_or_dev_files():
    for path in [
        ".env",
        ".env.local",
        "_env",
        "CustomGPTToken.txt",
        "AzureSecret.txt",
        "AnthropicKey.txt",
        "local-secrets.xml",
        "aces_task_cache.json",
        "notes.txt",
        ".git/config",
        "tests/test_server.py",
        "sql/54_SEASON_BUILDER.sql",
        "deploy.ps1",
        "start-aces.ps1",
        "README.md",
    ]:
        assert not _in_image(path), path
