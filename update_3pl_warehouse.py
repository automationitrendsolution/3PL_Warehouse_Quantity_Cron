#!/usr/bin/env python3
"""Weekly 3PL stock sync -> `3PL_Warehouse_Quantity`.

Logs in to each third-party warehouse portal with Playwright, reads the stock per ASIN and
writes it to the warehouse database, one row per (asin, marketplace):

    Tactical    (US)     Inventory Detail            Item Sku -> ASIN, Units      -> tatical_qty
    GTI         (CA)     Reporting > Inventory       Product ID -> ASIN, Qty      -> gti_qty
    Ideal       (UK)     Extensiv Manage Inventory   SKU -> ASIN, Available       -> ideal_qty
    TheFbaPrep  (UK/DE)  Inbounds > All inbounds     ASIN, Product qty            -> fullfilwave_qty

A row whose SKU has no ASIN (an EAN barcode, an FNSKU, an own SKU) is kept under that code.
Each portal is scraped and written on its own: one portal failing never blocks or blanks the
others. Exit status: 0 all good, 1 at least one portal failed, 2 configuration error.

    ./run_3pl_warehouse.sh --dry-run            # scrape every portal, show, write nothing
    ./run_3pl_warehouse.sh --only gti,tactical  # just these portals
    ./run_3pl_warehouse.sh --headed             # watch the browser (needs a display)
    ./run_3pl_warehouse.sh --logs               # run history + the last run's log
    ./run_3pl_warehouse.sh                      # the real run
"""

from __future__ import annotations

import argparse
import sys
import traceback

from warehouses import db, fbaprep, gti, ideal, tactical, ui
from warehouses.common import ScrapeError, new_context, save_state
from warehouses.config import TABLE, ConfigError, from_env, load_env, redact

SCRAPERS = {m.SITE: m for m in (tactical, gti, ideal, fbaprep)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help=f"comma-separated: {', '.join(SCRAPERS)}")
    ap.add_argument("--dry-run", action="store_true", help="scrape and show; no database writes")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--create-table", action="store_true", help="create the table and exit")
    ap.add_argument("--show", type=int, default=10, help="top ASINs to show per portal (default 10)")
    ap.add_argument("--logs", nargs="?", const=15, type=int, metavar="N",
                    help="show the last N runs (default 15) and the last run's log, then exit")
    ap.add_argument("--plain", action="store_true", help="plain log lines even on a terminal")
    args = ap.parse_args()

    if args.logs is not None:
        return ui.history(args.logs)

    sites = [x.strip().lower() for x in args.only.split(",") if x.strip()] or list(SCRAPERS)
    unknown = [x for x in sites if x not in SCRAPERS]
    if unknown:
        print(f"unknown portal {unknown} - use {', '.join(SCRAPERS)}", file=sys.stderr)
        return 2

    view = ui.UI(sites, "dry-run" if args.dry_run else "live", force_plain=args.plain)
    load_env()
    try:
        s = from_env([] if args.create_table else sites, need_db=not args.dry_run)
    except ConfigError as exc:
        view.config_error(str(exc))
        return 2
    if args.headed:
        s.headless = False
    view.header(s.describe())

    if args.create_table:
        db.ensure_table(s)
        view.log(f"table `{TABLE}` is ready")
        view.finish([])
        return 0
    if not args.dry_run:
        try:
            db.ensure_table(s)
        except Exception as exc:
            view.config_error(f"database not reachable: {redact(str(exc), s.secrets)}", status=1)
            return 1

    from playwright.sync_api import sync_playwright

    failed = []
    with view, sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chromium", headless=s.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-http2"])  # GTI's server drops HTTP/2 streams
        for site in sites:
            mod = SCRAPERS[site]
            view.start(site, mod.COLUMN, mod.MARKETS)
            ctx = new_context(browser, site, view.log)
            ctx.set_default_timeout(s.timeout_s * 1000)
            try:
                res = mod.scrape(ctx, s, view.log)
                save_state(ctx, site)
                if not res.qty:
                    raise ScrapeError(f"no products found in {res.rows_read} rows - not writing, "
                                      f"the last good figures stay")
                view.log(f"{res.rows_read} rows → {len(res.qty)} products, "
                         f"total qty {sum(res.qty.values()):,}")
                if res.non_asin:
                    view.log(f"{len(res.non_asin)} product(s) have no ASIN in the SKU - kept under "
                             f"their own code (EAN / FNSKU / SKU)")
                written = None
                if not args.dry_run:
                    view.log("writing to the database")
                    written = db.apply(s, mod.COLUMN, mod.MARKETS, res.qty, view.log)
                top = sorted(res.qty.items(), key=lambda x: -x[1])[:args.show]
                view.done(site, res, top, written, sorted(res.non_asin))
            except Exception as exc:
                failed.append(site)
                msg = redact(f"{exc.__class__.__name__}: {str(exc).splitlines()[0] if str(exc) else ''}",
                             s.secrets)
                trace = ([] if isinstance(exc, ScrapeError)
                         else redact(traceback.format_exc(), s.secrets).rstrip().splitlines()[-6:])
                view.failed(site, msg, trace)
            finally:
                ctx.close()
        browser.close()

    if not args.dry_run and len(failed) < len(sites):
        try:
            view.db_summary(db.summary(s))
        except Exception as exc:
            view._write("WARN", f"could not read the table summary: {redact(str(exc), s.secrets)}")
    view.finish(failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
