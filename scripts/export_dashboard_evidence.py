"""Export reviewed native-test observations without paths, raw operations or logs."""
import argparse
import json
from pathlib import Path

from sgr.paper.public_report import public_delivery_evidence

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("source", type=Path)
parser.add_argument("out", type=Path)
args = parser.parse_args()
model = public_delivery_evidence(json.loads(args.source.read_text()))
if args.out.suffix != ".json":
    parser.error("Output must be a .json file")
args.out.parent.mkdir(parents=True, exist_ok=True)
temporary = args.out.with_suffix(".json.tmp")
temporary.write_text(model.model_dump_json(indent=2) + "\n")
temporary.replace(args.out)
