import importlib.util
from pathlib import Path


def test_checked_in_publications_pass_the_build_validation():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("validate_public_dashboard", root / "scripts/validate_public_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.validate(root / "web/data") == 3
