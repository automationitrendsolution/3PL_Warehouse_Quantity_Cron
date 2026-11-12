"""Warehouse writes: one table, `3PL_Warehouse_Quantity`, one row per (asin, marketplace).

Each 3PL owns one quantity column. A successful scrape of a 3PL replaces that column for the
marketplaces the 3PL serves, in ONE transaction:

  1. its column -> 0 on every row of those marketplaces   (an ASIN the portal no longer lists
                                                            has no stock there any more)
  2. upsert the scraped quantities                          (new ASINs are inserted)
  3. overall_third_party_warehouse_qty = sum of the four columns, for those marketplaces

so the table never shows a half-applied week. A 3PL whose scrape failed is not touched at all:
its last good figures stay. Rows are never deleted.
"""

from __future__ import annotations

import time

import pymysql

from .config import TABLE, Settings

QTY_COLUMNS = ("tatical_qty", "ideal_qty", "fullfilwave_qty", "gti_qty")
OVERALL = "overall_third_party_warehouse_qty"
BATCH = 500
RETRY_ERRORS = (1205, 1213, 2003, 2006, 2013)

DDL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE}` (
  `id`                                INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `asin`                              VARCHAR(64)  NOT NULL COMMENT 'ASIN, or the SKU / EAN / FNSKU when the portal row has no ASIN',
  `marketplace`                       VARCHAR(5)   NOT NULL COMMENT 'US, CA, UK, DE',
  `tatical_qty`                       INT NOT NULL DEFAULT 0 COMMENT 'Tactical (US) - Inventory Detail, Units',
  `ideal_qty`                         INT NOT NULL DEFAULT 0 COMMENT 'Ideal Logistics / Extensiv (UK) - Manage Inventory, Available',
  `fullfilwave_qty`                   INT NOT NULL DEFAULT 0 COMMENT 'TheFbaPrep / FulfilWave (UK/DE by flag) - All products, In stock qty',
  `gti_qty`                           INT NOT NULL DEFAULT 0 COMMENT 'GTI (CA) - Inventory report, Qty',
  `{OVERALL}` INT NOT NULL DEFAULT 0 COMMENT 'sum of the four 3PL columns',
  `created_date`                      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_date`                      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_asin_marketplace` (`asin`, `marketplace`),
  KEY `idx_marketplace` (`marketplace`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Stock per ASIN and marketplace in the third-party warehouses - weekly scrape'
""".strip()


def connect(s: Settings):
    """TLS is REQUIRED: the database is reached over the internet, so a server (or a man in the
    middle) offering plain text is refused rather than sending the password unencrypted. With
    WAREHOUSE_DB_SSL_CA set, the server certificate is verified against that CA as well."""
    ssl = {"ca": s.db_ssl_ca} if s.db_ssl_ca else {"check_hostname": False}
    conn = pymysql.connect(host=s.db_host, port=s.db_port, user=s.db_user, password=s.db_password,
                           database=s.db_name, charset="utf8mb4", autocommit=False, ssl=ssl,
                           connect_timeout=20, read_timeout=300, write_timeout=300)
    with conn.cursor() as cur:
        cur.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        row = cur.fetchone()
    if not row or not row[1]:
        conn.close()
        raise RuntimeError("database connection is not encrypted - refusing to use it")
    return conn


def stocked_count(s: Settings, column: str, markets: tuple[str, ...]) -> int:
    """Rows this 3PL currently has stock on (non-zero), for the drop guard."""
    if column not in QTY_COLUMNS:
        raise ValueError(column)
    conn = connect(s)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM `{TABLE}` WHERE marketplace IN "
                        f"({', '.join(['%s'] * len(markets))}) AND `{column}` <> 0", markets)
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def ensure_table(s: Settings) -> None:
    conn = connect(s)
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            # Tables made when `asin` was VARCHAR(10): widen it for EAN / SKU identifiers.
            cur.execute("SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns "
                        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = 'asin'",
                        (TABLE,))
            row = cur.fetchone()
            if row and row[0] and row[0] < 64:
                cur.execute(f"ALTER TABLE `{TABLE}` MODIFY `asin` VARCHAR(64) NOT NULL COMMENT "
                            "'ASIN, or the SKU / EAN / FNSKU when the portal row has no ASIN'")
        conn.commit()
    finally:
        conn.close()


def apply(s: Settings, column: str, markets: tuple[str, ...], qty: dict[tuple[str, str], int],
          log=print) -> dict:
    """Replace one 3PL's column for its marketplaces. Returns counts for the run log."""
    if column not in QTY_COLUMNS:
        raise ValueError(column)
    marks = ", ".join(["%s"] * len(markets))
    total_sql = " + ".join(f"`{c}`" for c in QTY_COLUMNS)
    items = sorted(qty.items())
    for attempt in range(1, 6):
        conn = connect(s)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM `{TABLE}`")
                before = cur.fetchone()[0]
                zeroed = cur.execute(
                    f"UPDATE `{TABLE}` SET `{column}` = 0 "
                    f"WHERE marketplace IN ({marks}) AND `{column}` <> 0", markets)
                for start in range(0, len(items), BATCH):
                    chunk = items[start:start + BATCH]
                    cur.execute(
                        f"INSERT INTO `{TABLE}` (`asin`, `marketplace`, `{column}`, "
                        f"`created_date`, `updated_date`) VALUES "
                        + ", ".join(["(%s, %s, %s, NOW(), NOW())"] * len(chunk))
                        + f" ON DUPLICATE KEY UPDATE `{column}` = VALUES(`{column}`), "
                          f"`updated_date` = NOW()",
                        [v for (asin, mkt), q in chunk for v in (asin, mkt, q)])
                cur.execute(f"UPDATE `{TABLE}` SET `{OVERALL}` = {total_sql} "
                            f"WHERE marketplace IN ({marks})", markets)
                cur.execute(f"SELECT COUNT(*) FROM `{TABLE}`")
                after = cur.fetchone()[0]
            conn.commit()
            return {"inserted": after - before, "updated": len(items) - (after - before),
                    "reset_to_zero_first": zeroed}
        except pymysql.err.OperationalError as exc:
            conn.rollback()
            if exc.args[0] not in RETRY_ERRORS or attempt == 5:
                raise
            log(f"  database error {exc.args[0]} - retrying ({attempt}/5)")
            time.sleep(2 ** attempt)
        finally:
            conn.close()
    raise RuntimeError("unreachable")


def summary(s: Settings) -> list[tuple]:
    conn = connect(s)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT marketplace, COUNT(*), SUM(tatical_qty), SUM(ideal_qty), "
                        f"SUM(fullfilwave_qty), SUM(gti_qty), SUM(`{OVERALL}`), MAX(updated_date) "
                        f"FROM `{TABLE}` GROUP BY marketplace ORDER BY marketplace")
            return list(cur.fetchall())
    finally:
        conn.close()
