"""CLI: python -m jobcopilot.discovery yc|bsm|tiers|config|all

Prints per-step counts. `all` = config sync, yc-oss, Bangalore Startup Map,
then a tier recompute. Exit code 1 if any step reported an error.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .. import companies as co
from . import run_bsm, run_yc

log = logging.getLogger("jobcopilot.discovery")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jobcopilot.discovery", description=__doc__)
    ap.add_argument("step", choices=["yc", "bsm", "tiers", "config", "all"])
    ap.add_argument("--force", action="store_true", help="ignore ETag/hash and re-import")
    ap.add_argument("--max-company-pages", type=int, default=None, help="bsm: cap per run")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    results: dict[str, dict] = {}
    if args.step in ("config", "all"):
        results["config"] = {"synced": co.sync_config_companies(co.default_config_paths())}
    if args.step in ("yc", "all"):
        results["yc"] = run_yc(force=args.force)
    if args.step in ("bsm", "all"):
        kw = {} if args.max_company_pages is None else {"max_company_pages": args.max_company_pages}
        results["bsm"] = run_bsm(force=args.force, **kw)
    if args.step in ("tiers", "all"):
        results["tiers"] = co.recompute_tiers()
    for step, res in results.items():
        print(f"{step}: {json.dumps(res, sort_keys=True)}")
    return 1 if any(isinstance(r, dict) and r.get("error") for r in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
