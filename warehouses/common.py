"""Shared scraping helpers: ASIN extraction, number parsing, HTML-table and DataTables reading,
saved login sessions and debug dumps.

Every scraper finds its columns by HEADER TEXT, never by position, so a portal that adds or
reorders a column keeps working. When a page does not look as expected, `dump()` saves a
screenshot and the HTML under logs/debug/ so the selector can be fixed from what was actually
served.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import JOB_DIR

STATE_DIR = JOB_DIR / "state"
DEBUG_DIR = JOB_DIR / "logs" / "debug"

ASIN_PART = re.compile(r"^B[0-9A-Z]{9}$")
ASIN_ANY = re.compile(r"(?<![0-9A-Z])(B[0-9A-Z]{9})(?![0-9A-Z])")


class ScrapeError(Exception):
    """The portal did not give us what we need - nothing is written for that warehouse."""


def _fix_asin(code: str) -> str:
    # ASINs start "B0"; a SKU typed as "BOBXPR76Q2" (letter O) is the same product as B0BXPR76Q2.
    return "B0" + code[2:] if code[1] == "O" else code


def asin_from(sku: str) -> str | None:
    """'X002V164BB-B091Y6G2Q1' -> 'B091Y6G2Q1'; 'ITR-B01I90VP0G' -> 'B01I90VP0G'; a bare ASIN
    -> itself. The part after '-' that is a 10-character code starting with B wins; otherwise
    any such code in the text. None when there is no ASIN (EAN barcode, FNSKU, own SKU)."""
    text = (sku or "").strip().upper()
    if not text:
        return None
    for part in re.split(r"[-_\s/]+", text)[::-1]:
        if ASIN_PART.match(part):
            return _fix_asin(part)
    m = ASIN_ANY.search(text)
    return _fix_asin(m.group(1)) if m else None


def product_id(sku: str) -> str | None:
    """What goes in the `asin` column: the ASIN when the SKU has one, otherwise the SKU itself
    (an EAN like 5061105923910, an FNSKU like X004Y551MF, '5061105924214-7045') - nothing is
    dropped. None only for an empty cell."""
    asin = asin_from(sku)
    if asin:
        return asin
    return clean_code(sku)


# Characters a product code may contain once stored. Anything else (control characters, ANSI
# escape sequences, markup brackets, quotes) is dropped: portal text is untrusted input and is
# also printed to the terminal and written to logs.
_CODE_BAD = re.compile(r"[^A-Z0-9 _\-./#&+():,]")


def clean_code(text: str) -> str | None:
    text = re.sub(r"\s+", " ", str(text or "")).strip().upper()
    text = _CODE_BAD.sub("", text).strip()
    return text[:64] or None


QTY_MIN, QTY_MAX = -1_000_000, 10_000_000  # outside this a portal value is not a stock count


def to_int(value) -> int:
    """'1,450' -> 1450, '' / '-' / None -> 0, '12.0' -> 12. Raises ScrapeError for a value that
    cannot be a stock quantity, so a broken or tampered page is never written."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        n = int(value)
    else:
        text = re.sub(r"[^\d.\-]", "", str(value))
        if text in ("", "-", ".", "-."):
            return 0
        try:
            n = int(float(text))
        except ValueError:
            raise ScrapeError(f"quantity {str(value)[:30]!r} is not a number") from None
    if not QTY_MIN <= n <= QTY_MAX:
        raise ScrapeError(f"quantity {n:,} is outside {QTY_MIN:,}..{QTY_MAX:,} - page looks wrong")
    return n


def norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


def find_col(headers: list[str], *candidates: str, required: str = "") -> str | None:
    """First header equal to a candidate (normalised), else first containing one."""
    normed = {norm(h): h for h in headers}
    for c in candidates:
        if norm(c) in normed:
            return normed[norm(c)]
    for c in candidates:
        for n, h in normed.items():
            if norm(c) and norm(c) in n:
                return h
    if required:
        raise ScrapeError(f"no '{required}' column - headers seen: {headers}")
    return None


@dataclass
class Result:
    """What one warehouse scrape produced: qty per (asin, marketplace)."""
    site: str
    qty: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    rows_read: int = 0
    non_asin: dict[str, int] = field(default_factory=dict)  # kept under their own SKU/EAN: rows
    empty_rows: int = 0

    def add(self, sku: str, market: str, qty) -> None:
        self.rows_read += 1
        key = product_id(sku)
        if not key:
            self.empty_rows += 1
            return
        if not asin_from(sku):
            self.non_asin[key] = self.non_asin.get(key, 0) + 1
        self.qty[(key, market)] += to_int(qty)


# ── Browser plumbing ─────────────────────────────────────────────────────────────────────────

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/158.0.0.0 Safari/537.36")


def new_context(browser, site: str, log=print):
    """A context that reuses the portal's saved session (state/<site>.json) when there is one,
    so a run normally does not log in again - fewer logins, fewer security prompts. A session
    the portal has expired is simply ignored: the scraper sees the login form and logs in."""
    STATE_DIR.mkdir(exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    state = STATE_DIR / f"{site}.json"
    if state.exists():
        saved = datetime.fromtimestamp(state.stat().st_mtime)
        log(f"  using the saved session from {saved:%d %b %H:%M}")
    else:
        log("  no saved session yet - will log in")
    try:
        return browser.new_context(storage_state=str(state) if state.exists() else None, **CONTEXT)
    except Exception:  # a corrupt state file must not block the run
        state.unlink(missing_ok=True)
        log("  saved session unreadable - removed, will log in")
        return browser.new_context(**CONTEXT)


# Least capability: no file downloads, no service workers, certificate errors are fatal.
CONTEXT = dict(user_agent=UA, viewport={"width": 1600, "height": 1000}, locale="en-US",
               accept_downloads=False, ignore_https_errors=False, service_workers="block")


def assert_host(page, allowed: tuple[str, ...], site: str) -> None:
    """Never type a password into a page that is not the portal's own HTTPS login: a redirect
    to another site (phishing, hijacked DNS, a changed portal) stops the run instead."""
    from urllib.parse import urlparse
    u = urlparse(page.url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not any(host == a or host.endswith("." + a) for a in allowed):
        raise ScrapeError(f"refusing to send {site} credentials to {u.scheme}://{host} - "
                          f"expected https on {', '.join(allowed)}")


def save_state(ctx, site: str) -> None:
    """Store cookies + local storage of every origin (incl. iframes) for the next run."""
    STATE_DIR.mkdir(exist_ok=True)
    path = STATE_DIR / f"{site}.json"
    tmp = path.with_suffix(".tmp")
    ctx.storage_state(path=str(tmp))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def logged_in(page, site: str, log=print) -> None:
    """Call right after a successful login: the session is kept even if a later step fails."""
    save_state(page.context, site)
    log("  login OK - session saved for the next run")


def goto(page, url: str, log=print, attempts: int = 3, **kw):
    """page.goto with retries: the portals' servers sometimes drop a connection
    (ERR_HTTP2_PROTOCOL_ERROR, ERR_CONNECTION_RESET, timeouts)."""
    kw.setdefault("wait_until", "domcontentloaded")
    for i in range(1, attempts + 1):
        try:
            return page.goto(url, **kw)
        except Exception as exc:
            if i == attempts:
                raise
            log(f"  page load failed ({str(exc).splitlines()[0][:90]}) - retry {i}/{attempts - 1}")
            page.wait_for_timeout(3000 * i)


def dump(page, site: str, label: str, log=print) -> None:
    """Screenshot + HTML of the current page, for fixing a selector after a failed run."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(DEBUG_DIR, 0o700)
        stem = DEBUG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{site}_{label}"
        page.screenshot(path=f"{stem}.png", full_page=True)
        Path(f"{stem}.html").write_text(_redact_html(page.content()), encoding="utf-8")
        for ext in (".png", ".html"):
            os.chmod(f"{stem}{ext}", 0o600)
        log(f"  debug: saved {stem}.png / .html  (url: {page.url.split('?')[0]})")
    except Exception as exc:  # a dump must never hide the real error
        log(f"  debug: could not save page ({exc.__class__.__name__})")
    _prune(keep=60)


def _redact_html(html: str) -> str:
    """Blank the value of hidden and password inputs (CSRF tokens, session ids) and meta CSRF."""
    html = re.sub(r'(<input\b[^>]*\btype="(?:hidden|password)"[^>]*\bvalue=")[^"]*(")', r"\1[redacted]\2",
                  html, flags=re.I)
    html = re.sub(r'(<input\b[^>]*\bvalue=")[^"]*("[^>]*\btype="(?:hidden|password)")', r"\1[redacted]\2",
                  html, flags=re.I)
    return re.sub(r'(<meta\b[^>]*name="csrf-[^"]*"[^>]*content=")[^"]*(")', r"\1[redacted]\2", html, flags=re.I)


def _prune(keep: int) -> None:
    files = sorted(DEBUG_DIR.glob("*"), key=lambda p: p.stat().st_mtime)
    for p in files[:-keep]:
        p.unlink(missing_ok=True)


def visible(page, selector: str, timeout_ms: int = 0) -> bool:
    try:
        loc = page.locator(selector).first
        if timeout_ms:
            loc.wait_for(state="visible", timeout=timeout_ms)
        return loc.is_visible()
    except Exception:
        return False


# ── Tables ───────────────────────────────────────────────────────────────────────────────────

# Finds the table whose header row contains every wanted header, marks it, and returns its
# headers + body rows as text. With scrollX DataTables clones the header into a second table
# that has no body rows; the one with the most body rows wins.
_READ_TABLE_JS = """
(wanted) => {
  const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const want = wanted.map(norm);
  let best = null;
  for (const t of document.querySelectorAll('table')) {
    const heads = [...t.querySelectorAll('thead tr:last-child th, thead tr:last-child td')]
      .map(th => th.innerText.trim() || th.getAttribute('aria-label') || '');
    if (!heads.length) continue;
    const hn = heads.map(norm);
    if (!want.every(w => hn.some(h => h.includes(w)))) continue;
    const rows = [...t.querySelectorAll(':scope > tbody > tr')]
      .filter(tr => !tr.querySelector('td.dataTables_empty, td.dt-empty'))
      .map(tr => [...tr.children].map(td => td.innerText.trim()));
    if (!best || rows.length > best.rows.length) best = {el: t, headers: heads, rows};
  }
  if (!best) return null;
  document.querySelectorAll('[data-tpl-target]').forEach(e => e.removeAttribute('data-tpl-target'));
  best.el.setAttribute('data-tpl-target', '1');
  return {headers: best.headers, rows: best.rows};
}
"""


def read_table(page, wanted: list[str]) -> tuple[list[str], list[dict]] | None:
    got = page.evaluate(_READ_TABLE_JS, wanted)
    if not got:
        return None
    headers = got["headers"]
    rows = [dict(zip(headers, r)) for r in got["rows"] if len(r) >= len(headers) - 1]
    return headers, rows


_DT_INFO_JS = """
() => {
  const el = document.querySelector('[data-tpl-target]');
  const wrap = el && (el.closest('.dataTables_wrapper, .dt-container') || document);
  const info = wrap && wrap.querySelector('.dataTables_info, .dt-info');
  const m = info && info.innerText.replace(/,/g, '').match(/of\\s+(\\d+)/i);
  return m ? parseInt(m[1], 10) : null;
}
"""

_DT_SHOW_ALL_JS = """
() => {
  const el = document.querySelector('[data-tpl-target]');
  const dtApi = (el) => { const $ = window.jQuery; if (!el || !$ || !$.fn || !$.fn.dataTable) return null; if ($.fn.dataTable.isDataTable(el)) return $(el).DataTable(); const w = el.closest('.dataTables_wrapper, .dt-container'); const own = $.fn.dataTable.tables().find(x => w && x.closest('.dataTables_wrapper, .dt-container') === w); return own ? $(own).DataTable() : null; };
  const dt = dtApi(el);
  if (!dt) return 'none';
  const server = dt.settings()[0].oFeatures.bServerSide;
  if (server) return 'server';
  dt.search('').columns().search('');
  dt.page.len(-1).draw(false);
  return 'client';
}
"""

_DT_NEXT_JS = """
() => {
  const el = document.querySelector('[data-tpl-target]');
  const wrap = el && (el.closest('.dataTables_wrapper, .dt-container') || document);
  const btn = wrap && wrap.querySelector(
    '.paginate_button.next:not(.disabled), .dt-paging-button.next:not(.disabled), ' +
    'li.next:not(.disabled) a, li.page-item.next:not(.disabled) a');
  if (!btn || btn.classList.contains('disabled') || btn.getAttribute('aria-disabled') === 'true'
      || btn.closest('.disabled')) return false;
  btn.click();
  return true;
}
"""


_DT_RANGE_JS = """
() => {
  const el = document.querySelector('[data-tpl-target]');
  const wrap = el && (el.closest('.dataTables_wrapper, .dt-container') || document);
  const info = wrap && wrap.querySelector('.dataTables_info, .dt-info');
  const m = info && info.innerText.replace(/,/g, '').match(/(\\d+)\\s+to\\s+(\\d+)\\s+of\\s+(\\d+)/i);
  const sel = wrap && wrap.querySelector('.dataTables_length select, .dt-length select');
  const $ = window.jQuery;
  const dtApi = (el) => { const $ = window.jQuery; if (!el || !$ || !$.fn || !$.fn.dataTable) return null; if ($.fn.dataTable.isDataTable(el)) return $(el).DataTable(); const w = el.closest('.dataTables_wrapper, .dt-container'); const own = $.fn.dataTable.tables().find(x => w && x.closest('.dataTables_wrapper, .dt-container') === w); return own ? $(own).DataTable() : null; };
  const api = dtApi(el);
  const proc = wrap && wrap.querySelector('.dataTables_processing, .dt-processing');
  const busy = !!(proc && proc.offsetParent !== null && getComputedStyle(proc).display !== 'none');
  if (!m) return {busy};
  return {start: +m[1], end: +m[2], total: +m[3], len: api ? api.page.len() : (sel ? parseInt(sel.value, 10) : null), busy};
}
"""


def _wait_page(page, wanted, prev_start=None, timeout_s: int = 90, check_len: bool = True):
    """Wait until the table has finished drawing a page and return (headers, rows, range).

    'Finished' = not processing, the info line ('Showing X to Y of Z') has moved past
    prev_start, the rendered row count equals Y-X+1, and that page is as long as the selected
    page length allows - so a read never catches a half-drawn page or the page before a
    length change took effect."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        r = page.evaluate(_DT_RANGE_JS)
        got = read_table(page, wanted)
        if got is not None and r and "start" in r and not r["busy"]:
            headers, rows = got
            shown = r["end"] - r["start"] + 1 if r["total"] else 0
            full = (not check_len or r["total"] == 0 or r["len"] is None or r["len"] < 0
                    or shown == min(r["len"], r["total"] - r["start"] + 1))
            if (prev_start is None or r["start"] != prev_start) and len(rows) == shown and full:
                return headers, rows, r
        elif got is not None and r and "start" not in r and not r.get("busy"):
            # no info line: accept once two reads in a row agree
            if last is not None and _sig(got[1]) == _sig(last[1]) and len(got[1]) == len(last[1]):
                return got[0], got[1], None
            last = got
        page.wait_for_timeout(500)
    raise ScrapeError(f"table did not finish loading within {timeout_s}s")


def read_datatable(page, wanted: list[str], log=print, max_pages: int = 500) -> tuple[list[str], list[dict]]:
    """All rows of a (jQuery) DataTables table, whatever its page size: client-side tables are
    switched to 'show all'; server-side ones are paged through with the Next button, each page
    read only once it has fully drawn. The total is checked against 'of N entries'."""
    if read_table(page, wanted) is None:
        raise ScrapeError(f"no table with columns {wanted} on {page.url.split('?')[0]}")
    headers, rows, r = _wait_page(page, wanted)
    total = r["total"] if r else None
    mode = page.evaluate(_DT_SHOW_ALL_JS)
    if mode == "client":
        headers, rows, r = _wait_page(page, wanted, check_len=False)
        if total is None or len(rows) >= total:
            log(f"  table: {len(rows):,} rows (all on one page)")
            return headers, rows
    # Server-side (or no DataTables API): biggest page size, then Next until the last page.
    log(f"  table: {total if total is not None else '?'} rows, reading page by page")
    before = r["start"] if r and r.get("start", 1) != 1 else None
    _set_max_page_length(page)
    headers, rows, r = _wait_page(page, wanted, prev_start=before)  # moved back to row 1
    all_rows, pages = list(rows), 1
    seen = {r["start"]} if r else set()
    while pages < max_pages and (total is None or len(all_rows) < total):
        if not page.evaluate(_DT_NEXT_JS):
            break
        headers, rows, r = _wait_page(page, wanted, prev_start=r["start"] if r else None)
        if r and r["start"] in seen:
            break
        if r:
            seen.add(r["start"])
        all_rows += rows
        pages += 1
        log(f"  table: page {pages} - {len(all_rows):,}/{total if total is not None else '?'} rows")
    log(f"  table: {len(all_rows):,} rows over {pages} page(s)"
        + (f", portal says {total:,}" if total is not None else ""))
    if total is not None and len(all_rows) != total:
        raise ScrapeError(f"read {len(all_rows)} rows but the table says {total}")
    return headers, all_rows


def _sig(rows: list[dict]) -> str:
    return json.dumps(rows[:1] + rows[-1:], sort_keys=True)


def _set_max_page_length(page) -> None:
    """Back to page 1, no search filter, biggest 'Show N entries' option. DataTables can save
    its state (page, search) in local storage - and the saved session brings that back - so a
    table may otherwise open on the page the previous run ended on."""
    page.evaluate("""
    () => {
      const el = document.querySelector('[data-tpl-target]');
      const wrap = el && (el.closest('.dataTables_wrapper, .dt-container') || document);
      const sel = wrap && wrap.querySelector('.dataTables_length select, .dt-length select');
      const opts = sel ? [...sel.options].map(o => parseInt(o.value, 10)).filter(n => !isNaN(n)) : [];
      const best = opts.includes(-1) ? -1 : (opts.length ? Math.max(...opts) : 100);
      const dtApi = (el) => { const $ = window.jQuery; if (!el || !$ || !$.fn || !$.fn.dataTable) return null; if ($.fn.dataTable.isDataTable(el)) return $(el).DataTable(); const w = el.closest('.dataTables_wrapper, .dt-container'); const own = $.fn.dataTable.tables().find(x => w && x.closest('.dataTables_wrapper, .dt-container') === w); return own ? $(own).DataTable() : null; };
      const dt = dtApi(el);
      if (dt) {
        dt.search('').columns().search('');
        dt.page.len(best);
        dt.page(0);
        dt.draw(false);
        if (sel) sel.value = String(best);
        return;
      }
      if (sel && String(best) !== sel.value) {
        sel.value = String(best);
        sel.dispatchEvent(new Event('change', {bubbles: true}));
      }
      const first = wrap && (wrap.querySelector('.paginate_button.first:not(.disabled), .dt-paging-button.first:not(.disabled)')
        || [...wrap.querySelectorAll('.paginate_button, .dt-paging-button, .pagination a')].find(b => b.innerText.trim() === '1'));
      if (first) first.click();
    }""")
    page.wait_for_timeout(500)


def wait_quiet(page, ms: int = 30000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass


def retry(fn, attempts: int = 2, log=print, what: str = ""):
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if i == attempts:
                raise
            log(f"  {what} failed ({exc.__class__.__name__}: {str(exc)[:160]}) - retrying")
            time.sleep(5)
