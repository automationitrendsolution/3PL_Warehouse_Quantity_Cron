# 3PL warehouse stock sync

Weekly cron job that logs in to the four third-party warehouse portals with **Playwright**,
reads the stock per ASIN, and writes it to the MySQL table **`3PL_Warehouse_Quantity`**.

| Portal | Marketplace | Page | ASIN from | Qty from | Column |
| --- | --- | --- | --- | --- | --- |
| Tactical (tacticalwarehousing.com) | US | Inventory Detail | Item Sku, after `-` | **Units** | `tatical_qty` |
| GTI (ims.gti-co.com → mygti.gti-co.com) | CA | Reporting → Inventory → Run report | Product ID, after `-` | **Qty** (not Units) | `gti_qty` |
| Ideal Logistics (app.extensiv.com) | UK | 3PL Warehouse Manager → Inventory → Manage Inventory | SKU | **Available** | `ideal_qty` |
| TheFbaPrep / FulfilWave (thefbaprep.com) | UK / DE (by flag) | Products → All products | ASIN in the Alternative SKU list | **In stock qty** | `fullfilwave_qty` |

A SKU without an ASIN (EAN, FNSKU, own SKU) is stored under that code - nothing is skipped.
"After `-`" means the part of the SKU that is a 10-character code starting with `B`
(`X002V164BB-B091Y6G2Q1` → `B091Y6G2Q1`, `ITR-B01I90VP0G` → `B01I90VP0G`). An ASIN listed on
several rows (one per receipt) is added up. Rows without an ASIN are skipped and listed in the log.

## The table

One row per **(asin, marketplace)**:

`id, asin, marketplace, tatical_qty, ideal_qty, fullfilwave_qty, gti_qty,
overall_third_party_warehouse_qty, created_date, updated_date`

`overall_third_party_warehouse_qty` = the sum of the four columns (in practice: Ideal + TheFbaPrep
for UK, one portal for the others). The job creates the table on its first run.

How a run writes, per portal, in one transaction:

1. that portal's column → 0 for its marketplaces (an ASIN the portal no longer lists has no stock there),
2. upsert the scraped quantities (new ASINs are inserted, `updated_date` = now),
3. recompute `overall_third_party_warehouse_qty`.

If a portal fails (login, page changed, no rows), **nothing is written for it**: its last good
figures stay, and the other portals still update. Rows are never deleted.

## Setup

```bash
cd third-party-warehouse-data
uv sync                                  # .venv with playwright + PyMySQL
.venv/bin/playwright install chromium    # the browser (once per host)
# on a fresh server also: sudo .venv/bin/playwright install-deps chromium
cp -n .env.example .env && chmod 600 .env   # then fill in .env
```

Fill in `.env`: the warehouse DB login (the same `WAREHOUSE_*` values the other cron jobs use)
and the username/password of each portal.

Test without writing, then for real:

```bash
./run_3pl_warehouse.sh --dry-run             # all portals, print the top ASINs, write nothing
./run_3pl_warehouse.sh --dry-run --only gti  # one portal
./run_3pl_warehouse.sh --dry-run --headed    # watch the browser (needs a display)
./run_3pl_warehouse.sh                       # real run
```

## Schedule: weekly

Every Monday 06:00 (host time - IST here). `crontab -e`:

```cron
0 6 * * 1  /home/karthikeyan/Documents/iTrend-Workspace/apps/third-party-warehouse-data/run_3pl_warehouse.sh
```

The wrapper takes a lock (no overlapping runs), sets a UTF-8 locale and writes
one log file, `logs/cron.log` (rolled over to `cron.log.1` past 10 MB). Exit status: 0 OK, 1 a portal failed,
2 a setting is missing.

## When something breaks

- Every failure saves a **screenshot + HTML** of the page it was on in `logs/debug/` - open the
  PNG to see what the portal showed (login error, changed page, maintenance).
- Logins are saved in `state/<portal>.json` (cookies; git-ignored, mode 600) so a weekly run
  usually does not log in again. Delete the file to force a fresh login.
- **Extensiv asks for a verification code** (new-device / MFA check): run once with
  `--headed --only ideal` on a machine with a display, enter the code, and the saved session is
  reused afterwards.
- Protection seen on 2026-11-12: Tactical is behind Sucuri, TheFbaPrep behind Cloudflare; neither
  challenged headless Chromium, and no portal has a CAPTCHA on its login form (Tactical's
  reCAPTCHA is on the Register form only).

## Security

| Risk | Protection |
| --- | --- |
| Passwords / DB login leaked | Only in `.env` (mode 600, auto-fixed at start if looser, git-ignored). Never logged: login names are masked (`ja***@example.com`), secrets are redacted from errors. |
| Saved login sessions stolen | `state/<portal>.json` holds live session cookies: directory 700, files 600, git-ignored. Delete a file to force a fresh login (e.g. after a password change). |
| Password typed into the wrong site | Before every login the page must be **https** on the portal's own domain (`LOGIN_HOSTS` in each scraper) - a redirect elsewhere stops the run. |
| Database traffic sniffed / downgraded | TLS is **required**; a plain-text connection is refused. Set `WAREHOUSE_DB_SSL_CA` to also verify the server certificate. |
| SQL injection | Every value is a bound parameter; column names come from a fixed allow-list. |
| Malicious text in a portal (terminal escape codes, markup) | Product codes are reduced to `A-Z 0-9 _-./#&+():,` (max 64); the screen renders portal text as plain text. |
| Broken / tampered page wiping stock | Quantities outside -1,000,000..10,000,000 abort that portal; if a portal suddenly shows under half the products with stock it had, nothing is written (override: `--allow-big-drop`). A failed portal never touches its old figures. |
| Compromised portal page attacking the machine | Chromium runs **with its sandbox** (the wrapper refuses to run as root), no downloads, no service workers, certificate errors are fatal. |
| Logs / debug pages read by other users | `umask 077`: `logs/` 700, `cron.log` 600; debug HTML has hidden-input / CSRF values blanked. |

**Recommended (database side):** the job currently writes with `gen_ai`, which has ALL PRIVILEGES on
`gen_ai_db`. Give it its own account that can only touch this table, and put it in `.env` as
`WAREHOUSE_WRITER_USER` / `WAREHOUSE_WRITER_PASSWORD`:

```sql
CREATE USER 'tpl_writer'@'%' IDENTIFIED BY '<strong password>' REQUIRE SSL;
GRANT SELECT, INSERT, UPDATE ON gen_ai_db.`3PL_Warehouse_Quantity` TO 'tpl_writer'@'%';
```

## Settings (`.env`)

| Key | Default | |
| --- | --- | --- |
| `HEADLESS` | `true` | `false` shows the browser |
| `BROWSER_TIMEOUT_SECONDS` | `90` | wait per page element |
| `GTI_INCLUDE_ZERO_STOCK` | `true` | sold-out GTI products are written as 0 |



## Layout

```text
run_3pl_warehouse.sh       cron wrapper: lock, locale, one log (logs/cron.log)
update_3pl_warehouse.py    entry point: runs each portal, writes, prints a summary
warehouses/
  config.py                .env loading, settings, secret redaction
  common.py                ASIN/number parsing, table + DataTables reading, sessions, debug dumps
  tactical.py gti.py ideal.py fbaprep.py   one scraper per portal
  db.py                    table DDL and the per-portal transaction
```
