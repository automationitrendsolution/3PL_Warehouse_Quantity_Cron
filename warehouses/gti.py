"""GTI (myGTI IMS, ims.gti-co.com -> mygti.gti-co.com) -> gti_qty, marketplace CA.

Login -> Reporting -> Inventory -> tick "Include products with zero stock" -> Run report.
  ASIN = Product ID after '-'    e.g. ITR-B01I90VP0G -> B01I90VP0G
  QTY  = Qty                     (stock on hand - it is what the report's "Qty sum" adds up).
                                 NOT "Units", which is units per carton.
Zero-stock products are included (GTI_INCLUDE_ZERO_STOCK=true) so a sold-out ASIN is written as 0.
"""

from __future__ import annotations

import re

from .common import goto, logged_in, Result, ScrapeError, dump, find_col, read_datatable, visible, wait_quiet

SITE = "gti"
COLUMN = "gti_qty"
MARKETS = ("CA",)
ENTRY_URL = "https://ims.gti-co.com/view-users.php"  # redirects to mygti.gti-co.com
LOGIN_FIELD = 'input[name="form-username"]'


def login(page, creds, log=print) -> None:
    log("  logging in")
    page.locator(LOGIN_FIELD).first.fill(creds.username)
    page.locator('input[name="form-password"]').first.fill(creds.password)
    page.locator('form button[type="submit"]').first.click()
    page.wait_for_load_state("domcontentloaded")
    wait_quiet(page, 20000)
    if visible(page, LOGIN_FIELD):
        dump(page, SITE, "login_failed", log)
        raise ScrapeError("login did not complete - check GTI_USERNAME / GTI_PASSWORD "
                          "(debug screenshot saved)")
    logged_in(page, SITE, log)


# reports.php holds every report as a (hidden) tab pane with its own form, so all selectors are
# scoped to the Inventory form - a bare "zero stock" checkbox or "Run report" button would hit
# the hidden Products / Backorders forms first.
REPORT_URL = "https://mygti.gti-co.com/ims/reports.php?tab=inventory"
FORM = "#form-inventory-report"


def _open_inventory_report(page, log=print) -> None:
    log("  opening Reporting → Inventory")
    goto(page, REPORT_URL, log)
    wait_quiet(page, 20000)
    if not visible(page, FORM, timeout_ms=10000):  # tab not pre-selected: click it
        page.locator("#tab-inventory-report").click()
        page.locator(FORM).wait_for(state="visible", timeout=15000)


def scrape(ctx, settings, log=print) -> Result:
    page = ctx.new_page()
    goto(page, ENTRY_URL, log)
    if visible(page, LOGIN_FIELD, timeout_ms=5000):
        login(page, settings.logins[SITE], log)
    try:
        _open_inventory_report(page, log)
        box = page.locator(f'{FORM} input[name="show_zero_stock"]')
        if box.count():
            box.set_checked(settings.gti_include_zero_stock)
        log("  running the inventory report")
        page.locator(f'{FORM} button[type="submit"]').click()
        page.wait_for_load_state("domcontentloaded")
        page.locator("table thead th", has_text="Product ID").first.wait_for(timeout=120000)
        wait_quiet(page, 30000)
        headers, rows = read_datatable(page, ["Product ID", "Qty"], log)
    except Exception:
        dump(page, SITE, "inventory_report", log)
        raise
    pid_col = find_col(headers, "Product ID", required="Product ID")
    qty_col = find_col(headers, "Qty", required="Qty")
    if qty_col.strip().lower() == "units":  # never fall back to units-per-carton
        raise ScrapeError(f"'Qty' column not found - headers: {headers}")

    summary = page.locator("text=/Qty sum\\s*[\\d,]+/").first
    portal_sum = None
    if summary.count():
        m = re.search(r"Qty sum\s*([\d,]+)", summary.inner_text())
        portal_sum = int(m.group(1).replace(",", "")) if m else None

    res = Result(SITE)
    for r in rows:
        res.add(r.get(pid_col, ""), "CA", r.get(qty_col))
    page.close()
    got = sum(res.qty.values())
    if portal_sum is not None:
        log(f"  report header says Qty sum {portal_sum:,}; rows with an ASIN add up to {got:,}")
    return res
