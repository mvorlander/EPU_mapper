#!/usr/bin/env python3
"""Create a reproducible CryoSPARC-versus-EPU ice comparison."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ice_comparison import run_comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epu-session", required=True,
                        help="EPU session root containing EpuSession.dm and Metadata/GridSquare_*.dm")
    parser.add_argument("--exposures", required=True, help="CryoSPARC exposure .cs table")
    parser.add_argument("--passthrough", help="optional UID-aligned or UID-joinable passthrough .cs table")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ice-field", default="ctf_stats/ice_thickness_rel")
    parser.add_argument("--path-field", help="override automatic movie/micrograph path-field selection")
    parser.add_argument("--title", help="plot title")
    args = parser.parse_args()
    result = run_comparison(args.epu_session, args.exposures, args.output_dir,
                            passthrough_cs=args.passthrough, ice_field=args.ice_field,
                            path_field=args.path_field, title=args.title)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
