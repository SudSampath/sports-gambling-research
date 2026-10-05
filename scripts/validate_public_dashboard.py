"""Validate only the explicit dashboard publication allowlist."""
import json
from pathlib import Path
import re

from sgr.paper.public_report import PublicCampaign, PublicEvidence


def validate(data: Path) -> int:
    manifest = json.loads((data / "index.json").read_text())
    if set(manifest) != {"schema_version", "campaigns"} or manifest["schema_version"] != 1:
        raise ValueError("Invalid publication manifest")
    filenames = manifest["campaigns"]
    if not isinstance(filenames, list) or not 1 <= len(filenames) <= 20 or len(set(filenames)) != len(filenames):
        raise ValueError("Invalid campaign file list")
    reports = []
    for name in filenames:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]+\.json", name):
            raise ValueError("Invalid publication filename")
        reports.append(PublicCampaign.model_validate_json((data / name).read_text()))
    if len({r.public_id for r in reports}) != len(reports):
        raise ValueError("Duplicate public campaign identity")
    PublicEvidence.model_validate_json((data / "recovery-proof.json").read_text())
    return len(reports)


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    print(f"Validated {validate(root / 'web/data')} allowlisted public campaign reports.")
