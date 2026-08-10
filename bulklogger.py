#!/usr/bin/env python3
"""
Bulklogger - bulk worklog entry for Jira Cloud.

Fill in a day's worth of worklogs in one window, submit them with one click.
See documentation/jira-bulk-worklog-design.md for the specification.

Usage:
    python bulklogger.py
    python bulklogger.py --dry-run            # no network, simulated Jira
    python bulklogger.py --reset-credentials  # forget the stored API token
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tkinter as tk
from tkinter import messagebox, ttk

import theme

APP_NAME = "Bulklogger"          # the product: exe name, file names, docs
WINDOW_TITLE = "JIRA Bulklogger"  # what the user sees on windows and dialogs
ICON_ICO = "bulklogger.ico"       # Windows
ICON_PNG = "bulklogger.png"       # Tk iconphoto everywhere else

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


def _app_dir():
    """Where tickets.toml and the personal files live.

    Frozen by PyInstaller, __file__ points inside the unpacked temp directory,
    so config has to hang off the executable. On macOS the executable sits at
    Bulklogger.app/Contents/MacOS/Bulklogger, and writing config in there would
    bury it inside the bundle — step back out to the folder holding the .app.
    """
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    here = Path(sys.executable).resolve().parent
    parts = here.parts
    if (len(parts) >= 3 and parts[-1] == "MacOS" and parts[-2] == "Contents"
            and parts[-3].endswith(".app")):
        return here.parents[2]
    return here


APP_DIR = _app_dir()


def resource_path(name):
    """Bundled read-only assets, which live in the unpack dir when frozen."""
    return Path(getattr(sys, "_MEIPASS", APP_DIR)) / name


def set_window_icon(window):
    """Title bar / dock icon. `default` covers every Toplevel too."""
    if IS_WINDOWS:
        ico = resource_path(ICON_ICO)
        if ico.exists():
            try:
                window.iconbitmap(default=str(ico))
                return
            except tk.TclError:
                pass
    png = resource_path(ICON_PNG)
    if not png.exists():
        return
    try:
        # the reference has to outlive this call or Tk drops the image
        window._icon_image = tk.PhotoImage(file=str(png))
        window.iconphoto(True, window._icon_image)
    except tk.TclError:
        pass


def wheel_steps(event):
    """Normalise a scroll event to a signed number of units.

    Windows reports delta in multiples of 120, macOS in small integers (a /120
    there rounds every scroll to zero), and X11 sends Button-4/5 with no delta
    at all.
    """
    button = getattr(event, "num", None)
    if button == 4:
        return -1
    if button == 5:
        return 1
    delta = getattr(event, "delta", 0)
    if IS_MACOS:
        return -delta
    return int(-delta / 120)

CONFIG_PATH = APP_DIR / "tickets.toml"
CREDENTIALS_PATH = APP_DIR / "credentials.toml"
DRAFT_PATH = APP_DIR / "draft.json"
USAGE_PATH = APP_DIR / "usage.json"

MIN_ROWS = 4
WORKLOG_HOUR = 11  # see design doc s10: far enough from midnight that no
                   # DST or offset discrepancy can push a worklog to another day
HTTP_TIMEOUT = 30
DRAFT_DEBOUNCE_MS = 800

# Mono Industrial palette, via theme.py
FIELD_BG = theme.EDITOR_BG
FIELD_ERR_BG = "#3B2326"  # EDITOR_BG pushed towards ERROR, still legible
FG_MAIN = theme.FG
FG_MUTED = theme.MUTED
FG_ERR = theme.ERROR
FG_OK = theme.ACCENT
HEADER_FG = theme.JSON_PROPERTY


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


class ConfigError(Exception):
    pass


@dataclass
class Config:
    base_url: str
    tz_name: str
    tz: object
    groups: dict  # label -> [key, ...]
    retired: list
    all_keys: list


def resolve_zone(name):
    """IANA name -> (tzinfo, name). Windows has no system tz database."""
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError) as exc:
        try:
            import tzdata  # noqa: F401
        except ImportError:
            raise ConfigError(
                "Windows ships no IANA time zone database, so Python cannot "
                f'resolve "{name}".\n\nInstall it with:\n\n    pip install tzdata'
            ) from exc
        raise ConfigError(
            f'timezone "{name}" is not a known IANA zone name.\n'
            'Expected something like "Europe/Bucharest".'
        ) from exc


def _local_zone():
    """Best-effort system zone. Fixed-offset fallback is DST-naive for other dates."""
    try:
        import tzlocal  # optional; not a declared dependency
        return ZoneInfo(tzlocal.get_localzone_name()), tzlocal.get_localzone_name()
    except Exception:
        tz = datetime.now().astimezone().tzinfo
        return tz, str(tz)


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(
            f"No config file at:\n{path}\n\n"
            "Create it with at least:\n\n"
            '  base_url = "https://your-site.atlassian.net"\n'
            '  timezone = "Europe/Bucharest"\n\n'
            "  [groups]\n"
            '  "My goals" = ["PROJ-1", "PROJ-2"]'
        )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Could not parse {path}:\n\n{exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {path}:\n\n{exc}") from exc

    base_url = str(raw.get("base_url", "")).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"base_url is missing or malformed in {path}.\n"
            'Expected something like "https://your-site.atlassian.net".'
        )

    tz_name = raw.get("timezone")
    if tz_name:
        tz, tz_name = resolve_zone(str(tz_name))
    else:
        tz, tz_name = _local_zone()

    groups_raw = raw.get("groups", {})
    if not isinstance(groups_raw, dict):
        raise ConfigError("[groups] must be a table of label = [ticket keys].")

    retired_raw = raw.get("retired", [])
    if not isinstance(retired_raw, list):
        raise ConfigError("retired must be a list of ticket keys.")

    bad = []
    seen = set()
    groups: dict = {}
    for label, keys in groups_raw.items():
        if not isinstance(keys, list):
            raise ConfigError(f'Group "{label}" must be a list of ticket keys.')
        cleaned = []
        for key in keys:
            key = str(key).strip().upper()
            if not KEY_RE.match(key):
                bad.append(key)
                continue
            if key in seen:
                continue  # a key listed twice shows up once
            seen.add(key)
            cleaned.append(key)
        if cleaned:
            groups[str(label)] = cleaned

    retired = []
    for key in retired_raw:
        key = str(key).strip().upper()
        if not KEY_RE.match(key):
            bad.append(key)
            continue
        if key not in retired:
            retired.append(key)
        seen.add(key)

    if bad:
        raise ConfigError(
            "These do not look like Jira issue keys:\n\n  "
            + "\n  ".join(bad)
            + "\n\nExpected the PROJ-123 form."
        )

    all_keys = [k for keys in groups.values() for k in keys]
    all_keys += [k for k in retired if k not in all_keys]
    if not all_keys:
        raise ConfigError(f"No ticket keys configured in {path}.")

    return Config(base_url, tz_name, tz, groups, retired, all_keys)


def sample_config():
    """Stand-in config so --dry-run works with no tickets.toml present."""
    tz, tz_name = resolve_zone("Europe/Bucharest")
    groups = {
        "QATT-80 · Test automation": ["QATT-85", "QATT-86", "QATT-87"],
        "H2 developer experience": ["QATT-91", "QATT-92"],
        "Unparented": ["QATT-99"],
    }
    retired = ["QATT-40", "QATT-41"]
    all_keys = [k for keys in groups.values() for k in keys] + retired
    return Config("https://example.atlassian.net", tz_name, tz,
                  groups, retired, all_keys)


# ---------------------------------------------------------------------------
# Jira client
# ---------------------------------------------------------------------------


class JiraError(Exception):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class JiraClient:
    """Jira Cloud REST v2.

    v2 rather than v3 because v2 accepts a plain string worklog comment; v3
    demands an Atlassian Document Format tree for no benefit here.
    """

    def __init__(self, base_url, email, token, timeout=HTTP_TIMEOUT):
        import requests  # imported here so --dry-run works without it

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (email, token)
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self._legacy_search = False

    def _request(self, method, path, params=None, body=None):
        import requests

        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(
                method, url, params=params, json=body, timeout=self.timeout
            )
        except requests.exceptions.SSLError as exc:
            raise JiraError(f"TLS handshake with {self.base_url} failed: {exc}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise JiraError(f"Could not reach {self.base_url}. Check your network.") from exc
        except requests.exceptions.Timeout as exc:
            raise JiraError(f"{self.base_url} did not respond within {self.timeout}s.") from exc
        except requests.exceptions.RequestException as exc:
            raise JiraError(str(exc)) from exc

        if resp.status_code == 204 or not resp.content:
            data = {}
        else:
            try:
                data = resp.json()
            except ValueError:
                data = {}

        if resp.status_code < 300:
            return data

        raise JiraError(
            self._describe(resp.status_code, data),
            status=resp.status_code,
            retry_after=_parse_retry_after(resp.headers.get("Retry-After")),
        )

    @staticmethod
    def _describe(status, data):
        detail = ""
        if isinstance(data, dict):
            msgs = list(data.get("errorMessages") or [])
            errs = data.get("errors")
            if isinstance(errs, dict):
                msgs += [f"{k}: {v}" for k, v in errs.items()]
            detail = "; ".join(str(m) for m in msgs if m)

        canned = {
            401: "Authentication failed - the API token is invalid or has been revoked.",
            403: "Permission denied - the account lacks 'Work On Issues' on this project.",
            404: "Issue not found, or not visible to this account.",
            429: "Rate limited by Jira.",
        }.get(status)
        if canned:
            return f"{canned} {detail}".strip()
        if 500 <= status < 600:
            return f"Jira returned a server error ({status}). {detail}".strip()
        return f"Jira returned HTTP {status}. {detail}".strip()

    # -- public API --------------------------------------------------------

    def myself(self):
        return self._request("GET", "/rest/api/2/myself")

    def fetch_summaries(self, keys):
        """key -> summary. Missing keys simply do not appear in the result."""
        out = {}
        jql = "key in ({})".format(", ".join(keys))
        for issue in self._search(jql):
            fields = issue.get("fields") or {}
            out[issue["key"]] = fields.get("summary") or "(no summary)"
        return out

    def _search(self, jql):
        # /rest/api/2/search was deprecated in favour of /search/jql on Jira
        # Cloud. Try the new endpoint, fall back if this instance predates it.
        if not self._legacy_search:
            try:
                return self._search_jql(jql)
            except JiraError as exc:
                if exc.status not in (404, 410):
                    raise
                self._legacy_search = True
        return self._search_legacy(jql)

    def _search_jql(self, jql):
        issues, token = [], None
        while True:
            params = {"jql": jql, "fields": "summary", "maxResults": 100}
            if token:
                params["nextPageToken"] = token
            data = self._request("GET", "/rest/api/2/search/jql", params=params)
            batch = data.get("issues") or []
            issues.extend(batch)
            token = data.get("nextPageToken")
            if not token or not batch:
                return issues

    def _search_legacy(self, jql):
        issues, start = [], 0
        while True:
            params = {"jql": jql, "fields": "summary",
                      "maxResults": 100, "startAt": start}
            data = self._request("GET", "/rest/api/2/search", params=params)
            batch = data.get("issues") or []
            issues.extend(batch)
            start += len(batch)
            if not batch or start >= int(data.get("total", 0)):
                return issues

    def fetch_day_worklogs(self, account_id, day, tz):
        """This account's worklogs for one day, oldest first.

        Deliberately not limited to the configured tickets: the point of the
        review tab is spotting time logged somewhere unexpected, or not at all.
        """
        jql = ('worklogAuthor = currentUser() '
               f'AND worklogDate = "{day.isoformat()}"')
        start = datetime(day.year, day.month, day.day, tzinfo=tz)
        end = start + timedelta(days=1)

        entries = []
        for issue in self._search(jql):
            key = issue["key"]
            summary = (issue.get("fields") or {}).get("summary") or ""
            for log in self._issue_worklogs(key, start, end):
                # startedAfter/Before already bound the window in absolute time;
                # the author still has to be filtered, the endpoint returns all.
                if (log.get("author") or {}).get("accountId") != account_id:
                    continue
                entries.append({
                    "key": key,
                    "summary": summary,
                    "seconds": int(log.get("timeSpentSeconds") or 0),
                    "comment": (log.get("comment") or "").strip(),
                    "started": log.get("started") or "",
                })
        entries.sort(key=lambda e: (e["started"], e["key"]))
        return entries

    def _issue_worklogs(self, key, start, end):
        logs, at = [], 0
        while True:
            data = self._request(
                "GET", f"/rest/api/2/issue/{key}/worklog",
                params={"startedAfter": int(start.timestamp() * 1000),
                        "startedBefore": int(end.timestamp() * 1000),
                        "startAt": at, "maxResults": 100})
            batch = data.get("worklogs") or []
            logs.extend(batch)
            at += len(batch)
            if not batch or at >= int(data.get("total", 0)):
                return logs

    def add_worklog(self, key, seconds, started, comment):
        """Returns the new worklog id. adjustEstimate=leave is mandatory."""
        body = {"timeSpentSeconds": int(seconds), "started": started, "comment": comment}
        attempts = 0
        while True:
            attempts += 1
            try:
                data = self._request(
                    "POST",
                    f"/rest/api/2/issue/{key}/worklog",
                    params={"adjustEstimate": "leave"},
                    body=body,
                )
                return str(data.get("id", ""))
            except JiraError as exc:
                if exc.status == 429 and attempts <= 3:
                    time.sleep(min(exc.retry_after or 5, 60))
                    continue
                raise


def _parse_retry_after(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class DryRunClient:
    """Simulated Jira. No network, no keyring.

    A row whose comment contains the word "fail" is rejected, so the
    partial-failure path can be exercised deliberately.
    """

    FAKE_TITLES = [
        "Migrate auth service to OIDC",
        "Flaky test triage for the nightly suite",
        "Improve CI feedback time",
        "Document the release checklist",
        "Reduce staging environment drift",
        "Onboarding guide refresh",
        "Retire the legacy reporting job",
        "Spike: contract testing options",
    ]

    def __init__(self, keys):
        self._keys = list(keys)
        self._next_id = 90001
        self._logged = []  # anything submitted this session shows in review

    def myself(self):
        return {"accountId": "dryrun", "emailAddress": "dry-run@example.com",
                "displayName": "Dry Run", "timeZone": "Europe/Bucharest"}

    def fetch_summaries(self, keys):
        return {k: self.FAKE_TITLES[i % len(self.FAKE_TITLES)]
                for i, k in enumerate(keys)}

    def add_worklog(self, key, seconds, started, comment):
        time.sleep(0.4)  # make the per-row UI progression visible
        if "fail" in comment.lower():
            raise JiraError(
                "Permission denied - the account lacks 'Work On Issues' on this "
                "project. (simulated: comment contained 'fail')",
                status=403,
            )
        self._next_id += 1
        self._logged.append({
            "key": key, "summary": self.FAKE_TITLES[0],
            "seconds": seconds, "comment": comment, "started": started,
        })
        return str(self._next_id)

    def fetch_day_worklogs(self, account_id, day, tz):
        time.sleep(0.3)
        stamp = day.isoformat()
        entries = [e for e in self._logged if e["started"].startswith(stamp)]
        if not entries and day.weekday() < 5:
            # a plausible-looking day so the review tab can be seen working
            entries = [
                {"key": "QATT-85", "summary": "Migrate auth service to OIDC",
                 "seconds": 7200, "comment": "Reviewed the auth PR.\n"
                                             "Paired with Anna on retry logic.",
                 "started": f"{stamp}T11:00:00.000+0300"},
                {"key": "QATT-91", "summary": "Improve CI feedback time",
                 "seconds": 5400, "comment": "Cut the nightly suite runtime.",
                 "started": f"{stamp}T11:00:00.000+0300"},
            ]
        return sorted(entries, key=lambda e: (e["started"], e["key"]))


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


CREDENTIALS_TEMPLATE = """\
# {app} credentials - personal to you.
#
# NEVER COMMIT THIS FILE. It holds an API token in plain text, which is
# equivalent to your Jira password for API purposes. It is gitignored; if you
# copy this project elsewhere, leave this file behind.
#
# Create a token at id.atlassian.com -> Security -> API tokens.
# Revoke it there if it is ever exposed.

email = {email}
api_token = {token}

# When this token was saved. Atlassian tokens expire, typically after a year.
created = {created}
"""

TOKEN_STALE_DAYS = 330  # ~11 months: close enough to a yearly expiry to warn


@dataclass
class Credentials:
    email: str
    token: str
    created: str = ""  # ISO date, or "" when it predates this field


def _toml_string(value):
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def load_credentials():
    """Credentials from credentials.toml, or None if absent/incomplete."""
    if not CREDENTIALS_PATH.exists():
        return None
    try:
        raw = tomllib.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Could not parse {CREDENTIALS_PATH}:\n\n{exc}\n\n"
            'Both values must be quoted, e.g. email = "you@example.com".'
        ) from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {CREDENTIALS_PATH}:\n\n{exc}") from exc

    email = str(raw.get("email") or "").strip()
    token = str(raw.get("api_token") or "").strip()
    if not (email and token) or token.startswith("PASTE"):
        return None
    return Credentials(email, token, _created_date(raw.get("created")))


def _created_date(value):
    """Fall back to the file's timestamp for tokens saved before this field."""
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        stamp = CREDENTIALS_PATH.stat().st_mtime
        return date.fromtimestamp(stamp).isoformat()
    except OSError:
        return ""


def describe_token_age(created):
    """(text, stale) for the header. Empty text when the date is unknown."""
    if not created:
        return "", False
    try:
        age = (date.today() - date.fromisoformat(created)).days
    except ValueError:
        return "", False
    if age >= TOKEN_STALE_DAYS:
        months = max(age // 30, 1)
        return f"token added {created} · {months} months ago, may expire soon", True
    return f"token added {created}", False


def store_credentials(email, token):
    CREDENTIALS_PATH.write_text(
        CREDENTIALS_TEMPLATE.format(app=APP_NAME,
                                    email=_toml_string(email),
                                    token=_toml_string(token),
                                    created=_toml_string(date.today().isoformat())),
        encoding="utf-8")
    # Best effort: meaningful on POSIX, largely cosmetic on Windows, where the
    # file inherits the user profile's ACL anyway.
    try:
        os.chmod(CREDENTIALS_PATH, 0o600)
    except OSError:
        pass


def clear_credentials():
    try:
        CREDENTIALS_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([a-z]+)")
_UNIT_SECONDS = {"h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
                 "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60}


def parse_duration(text):
    """'2h 30m' -> (9000, None). Invalid -> (None, 'reason').

    d and w are refused on purpose: their length depends on the Jira
    instance's working-day configuration, so they are ambiguous here.
    """
    raw = (text or "").strip().lower()
    if not raw:
        return None, "no time entered"

    pos, total = 0, 0
    matched_any = False
    for match in _TOKEN_RE.finditer(raw):
        if raw[pos:match.start()].strip():
            return None, f"could not read {text.strip()!r}"
        amount = float(match.group(1).replace(",", "."))
        unit = match.group(2)
        if unit in ("d", "day", "days", "w", "week", "weeks"):
            return None, "days and weeks are ambiguous here - use h and m"
        if unit not in _UNIT_SECONDS:
            return None, f"unknown unit {unit!r} - use h and m"
        total += amount * _UNIT_SECONDS[unit]
        pos = match.end()
        matched_any = True

    if not matched_any:
        if re.fullmatch(r"\d+(?:[.,]\d+)?", raw):
            return None, "add a unit, e.g. 2h or 90m"
        return None, f"could not read {text.strip()!r}"
    if raw[pos:].strip():
        return None, f"could not read {text.strip()!r}"

    seconds = int(round(total))
    if seconds <= 0:
        return None, "must be more than zero"
    return seconds, None


def format_duration(seconds):
    if not seconds:
        return "0m"
    minutes = int(round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def build_started(day: date, tz) -> str:
    """Jira wants yyyy-MM-ddTHH:mm:ss.SSS+HHMM - milliseconds, no colon.

    The offset is derived from the *selected* date so a December entry gets
    +0200 and an August entry +0300 (for Europe/Bucharest).
    """
    stamp = datetime(day.year, day.month, day.day, WORKLOG_HOUR, 0, tzinfo=tz)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.000%z")


# ---------------------------------------------------------------------------
# Ticket catalogue
# ---------------------------------------------------------------------------

HEADER, ITEM, TOGGLE, NOTE = "header", "item", "toggle", "note"


def entry_options(fonts):
    """Classic tk.Entry styled to match theme.py's ttk widgets.

    The inputs stay classic rather than ttk so their background can be tinted
    directly when a field is flagged; ttk.Entry hides fieldbackground behind
    the style engine.
    """
    return {
        "background": FIELD_BG,
        "foreground": FG_MAIN,
        "disabledbackground": theme.CHROME_BG,
        "disabledforeground": theme.BORDER,
        "insertbackground": theme.CARET,
        "selectbackground": theme.SELECT_BG,
        "selectforeground": theme.SELECT_FG,
        "relief": "flat",
        "borderwidth": 0,
        "highlightthickness": 1,
        "highlightbackground": theme.BORDER,
        "highlightcolor": theme.ACCENT,
        "font": fonts["mono"],
    }


@dataclass
class Ticket:
    key: str
    title: str
    group: str
    retired: bool


class Catalog:
    def __init__(self, cfg: Config, titles: dict, usage: dict):
        self.usage = usage
        self.tickets = {}
        self.order = []  # config order, for stable tie-breaking
        for label, keys in cfg.groups.items():
            for key in keys:
                if key in cfg.retired:
                    continue
                self.tickets[key] = Ticket(key, titles.get(key, ""), label, False)
                self.order.append(key)
        for key in cfg.retired:
            label = next((lbl for lbl, keys in cfg.groups.items() if key in keys),
                         "Past goals")
            self.tickets[key] = Ticket(key, titles.get(key, ""), label, True)
            self.order.append(key)

    def title(self, key):
        ticket = self.tickets.get(key)
        return ticket.title if ticket else ""

    def label(self, key):
        ticket = self.tickets.get(key)
        if not ticket:
            return key
        return f"{key} · {ticket.title}" if ticket.title else key

    def _rank(self, key):
        """Most recently logged first; never-logged keep config order."""
        try:
            index = self.order.index(key)
        except ValueError:
            index = 9999
        stamp = self.usage.get(key)
        if stamp:
            try:
                return (0, -datetime.fromisoformat(stamp).timestamp(), index)
            except ValueError:
                pass
        return (1, 0.0, index)

    def _matches(self, ticket, query):
        return (query in ticket.key.lower()
                or query in ticket.title.lower())

    def build(self, query="", show_retired=False):
        """Returns [(kind, text, payload)] for the picker listbox."""
        query = (query or "").strip().lower()
        rows = []

        active = [t for t in self.tickets.values() if not t.retired]
        by_group = {}
        for ticket in active:
            by_group.setdefault(ticket.group, []).append(ticket)

        group_hits = {}
        for label, tickets in by_group.items():
            if not query:
                hits = tickets
            elif query in label.lower():
                hits = tickets  # a group match reveals all its children
            else:
                hits = [t for t in tickets if self._matches(t, query)]
            if hits:
                group_hits[label] = sorted(hits, key=lambda t: self._rank(t.key))

        # groups ordered by their most recently used member
        for label in sorted(group_hits, key=lambda l: self._rank(group_hits[l][0].key)):
            rows.append((HEADER, label, None))
            for ticket in group_hits[label]:
                rows.append((ITEM, f"    {ticket.key} · {ticket.title}", ticket.key))

        retired = [t for t in self.tickets.values() if t.retired]
        if retired:
            if query:
                hits = [t for t in retired
                        if self._matches(t, query) or query in t.group.lower()]
            else:
                hits = retired
            hits.sort(key=lambda t: self._rank(t.key))
            expanded = show_retired or (bool(query) and bool(hits))
            count = f" ({len(hits)} match{'es' if len(hits) != 1 else ''})" if query \
                else f" ({len(retired)})"
            arrow = "▾" if expanded else "▸"
            rows.append((TOGGLE, f"{arrow} Past goals{count}", None))
            if expanded:
                for ticket in hits:
                    rows.append((ITEM, f"    {ticket.key} · {ticket.title}", ticket.key))

        if not any(kind == ITEM for kind, _, _ in rows):
            rows.append((NOTE, "    no matching tickets", None))
        return rows


# ---------------------------------------------------------------------------
# Ticket picker widget
# ---------------------------------------------------------------------------


class TicketPicker(tk.Frame):
    """Entry + popup Listbox.

    ttk.Combobox is deliberately not used: it takes a flat list of strings, so
    group headers would be selectable and indentation would be a hack.
    """

    MAX_VISIBLE = 14

    def __init__(self, master, catalog: Catalog, fonts, on_change=None, width=34):
        super().__init__(master, background=theme.CHROME_BG)
        self.catalog = catalog
        self.fonts = fonts
        self.on_change = on_change
        self.key = None
        self._display = ""
        self._popup = None
        self._listbox = None
        self._rows = []
        self._active = -1
        self._show_retired = False

        self.entry = tk.Entry(self, width=width, **entry_options(fonts))
        self.entry.pack(fill="x")
        self.entry.bind("<Button-1>", self._open)
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self.entry.bind("<KeyRelease>", self._on_key)
        self.entry.bind("<Down>", lambda e: self._move(1) or "break")
        self.entry.bind("<Up>", lambda e: self._move(-1) or "break")
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Escape>", lambda e: self._close() or "break")

    # -- state -------------------------------------------------------------

    def set_key(self, key, notify=False):
        if key and key in self.catalog.tickets:
            self.key = key
            self._display = self.catalog.label(key)
        else:
            self.key = None
            self._display = ""
        self._restore_text()
        if notify and self.on_change:
            self.on_change()

    def clear(self):
        self.set_key(None)

    def set_enabled(self, enabled):
        self.entry.configure(state="normal" if enabled else "disabled")

    def mark(self, bad):
        self.entry.configure(background=FIELD_ERR_BG if bad else FIELD_BG)

    def _restore_text(self):
        self.entry.delete(0, "end")
        if self._display:
            self.entry.insert(0, self._display)

    # -- popup -------------------------------------------------------------

    def _on_focus_in(self, _event=None):
        self.entry.select_range(0, "end")  # typing replaces the shown selection
        self._open()

    def _open(self, _event=None):
        if self._popup:
            return
        self._show_retired = False
        popup = tk.Toplevel(self)
        popup.wm_overrideredirect(True)
        popup.attributes("-topmost", True)
        # a 1px BORDER-coloured frame standing in for a themed window border
        frame = tk.Frame(popup, background=theme.BORDER, padx=1, pady=1)
        frame.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, orient="vertical",
                               style="Editor.Vertical.TScrollbar")
        listbox = tk.Listbox(
            frame, activestyle="none", exportselection=False, height=12,
            yscrollcommand=scroll.set, highlightthickness=0, borderwidth=0,
            background=theme.EDITOR_BG, foreground=FG_MAIN,
            selectbackground=theme.ACCENT, selectforeground=theme.SELECT_FG,
            font=self.fonts["mono"],
        )
        scroll.config(command=listbox.yview)
        scroll.pack(side="right", fill="y")
        listbox.pack(side="left", fill="both", expand=True)
        listbox.bind("<Button-1>", self._on_click)
        # Scroll the list, and stop there. The main window scrolls the row area
        # from a bind_all on "all", which runs after this widget's own bindings
        # unless one of them breaks -- otherwise one wheel notch moves both.
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            listbox.bind(sequence, self._on_wheel)
            scroll.bind(sequence, self._on_wheel)
        self._popup, self._listbox = popup, listbox
        self._refill()

    def _place(self):
        """Size the popup to its contents, up to MAX_VISIBLE rows."""
        if not self._popup:
            return
        self._listbox.configure(height=max(1, min(self.MAX_VISIBLE, len(self._rows))))
        self._popup.update_idletasks()
        self.entry.update_idletasks()
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height()
        width = max(self.entry.winfo_width(), 460)
        height = max(self._popup.winfo_reqheight(), 24)
        self._popup.wm_geometry(f"{width}x{height}+{x}+{y}")

    def _close(self, _event=None):
        if self._popup:
            self._popup.destroy()
        self._popup = self._listbox = None
        self._rows, self._active = [], -1
        self._restore_text()

    def _on_focus_out(self, _event=None):
        self.after(120, self._maybe_close)

    def _maybe_close(self):
        if not self._popup:
            return
        try:
            focused = self.focus_get()
        except KeyError:
            focused = None
        if focused is not None and str(focused).startswith(str(self._popup)):
            return
        self._close()

    # -- contents ----------------------------------------------------------

    def _query(self):
        text = self.entry.get()
        return "" if text == self._display else text

    def _refill(self, keep_active=False):
        if not self._listbox:
            return
        self._rows = self.catalog.build(self._query(), self._show_retired)
        self._listbox.delete(0, "end")
        for index, (kind, text, _payload) in enumerate(self._rows):
            self._listbox.insert("end", text)
            if kind == HEADER:
                self._listbox.itemconfig(index, foreground=HEADER_FG)
            elif kind == TOGGLE:
                self._listbox.itemconfig(index, foreground=theme.ACCENT)
            elif kind == NOTE:
                self._listbox.itemconfig(index, foreground=FG_MUTED)
        if not keep_active or self._active >= len(self._rows):
            self._active = self._first_selectable()
        self._place()  # the row count changes as the filter narrows
        self._highlight()

    def _first_selectable(self):
        for index, (kind, _text, _payload) in enumerate(self._rows):
            if kind == ITEM:
                return index
        return -1

    def _highlight(self):
        if not self._listbox:
            return
        self._listbox.selection_clear(0, "end")
        if 0 <= self._active < len(self._rows):
            self._listbox.selection_set(self._active)
            self._listbox.see(self._active)

    def _move(self, step):
        if not self._popup:
            self._open()
            return True
        index = self._active
        for _ in range(len(self._rows)):
            index += step
            if index < 0 or index >= len(self._rows):
                return True
            if self._rows[index][0] == ITEM:
                self._active = index
                self._highlight()
                return True
        return True

    def _on_key(self, event):
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab",
                            "Shift_L", "Shift_R", "Control_L", "Control_R"):
            return
        if not self._popup:
            self._open()
        self._refill()

    def _on_return(self, _event=None):
        if self._popup and 0 <= self._active < len(self._rows):
            self._choose(self._active)
        return "break"

    def _on_wheel(self, event):
        if self._listbox:
            self._listbox.yview_scroll(wheel_steps(event), "units")
        return "break"

    def is_open(self):
        return self._popup is not None

    def _on_click(self, event):
        if not self._listbox:
            return "break"
        self._choose(self._listbox.nearest(event.y))
        self.entry.focus_set()
        return "break"

    def _choose(self, index):
        if not (0 <= index < len(self._rows)):
            return
        kind, _text, payload = self._rows[index]
        if kind == TOGGLE:
            self._show_retired = not self._show_retired
            self._refill()
            return
        if kind != ITEM:
            return
        self.key = payload
        self._display = self.catalog.label(payload)
        self._close()
        if self.on_change:
            self.on_change()


# ---------------------------------------------------------------------------
# Row
# ---------------------------------------------------------------------------

EMPTY, PARTIAL, READY, SUBMITTING, LOGGED, FAILED = (
    "empty", "partial", "ready", "submitting", "logged", "failed")

GLYPHS = {
    EMPTY: ("", FG_MUTED),
    PARTIAL: ("!", FG_ERR),
    READY: ("•", "#2c6fbb"),
    SUBMITTING: ("…", "#2c6fbb"),
    LOGGED: ("✓", FG_OK),
    FAILED: ("✗", FG_ERR),
}


class DateField(ttk.Frame):
    """◀ [YYYY-MM-DD] ▶ [Today], with a "Fri · today" style note.

    Tkinter has no date widget and no third-party one was added; back-dating a
    day or two is the common case and a single click covers it.
    """

    def __init__(self, master, fonts, on_change=None, label="Date:"):
        super().__init__(master)
        self.on_change = on_change
        self._value = date.today()
        self._note_job = None

        ttk.Label(self, text=label).pack(side="left")
        ttk.Button(self, text="◀", width=3,
                   command=lambda: self.shift(-1)).pack(side="left", padx=(6, 2))
        self.var = tk.StringVar(value=self._value.isoformat())
        self.entry = tk.Entry(self, textvariable=self.var, width=12,
                              justify="center", **entry_options(fonts))
        self.entry.pack(side="left")
        self.entry.bind("<Return>", lambda e: self.commit())
        self.entry.bind("<FocusOut>", lambda e: self.commit())
        ttk.Button(self, text="▶", width=3,
                   command=lambda: self.shift(1)).pack(side="left", padx=(2, 6))
        ttk.Button(self, text="Today", command=self.today).pack(side="left")

        self.note = ttk.Label(self, text="", style="Muted.TLabel")
        self.note.pack(side="left", padx=(10, 0))
        self._describe()

    @property
    def value(self):
        return self._value

    def set(self, day, notify=True):
        changed = day != self._value
        self._value = day
        self.var.set(day.isoformat())
        self._describe()
        if changed and notify and self.on_change:
            self.on_change()

    def shift(self, days):
        self.set(self._value + timedelta(days=days))

    def today(self):
        self.set(date.today())

    def commit(self):
        try:
            day = date.fromisoformat(self.var.get().strip())
        except ValueError:
            self.var.set(self._value.isoformat())
            self.note.configure(text="use YYYY-MM-DD", foreground=FG_ERR)
            self._cancel_note_job()
            self._note_job = self.after(2500, self._describe)
            return
        self.set(day)

    def _describe(self):
        self._note_job = None
        delta = (self._value - date.today()).days
        if delta == 0:
            when = "today"
        elif delta == -1:
            when = "yesterday"
        elif delta < 0:
            when = f"{-delta} days ago"
        else:
            when = f"in {delta} days"
        self.note.configure(text=f"{self._value.strftime('%a')} · {when}",
                            foreground=FG_MUTED)

    def _cancel_note_job(self):
        if self._note_job:
            try:
                self.after_cancel(self._note_job)
            except Exception:
                pass
            self._note_job = None


class StickyNote(tk.Frame):
    """Free-text scratchpad. Survives Clear and date changes, never submitted."""

    def __init__(self, master, fonts, on_change=None):
        super().__init__(master, background=theme.CHROME_BG)
        self.on_change = on_change

        header = tk.Frame(self, background=theme.CHROME_BG)
        header.pack(fill="x")
        tk.Label(header, text="Current work", background=theme.CHROME_BG,
                 foreground=theme.ACCENT, font=fonts["ui_bold"]).pack(side="left")
        tk.Label(header, text="  notes to yourself · kept across days · "
                              "never cleared · never sent to Jira",
                 background=theme.CHROME_BG, foreground=FG_MUTED,
                 font=fonts["ui"]).pack(side="left")

        self.view = theme.ScrollableText(self, fonts["mono"], foreground=FG_MAIN,
                                         wrap="word", undo=True)
        self.view.pack(fill="both", expand=True, pady=(5, 0))
        self.text = self.view.text
        self.text.bind("<<Modified>>", self._modified)
        self.text.bind("<Tab>", self._focus_next)

    def _modified(self, _event=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            if self.on_change:
                self.on_change()

    def _focus_next(self, event):
        event.widget.tk_focusNext().focus()
        return "break"

    def get(self):
        return self.text.get("1.0", "end-1c").rstrip()

    def set(self, value):
        self.text.delete("1.0", "end")
        if value:
            self.text.insert("1.0", value)
        self.text.edit_modified(False)


class EntryRow:
    def __init__(self, app, parent, catalog):
        self.app = app
        self.catalog = catalog
        self.fonts = app.fonts
        self.error = None
        self.worklog_id = None
        self._locked_state = None  # SUBMITTING / LOGGED, or None

        self.frame = tk.Frame(parent, background=theme.CHROME_BG)
        self.frame.columnconfigure(1, weight=1)

        self.picker = TicketPicker(self.frame, catalog, self.fonts,
                                   on_change=self._changed)
        self.picker.grid(row=0, column=0, sticky="new", padx=(0, 6), pady=2)

        self.comment = tk.Text(self.frame, height=3, wrap="word", undo=True)
        theme.style_text(self.comment, self.fonts["mono"], foreground=FG_MAIN)
        self.comment.configure(highlightthickness=1,
                               highlightbackground=theme.BORDER,
                               highlightcolor=theme.ACCENT)
        self.comment.grid(row=0, column=1, sticky="nsew", padx=(0, 6), pady=2)
        self.comment.bind("<<Modified>>", self._on_comment_modified)
        self.comment.bind("<Tab>", self._focus_next)
        self.comment.bind("<Shift-Tab>", self._focus_prev)
        self.comment.bind("<ISO_Left_Tab>", self._focus_prev)
        self.comment.bind("<Control-Return>", self._submit_hotkey)

        self.time_var = tk.StringVar()
        self.time_var.trace_add("write", lambda *_: self._changed())
        self.time = tk.Entry(self.frame, width=8, textvariable=self.time_var,
                             justify="center", **entry_options(self.fonts))
        self.time.grid(row=0, column=2, sticky="new", padx=(0, 6), pady=2)

        self.status = tk.Label(self.frame, text="", width=2, anchor="center",
                               background=theme.CHROME_BG,
                               font=self.fonts["ui_bold"])
        self.status.grid(row=0, column=3, sticky="n", pady=4)

        self.remove_btn = ttk.Button(self.frame, text="✕", width=3,
                                     command=self._remove)
        self.remove_btn.grid(row=0, column=4, sticky="n", pady=2)

        self.message = tk.Label(self.frame, text="", anchor="w", justify="left",
                                background=theme.CHROME_BG, fg=FG_ERR,
                                font=self.fonts["ui"], wraplength=620)
        self.message.grid(row=1, column=0, columnspan=5, sticky="w", padx=(4, 0))
        self.message.grid_remove()

        self.summary = tk.Label(self.frame, text="", anchor="w", justify="left",
                                background=theme.CHROME_BG, fg=FG_OK,
                                font=self.fonts["mono"], wraplength=760)
        self.summary.grid(row=2, column=0, columnspan=5, sticky="w", padx=(4, 0))
        self.summary.grid_remove()

    # -- content -----------------------------------------------------------

    @property
    def comment_text(self):
        return self.comment.get("1.0", "end-1c").strip()

    @property
    def time_text(self):
        return self.time_var.get().strip()

    @property
    def key(self):
        return self.picker.key

    def as_dict(self):
        return {"key": self.key or "", "comment": self.comment_text,
                "time": self.time_text}

    def load(self, data):
        self.picker.set_key(data.get("key") or None)
        self.comment.delete("1.0", "end")
        self.comment.insert("1.0", data.get("comment") or "")
        self.comment.edit_modified(False)
        self.time_var.set(data.get("time") or "")

    # -- state -------------------------------------------------------------

    @property
    def seconds(self):
        value, _err = parse_duration(self.time_text)
        return value

    @property
    def state(self):
        if self._locked_state:
            return self._locked_state
        filled = [bool(self.key), bool(self.comment_text), bool(self.time_text)]
        if not any(filled):
            return EMPTY
        if not all(filled) or self.seconds is None:
            return PARTIAL
        return FAILED if self.error else READY

    @property
    def is_blank(self):
        return self.state == EMPTY

    @property
    def is_locked(self):
        return self._locked_state in (SUBMITTING, LOGGED)

    @property
    def is_submittable(self):
        return self.state in (READY, FAILED)

    def missing(self):
        gaps = []
        if not self.key:
            gaps.append("ticket")
        if not self.comment_text:
            gaps.append("comment")
        if not self.time_text:
            gaps.append("time")
        elif self.seconds is None:
            _v, why = parse_duration(self.time_text)
            gaps.append(f"time ({why})")
        return gaps

    def mark_submitting(self):
        self._locked_state = SUBMITTING
        self.error = None
        self.refresh()

    def mark_logged(self, worklog_id):
        self._locked_state = LOGGED
        self.worklog_id = worklog_id
        self.error = None
        self.refresh()

    def mark_failed(self, message):
        self._locked_state = None
        self.error = message
        self.refresh()

    # -- display -----------------------------------------------------------

    def refresh(self, highlight_gaps=False):
        state = self.state
        glyph, colour = GLYPHS[state]
        self.status.configure(text=glyph, fg=colour)

        if state == LOGGED:
            # collapse to one line: the glyph lives in the summary text itself,
            # so the status column must go too or it occupies a line of its own
            for widget in (self.picker, self.comment, self.time,
                           self.remove_btn, self.status):
                widget.grid_remove()
            self.message.grid_remove()
            first_line = self.comment_text.splitlines()[0] if self.comment_text else ""
            if len(first_line) > 70:
                first_line = first_line[:69] + "…"
            self.summary.configure(
                text=f"✓  {self.key}  ·  {format_duration(self.seconds)}"
                     f"  ·  {first_line}")
            self.summary.grid()
            return

        self.summary.grid_remove()
        for widget in (self.picker, self.comment, self.time,
                       self.remove_btn, self.status):
            widget.grid()

        editable = state != SUBMITTING
        self.picker.set_enabled(editable)
        self.comment.configure(state="normal" if editable else "disabled")
        self.time.configure(state="normal" if editable else "disabled")
        self.remove_btn.configure(state="normal" if editable else "disabled")

        gaps = self.missing() if (highlight_gaps and state == PARTIAL) else []
        self.picker.mark("ticket" in gaps)
        self.comment.configure(
            background=FIELD_ERR_BG if "comment" in gaps else FIELD_BG)
        self.time.configure(
            background=FIELD_ERR_BG if any(g.startswith("time") for g in gaps)
            else FIELD_BG)

        if self.error:
            self.message.configure(text=self.error, fg=FG_ERR)
            self.message.grid()
        elif gaps:
            self.message.configure(text="missing: " + ", ".join(gaps), fg=FG_ERR)
            self.message.grid()
        else:
            self.message.grid_remove()

    # -- events ------------------------------------------------------------

    def _on_comment_modified(self, _event=None):
        if self.comment.edit_modified():
            self.comment.edit_modified(False)
            self._changed()

    def _changed(self):
        self.app.on_row_change()

    def _remove(self):
        self.app.remove_row(self)

    def _focus_next(self, event):
        event.widget.tk_focusNext().focus()
        return "break"

    def _focus_prev(self, event):
        event.widget.tk_focusPrev().focus()
        return "break"

    def _submit_hotkey(self, _event=None):
        self.app.submit()
        return "break"

    def destroy(self):
        self.picker._close()
        self.frame.destroy()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # a lost draft must never take the app down


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class App(tk.Tk):
    def __init__(self, cfg: Config, client, catalog: Catalog, usage: dict,
                 account="", account_id="", dry_run=False):
        super().__init__()
        self.cfg = cfg
        self.client = client
        self.catalog = catalog
        self.usage = usage
        self.account = account
        self.account_id = account_id
        self.dry_run = dry_run
        self.rows = []
        self.submitting = False
        self.reviewing = False
        self._loading = True
        self._draft_job = None
        self._resync_job = None
        self._queue = queue.Queue()
        self._review_queue = queue.Queue()

        self.fonts = theme.apply_theme(self)
        self._style_extras()

        set_window_icon(self)
        title = f"{WINDOW_TITLE} — {cfg.base_url.split('//')[-1]}"
        self.title(title + ("  [DRY RUN]" if dry_run else ""))
        self.geometry("1020x700")
        self.minsize(860, 460)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_topbar()
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.notebook = notebook

        log_tab = ttk.Frame(notebook)
        notebook.add(log_tab, text="  Log work  ")
        self._build_log_tab(log_tab)

        # between the two similarly-named tabs, which also keeps them apart
        notes_tab = ttk.Frame(notebook)
        notebook.add(notes_tab, text="  Current work  ")
        self._build_notes_tab(notes_tab)

        review_tab = ttk.Frame(notebook)
        notebook.add(review_tab, text="  Logged work  ")
        self._build_review_tab(review_tab)

        self.bind_all("<Control-Return>", lambda e: self.submit())

        self._load_draft()
        self._loading = False
        self.normalize_rows()
        self.refresh()
        self.after(10, lambda: theme.apply_window_chrome(self))

    # -- construction ------------------------------------------------------

    def _style_extras(self):
        style = ttk.Style(self)
        style.configure("Total.TLabel", foreground=theme.ACCENT,
                        font=(*self.fonts["ui"][:1], 12, "bold"))
        style.configure("Muted.TLabel", foreground=FG_MUTED)
        style.configure("Submit.TButton", font=self.fonts["ui_bold"])
        style.configure("TNotebook", background=theme.CHROME_BG, borderwidth=0,
                        tabmargins=(2, 6, 2, 0))
        style.configure("TNotebook.Tab", background=theme.EDITOR_BG,
                        foreground=FG_MUTED, borderwidth=0, padding=(16, 7),
                        font=self.fonts["ui"])
        style.map("TNotebook.Tab",
                  background=[("selected", theme.CHROME_BG),
                              ("active", theme.CURRENT_LINE)],
                  foreground=[("selected", theme.ACCENT)])

    def _build_topbar(self):
        """Account and sign-out live above the tabs: they apply to both."""
        bar = ttk.Frame(self, padding=(12, 8, 12, 2))
        bar.pack(fill="x")

        self.signout_btn = ttk.Button(bar, text="Sign out", width=9,
                                      command=self.sign_out)
        self.token_label = ttk.Label(bar, text="", style="Muted.TLabel")
        self.account_label = ttk.Label(bar, text="", style="Muted.TLabel")
        if not self.dry_run:
            self.signout_btn.pack(side="right")
            self.token_label.pack(side="right", padx=(0, 12))
            self.account_label.pack(side="right", padx=(0, 12))
        self._update_account_label()

    # -- tab 1: log work ---------------------------------------------------

    def _build_log_tab(self, parent):
        bar = ttk.Frame(parent, padding=(10, 8))
        bar.pack(fill="x")
        self.date_field = DateField(bar, self.fonts,
                                    on_change=self._on_date_changed)
        self.date_field.pack(side="left")
        self.date_var = self.date_field.var  # kept for the existing API
        self.total_label = ttk.Label(bar, text="Total: 0m", style="Total.TLabel")
        self.total_label.pack(side="right")

        ttk.Separator(parent).pack(fill="x")
        self._build_rows_area(parent)
        self._build_footer(parent)

    def _build_rows_area(self, parent):
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(container, highlightthickness=0,
                                background=theme.CHROME_BG)
        scroll = ttk.Scrollbar(container, orient="vertical",
                               style="Editor.Vertical.TScrollbar",
                               command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.rows_frame = ttk.Frame(self.canvas, padding=(10, 6))
        self._window = self.canvas.create_window((0, 0), window=self.rows_frame,
                                                 anchor="nw")
        self.rows_frame.bind("<Configure>", self._resync_scroll)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind_all(sequence, self._on_wheel)

    def _on_canvas_resize(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)
        self._resync_scroll()

    def _schedule_resync(self):
        """Resync once the pending layout has settled.

        <Configure> on the rows frame fires when it grows but not reliably when
        it shrinks, so the refresh cycle drives this instead of the binding
        alone. Coalesced through a single idle callback: refresh() runs on
        every keystroke.
        """
        if self._resync_job is None:
            self._resync_job = self.after_idle(self._run_resync)

    def _run_resync(self):
        self._resync_job = None
        self._resync_scroll()

    def _resync_scroll(self, _event=None):
        """Keep the scrollregion current and the view anchored inside it.

        Two separate Tk behaviours conspire here:

        1. A scrollregion *smaller than the widget* does not stop the canvas
           scrolling above it, and yview() reports (0.0, 1.0) the whole time —
           so the rows slide downwards leaving blank space on top, invisibly to
           anything checking yview. Padding the region out to the viewport
           height restores confinement and makes yview meaningful again.
        2. Tk keeps the old yview fraction when the region shrinks, so removing
           rows leaves the list stranded mid-scroll.
        """
        bbox = self.canvas.bbox("all")
        if not bbox:
            return
        viewport = self.canvas.winfo_height()
        content = bbox[3]
        self.canvas.configure(scrollregion=(0, 0, bbox[2], max(content, viewport)))

        if content <= viewport:
            self.canvas.yview_moveto(0)
            return
        furthest = 1.0 - viewport / content
        if self.canvas.yview()[0] > furthest:
            self.canvas.yview_moveto(furthest)

    # -- tab 2: current work -----------------------------------------------

    def _build_notes_tab(self, parent):
        wrap = ttk.Frame(parent, padding=(10, 10))
        wrap.pack(fill="both", expand=True)
        self.sticky = StickyNote(wrap, self.fonts,
                                 on_change=self.schedule_draft_save)
        self.sticky.pack(fill="both", expand=True)

    # -- tab 3: logged work ------------------------------------------------

    def _build_review_tab(self, parent):
        bar = ttk.Frame(parent, padding=(10, 10))
        bar.pack(fill="x")
        self.review_date = DateField(bar, self.fonts)
        self.review_date.pack(side="left")
        self.review_btn = ttk.Button(bar, text="Show work logged",
                                     style="Submit.TButton",
                                     command=self.load_logged_work)
        self.review_btn.pack(side="left", padx=(14, 0))
        self.review_total = ttk.Label(bar, text="", style="Total.TLabel")
        self.review_total.pack(side="right")

        ttk.Separator(parent).pack(fill="x")

        body = ttk.Frame(parent, padding=(10, 8))
        body.pack(fill="both", expand=True)
        self.review_view = theme.ScrollableText(
            body, self.fonts["mono"], foreground=FG_MAIN, wrap="word", height=10)
        self.review_view.pack(fill="both", expand=True)

        text = self.review_view.text
        text.tag_configure("key", foreground=HEADER_FG,
                           font=(*self.fonts["mono"], "bold"))
        text.tag_configure("summary", foreground=FG_MUTED)
        text.tag_configure("time", foreground=theme.ACCENT,
                           font=(*self.fonts["mono"], "bold"))
        text.tag_configure("comment", foreground=theme.JSON_DEFAULT)
        text.tag_configure("muted", foreground=FG_MUTED)
        text.tag_configure("error", foreground=FG_ERR)
        self._review_message("Pick a date and press Show work logged.")

    def _on_wheel(self, event):
        # Only reached when the pointer is outside an open picker, since the
        # popup breaks the event. The popup is positioned absolutely, so it
        # would hang in mid-air over scrolled content -- close it instead.
        self._close_pickers()
        self.canvas.yview_scroll(wheel_steps(event), "units")

    def _close_pickers(self):
        for row in self.rows:
            if row.picker.is_open():
                row.picker._close()

    def _build_footer(self, parent):
        ttk.Separator(parent).pack(fill="x")
        bar = ttk.Frame(parent, padding=(10, 8))
        bar.pack(fill="x")

        # No "add entry" button: a blank row is appended automatically as soon
        # as the last one is used, so there is never nothing to type into.
        self.hint = ttk.Label(bar, text="Ctrl+Enter submits", style="Muted.TLabel")
        self.hint.pack(side="left")

        self.submit_btn = ttk.Button(bar, text="Submit", style="Submit.TButton",
                                     command=self.submit)
        self.submit_btn.pack(side="right")
        self.clear_btn = ttk.Button(bar, text="Clear", command=self.clear_all)
        self.clear_btn.pack(side="right", padx=(0, 8))

    # -- rows --------------------------------------------------------------

    def add_row(self, data=None):
        row = EntryRow(self, self.rows_frame, self.catalog)
        row.frame.pack(fill="x", pady=1)
        self.rows.append(row)
        if data:
            row.load(data)
        return row

    def remove_row(self, row):
        if row.is_locked:
            return
        row.destroy()
        self.rows.remove(row)
        self.normalize_rows()
        self.refresh()

    def normalize_rows(self):
        """Exactly one trailing blank row, never fewer than MIN_ROWS total."""
        while (len(self.rows) > MIN_ROWS
               and len(self.rows) >= 2
               and self.rows[-1].is_blank
               and self.rows[-2].is_blank):
            self.rows.pop().destroy()
        if not self.rows or not self.rows[-1].is_blank:
            self.add_row()
        while len(self.rows) < MIN_ROWS:
            self.add_row()

    def on_row_change(self):
        if self.submitting or self._loading:
            return
        self.normalize_rows()
        self.refresh()
        self.schedule_draft_save()

    def refresh(self, highlight_gaps=False):
        for row in self.rows:
            row.refresh(highlight_gaps=highlight_gaps)

        total = sum(row.seconds or 0 for row in self.rows if not row.is_blank)
        self.total_label.configure(text=f"Total: {format_duration(total)}")

        count = sum(1 for row in self.rows if row.is_submittable)
        noun = "entry" if count == 1 else "entries"
        self.submit_btn.configure(
            text=f"Submit {count} {noun}" if count else "Submit",
            state="disabled" if (self.submitting or not count) else "normal")
        self.clear_btn.configure(state="disabled" if self.submitting else "normal")
        self.signout_btn.configure(state="disabled" if self.submitting else "normal")
        self._schedule_resync()

    # -- account -----------------------------------------------------------

    def _update_account_label(self):
        self.account_label.configure(text=self.account or "")
        if self.dry_run:
            return
        try:
            stored = load_credentials()
        except ConfigError:
            stored = None
        text, stale = describe_token_age(stored.created if stored else "")
        self.token_label.configure(
            text=text, foreground=theme.WARNING if stale else FG_MUTED)

    def sign_out(self):
        """Forget the token and sign in again without restarting.

        API tokens expire, so this has to be reachable while the app is open.
        Typed rows survive: the draft is written first and the row widgets are
        never rebuilt, only re-pointed at the new catalogue.
        """
        if self.submitting or self.dry_run:
            return
        if not messagebox.askyesno(
                WINDOW_TITLE,
                "Sign out and enter a different API token?\n\n"
                "Your typed entries are kept. Anything already logged stays "
                "in Jira.",
                parent=self):
            return

        self.save_draft()
        clear_credentials()
        self.withdraw()  # the sign-in dialog stands alone while we are hidden
        try:
            client, profile, account = authenticate(
                self, self.cfg, "Signed out. Enter your new API token.")
        except ConfigError as exc:
            messagebox.showerror(WINDOW_TITLE, str(exc), parent=self)
            self.destroy()
            return
        if client is None:  # cancelled - there is no signed-in state to return to
            self.destroy()
            return

        try:
            titles = client.fetch_summaries(self.cfg.all_keys)
        except JiraError as exc:
            messagebox.showerror(
                WINDOW_TITLE, f"Signed in, but could not load ticket titles.\n\n{exc}",
                parent=self)
            self.destroy()
            return

        unknown = [key for key in self.cfg.all_keys if key not in titles]
        if unknown:
            messagebox.showerror(
                WINDOW_TITLE,
                "This account cannot see these tickets:\n\n  "
                + "\n  ".join(unknown) + f"\n\nCheck {CONFIG_PATH}",
                parent=self)
            self.destroy()
            return

        self.client = client
        self.account = account
        self.account_id = (profile or {}).get("accountId", "")
        self.catalog = Catalog(self.cfg, titles, self.usage)
        self._rebind_catalog()
        self._update_account_label()
        self._warn_on_timezone(profile)

        self.deiconify()
        self.lift()
        self.refresh()

    def _rebind_catalog(self):
        """Point existing rows at the new catalogue, refreshing their titles.

        A key the new account cannot see would already have aborted the sign-in,
        so set_key only ever refreshes the displayed title here.
        """
        for row in self.rows:
            row.catalog = self.catalog
            row.picker.catalog = self.catalog
            row.picker.set_key(row.picker.key)

    def _warn_on_timezone(self, profile):
        jira_tz = (profile or {}).get("timeZone")
        if jira_tz and jira_tz != self.cfg.tz_name:
            messagebox.showwarning(
                WINDOW_TITLE,
                f"Your Jira profile timezone is {jira_tz}, but tickets.toml "
                f"says {self.cfg.tz_name}. Worklogs may land on a different "
                "day in reports.",
                parent=self)

    # -- logged work -------------------------------------------------------

    def _review_message(self, text, tag="muted"):
        view = self.review_view.text
        view.configure(state="normal")
        view.delete("1.0", "end")
        view.insert("1.0", text, tag)
        view.configure(state="disabled")

    def load_logged_work(self):
        if self.reviewing:
            return
        self.reviewing = True
        self.review_btn.configure(state="disabled")
        self.review_total.configure(text="")
        day = self.review_date.value
        self._review_message(f"Loading {day.isoformat()}…")

        threading.Thread(target=self._review_worker, args=(day,),
                         daemon=True).start()
        self.after(80, self._drain_review)

    def _review_worker(self, day):
        try:
            entries = self.client.fetch_day_worklogs(
                self.account_id, day, self.cfg.tz)
            self._review_queue.put(("ok", day, entries))
        except JiraError as exc:
            self._review_queue.put(("err", day, str(exc)))
        except Exception as exc:
            self._review_queue.put(("err", day, f"Unexpected error: {exc}"))

    def _drain_review(self):
        try:
            kind, day, payload = self._review_queue.get_nowait()
        except queue.Empty:
            self.after(80, self._drain_review)
            return

        self.reviewing = False
        self.review_btn.configure(state="normal")
        if kind == "err":
            self._review_message(f"Could not load {day.isoformat()}.\n\n{payload}",
                                 tag="error")
            return
        self._render_logged_work(day, payload)

    def _render_logged_work(self, day, entries):
        view = self.review_view.text
        view.configure(state="normal")
        view.delete("1.0", "end")

        if not entries:
            view.insert("end", f"Nothing logged on {day.isoformat()} "
                               f"({day.strftime('%A')}).", "muted")
            self.review_total.configure(text="Total: 0m")
            view.configure(state="disabled")
            return

        total = 0
        for entry in entries:
            total += entry["seconds"]
            view.insert("end", entry["key"], "key")
            if entry["summary"]:
                view.insert("end", f"  {entry['summary']}", "summary")
            view.insert("end", "\n")
            view.insert("end", f"  {format_duration(entry['seconds'])}\n", "time")
            comment = entry["comment"] or "(no comment)"
            for line in comment.splitlines() or [""]:
                view.insert("end", f"    {line}\n", "comment")
            view.insert("end", "\n")

        count = len(entries)
        view.insert("end", f"{count} {'entry' if count == 1 else 'entries'} "
                           f"on {day.isoformat()}\n", "muted")
        view.configure(state="disabled")
        self.review_total.configure(text=f"Total: {format_duration(total)}")

    # -- date --------------------------------------------------------------

    @property
    def selected_date(self):
        return self.date_field.value

    def _on_date_changed(self):
        """Typed rows and the sticky note carry over; logged rows do not."""
        self._drop_logged_rows()
        self.normalize_rows()
        self.refresh()
        self.schedule_draft_save()

    def _drop_logged_rows(self):
        """Their work is already in Jira; keeping them under a new date is wrong."""
        for row in list(self.rows):
            if row.state == LOGGED:
                row.destroy()
                self.rows.remove(row)

    def commit_date(self):
        self.date_field.commit()

    def shift_date(self, days):
        self.date_field.shift(days)

    def goto_today(self):
        self.date_field.today()

    # -- clear -------------------------------------------------------------

    def clear_all(self):
        """Rows only. The sticky note and the date are deliberately untouched."""
        has_content = any(not row.is_blank and not row.is_locked for row in self.rows)
        if has_content and not messagebox.askyesno(
                WINDOW_TITLE, "Clear all unsubmitted entries?\n\n"
                              "Your Current work notes are kept.", parent=self):
            return
        for row in self.rows:
            row.destroy()
        self.rows = []
        self.normalize_rows()
        self.refresh()
        self.schedule_draft_save()

    # -- submit ------------------------------------------------------------

    def submit(self):
        if self.submitting:
            return

        partials = [row for row in self.rows if row.state == PARTIAL]
        if partials:
            self.refresh(highlight_gaps=True)
            lines = [f"Row {self.rows.index(r) + 1}: missing {', '.join(r.missing())}"
                     for r in partials]
            messagebox.showwarning(
                WINDOW_TITLE,
                "Nothing was sent.\n\nThese rows are half-filled:\n\n"
                + "\n".join(lines),
                parent=self)
            return

        jobs = [row for row in self.rows if row.is_submittable]
        if not jobs:
            return

        started = build_started(self.selected_date, self.cfg.tz)
        payloads = []
        for row in jobs:
            row.mark_submitting()
            payloads.append((id(row), row.key, row.seconds, started, row.comment_text))

        self.submitting = True
        self.refresh()
        self.hint.configure(text=f"Submitting {len(payloads)}…")

        threading.Thread(target=self._worker, args=(payloads,), daemon=True).start()
        self.after(80, self._drain)

    def _worker(self, payloads):
        for index, (row_id, key, seconds, started, comment) in enumerate(payloads):
            try:
                worklog_id = self.client.add_worklog(key, seconds, started, comment)
                self._queue.put(("ok", row_id, worklog_id, key))
            except JiraError as exc:
                self._queue.put(("err", row_id, str(exc), exc.status))
                if exc.status == 401:
                    # every remaining request would fail the same way
                    self._queue.put(("abort", [p[0] for p in payloads[index + 1:]]))
                    break
            except Exception as exc:  # never let the worker die silently
                self._queue.put(("err", row_id, f"Unexpected error: {exc}", None))
        self._queue.put(("done",))

    def _drain(self):
        keep_going = True
        while True:
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                break
            kind = message[0]
            if kind == "ok":
                _, row_id, worklog_id, key = message
                row = self._row_by_id(row_id)
                if row:
                    row.mark_logged(worklog_id)
                self.usage[key] = datetime.now().isoformat(timespec="seconds")
                write_json(USAGE_PATH, self.usage)
            elif kind == "err":
                _, row_id, text, status = message
                row = self._row_by_id(row_id)
                if row:
                    row.mark_failed(text)
                if status == 401 and not self.dry_run:
                    clear_credentials()
            elif kind == "abort":
                for row_id in message[1]:
                    row = self._row_by_id(row_id)
                    if row:
                        row.mark_failed("Not sent - authentication failed earlier "
                                        "in this batch. Use Sign out to enter a "
                                        "new API token.")
            elif kind == "done":
                keep_going = False

        if keep_going:
            self.refresh()
            self.after(80, self._drain)
            return

        self.submitting = False
        self.hint.configure(text="Ctrl+Enter submits")
        self.normalize_rows()
        self.refresh()
        self.schedule_draft_save()

    def _row_by_id(self, row_id):
        return next((row for row in self.rows if id(row) == row_id), None)

    # -- draft -------------------------------------------------------------

    def schedule_draft_save(self):
        if self._draft_job:
            self.after_cancel(self._draft_job)
        self._draft_job = self.after(DRAFT_DEBOUNCE_MS, self.save_draft)

    def save_draft(self):
        if self._draft_job:
            try:
                self.after_cancel(self._draft_job)
            except Exception:
                pass
        self._draft_job = None
        rows = [row.as_dict() for row in self.rows
                if not row.is_locked and not row.is_blank]
        write_json(DRAFT_PATH, {"date": self.selected_date.isoformat(),
                                "current_work": self.sticky.get(),
                                "rows": rows})

    def _load_draft(self):
        draft = read_json(DRAFT_PATH, {})
        try:
            self.date_field.set(date.fromisoformat(draft.get("date", "")),
                                notify=False)
        except ValueError:
            self.date_field.set(date.today(), notify=False)
        self.sticky.set(draft.get("current_work") or "")
        for data in draft.get("rows", []):
            if isinstance(data, dict):
                self.add_row(data)

    def _on_close(self):
        self.save_draft()
        self.destroy()

    def destroy(self):
        # cancel pending timers so nothing fires against a dead interpreter
        for attr in ("_draft_job", "_resync_job"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)
        for field in ("date_field", "review_date"):
            widget = getattr(self, field, None)
            if widget is not None:
                widget._cancel_note_job()
        super().destroy()


# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------


class CredentialDialog(tk.Toplevel):
    """Modal sign-in. Verifies before closing, so a bad token is reported here.

    `verify` is (email, token) -> (client, profile) and may raise JiraError.
    On success `result` is (client, profile, email, token); None if cancelled.
    """

    def __init__(self, master, base_url, verify, prefill_email=""):
        super().__init__(master, background=theme.CHROME_BG)
        self.title(f"{WINDOW_TITLE} — sign in")
        self.resizable(False, False)
        self.verify = verify
        self.result = None

        wrap = ttk.Frame(self, padding=16)
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, text=f"Connect to {base_url}",
                  foreground=theme.ACCENT,
                  font=("TkDefaultFont", 11, "bold")).grid(
                      row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(wrap, wraplength=430, foreground=FG_MUTED, justify="left",
                  text="Create an API token at id.atlassian.com → Security "
                       "→ API tokens. This is not your account password.\n\n"
                       f"It will be saved to credentials.toml beside the "
                       f"application. That file is personal to you and must "
                       f"never be committed or shared.").grid(
                           row=1, column=0, columnspan=2, sticky="w", pady=(4, 12))

        ttk.Label(wrap, text="Email").grid(row=2, column=0, sticky="w")
        self.email = ttk.Entry(wrap, width=42)
        self.email.grid(row=2, column=1, sticky="ew", pady=3)
        self.email.insert(0, prefill_email)

        ttk.Label(wrap, text="API token").grid(row=3, column=0, sticky="w")
        self.token = ttk.Entry(wrap, width=42, show="•")
        self.token.grid(row=3, column=1, sticky="ew", pady=3)

        self.error = ttk.Label(wrap, text="", foreground=FG_ERR, wraplength=420,
                               justify="left")
        self.error.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(wrap)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right")
        self.ok = ttk.Button(buttons, text="Connect", command=self._ok)
        self.ok.pack(side="right", padx=(0, 8))

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        # A Toplevel made transient to a withdrawn master is itself withdrawn by
        # Tk on Windows, which is how this dialog can exist and never appear.
        # The root is hidden at this point, so skip transient and stand alone.
        if master is not None and master.winfo_viewable():
            self.transient(master)
        self._centre()
        self.deiconify()
        self.lift()
        self.focus_force()
        self.grab_set()
        (self.token if prefill_email else self.email).focus_set()

    def _centre(self):
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        x = (self.winfo_screenwidth() - width) // 2
        y = (self.winfo_screenheight() - height) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def set_busy(self, busy, message=""):
        self.ok.configure(state="disabled" if busy else "normal")
        self.error.configure(text=message,
                             foreground=FG_MUTED if busy else FG_ERR)
        self.update_idletasks()

    def _ok(self):
        email = self.email.get().strip()
        token = self.token.get().strip()
        if not email or not token:
            self.error.configure(text="Both fields are required.", foreground=FG_ERR)
            return

        self.set_busy(True, "Checking…")
        try:
            client, profile = self.verify(email, token)
        except JiraError as exc:
            self.set_busy(False, str(exc))  # stay open so the token can be fixed
            return
        self.result = (client, profile, email, token)
        self.grab_release()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()


def prompt_for_credentials(parent, cfg, message=""):
    """Modal sign-in. Returns (client, profile, email), or Nones if cancelled.

    The token is only written to disk once Jira has accepted it.
    """
    def verify(email, token):
        client = JiraClient(cfg.base_url, email, token)
        return client, client.myself()

    dialog = CredentialDialog(parent, cfg.base_url, verify)
    if message:
        dialog.error.configure(text=message, foreground=FG_ERR)
    parent.wait_window(dialog)

    if dialog.result is None:
        return None, None, None
    client, profile, email, token = dialog.result
    store_credentials(email, token)
    return client, profile, email


def authenticate(parent, cfg, message=""):
    """(client, profile, account) using stored credentials, prompting if needed.

    Returns Nones if the user cancels the dialog. Raises ConfigError for
    failures no dialog can fix, such as the site being unreachable.
    """
    stored = load_credentials()
    if stored:
        client = JiraClient(cfg.base_url, stored.email, stored.token)
        try:
            profile = client.myself()
        except JiraError as exc:
            if exc.status != 401:
                raise ConfigError(f"Could not reach Jira.\n\n{exc}") from exc
            clear_credentials()
            message = ("The saved API token was rejected — tokens expire. "
                       "Please enter a new one.")
        else:
            return client, profile, describe_account(profile, stored.email)

    client, profile, email = prompt_for_credentials(parent, cfg, message)
    if client is None:
        return None, None, None
    return client, profile, describe_account(profile, email)


def describe_account(profile, fallback_email):
    """Jira Cloud hides emailAddress unless the profile is public."""
    return ((profile or {}).get("emailAddress")
            or (profile or {}).get("displayName")
            or fallback_email)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def fatal(root, message):
    messagebox.showerror(WINDOW_TITLE, message, parent=root)
    root.destroy()
    sys.exit(1)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bulklogger", description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="simulate Jira; no network, no keyring")
    parser.add_argument("--reset-credentials", action="store_true",
                        help="forget the stored token and exit")
    args = parser.parse_args(argv)

    if args.reset_credentials:
        clear_credentials()
        print(f"Deleted {CREDENTIALS_PATH}")
        return 0

    root = tk.Tk()
    root.withdraw()
    theme.apply_theme(root)  # so the sign-in dialog matches the main window

    # 1. config
    try:
        cfg = load_config(CONFIG_PATH)
    except ConfigError as exc:
        if args.dry_run and not CONFIG_PATH.exists():
            cfg = sample_config()
        else:
            fatal(root, str(exc))

    # 2-3. credentials, verified
    if args.dry_run:
        client = DryRunClient(cfg.all_keys)
        profile = client.myself()
        account = "dry run"
    else:
        try:
            client, profile, account = authenticate(root, cfg)
        except ConfigError as exc:
            fatal(root, str(exc))
        if client is None:  # cancelled at the sign-in dialog
            root.destroy()
            return 1

    # 4-5. ticket list, titles. No network means no titles means no window.
    try:
        titles = client.fetch_summaries(cfg.all_keys)
    except JiraError as exc:
        fatal(root, f"Could not load ticket titles from Jira.\n\n{exc}")

    unknown = [key for key in cfg.all_keys if key not in titles]
    if unknown:
        fatal(root,
              "These keys in tickets.toml do not exist, or are not visible to "
              "this account:\n\n  " + "\n  ".join(unknown) +
              f"\n\nFix them in:\n{CONFIG_PATH}")

    # 6. timezone mismatch is a warning, not a blocker
    jira_tz = (profile or {}).get("timeZone")
    if jira_tz and jira_tz != cfg.tz_name:
        messagebox.showwarning(
            WINDOW_TITLE,
            f"Your Jira profile timezone is {jira_tz}, but tickets.toml says "
            f"{cfg.tz_name}.\n\nJira buckets worklogs by the profile timezone, so "
            "entries may land on a different day in reports. Logging will "
            "continue.",
            parent=root)

    # 7. draft
    usage = read_json(USAGE_PATH, {})
    if not isinstance(usage, dict):
        usage = {}
    catalog = Catalog(cfg, titles, usage)

    root.destroy()
    app = App(cfg, client, catalog, usage, account=account,
              account_id=(profile or {}).get("accountId", ""),
              dry_run=args.dry_run)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
