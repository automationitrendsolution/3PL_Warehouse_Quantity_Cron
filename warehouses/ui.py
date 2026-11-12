"""Run output: a premium live Rich dashboard on a terminal, a plain log file always.

Two audiences, one writer:
  * run by hand (stdout is a terminal) - a brand header, then ONE live dashboard that redraws in
    place: overall progress bar, a card per portal (spinner, current step, its own progress bar
    while a table is paged / a grid is scrolled, rows, products, qty, time) and a scrolling
    activity feed. Each finished portal leaves a result card (top ASINs as bars) above it, and
    the run ends with a summary table, the table as stored in the database and a verdict;
  * run by cron - nothing fancy: every event is one timestamped plain-text line in
    logs/cron.log, which `--logs` reads back as history.

The log file is written in both cases, so a run by hand is in the history too.
"""

from __future__ import annotations

import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from .config import JOB_DIR, TABLE

LOG_DIR = JOB_DIR / "logs"
JOB = "3pl_warehouse"

PORTALS = {  # display name, marketplaces, accent colour, what is read
    "tactical": ("Tactical", "US", "#22d3ee", "Inventory Detail · Units"),
    "gti": ("GTI", "CA", "#60a5fa", "Inventory report · Qty"),
    "ideal": ("Ideal · Extensiv", "UK", "#f472b6", "Manage Inventory · Available"),
    "fbaprep": ("FulfilWave", "UK · DE", "#a78bfa", "All products · In stock"),
}

THEME = Theme({
    "muted": "#8b95a7", "dim2": "#5b6475", "ok": "bold #34d399", "fail": "bold #f87171",
    "warn": "#fbbf24", "accent": "bold #38bdf8", "num": "bold #f8fafc", "head": "bold #cbd5e1",
})
BORDER = "#334155"
PROGRESS_RE = re.compile(r"([\d,]+)\s*/\s*([\d,]+)")


@dataclass
class PortalState:
    site: str
    status: str = "waiting"          # waiting | running | ok | failed | dry
    step: str = "queued"
    rows: int | None = None
    asins: int | None = None
    qty: int | None = None
    written: str = ""
    started: float | None = None
    ended: float | None = None
    error: str = ""
    done_n: int | None = None        # sub-progress parsed from "n/total" in the step
    total_n: int | None = None
    debug: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> int:
        if self.started is None:
            return 0
        return int((self.ended or time.monotonic()) - self.started)

    @property
    def elapsed(self) -> str:
        return "" if self.started is None else f"{self.seconds // 60}:{self.seconds % 60:02d}"


def _fmt_secs(s: int) -> str:
    return f"{s // 60}m {s % 60:02d}s"


class UI:
    def __init__(self, sites: list[str], mode: str, force_plain: bool = False) -> None:
        self.sites = sites
        self.mode = mode
        self.tty = sys.stdout.isatty() and not force_plain
        self.console = Console(theme=THEME, highlight=False)
        self.state = {s: PortalState(s) for s in sites}
        self.current: str | None = None
        self.t0 = time.monotonic()
        self.live: Live | None = None
        self.feed: deque = deque(maxlen=9)
        LOG_DIR.mkdir(exist_ok=True)
        # ONE log file, whether run by hand or by cron: logs/cron.log (the wrapper rolls it over
        # to cron.log.1 when it passes 10 MB). `--logs` reads the run history back from it.
        self.log_path = LOG_DIR / "cron.log"
        self._files = [open(self.log_path, "a", encoding="utf-8")]

    # ── plain log files ─────────────────────────────────────────────────────────────────────
    def _write(self, level: str, msg: str) -> None:
        site = f"[{self.current}] " if self.current else ""
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {level:<5} {site}{msg}"
        for f in self._files:
            if not f.closed:
                f.write(line + "\n")
                f.flush()
        if not self.tty:
            print(line, flush=True)

    def _close(self) -> None:
        for f in self._files:
            f.close()

    # ── header ──────────────────────────────────────────────────────────────────────────────
    def header(self, settings_rows: list[tuple[str, str]]) -> None:
        self._write("INFO", f"=== run start mode={self.mode} portals={','.join(self.sites)} ===")
        for k, v in settings_rows:
            self._write("INFO", f"{k}: {v}")
        if not self.tty:
            return
        brand = Text.assemble(("◆ ", "accent"), ("3PL WAREHOUSE STOCK SYNC", "bold #f8fafc"),
                              ("   ", ""), (f"→ {TABLE}", "muted"))
        mode = (Text(" ● DRY RUN  nothing is written ", style="bold #1f2937 on #fbbf24")
                if self.mode == "dry-run" else
                Text(" ● LIVE  writes to the database ", style="bold #052e16 on #34d399"))
        info = Table.grid(padding=(0, 3))
        info.add_column(style="muted", justify="right")
        info.add_column()
        for k, v in settings_rows:
            bad = "MISSING" in v or "NO PASSWORD" in v
            info.add_row(k, Text(v, style="fail" if bad else "#e2e8f0"))
        roster = Table.grid(padding=(0, 2))
        for _ in range(3):
            roster.add_column()
        for s in self.sites:
            name, mkt, colour, what = PORTALS[s]
            roster.add_row(Text("▍", style=colour) + Text(name, style=f"bold {colour}"),
                           Text(mkt, style="#e2e8f0"), Text(what, style="muted"))
        body = Group(Align.left(mode), Text(""), info, Text(""), roster)
        self.console.print(Panel(
            body, title=brand, title_align="left", border_style="#38bdf8", box=box.HEAVY,
            subtitle=Text(f"{datetime.now():%A %d %b %Y · %H:%M}", style="muted"),
            subtitle_align="right", padding=(1, 3)))

    def config_error(self, msg: str, status: int = 2) -> None:
        """A setting is missing / the database is unreachable: nothing was attempted."""
        self._write("ERROR", msg)
        self._write("INFO", f"=== run end status={status} failed=setup seconds=0 ===")
        self._close()
        if self.tty:
            self.console.print(Panel(Text(msg, style="#f1f5f9"), box=box.HEAVY, border_style="#f87171",
                                     title=Text("✖ Cannot start", style="fail"), title_align="left",
                                     subtitle=Text("fill in .env, then run again", style="muted"),
                                     subtitle_align="right", padding=(0, 1)))

    # ── live dashboard ──────────────────────────────────────────────────────────────────────
    def __enter__(self):
        if self.tty:
            # The board is sized to the terminal on every refresh (see _render) and cropped as a
            # last resort: a live area taller than the screen cannot be redrawn in place and
            # leaves stacked copies behind.
            self.live = Live(console=self.console, refresh_per_second=8, transient=True,
                             get_renderable=self._render, redirect_stdout=False,
                             vertical_overflow="crop")
            self.live.__enter__()
        return self

    def __exit__(self, *exc):
        if self.live:
            self.live.__exit__(*exc)
            self.live = None
            # the final state of the board stays on screen
            self.console.print(self._render(final=True))

    def print(self, renderable) -> None:
        if self.tty:
            (self.live.console if self.live else self.console).print(renderable)

    # ── per portal ──────────────────────────────────────────────────────────────────────────
    def start(self, site: str, column: str, markets: tuple[str, ...]) -> None:
        self.current = site
        st = self.state[site]
        st.status, st.step, st.started = "running", "starting browser", time.monotonic()
        self._write("INFO", f"--- {site} ({'/'.join(markets)} -> {column}) ---")
        self._feed(site, "starting", "start")

    def log(self, msg: str) -> None:
        """The scrapers' log callback: a log line, the portal's current step and sub-progress."""
        msg = msg.strip()
        if self.current:
            st = self.state[self.current]
            st.step = msg
            m = PROGRESS_RE.search(msg)
            if m:
                st.done_n = int(m.group(1).replace(",", ""))
                st.total_n = int(m.group(2).replace(",", "")) or None
            m = re.search(r"saved (\S+\.png)", msg)
            if m:
                st.debug.append(m.group(1))
        self._write("INFO", msg)
        self._feed(self.current, msg, "warn" if "debug:" in msg or "retry" in msg else "info")

    def _feed(self, site, msg, kind) -> None:
        self.feed.append((datetime.now().strftime("%H:%M:%S"), site, msg, kind))

    def done(self, site: str, res, top: list[tuple[tuple[str, str], int]], written: dict | None,
             non_asin: list[str]) -> None:
        st = self.state[site]
        st.ended = time.monotonic()
        st.rows, st.asins, st.qty = res.rows_read, len(res.qty), sum(res.qty.values())
        st.done_n = st.total_n = None
        if written is None:
            st.status, st.step, st.written = "dry", "read OK · dry run, not written", "—"
        else:
            st.status, st.step = "ok", "written to the database"
            st.written = f"+{written['inserted']} new · {written['updated']} upd"
        self._write("INFO", f"RESULT site={site} status={st.status} rows={st.rows} asins={st.asins} "
                            f"qty={st.qty} written={st.written!r} seconds={st.seconds}")
        if non_asin:
            self._write("INFO", f"{len(non_asin)} product(s) without an ASIN kept under their own "
                                f"code: {', '.join(non_asin[:8])}{' ...' if len(non_asin) > 8 else ''}")
        self._feed(site, f"✔ {st.rows:,} rows → {st.asins:,} products · qty {st.qty:,}", "ok")
        self.current = None
        if self.tty:
            self.print(self._result_card(site, res, top, non_asin))

    def _result_card(self, site, res, top, non_asin):
        st = self.state[site]
        name, mkt, colour, what = PORTALS[site]
        stats = Table.grid(padding=(0, 4))
        for _ in range(4):
            stats.add_column()
        def stat(label, value, style="num"):
            return Group(Text(label.upper(), style="dim2"), Text(value, style=style))
        stats.add_row(stat("rows read", f"{st.rows:,}"), stat("products", f"{st.asins:,}"),
                      stat("total qty", f"{st.qty:,}", f"bold {colour}"),
                      stat("written" if st.status == "ok" else "mode",
                           st.written if st.status == "ok" else "dry run",
                           "ok" if st.status == "ok" else "warn"))
        t = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style="dim2",
                  expand=False)
        t.add_column("#", style="dim2", justify="right")
        t.add_column("ASIN / code", style="bold #e2e8f0", min_width=14)
        t.add_column("Mkt", style="muted")
        t.add_column("Qty", justify="right", style="num")
        t.add_column("", min_width=26)
        peak = max((q for _, q in top), default=1) or 1
        for i, ((asin, m), q) in enumerate(top, 1):
            t.add_row(str(i), Text(asin), Text(m), f"{q:,}",  # Text(): portal data is never markup
                      Text("━" * max(1, round(26 * q / peak)) if q else "", style=colour))
        parts = [stats, Text(""), t]
        if non_asin:
            parts.append(Text(f"ℹ {len(non_asin)} product(s) have no ASIN and are kept under their own "
                              f"code (EAN / FNSKU / SKU): {', '.join(non_asin[:4])}"
                              f"{' …' if len(non_asin) > 4 else ''}", style="#93c5fd"))
        title = Text.assemble(("✔ ", "ok"), (name, f"bold {colour}"), (f"  {mkt}", "muted"),
                              (f"  ·  {what}", "dim2"))
        return Panel(Group(*parts), title=title, title_align="left", border_style=colour,
                     box=box.ROUNDED, padding=(1, 2), subtitle=Text(f"{st.elapsed}", style="muted"),
                     subtitle_align="right")

    def failed(self, site: str, msg: str, trace: list[str]) -> None:
        st = self.state[site]
        st.ended = time.monotonic()
        st.status, st.step, st.error = "failed", msg.split(":", 1)[-1].strip()[:90], msg
        st.done_n = st.total_n = None
        self._write("ERROR", msg)
        for line in trace:
            self._write("ERROR", "  " + line)
        self._write("INFO", f"RESULT site={site} status=failed error={msg[:200]!r} "
                            f"seconds={st.seconds}")
        self._feed(site, f"✖ {msg[:110]}", "fail")
        self.current = None
        if self.tty:
            body = [Text(msg, style="#fecaca")]
            body += [Text(f"  {line}", style="dim2") for line in trace[-4:]]
            body += [Text.assemble(("  📸 screenshot  ", "muted"), (p, "underline #38bdf8"))
                     for p in st.debug]
            body.append(Text("  Its last good figures stay in the database.", style="muted"))
            self.print(Panel(Group(*body), title=Text(f"✖ {PORTALS[site][0]} failed", style="fail"),
                             title_align="left", border_style="#f87171", box=box.ROUNDED,
                             padding=(1, 2)))

    def _render(self, final: bool = False):
        """The live board, fitted to the terminal: never taller than the screen (the activity
        feed shrinks / disappears first) and never wider (less important columns drop out)."""
        width, height = self.console.size
        done = sum(1 for s in self.state.values() if s.status in ("ok", "dry", "failed"))
        failed = sum(1 for s in self.state.values() if s.status == "failed")
        secs = int(time.monotonic() - self.t0)
        show_progress, show_products, show_rows = width >= 104, width >= 90, width >= 78

        t = Table(box=box.SIMPLE_HEAD, show_edge=False, header_style="dim2", expand=True,
                  pad_edge=False, padding=(0, 1))
        t.add_column("", width=2, no_wrap=True)
        t.add_column("PORTAL", width=16, no_wrap=True, overflow="ellipsis")
        t.add_column("MKT", width=7, no_wrap=True, style="muted")
        t.add_column("STEP", ratio=1, min_width=10, no_wrap=True, overflow="ellipsis")
        if show_progress:
            t.add_column("PROGRESS", width=15, no_wrap=True)
        if show_rows:
            t.add_column("ROWS", width=6, justify="right", no_wrap=True)
        if show_products:
            t.add_column("PRODUCTS", width=8, justify="right", no_wrap=True)
        t.add_column("QTY", width=9, justify="right", no_wrap=True)
        t.add_column("TIME", width=5, justify="right", no_wrap=True, style="muted")
        for site in self.sites:
            st = self.state[site]
            name, mkt, colour, _ = PORTALS[site]
            icon = {"waiting": Text("○", style="dim2"), "ok": Text("✔", style="ok"),
                    "dry": Text("✔", style="warn"), "failed": Text("✖", style="fail")
                    }.get(st.status) or Spinner("dots", style=colour)
            step_style = {"failed": "#fca5a5", "waiting": "dim2", "ok": "#a7f3d0",
                          "dry": "#fde68a"}.get(st.status, "#e2e8f0")
            row = [icon, Text(name, style=f"bold {colour}" if st.status != "waiting" else "dim2"),
                   Text(mkt), Text(st.step, style=step_style)]
            if show_progress:
                row.append(self._progress_cell(st, colour))
            if show_rows:
                row.append(self._opt(st.rows))
            if show_products:
                row.append(self._opt(st.asins))
            row += [self._opt(st.qty, colour), Text(st.elapsed)]
            t.add_row(*row)

        overall = ProgressBar(total=len(self.sites), completed=done, width=None,
                              complete_style="#38bdf8",
                              finished_style="#f87171" if failed else "#34d399", style="#1e293b")
        bar = Table.grid(expand=True)  # own row: a bare bar would swallow the next line
        bar.add_column(ratio=1)
        bar.add_row(overall)
        parts = [bar, t]
        # height budget: panel border 2 + bar 1 + table header 2 + one line per portal
        fixed = 2 + 1 + 2 + len(self.sites)
        room = height - fixed - 3            # keep a little space for the prompt / cursor
        feed_lines = 0 if final else max(0, min(6, room - 1))
        if feed_lines >= 2 and self.feed:
            feed = Table.grid(padding=(0, 1), expand=True)
            feed.add_column(width=8, no_wrap=True, style="dim2")
            feed.add_column(width=16, no_wrap=True, overflow="ellipsis")
            feed.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
            for ts, site, msg, kind in list(self.feed)[-(feed_lines - 1):]:
                name, _, colour, _ = PORTALS.get(site or "", ("", "", "#94a3b8", ""))
                style = {"ok": "#a7f3d0", "fail": "#fca5a5", "warn": "#fde68a",
                         "start": "bold #e2e8f0"}.get(kind, "#cbd5e1")
                feed.add_row(ts, Text(name, style=colour), Text(msg, style=style))
            parts += [Rule(Text("activity", style="dim2"), style=BORDER, characters="─"), feed]
        title = Text.assemble(("● " if not final else "■ ", "#34d399" if not final else "dim2"),
                              ("LIVE" if not final else "RUN BOARD", "bold #e2e8f0"),
                              (f"  {done}/{len(self.sites)} portals", "#e2e8f0"),
                              (f" · {failed} failed" if failed else "", "fail"),
                              (f" · {_fmt_secs(secs)}", "muted"))
        return Panel(Group(*parts), title=title, title_align="left", border_style=BORDER,
                     box=box.ROUNDED, padding=(0, 1), width=width)

    def _progress_cell(self, st, colour):
        if st.status == "running" and st.total_n:
            done = min(st.done_n or 0, st.total_n)
            cell = Table.grid(padding=(0, 1))
            cell.add_column(width=10)
            cell.add_column(width=4, justify="right")
            cell.add_row(ProgressBar(total=st.total_n, completed=done, width=10,
                                     complete_style=colour, finished_style=colour, style="#1e293b"),
                         Text(f"{done * 100 // st.total_n}%", style=colour))
            return cell
        if st.status in ("ok", "dry"):
            return ProgressBar(total=1, completed=1, width=10, complete_style=colour,
                               finished_style=colour)
        if st.status == "running":
            return ProgressBar(total=None, width=10, pulse=True, complete_style=colour,
                               finished_style=colour, pulse_style=colour, style="#1e293b")
        return Text("")

    @staticmethod
    def _opt(v: int | None, colour: str | None = None) -> Text:
        return Text("" if v is None else f"{v:,}", style=f"bold {colour}" if colour else "#e2e8f0")

    # ── end of run ──────────────────────────────────────────────────────────────────────────
    def db_summary(self, rows: list[tuple]) -> None:
        for mkt, n, t, i, f, g, o, upd in rows:
            self._write("INFO", f"TABLE mkt={mkt} rows={n} tactical={t or 0} ideal={i or 0} "
                                f"fbaprep={f or 0} gti={g or 0} overall={o or 0} updated={upd}")
        if not self.tty:
            return
        tb = Table(box=box.ROUNDED, border_style=BORDER, header_style="dim2", padding=(0, 2),
                   title=Text.assemble(("▤ ", "accent"), (TABLE, "bold #f8fafc"),
                                       ("   as stored now", "muted")), title_justify="left")
        cols = [("MKT", "left"), ("ROWS", "right"), ("TACTICAL", "right"), ("IDEAL", "right"),
                ("FULFILWAVE", "right"), ("GTI", "right"), ("OVERALL", "right"),
                ("LAST UPDATE", "left")]
        for col, just in cols:
            tb.add_column(col, justify=just)
        tot = [0] * 6
        for mkt, n, t, i, f, g, o, upd in rows:
            vals = [int(x or 0) for x in (n, t, i, f, g, o)]
            tot = [a + b for a, b in zip(tot, vals)]
            tb.add_row(Text(mkt, style="bold #f8fafc"), *[self._n(v) for v in vals[:5]],
                       Text(f"{vals[5]:,}", style="bold #34d399"),
                       Text(f"{upd:%d %b %H:%M}", style="muted"))
        tb.add_section()
        tb.add_row(Text("ALL", style="bold"), *[Text(f"{v:,}", style="bold") for v in tot[:5]],
                   Text(f"{tot[5]:,}", style="bold #34d399"), "")
        self.print(tb)

    @staticmethod
    def _n(v: int) -> Text:
        return Text(f"{v:,}", style="#e2e8f0" if v else "dim2")

    def finish(self, failed: list[str]) -> None:
        secs = int(time.monotonic() - self.t0)
        status = 1 if failed else 0
        self._write("INFO", f"=== run end status={status} failed={','.join(failed) or '-'} "
                            f"seconds={secs} ===")
        self._close()
        if not self.tty:
            return
        # summary across portals
        sm = Table(box=box.SIMPLE_HEAD, show_edge=False, header_style="dim2", padding=(0, 2))
        for col, just in (("", "left"), ("PORTAL", "left"), ("MKT", "left"), ("ROWS", "right"),
                          ("PRODUCTS", "right"), ("QTY", "right"), ("RESULT", "left"),
                          ("TIME", "right")):
            sm.add_column(col, justify=just)
        for site in self.sites:
            st = self.state[site]
            name, mkt, colour, _ = PORTALS[site]
            res = {"ok": Text(st.written, style="ok"), "dry": Text("read OK · not written", style="warn"),
                   "failed": Text("failed - last good figures kept", style="fail")
                   }.get(st.status, Text("not run", style="dim2"))
            icon = {"ok": Text("✔", style="ok"), "dry": Text("✔", style="warn"),
                    "failed": Text("✖", style="fail")}.get(st.status, Text("○", style="dim2"))
            sm.add_row(icon, Text(name, style=f"bold {colour}"), mkt, self._opt(st.rows),
                       self._opt(st.asins), self._opt(st.qty, colour), res, st.elapsed)
        if failed:
            names = ", ".join(PORTALS[s][0] for s in failed)
            verdict = Text.assemble(("✖  FINISHED WITH ERRORS", "fail"), (f"   failed: {names}", "#f1f5f9"))
            hint = "Their last good figures were kept · screenshots in logs/debug/"
            style = "#f87171"
        else:
            verdict = Text.assemble(("✔  ALL PORTALS SYNCED", "ok"),
                                    ("   dry run - nothing written" if self.mode == "dry-run" else
                                     f"   {TABLE} is up to date", "warn" if self.mode == "dry-run" else "#f1f5f9"))
            hint = "Run without --dry-run to write to the database" if self.mode == "dry-run" else \
                "./run_3pl_warehouse.sh --logs  shows the run history"
            style = "#34d399"
        foot = Text.assemble((f"{_fmt_secs(secs)}", "#e2e8f0"), ("  ·  log ", "muted"),
                             (str(self.log_path.relative_to(JOB_DIR)), "#93c5fd"),
                             ("  ·  ", "muted"), (hint, "muted"))
        self.console.print(Panel(Group(verdict, Text(""), sm, Text(""), foot), border_style=style,
                                 box=box.HEAVY, padding=(1, 2)))


# ── run history (--logs) ────────────────────────────────────────────────────────────────────

_START = re.compile(r"^(\S+ \S+) INFO\s+=== run start mode=(\S+) portals=(\S+) ===")
_END = re.compile(r"^(\S+ \S+) INFO\s+=== run end status=(\d+) failed=(\S+) seconds=(\d+) ===")
_RESULT = re.compile(r"RESULT site=(\w+) status=(\w+)(.*)")


def history(limit: int = 15, show_last: bool = True) -> int:
    console = Console(theme=THEME, highlight=False)
    files = [f for f in (LOG_DIR / "cron.log.1", LOG_DIR / "cron.log") if f.exists()]
    runs, cur = [], None
    for path in files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if m := _START.match(line):
                cur = {"start": m.group(1), "mode": m.group(2), "portals": {}, "lines": [line],
                       "status": None, "seconds": None, "file": path.name}
                runs.append(cur)
                continue
            if cur is None:
                continue
            cur["lines"].append(line)
            if m := _RESULT.search(line):
                kv = dict(re.findall(r"(\w+)=('[^']*'|\S+)", m.group(3)))
                cur["portals"][m.group(1)] = {"status": m.group(2), **{k: v.strip("'") for k, v in kv.items()}}
            elif m := _END.match(line):
                cur["status"], cur["seconds"] = int(m.group(2)), int(m.group(4))
                cur = None
    if not runs:
        console.print(Panel(Text("No runs logged yet. Start one with ./run_3pl_warehouse.sh --dry-run",
                                 style="muted"), border_style="grey35"))
        return 0

    t = Table(box=box.ROUNDED, border_style="grey35", header_style="head",
              title=Text.assemble(("◆ ", "accent"), ("3PL sync · run history", "bold white")),
              title_justify="left")
    t.add_column("Started")
    t.add_column("Mode", style="muted")
    for site, (name, _, colour, _w) in PORTALS.items():
        t.add_column(name, justify="center", header_style=f"bold {colour}")
    t.add_column("Time", justify="right", style="muted")
    t.add_column("Result")
    for r in runs[-limit:][::-1]:
        cells = []
        for site in PORTALS:
            p = r["portals"].get(site)
            if not p:
                cells.append(Text("—", style="grey35"))
            elif p["status"] == "failed":
                cells.append(Text("✖ failed", style="fail"))
            else:
                cells.append(Text.assemble(("✔ " if p["status"] == "ok" else "◌ ",
                                            "ok" if p["status"] == "ok" else "warn"),
                                           (f"{int(p.get('qty', 0)):,}", "white")))
        res = (Text("running / interrupted", style="warn") if r["status"] is None
               else Text("✔ OK", style="ok") if r["status"] == 0 else Text("✖ errors", style="fail"))
        secs = r["seconds"]
        t.add_row(r["start"][:16], r["mode"], *cells,
                  f"{secs // 60}m {secs % 60:02d}s" if secs is not None else "", res)
    console.print(t)
    console.print(Text("  ✔ written  ◌ dry run (qty read, not written)  ✖ failed  — not run",
                       style="muted"))

    if show_last:
        last = runs[-1]
        console.print(Rule(Text(f"last run · {last['start']} · {last['file']}", style="muted"),
                           style="grey35"))
        for line in last["lines"]:
            style = ("fail" if " ERROR " in line else "warn" if " WARN " in line
                     else "accent" if "RESULT" in line or "===" in line or "---" in line else "grey70")
            console.print(Text(line, style=style), soft_wrap=True)
    return 0
