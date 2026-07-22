from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_python_sources_do_not_import_parent_pyvideotrans_package():
    forbidden = ("import videotrans", "from videotrans")
    violations = []
    for path in (PROJECT_ROOT / "cineflow").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in forbidden):
            violations.append(path.relative_to(PROJECT_ROOT).as_posix())
    assert violations == []


def test_directory_contains_everything_needed_when_moved_to_a_new_repo():
    required = [
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "Dockerfile",
        ".env.example",
        ".github/workflows/ci.yml",
    ]
    missing = [name for name in required if not (PROJECT_ROOT / name).is_file()]
    assert missing == []
