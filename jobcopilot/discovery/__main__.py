"""CLI: python -m jobcopilot.discovery yc|bsm|seeds|tiers|config|detect|probe|renormalize|all

Prints per-step counts. `all` = renormalize (re-key + merge rows after a
normaliser change), config sync, yc-oss, Bangalore Startup Map,
the employer-board seed datasets (Phase 1b), a tier recompute, ATS detection
(capped, see discovery/detect.py), then the rotating India probe (capped, see
discovery/probe.py and config/seeds.yaml).
Exit code 1 if any step reported an error.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .. import companies as co
from . import detect, probe, run_bsm, run_yc, seeds

log = logging.getLogger("jobcopilot.discovery")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jobcopilot.discovery", description=__doc__)
    ap.add_argument("step", choices=["yc", "bsm", "seeds", "tiers", "config", "detect", "probe", "renormalize", "all"])
    ap.add_argument("--force", action="store_true", help="ignore ETag/hash and re-import")
    ap.add_argument("--max-company-pages", type=int, default=None, help="bsm: cap per run")
    ap.add_argument("--detect-limit", type=int, default=detect.DEFAULT_LIMIT,
                    help="detect: companies checked per run")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    results: dict[str, dict] = {}
    if args.step in ("renormalize", "all"):
        results["renormalize"] = co.renormalize_names()
    if args.step in ("config", "all"):
        results["config"] = {"synced": co.sync_config_companies(co.default_config_paths())}
    if args.step in ("yc", "all"):
        results["yc"] = run_yc(force=args.force)
    if args.step in ("bsm", "all"):
        kw = {} if args.max_company_pages is None else {"max_company_pages": args.max_company_pages}
        results["bsm"] = run_bsm(force=args.force, **kw)
    if args.step in ("seeds", "all"):
        results["seeds"] = seeds.run_seeds(force=args.force)
    if args.step in ("tiers", "all"):
        results["tiers"] = co.recompute_tiers()
    if args.step in ("detect", "all"):
        results["detect"] = detect.run(limit=args.detect_limit)
    if args.step in ("probe", "all"):
        results["probe"] = probe.run_probe()
    for step, res in results.items():
        print(f"{step}: {json.dumps(res, sort_keys=True)}")
    return 1 if any(isinstance(r, dict) and r.get("error") for r in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
