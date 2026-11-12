"""TheFbaPrep / FulfilWave (thefbaprep.com) -> fullfilwave_qty, marketplace UK or DE.

Login -> Products -> All products (/cabinet/lot, per page "all") -> every product row.
(The Inbounds list has no ASIN - its rows are boxes - so stock is read from Products.)
  asin        = the product's ASIN from its "Alternative SKU" list (type ASIN); a product with
                no ASIN there keeps its Master SKU (FNSKU / EAN / own SKU) - nothing is dropped
  QTY         = IN STOCK QTY          (empty -> 0); products sharing an ASIN are added up
  marketplace = the row's warehouse flag: fi-gb "GB Great Blakenham" -> UK, fi-de "DE Leipzig" -> DE
Products in any other FulfilWave warehouse (PL, IT, FR, ...) are skipped and listed in the log.
"""

from __future__ import annotations

import re

from .common import (Result, ScrapeError, asin_from, dump, goto, logged_in, product_id, visible,
                     wait_quiet)

SITE = "fbaprep"
COLUMN = "fullfilwave_qty"
MARKETS = ("UK", "DE")
BASE = "https://thefbaprep.com"
LOGIN_URL = f"{BASE}/cabinet/users/login"
LOGIN_FORM = "#signin-form"
PRODUCTS_URL = f"{BASE}/cabinet/lot?Lot%5Bper_page%5D=500&per-page=500"  # "All products", all rows
FLAG_MARKET = {"gb": "UK", "uk": "UK", "de": "DE"}


def market_of(text: str, default: str) -> str:
    t = (text or "").lower()
    if re.search(r"\b(de|deu|germany|deutschland|german)\b", t):
        return "DE"
    if re.search(r"\b(gb|uk|united kingdom|england|great blakenham|blakenham)\b", t):
        return "UK"
    return default


def login(page, creds, log=print) -> None:
    log("  logging in")
    goto(page, LOGIN_URL, log)
    page.locator('input[name="Users[email]"]').fill(creds.username)
    page.locator('input[name="Users[password]"]').fill(creds.password)
    page.locator(f"{LOGIN_FORM} button[type=submit]").click()
    page.wait_for_load_state("domcontentloaded")
    wait_quiet(page, 20000)
    if visible(page, LOGIN_FORM):
        dump(page, SITE, "login_failed", log)
        raise ScrapeError("login did not complete - check FBAPREP_USERNAME / FBAPREP_PASSWORD "
                          "(debug screenshot saved)")
    logged_in(page, SITE, log)


def _close_popups(page) -> None:
    """The cabinet shows news pop-ups (Bootstrap modals) on load - click their Close button."""
    for _ in range(3):
        btn = page.locator(".modal.show .modal-footer button, .modal.in .modal-footer button, "
                           ".modal.show button.close, .modal.in button.close")
        if not btn.count() or not btn.first.is_visible():
            return
        btn.first.click()
        page.wait_for_timeout(600)


# One entry per product row (a tr with its own "ID:" cell); its Alternative SKUs live in a
# nested table inside the row.
_ROWS_JS = r"""
() => [...document.querySelectorAll('tr[data-key]')]
  .filter(tr => tr.querySelector(':scope > td[data-label="ID:"]'))
  .map(tr => {
    const own = l => { const c = tr.querySelector(`:scope > td[data-label="${l}"]`);
                       return c ? c.innerText.trim() : ''; };
    const flag = tr.querySelector(':scope > td span.fi');
    const code = flag ? ([...flag.classList].find(c => /^fi-[a-z]{2}$/.test(c)) || '').slice(3) : '';
    const alts = [...tr.querySelectorAll('tr')].map(r => {
      const ty = r.querySelector('td[data-label="Type:"]');
      const sku = r.querySelector('td[data-label="Alternative SKU:"]');
      return ty && sku ? [ty.innerText.trim(), sku.innerText.trim()] : null;
    }).filter(Boolean);
    return {id: own('ID:'), master: own('Master SKU:'), qty: own('In stock qty:'),
            flag: code, warehouse: flag ? (flag.getAttribute('title') || '') : '', alts};
  })
"""


def _next_page_href(page) -> str | None:
    return page.evaluate("""
    () => {
      const a = document.querySelector(
        'ul.pagination li.next:not(.disabled) a, ul.pagination a[rel="next"], a[rel="next"]');
      return a ? a.href : null;
    }""")


def _found(page) -> int | None:
    """'Found: 946' above the grid."""
    m = re.search(r"Found:\s*([\d\s,]+)", page.evaluate("() => document.body.innerText") or "")
    return int(re.sub(r"\D", "", m.group(1))) if m else None


def product_key(master: str, alts: list[list[str]]) -> str | None:
    """The ASIN from the Alternative SKU list; else an ASIN inside the Master SKU; else the
    Master SKU itself (or the first alternative code when the Master SKU is empty)."""
    for typ, code in alts:
        if typ.strip().upper() == "ASIN":
            a = asin_from(code)
            if a:
                return a
    return asin_from(master) or product_id(master) or next(
        (product_id(code) for _, code in alts if product_id(code)), None)


def scrape(ctx, settings, log=print) -> Result:
    page = ctx.new_page()
    goto(page, f"{BASE}/cabinet", log)
    if visible(page, LOGIN_FORM) or "/users/login" in page.url:
        login(page, settings.logins[SITE], log)
    _close_popups(page)
    try:
        log("  opening Products → All products (all rows)")
        goto(page, PRODUCTS_URL, log)
        wait_quiet(page, 30000)
        _close_popups(page)
        page.locator('td[data-label="ID:"]').first.wait_for(timeout=60000)
    except Exception:
        dump(page, SITE, "all_products", log)
        raise ScrapeError("All products list did not load (debug screenshot saved)") from None

    expected = _found(page)
    res = Result(SITE)
    seen: set[str] = set()
    seen_urls, pages = set(), 0
    markets_seen: dict[str, int] = {}
    other_wh: dict[str, int] = {}
    no_asin = 0
    while True:
        rows = page.evaluate(_ROWS_JS)
        for r in rows:
            if not r["id"] or r["id"] in seen:
                continue
            seen.add(r["id"])
            mkt = FLAG_MARKET.get(r["flag"].lower())
            if not mkt:
                other_wh[r["warehouse"] or r["flag"] or "?"] = other_wh.get(r["warehouse"] or r["flag"] or "?", 0) + 1
                continue
            key = product_key(r["master"], r["alts"])
            if not key:
                continue
            if not any(t.strip().upper() == "ASIN" for t, _ in r["alts"]):
                no_asin += 1
            markets_seen[mkt] = markets_seen.get(mkt, 0) + 1
            res.add(key, mkt, r["qty"])
        pages += 1
        log(f"  page {pages}: {len(seen)}" + (f"/{expected}" if expected else "") + " products")
        seen_urls.add(page.url)
        nxt = _next_page_href(page)
        if not nxt or nxt in seen_urls or pages >= 100:
            break
        goto(page, nxt, log)
        wait_quiet(page, 20000)
        _close_popups(page)
    log(f"  {len(seen)} products over {pages} page(s): {markets_seen}"
        + (f"; {no_asin} without an ASIN entry" if no_asin else "")
        + (f"; other warehouses skipped: {other_wh}" if other_wh else ""))
    if expected is not None and len(seen) < expected:
        dump(page, SITE, "all_products_count", log)
        raise ScrapeError(f"read {len(seen)} products but the page says Found: {expected}")
    page.close()
    return res
