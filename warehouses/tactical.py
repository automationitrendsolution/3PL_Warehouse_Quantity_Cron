"""Tactical Logistic Solutions (tacticalwarehousing.com) -> tatical_qty, marketplace US.

Login -> Inventory Detail (/Order/InventoryDetail3PL) -> every row of the table.
  ASIN = Item Sku after '-'      e.g. X002V164BB-B091Y6G2Q1 -> B091Y6G2Q1
  QTY  = Units                   (not Carton Quantity); one ASIN has a row per receipt, so the
                                 Units of all its rows are added up.
The page lists every warehouse (today only Perris) unless the dropdown is changed, so it is
left as the page loads.
"""

from __future__ import annotations

from .common import assert_host, goto, logged_in, Result, ScrapeError, dump, find_col, read_datatable, visible, wait_quiet

SITE = "tactical"
LOGIN_HOSTS = ("tacticalwarehousing.com",)  # credentials are only ever typed on these (https)
COLUMN = "tatical_qty"
MARKETS = ("US",)
BASE = "https://tacticalwarehousing.com"
INVENTORY_URL = f"{BASE}/Order/InventoryDetail3PL"
LOGIN_URL = f"{BASE}/account/login"


def _logged_out(page) -> bool:
    # Logged out, the inventory URL redirects to /Home/Index (a landing page with a
    # Login/Register button), so "not on the inventory page" means "log in".
    return "inventorydetail3pl" not in page.url.lower() or visible(page, "#m_login_signin_submit")


def login(page, creds, log=print) -> None:
    log("  logging in")
    goto(page, LOGIN_URL, log)
    if not visible(page, "#email", timeout_ms=15000):
        page.get_by_role("button", name="Login/Register").first.click()
    page.locator("#email").first.wait_for(state="visible", timeout=30000)
    assert_host(page, LOGIN_HOSTS, SITE)
    page.locator("#email").first.fill(creds.username)
    page.locator("#pwd").first.fill(creds.password)
    page.locator("#m_login_signin_submit").first.click()
    try:
        page.wait_for_url(lambda u: "/account/login" not in u.lower(), timeout=45000)
    except Exception:
        dump(page, SITE, "login_failed", log)
        raise ScrapeError("login did not complete - check TACTICAL_USERNAME / TACTICAL_PASSWORD "
                          "(debug screenshot saved)") from None
    logged_in(page, SITE, log)


def scrape(ctx, settings, log=print) -> Result:
    page = ctx.new_page()
    goto(page, INVENTORY_URL, log)
    if _logged_out(page):
        login(page, settings.logins[SITE], log)
        goto(page, INVENTORY_URL, log)
    wait_quiet(page)
    # DataTables keeps the last page / search in local storage (and the saved session brings it
    # back), so the table could open on page 501-523: forget that state and reload.
    if page.evaluate("() => { const k = Object.keys(localStorage).filter(k => k.startsWith('DataTables')); "
                     "k.forEach(x => localStorage.removeItem(x)); return k.length; }"):
        goto(page, INVENTORY_URL, log)
        wait_quiet(page)
    try:
        page.locator("table thead th", has_text="Item Sku").first.wait_for(timeout=60000)
        page.wait_for_function(
            "() => [...document.querySelectorAll('table tbody tr td')].some(td => td.innerText.trim())",
            timeout=60000)
        headers, rows = read_datatable(page, ["Item Sku", "Units"], log)
    except Exception:
        dump(page, SITE, "inventory_detail", log)
        raise
    sku_col = find_col(headers, "Item Sku", required="Item Sku")
    units_col = find_col(headers, "Units", required="Units")

    res = Result(SITE)
    for r in rows:
        res.add(r.get(sku_col, ""), "US", r.get(units_col))
    page.close()
    return res
