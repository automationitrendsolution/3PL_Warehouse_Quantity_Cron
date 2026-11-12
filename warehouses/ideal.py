"""Ideal Logistics (Extensiv 3PL Warehouse Manager, app.extensiv.com) -> ideal_qty, marketplace UK.

Login (Extensiv hub, AWS Cognito) -> 3PL Warehouse Manager -> Inventory -> Manage Inventory.
  ASIN = SKU                     (a bare ASIN, or the part after '-' that starts with B)
  QTY  = Available               one SKU can have a row per receipt - all are added up.

The Warehouse Manager is NOT part of the Extensiv page: it is an iframe (#spaWrapperIframe)
served from 3w.secure-wms.com, so every click and DOM read happens inside that frame.

Manage Inventory is a Kendo grid (one row per receipt line, "245 RESULTS") that only draws the
rows on screen. Cells carry their field name - data-field="Sku", "PrimaryAvailable" (the
"Available Primary" column), and the unique "ReceiveItemId" - so the grid is scrolled step by step
and rows are collected by ReceiveItemId until the count matches the RESULTS line. Matching by
field name also ignores the hidden Find Orders grid that lives in the same page.
"""

from __future__ import annotations

import json
import re

from .common import goto, logged_in, Result, ScrapeError, dump, norm, visible, wait_quiet

SITE = "ideal"
COLUMN = "ideal_qty"
MARKETS = ("UK",)
HUB_URL = "https://app.extensiv.com/hub/landing"
WMS_URL = "https://app.extensiv.com/3pl-warehouse-manager"
IFRAME = "#spaWrapperIframe"
DATA_HOSTS = ("secure-wms.com", "extensiv", "3plcentral")

EMAIL_SEL = ('input[type="email"], input[name="email"], input[name="username"], '
             'input#username, input#email')
PASSWORD_SEL = 'input[type="password"]'


def _needs_login(page) -> bool:
    return visible(page, PASSWORD_SEL) or visible(page, EMAIL_SEL) or "/login" in page.url.lower()


def login(page, creds, log=print) -> None:
    log("  logging in")
    page.locator(EMAIL_SEL).first.wait_for(timeout=30000)
    page.locator(EMAIL_SEL).first.fill(creds.username)
    if not visible(page, PASSWORD_SEL):  # two-step form: e-mail first, then password
        page.get_by_role("button", name=re.compile(r"next|continue|sign in|log in", re.I)).first.click()
        page.locator(PASSWORD_SEL).first.wait_for(timeout=30000)
    page.locator(PASSWORD_SEL).first.fill(creds.password)
    submit = page.locator('button[type="submit"]')  # the "LOGIN" button on app.extensiv.com/login
    if submit.count() and submit.first.is_visible():
        submit.first.click()
    else:
        page.locator(PASSWORD_SEL).first.press("Enter")
    try:
        page.get_by_text("3PL Warehouse Manager").first.wait_for(timeout=60000)
    except Exception:
        dump(page, SITE, "login_failed", log)
        if re.search(r"verification code|mfa|authenticator", page.content(), re.I):
            raise ScrapeError("Extensiv asked for a verification code (MFA / new-device check) - "
                              "log in once by hand with --headed so the session is saved") from None
        raise ScrapeError("login did not complete - check IDEAL_USERNAME / IDEAL_PASSWORD "
                          "(debug screenshot saved)") from None
    logged_in(page, SITE, log)


def _sku_key(r: dict) -> str | None:
    keys = {norm(k): k for k in r}
    return next((keys[k] for k in keys if k in ("sku", "itemsku", "skuid") or k.endswith("sku")), None)


def _avail_key(r: dict) -> str | None:
    keys = {norm(k): k for k in r}
    return next((keys[k] for k in keys if k.startswith("avail") or k in ("availableqty", "qtyavailable")),
                None)


def _records(obj, out: list) -> None:
    """Collect every dict that has a SKU-like and an Available-like key, at any depth."""
    if isinstance(obj, list):
        for x in obj:
            _records(x, out)
    elif isinstance(obj, dict):
        sku, avail = _sku_key(obj), _avail_key(obj)
        if sku and avail and not isinstance(obj[sku], (dict, list)):
            out.append(obj)
            return
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _records(v, out)


def _frame(page):
    """The Warehouse Manager iframe's current frame (it navigates: JWT login -> smartui)."""
    handle = page.wait_for_selector(IFRAME, state="attached", timeout=60000)
    frame = handle.content_frame()
    if frame is None:
        raise ScrapeError("3PL Warehouse Manager frame did not load")
    return frame


def scrape(ctx, settings, log=print) -> Result:
    page = ctx.new_page()
    goto(page, HUB_URL, log)
    wait_quiet(page, 20000)
    if _needs_login(page):
        login(page, settings.logins[SITE], log)

    try:
        log("  opening 3PL Warehouse Manager")
        goto(page, WMS_URL, log)
        fl = page.frame_locator(IFRAME)
        menu = fl.get_by_text(re.compile(r"^\s*Inventory\s*$")).filter(visible=True).first
        menu.wait_for(state="visible", timeout=90000)
        log("  opening Inventory → Manage Inventory")
        menu.click()
        manage = fl.get_by_text(re.compile(r"^\s*Manage Inventory\s*$", re.I)).filter(visible=True).first
        manage.wait_for(state="visible", timeout=30000)
        manage.click()
        fl.locator('td[data-field="Sku"]').filter(visible=True).first.wait_for(timeout=90000)
        wait_quiet(page, 20000)
        frame = _frame(page)
        rows, expected = _collect_grid(frame, log)
    except Exception:
        dump(page, SITE, "manage_inventory", log)
        raise

    if expected is not None and len(rows) != expected:
        dump(page, SITE, "manage_inventory_count", log)
        raise ScrapeError(f"read {len(rows)} grid rows but the grid says {expected} results")
    res = Result(SITE)
    for sku, avail in rows.values():
        res.add(sku, "UK", avail)
    if not res.rows_read:
        dump(page, SITE, "manage_inventory_empty", log)
        raise ScrapeError("Manage Inventory returned no rows (debug screenshot saved)")
    page.close()
    return res


_GRID_JS = r"""
() => {
  const vis = e => !!(e && e.offsetParent !== null);
  const rows = [...document.querySelectorAll('tr')].filter(tr =>
      vis(tr) && tr.querySelector('td[data-field="ReceiveItemId"]') && tr.querySelector('td[data-field="Sku"]'));
  const cell = (tr, f) => { const c = tr.querySelector(`td[data-field="${f}"]`); return c ? c.innerText.trim() : ''; };
  const got = rows.map(tr => [cell(tr, 'ReceiveItemId'), cell(tr, 'Sku'),
      cell(tr, 'PrimaryAvailable') || cell(tr, 'Available') || '0']);
  // "245 RESULTS" in the visible grid header: the tightest element whose whole text is that
  let expected = null;
  for (const e of document.querySelectorAll('div, span, label, p')) {
    if (!vis(e)) continue;
    const m = (e.innerText || '').replace(/,/g, '').trim().match(/^(\d+)\s+RESULTS?$/i);
    if (m) { expected = parseInt(m[1], 10); break; }
  }
  return {rows: got, expected};
}
"""

# The Kendo virtual grid loads further rows only on real wheel scrolling over its body.
GRID_BODY = ".k-virtual-scrollable-wrap, .k-grid-content"


def _collect_grid(frame, log=print) -> tuple[dict[str, tuple[str, str]], int | None]:
    """Wheel-scroll the virtual grid top to bottom; rows keyed by ReceiveItemId (no double
    counting) until the count matches the grid's RESULTS line, or nothing new appears."""
    rows: dict[str, tuple[str, str]] = {}
    body = frame.locator(GRID_BODY).filter(visible=True).first
    body.hover()
    mouse = frame.page.mouse
    mouse.wheel(0, -100000)  # start at the top
    frame.page.wait_for_timeout(1000)
    expected, idle = None, 0
    for i in range(3000):
        g = frame.evaluate(_GRID_JS)
        expected = g["expected"] if g["expected"] is not None else expected
        before = len(rows)
        for rid, sku, avail in g["rows"]:
            if rid:
                rows[rid] = (sku, avail)
        if expected is not None and len(rows) >= expected:
            break
        idle = idle + 1 if len(rows) == before else 0
        if idle >= 8:  # ~8 wheel turns with nothing new: end of the grid
            break
        if i % 10 == 0:
            log(f"  grid: {len(rows)}" + (f"/{expected}" if expected else "") + " rows")
        mouse.wheel(0, 450)
        frame.page.wait_for_timeout(450 if idle == 0 else 900)
    log(f"  grid: {len(rows)} rows read" + (f" of {expected} results" if expected is not None else ""))
    return rows, expected
