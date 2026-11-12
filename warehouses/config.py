"""Settings from .env next to the job - read the way the iTrend-Cron-Scripts jobs read theirs.

Values already in the environment win over .env, so a managed host can inject secrets without
touching the file. Nothing here is ever printed in clear: `Settings.describe()` masks secrets.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

JOB_DIR = Path(__file__).resolve().parent.parent
TABLE = "3PL_Warehouse_Quantity"


class ConfigError(Exception):
    """A setting is missing or malformed - exit status 2, nothing is fetched or written."""


def mask(user: str) -> str:
    """'jane.doe@example.com' -> 'ja***@example.com', 'warehouse1' -> 'wa***1' - logs never hold a
    full login name."""
    if not user:
        return ""
    name, at, domain = user.partition("@")
    shown = name[:2] + "***" + ("" if at else name[-1:] if len(name) > 3 else "")
    return shown + (at + domain if at else "")


def load_env(path: str | Path = JOB_DIR / ".env") -> None:
    """Read KEY=value lines; a trailing ' #' comment and one layer of quotes are dropped.
    A .env other users can read is made private (600) first - it holds every password."""
    path = Path(path)
    if not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        os.chmod(path, 0o600)
        print(f"security: {path.name} was readable by other users (mode {mode:o}) - set to 600",
              file=sys.stderr)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split(" #")[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return default


def flag(name: str, default: bool) -> bool:
    return env(name, default="1" if default else "0").lower() in ("1", "true", "yes", "on")


@dataclass
class Login:
    username: str
    password: str = field(repr=False)


@dataclass
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str = field(repr=False, default="")
    db_ssl_ca: str = ""                # CA file to verify the DB certificate (optional)
    logins: dict[str, Login] = field(default_factory=dict)
    headless: bool = True
    timeout_s: int = 90
    gti_include_zero_stock: bool = True
    fbaprep_default_market: str = "UK"
    fbaprep_statuses: list[str] = field(default_factory=list)
    secrets: tuple[str, ...] = field(default=(), repr=False)

    def describe(self) -> list[tuple[str, str]]:
        rows = [("Database", f"{self.db_user or 'MISSING'}@{self.db_host or 'MISSING'}:"
                             f"{self.db_port}/{self.db_name or 'MISSING'}")]
        for site, lg in self.logins.items():
            rows.append((f"{site} login", f"{mask(lg.username) or 'MISSING'} / "
                                          f"{'password set' if lg.password else 'NO PASSWORD'}"))
        rows.append(("Browser", "headless" if self.headless else "headed (visible window)"))
        return rows


# .env key prefix for each portal's login.
LOGIN_PREFIX = {"tactical": "TACTICAL", "gti": "GTI", "ideal": "IDEAL", "fbaprep": "FBAPREP"}


def from_env(sites: list[str], need_db: bool = True) -> Settings:
    missing = []
    logins = {}
    for site in sites:
        p = LOGIN_PREFIX[site]
        user, pwd = env(f"{p}_USERNAME", f"{p}_EMAIL"), env(f"{p}_PASSWORD")
        if not user:
            missing.append(f"{p}_USERNAME")
        if not pwd:
            missing.append(f"{p}_PASSWORD")
        logins[site] = Login(user, pwd)

    db = {
        "host": env("WAREHOUSE_DB_HOST"),
        "port": env("WAREHOUSE_DB_PORT", default="3306"),
        "name": env("WAREHOUSE_DB_NAME"),
        "user": env("WAREHOUSE_WRITER_USER", "WAREHOUSE_USER", "WAREHOUSE_DB_USER"),
        "password": env("WAREHOUSE_WRITER_PASSWORD", "WAREHOUSE_PASSWORD", "WAREHOUSE_DB_PASSWORD"),
    }
    if need_db:
        missing += [k for k, v in (("WAREHOUSE_DB_HOST", db["host"]),
                                   ("WAREHOUSE_DB_NAME", db["name"]),
                                   ("WAREHOUSE_USER", db["user"])) if not v]
    if missing:
        raise ConfigError("missing in .env: " + ", ".join(missing))
    try:
        port = int(db["port"])
        timeout_s = int(env("BROWSER_TIMEOUT_SECONDS", default="90"))
    except ValueError:
        raise ConfigError("WAREHOUSE_DB_PORT and BROWSER_TIMEOUT_SECONDS must be numbers") from None

    s = Settings(
        db_host=db["host"], db_port=port, db_name=db["name"], db_user=db["user"],
        db_password=db["password"], db_ssl_ca=env("WAREHOUSE_DB_SSL_CA"), logins=logins,
        headless=flag("HEADLESS", True),
        timeout_s=max(15, timeout_s),
        gti_include_zero_stock=flag("GTI_INCLUDE_ZERO_STOCK", True),
        fbaprep_default_market=env("FBAPREP_DEFAULT_MARKETPLACE", default="UK").upper(),
        fbaprep_statuses=[x.strip().lower() for x in env("FBAPREP_INBOUND_STATUSES").split(",")
                          if x.strip()],
    )
    s.secrets = tuple(x for x in (s.db_password, *(lg.password for lg in logins.values())) if x)
    return s


def redact(text: str, secrets: tuple[str, ...]) -> str:
    """Remove any secret that an exception message might carry before it is logged."""
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return text
