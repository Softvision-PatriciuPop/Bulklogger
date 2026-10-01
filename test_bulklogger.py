"""Offline logic tests for Bulklogger. Run: python -m unittest -v test_bulklogger"""

import re
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import bulklogger as bl


class TestParseDuration(unittest.TestCase):
    def test_accepted_forms(self):
        cases = {
            "2h": 7200, "30m": 1800, "2h 30m": 9000, "2h30m": 9000,
            "1.5h": 5400, "90m": 5400, "  45M  ": 2700, "1H 5M": 3900,
            "0.25h": 900, "1,5h": 5400, "120min": 7200, "2hours": 7200,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                seconds, err = bl.parse_duration(text)
                self.assertIsNone(err)
                self.assertEqual(seconds, expected)

    def test_days_and_weeks_rejected(self):
        for text in ("1d", "2 days", "1w", "3weeks"):
            with self.subTest(text=text):
                seconds, err = bl.parse_duration(text)
                self.assertIsNone(seconds)
                self.assertIn("ambiguous", err)

    def test_bare_number_asks_for_a_unit(self):
        seconds, err = bl.parse_duration("2")
        self.assertIsNone(seconds)
        self.assertIn("add a unit", err)

    def test_rejected(self):
        for text in ("", "   ", "abc", "2x", "h", "2h banana", "-3h"):
            with self.subTest(text=text):
                seconds, _err = bl.parse_duration(text)
                self.assertIsNone(seconds)

    def test_zero_rejected(self):
        seconds, err = bl.parse_duration("0h")
        self.assertIsNone(seconds)
        self.assertIn("more than zero", err)


class TestFormatDuration(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(bl.format_duration(0), "0m")
        self.assertEqual(bl.format_duration(1800), "30m")
        self.assertEqual(bl.format_duration(7200), "2h")
        self.assertEqual(bl.format_duration(26100), "7h 15m")


class TestStartedTimestamp(unittest.TestCase):
    """The offset must come from the selected date, not from today."""

    def setUp(self):
        self.tz = bl.ZoneInfo("Europe/Bucharest")

    def test_summer_offset(self):
        self.assertEqual(bl.build_started(date(2026, 8, 7), self.tz),
                         "2026-08-07T11:00:00.000+0300")

    def test_winter_offset(self):
        self.assertEqual(bl.build_started(date(2026, 12, 7), self.tz),
                         "2026-12-07T11:00:00.000+0200")

    def test_no_colon_in_offset(self):
        stamp = bl.build_started(date(2026, 8, 7), self.tz)
        self.assertNotIn(":", stamp[-5:])
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4}$")


class TestConfig(unittest.TestCase):
    def _write(self, text):
        tmp = Path(tempfile.mkdtemp()) / "tickets.toml"
        tmp.write_text(text, encoding="utf-8")
        return tmp

    def test_valid(self):
        cfg = bl.load_config(self._write("""
base_url = "https://example.atlassian.net/"
timezone = "Europe/Bucharest"
retired = ["QATT-40"]
[groups]
"Group A" = ["QATT-85", "QATT-86"]
"""))
        self.assertEqual(cfg.base_url, "https://example.atlassian.net")
        self.assertEqual(cfg.groups["Group A"], ["QATT-85", "QATT-86"])
        self.assertEqual(cfg.retired, ["QATT-40"])
        self.assertIn("QATT-40", cfg.all_keys)

    def test_missing_file(self):
        with self.assertRaises(bl.ConfigError):
            bl.load_config(Path(tempfile.mkdtemp()) / "nope.toml")

    def test_bad_toml(self):
        with self.assertRaises(bl.ConfigError):
            bl.load_config(self._write("base_url = "))

    def test_bad_base_url(self):
        with self.assertRaisesRegex(bl.ConfigError, "base_url"):
            bl.load_config(self._write('base_url = "example.atlassian.net"\n'))

    def test_bad_key(self):
        with self.assertRaisesRegex(bl.ConfigError, "PROJ_9"):
            bl.load_config(self._write("""
base_url = "https://x.atlassian.net"
[groups]
"A" = ["PROJ_9"]
"""))

    def test_bad_timezone(self):
        with self.assertRaisesRegex(bl.ConfigError, "IANA"):
            bl.load_config(self._write("""
base_url = "https://x.atlassian.net"
timezone = "Mars/Olympus"
[groups]
"A" = ["QATT-1"]
"""))

    def test_no_tickets(self):
        with self.assertRaisesRegex(bl.ConfigError, "No ticket keys"):
            bl.load_config(self._write('base_url = "https://x.atlassian.net"\n'))


def make_catalog(usage=None):
    cfg = bl.Config(
        base_url="https://x.atlassian.net", tz_name="Europe/Bucharest",
        tz=bl.ZoneInfo("Europe/Bucharest"),
        groups={"QATT-80 · Test automation": ["QATT-85", "QATT-86"],
                "Developer experience": ["QATT-91"]},
        retired=["QATT-40"],
        all_keys=["QATT-85", "QATT-86", "QATT-91", "QATT-40"])
    titles = {"QATT-85": "Migrate auth service to OIDC",
              "QATT-86": "Flaky test triage",
              "QATT-91": "Improve CI feedback time",
              "QATT-40": "Retired reporting job"}
    return bl.Catalog(cfg, titles, usage or {})


class TestCatalog(unittest.TestCase):
    def items(self, rows):
        return [payload for kind, _text, payload in rows if kind == bl.ITEM]

    def headers(self, rows):
        return [text for kind, text, _p in rows if kind == bl.HEADER]

    def test_unfiltered_shows_active_and_collapses_retired(self):
        rows = make_catalog().build("")
        self.assertEqual(sorted(self.items(rows)), ["QATT-85", "QATT-86", "QATT-91"])
        self.assertTrue(any(kind == bl.TOGGLE for kind, _t, _p in rows))

    def test_match_on_key_fragment(self):
        self.assertEqual(self.items(make_catalog().build("85")), ["QATT-85"])

    def test_match_on_title_substring(self):
        self.assertEqual(self.items(make_catalog().build("oidc")), ["QATT-85"])

    def test_group_match_reveals_children(self):
        rows = make_catalog().build("test automation")
        self.assertEqual(sorted(self.items(rows)), ["QATT-85", "QATT-86"])

    def test_child_match_still_renders_its_header(self):
        rows = make_catalog().build("oidc")
        self.assertEqual(self.headers(rows), ["QATT-80 · Test automation"])

    def test_retired_reachable_when_expanded(self):
        rows = make_catalog().build("", show_retired=True)
        self.assertIn("QATT-40", self.items(rows))

    def test_retired_auto_expands_on_match(self):
        rows = make_catalog().build("reporting")
        self.assertIn("QATT-40", self.items(rows))

    def test_retired_excluded_from_its_group(self):
        rows = make_catalog().build("", show_retired=False)
        self.assertNotIn("QATT-40", self.items(rows))

    def test_recency_ordering(self):
        catalog = make_catalog({"QATT-91": "2026-08-06T10:00:00",
                                "QATT-86": "2026-08-07T10:00:00"})
        rows = catalog.build("")
        # QATT-86 logged most recently, so its group comes first and it leads
        self.assertEqual(self.items(rows)[0], "QATT-86")
        self.assertEqual(self.headers(rows)[0], "QATT-80 · Test automation")

    def test_no_match_note(self):
        rows = make_catalog().build("zzzz")
        self.assertEqual(self.items(rows), [])
        self.assertTrue(any(kind == bl.NOTE for kind, _t, _p in rows))


class TestCredentialsFile(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "credentials.toml"
        self._orig = bl.CREDENTIALS_PATH
        bl.CREDENTIALS_PATH = self.path

    def tearDown(self):
        bl.CREDENTIALS_PATH = self._orig

    def test_absent_file_is_not_an_error(self):
        self.assertIsNone(bl.load_credentials())

    def test_round_trip(self):
        bl.store_credentials("me@mozilla.com", "abc123")
        creds = bl.load_credentials()
        self.assertEqual((creds.email, creds.token), ("me@mozilla.com", "abc123"))

    def test_records_when_the_token_was_saved(self):
        bl.store_credentials("me@mozilla.com", "abc123")
        self.assertEqual(bl.load_credentials().created, date.today().isoformat())

    def test_missing_created_falls_back_to_the_file_timestamp(self):
        """Credentials written before the field existed still report a date."""
        self.path.write_text('email = "me@mozilla.com"\napi_token = "abc"\n',
                             encoding="utf-8")
        self.assertEqual(bl.load_credentials().created, date.today().isoformat())

    def test_unparseable_created_falls_back(self):
        self.path.write_text('email = "m@x.com"\napi_token = "a"\n'
                             'created = "not a date"\n', encoding="utf-8")
        self.assertEqual(bl.load_credentials().created, date.today().isoformat())

    def test_written_file_warns_against_committing(self):
        bl.store_credentials("me@mozilla.com", "abc123")
        self.assertIn("NEVER COMMIT", self.path.read_text(encoding="utf-8"))

    def test_tokens_with_quotes_and_backslashes_survive(self):
        nasty = r'tok"en\with"quotes\\'
        bl.store_credentials("me@mozilla.com", nasty)
        self.assertEqual(bl.load_credentials().token, nasty)

    def test_untouched_placeholder_counts_as_absent(self):
        self.path.write_text('email = "you@mozilla.com"\n'
                             'api_token = "PASTE-YOUR-API-TOKEN-HERE"\n',
                             encoding="utf-8")
        self.assertIsNone(bl.load_credentials())

    def test_missing_token_counts_as_absent(self):
        self.path.write_text('email = "you@mozilla.com"\n', encoding="utf-8")
        self.assertIsNone(bl.load_credentials())

    def test_malformed_file_names_the_path(self):
        self.path.write_text("email = you@mozilla.com\n", encoding="utf-8")
        with self.assertRaises(bl.ConfigError) as caught:
            bl.load_credentials()
        self.assertIn("credentials.toml", str(caught.exception))

    def test_clear_removes_the_file(self):
        bl.store_credentials("me@mozilla.com", "abc123")
        bl.clear_credentials()
        self.assertFalse(self.path.exists())
        self.assertIsNone(bl.load_credentials())

    def test_clear_is_safe_when_already_absent(self):
        bl.clear_credentials()  # must not raise


class TestExampleFilesMatchTheParser(unittest.TestCase):
    """The shipped examples must actually load."""

    def test_credentials_example_parses_as_placeholder(self):
        example = bl.APP_DIR / "credentials.toml.example"
        self.assertTrue(example.exists())
        orig = bl.CREDENTIALS_PATH
        bl.CREDENTIALS_PATH = example
        try:
            self.assertIsNone(bl.load_credentials())  # placeholder, not usable
        finally:
            bl.CREDENTIALS_PATH = orig

    def test_shipped_tickets_toml_loads(self):
        cfg = bl.load_config(bl.APP_DIR / "tickets.toml")
        self.assertEqual(cfg.tz_name, "Europe/Bucharest")
        self.assertIn("QATT-85", cfg.all_keys)


class TestFetchWeekWorklogs(unittest.TestCase):
    """The review tab's query. Network is stubbed at the two seams below."""

    MONDAY = date(2026, 8, 3)

    def setUp(self):
        self.tz = bl.ZoneInfo("Europe/Bucharest")
        self.client = bl.JiraClient("https://x.atlassian.net", "e@x", "tok")
        self.jql = None
        self.window = None
        self.calls = 0

        def search(jql):
            self.jql = jql
            return [{"key": "QATT-85", "fields": {"summary": "Auth work"}}]

        def worklogs(key, start, end):
            self.calls += 1
            self.window = (start, end)
            return [
                {"author": {"accountId": "me"}, "timeSpentSeconds": 7200,
                 "comment": "mine", "started": "2026-08-07T11:00:00.000+0300"},
                {"author": {"accountId": "someone-else"}, "timeSpentSeconds": 3600,
                 "comment": "theirs", "started": "2026-08-07T09:00:00.000+0300"},
            ]

        self.client._search = search
        self.client._issue_worklogs = worklogs

    def run_it(self):
        return self.client.fetch_week_worklogs("me", self.MONDAY, self.tz)

    def test_queries_by_author_and_date_range(self):
        self.run_it()
        self.assertIn("worklogAuthor = currentUser()", self.jql)
        self.assertIn('worklogDate >= "2026-08-03"', self.jql)
        self.assertIn('worklogDate <= "2026-08-09"', self.jql)

    def test_other_peoples_worklogs_are_excluded(self):
        entries = self.run_it()
        self.assertEqual([e["comment"] for e in entries], ["mine"])

    def test_returns_key_summary_time_comment_and_day(self):
        entry = self.run_it()[0]
        self.assertEqual(entry["key"], "QATT-85")
        self.assertEqual(entry["summary"], "Auth work")
        self.assertEqual(entry["seconds"], 7200)
        self.assertEqual(entry["comment"], "mine")
        self.assertEqual(entry["day"], date(2026, 8, 7))

    def test_window_is_the_local_week(self):
        self.run_it()
        start, end = self.window
        self.assertEqual(start.isoformat(), "2026-08-03T00:00:00+03:00")
        self.assertEqual(end.isoformat(), "2026-08-10T00:00:00+03:00")

    def test_one_worklog_request_per_issue_not_per_day(self):
        """A week must not cost seven times a day in round trips."""
        self.run_it()
        self.assertEqual(self.calls, 1)

    def test_not_limited_to_configured_tickets(self):
        """Spotting time logged somewhere unexpected is the point."""
        self.run_it()
        self.assertNotIn("key in", self.jql)


class TestWorklogDay(unittest.TestCase):
    """Which column a worklog lands in, once the offset is resolved."""

    def setUp(self):
        self.tz = bl.ZoneInfo("Europe/Bucharest")  # +03:00 in August

    def test_uses_the_configured_zone_not_the_raw_prefix(self):
        """23:30 UTC on the 6th is already the 7th in Bucharest."""
        day = bl.worklog_day("2026-08-06T23:30:00.000+0000", self.tz)
        self.assertEqual(day, date(2026, 8, 7))

    def test_keeps_a_same_zone_stamp_on_its_own_day(self):
        self.assertEqual(bl.worklog_day("2026-08-07T11:00:00.000+0300", self.tz),
                         date(2026, 8, 7))

    def test_naive_stamps_are_taken_at_face_value(self):
        self.assertEqual(bl.worklog_day("2026-08-07T11:00:00", self.tz),
                         date(2026, 8, 7))

    def test_unparseable_falls_back(self):
        fallback = date(2026, 8, 3)
        self.assertEqual(bl.worklog_day("", self.tz, fallback), fallback)
        self.assertEqual(bl.worklog_day(None, self.tz, fallback), fallback)


class TestWeekColumns(unittest.TestCase):
    MONDAY = date(2026, 8, 3)

    def columns(self, *offsets):
        entries = [{"day": self.MONDAY + timedelta(days=o)} for o in offsets]
        return bl.week_columns(self.MONDAY, entries)

    def test_monday_of_snaps_to_the_start_of_the_week(self):
        for offset in range(7):
            self.assertEqual(bl.monday_of(self.MONDAY + timedelta(days=offset)),
                             self.MONDAY)

    def test_weekdays_always_appear_even_when_empty(self):
        self.assertEqual(self.columns(), [self.MONDAY + timedelta(days=o)
                                          for o in range(5)])

    def test_saturday_appears_only_when_worked(self):
        self.assertEqual(len(self.columns(5)), 6)
        self.assertEqual(self.columns(5)[-1], self.MONDAY + timedelta(days=5))

    def test_sunday_without_saturday_still_appears(self):
        days = self.columns(6)
        self.assertEqual(len(days), 6)
        self.assertEqual(days[-1], self.MONDAY + timedelta(days=6))

    def test_both_weekend_days_keep_their_order(self):
        days = self.columns(6, 5)
        self.assertEqual(days[-2:], [self.MONDAY + timedelta(days=5),
                                     self.MONDAY + timedelta(days=6)])


class TestBuildWeekGrid(unittest.TestCase):
    MONDAY = date(2026, 8, 3)

    def entry(self, key, offset, seconds, comment="", summary="Some work"):
        day = self.MONDAY + timedelta(days=offset)
        return {"key": key, "summary": summary, "seconds": seconds,
                "comment": comment, "day": day,
                "started": f"{day.isoformat()}T11:00:00.000+0300"}

    def test_an_empty_week_still_has_weekday_columns(self):
        grid = bl.build_week_grid(self.MONDAY, [])
        self.assertTrue(grid.is_empty)
        self.assertEqual(len(grid.days), 5)
        self.assertEqual(grid.total, 0)

    def test_one_row_per_issue_with_a_cell_per_worked_day(self):
        grid = bl.build_week_grid(self.MONDAY, [
            self.entry("QATT-85", 0, 7200, "monday"),
            self.entry("QATT-85", 2, 3600, "wednesday"),
            self.entry("QATT-91", 0, 1800, "also monday"),
        ])
        self.assertEqual([r.key for r in grid.rows], ["QATT-85", "QATT-91"])
        row = grid.rows[0]
        self.assertEqual(sorted(row.cells), [self.MONDAY,
                                             self.MONDAY + timedelta(days=2)])
        self.assertEqual(row.cells[self.MONDAY].comment, "monday")

    def test_rows_are_ordered_by_key_not_by_time(self):
        """Chronology is in the columns; a stable order makes weeks comparable."""
        grid = bl.build_week_grid(self.MONDAY, [
            self.entry("QATT-91", 0, 60),
            self.entry("QATT-85", 4, 60),
        ])
        self.assertEqual([r.key for r in grid.rows], ["QATT-85", "QATT-91"])

    def test_same_issue_twice_in_a_day_collapses_into_one_cell(self):
        grid = bl.build_week_grid(self.MONDAY, [
            self.entry("QATT-85", 0, 3600, "morning"),
            self.entry("QATT-85", 0, 1800, "afternoon"),
        ])
        cell = grid.rows[0].cells[self.MONDAY]
        self.assertEqual(cell.seconds, 5400)
        self.assertEqual(cell.comment, "morning\nafternoon")

    def test_blank_comments_are_not_joined_as_empty_lines(self):
        grid = bl.build_week_grid(self.MONDAY, [
            self.entry("QATT-85", 0, 3600, ""),
            self.entry("QATT-85", 0, 1800, "only this one"),
        ])
        self.assertEqual(grid.rows[0].cells[self.MONDAY].comment, "only this one")

    def test_totals_add_up_across_rows_and_columns(self):
        grid = bl.build_week_grid(self.MONDAY, [
            self.entry("QATT-85", 0, 7200),
            self.entry("QATT-85", 1, 3600),
            self.entry("QATT-91", 0, 1800),
        ])
        self.assertEqual(grid.rows[0].total, 10800)
        self.assertEqual(grid.rows[1].total, 1800)
        self.assertEqual(grid.day_totals[self.MONDAY], 9000)
        self.assertEqual(grid.day_totals[self.MONDAY + timedelta(days=1)], 3600)
        self.assertEqual(grid.total, 12600)

    def test_every_column_has_a_total_even_when_nothing_was_logged(self):
        grid = bl.build_week_grid(self.MONDAY, [self.entry("QATT-85", 0, 60)])
        self.assertEqual(sorted(grid.day_totals), grid.days)
        self.assertEqual(grid.day_totals[self.MONDAY + timedelta(days=3)], 0)

    def test_entries_outside_the_week_are_dropped(self):
        """A column it cannot be filed under would silently skew the total."""
        stray = self.entry("QATT-85", 0, 7200)
        stray["day"] = self.MONDAY - timedelta(days=1)
        grid = bl.build_week_grid(self.MONDAY, [stray])
        self.assertTrue(grid.is_empty)
        self.assertEqual(grid.total, 0)

    def test_a_weekend_entry_brings_its_column_with_it(self):
        grid = bl.build_week_grid(self.MONDAY,
                                  [self.entry("QATT-85", 5, 3600, "saturday")])
        self.assertEqual(len(grid.days), 6)
        self.assertEqual(grid.total, 3600)


class TestClampText(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(bl.clamp_text("brief", 20), "brief")

    def test_newlines_become_spaces(self):
        self.assertEqual(bl.clamp_text("two\nlines", 20), "two lines")

    def test_long_text_is_ellipsised_within_the_budget(self):
        clamped = bl.clamp_text("x" * 40, 10)
        self.assertEqual(len(clamped), 10)
        self.assertTrue(clamped.endswith("…"))


class TestTokenAge(unittest.TestCase):
    """The header tells you when the token was added, and nags near expiry."""

    def describe(self, days_old):
        created = (date.today() - timedelta(days=days_old)).isoformat()
        return bl.describe_token_age(created)

    def test_fresh_token_shows_the_date_plainly(self):
        text, severity = self.describe(3)
        self.assertTrue(text.startswith("token added "))
        self.assertNotIn("expire", text)
        self.assertEqual(severity, bl.TOKEN_OK)

    def test_old_token_warns_about_expiry(self):
        text, severity = self.describe(bl.TOKEN_STALE_DAYS + 5)
        self.assertIn("expires", text)
        self.assertEqual(severity, bl.TOKEN_STALE)

    def test_boundary_is_inclusive(self):
        self.assertEqual(self.describe(bl.TOKEN_STALE_DAYS)[1], bl.TOKEN_STALE)
        self.assertEqual(self.describe(bl.TOKEN_STALE_DAYS - 1)[1], bl.TOKEN_OK)

    def test_unknown_date_shows_nothing(self):
        self.assertEqual(bl.describe_token_age(""), ("", bl.TOKEN_OK))
        self.assertEqual(bl.describe_token_age("garbage"), ("", bl.TOKEN_OK))

    def test_every_severity_has_a_colour(self):
        for severity in (bl.TOKEN_OK, bl.TOKEN_STALE, bl.TOKEN_EXPIRED):
            self.assertIn(severity, bl.TOKEN_COLOURS)


class TestTokenExpiry(unittest.TestCase):
    """A token lives a calendar year; the warning lands on its birthday."""

    def test_expiry_is_the_anniversary(self):
        self.assertEqual(bl.token_expiry("2025-10-01"), date(2026, 10, 1))

    def test_leap_day_errs_a_day_early(self):
        """1 March would be a day late, and late is the failure mode."""
        self.assertEqual(bl.token_expiry("2024-02-29"), date(2025, 2, 28))

    def test_unknown_dates_have_no_expiry(self):
        for value in ("", "garbage", None, "2025-13-01"):
            self.assertIsNone(bl.token_expiry(value))

    def test_expired_exactly_on_the_day(self):
        self.assertEqual(bl.token_severity("2025-10-01", date(2026, 10, 1)),
                         bl.TOKEN_EXPIRED)

    def test_not_expired_the_day_before(self):
        self.assertEqual(bl.token_severity("2025-10-01", date(2026, 9, 30)),
                         bl.TOKEN_STALE)

    def test_still_expired_long_after(self):
        self.assertEqual(bl.token_severity("2020-01-01", date(2026, 10, 1)),
                         bl.TOKEN_EXPIRED)

    def test_header_text_names_the_expiry_day(self):
        text, severity = bl.describe_token_age("2025-10-01", date(2026, 10, 1))
        self.assertEqual(severity, bl.TOKEN_EXPIRED)
        self.assertIn("2026-10-01", text)
        self.assertIn("expired", text)

    def test_warning_appears_on_the_expiry_day(self):
        message = bl.token_expiry_warning("2025-10-01", date(2026, 10, 1))
        self.assertIn("2026-10-01", message)
        self.assertIn("Sign out", message)

    def test_no_warning_before_expiry(self):
        self.assertEqual(bl.token_expiry_warning("2025-10-01",
                                                 date(2026, 9, 30)), "")

    def test_no_warning_without_a_date(self):
        self.assertEqual(bl.token_expiry_warning(""), "")


class TestWheelNormalisation(unittest.TestCase):
    """Windows sends multiples of 120, macOS small integers, X11 Button-4/5."""

    class Event:
        def __init__(self, delta=0, num=None):
            self.delta = delta
            if num is not None:
                self.num = num

    def steps(self, event, platform):
        original = bl.IS_MACOS
        bl.IS_MACOS = platform == "darwin"
        try:
            return bl.wheel_steps(event)
        finally:
            bl.IS_MACOS = original

    def test_windows_notch(self):
        self.assertEqual(self.steps(self.Event(delta=-120), "win32"), 1)
        self.assertEqual(self.steps(self.Event(delta=120), "win32"), -1)

    def test_macos_notch_is_not_rounded_away(self):
        """delta/120 on macOS floors every real scroll to zero."""
        self.assertEqual(self.steps(self.Event(delta=-1), "darwin"), 1)
        self.assertEqual(self.steps(self.Event(delta=3), "darwin"), -3)
        self.assertEqual(int(-1 / 120), 0)  # what the old code would have done

    def test_x11_buttons(self):
        self.assertEqual(self.steps(self.Event(num=4), "linux"), -1)
        self.assertEqual(self.steps(self.Event(num=5), "linux"), 1)

    def test_button_wins_over_a_missing_delta(self):
        self.assertEqual(self.steps(self.Event(delta=0, num=5), "darwin"), 1)


class TestAppDirResolution(unittest.TestCase):
    """Config sits beside the executable - and beside the .app, not inside it."""

    def resolve(self, executable, frozen=True, platform="darwin"):
        original = (sys.executable, getattr(sys, "frozen", None))
        sys.executable = executable
        if frozen:
            sys.frozen = True
        try:
            return bl._app_dir()
        finally:
            sys.executable = original[0]
            if original[1] is None:
                if hasattr(sys, "frozen"):
                    del sys.frozen
            else:
                sys.frozen = original[1]

    # resolve() attaches the current drive on Windows, so compare like for like
    def test_macos_bundle_resolves_outside_the_app(self):
        got = self.resolve("/Users/me/Apps/Bulklogger.app/Contents/MacOS/Bulklogger")
        self.assertEqual(got, Path("/Users/me/Apps").resolve())

    def test_plain_binary_resolves_to_its_own_folder(self):
        got = self.resolve("/Users/me/Apps/Bulklogger")
        self.assertEqual(got, Path("/Users/me/Apps").resolve())

    def test_unfrozen_uses_the_source_directory(self):
        self.assertEqual(self.resolve("/anything", frozen=False),
                         Path(bl.__file__).resolve().parent)


class TestBrandingAndAssets(unittest.TestCase):
    def test_window_title_is_jira_bulklogger(self):
        self.assertEqual(bl.WINDOW_TITLE, "JIRA Bulklogger")

    def test_product_name_stays_bulklogger(self):
        self.assertEqual(bl.APP_NAME, "Bulklogger")

    def test_every_platform_icon_ships(self):
        for name in (bl.ICON_ICO, bl.ICON_PNG, "bulklogger.icns"):
            with self.subTest(icon=name):
                self.assertTrue((bl.APP_DIR / name).exists(),
                                f"{name} is missing; run make_icons.py")

    def test_ico_is_a_real_multi_size_ico(self):
        data = (bl.APP_DIR / bl.ICON_ICO).read_bytes()
        self.assertEqual(data[:4], b"\x00\x00\x01\x00", "not an ICO header")
        count = int.from_bytes(data[4:6], "little")
        self.assertGreaterEqual(count, 4, "should carry several icon sizes")

    def test_icns_is_a_real_icns(self):
        data = (bl.APP_DIR / "bulklogger.icns").read_bytes()
        self.assertEqual(data[:4], b"icns", "not an ICNS header")

    def test_png_is_a_real_png(self):
        data = (bl.APP_DIR / bl.ICON_PNG).read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n", "not a PNG header")


class TestGitignore(unittest.TestCase):
    """Nothing personal may ever be committable. A real token already leaked
    into a hand-made distribution folder once; these are the patterns that
    would have stopped it reaching a repository."""

    @classmethod
    def setUpClass(cls):
        cls.rules = (bl.APP_DIR / ".gitignore").read_text(encoding="utf-8")

    def assert_ignored(self, pattern):
        self.assertIn(pattern, self.rules, f"{pattern!r} is not gitignored")

    def test_secrets_and_personal_state(self):
        for pattern in ("credentials.toml", "**/credentials.toml",
                        "draft.json", "**/draft.json",
                        "usage.json", "**/usage.json"):
            self.assert_ignored(pattern)

    def test_hand_made_distribution_archives(self):
        """Copies of dist/ made to hand out can contain a credentials.toml."""
        for pattern in ("*.zip", "*.rar", "*.7z", "JBulklogger*/"):
            self.assert_ignored(pattern)

    def test_build_output(self):
        for pattern in ("build/", "dist/", "*.spec"):
            self.assert_ignored(pattern)

    def test_committed_files_are_not_ignored(self):
        """The things a fresh clone genuinely needs."""
        needed = ["bulklogger.py", "theme.py", "tickets.toml",
                  "credentials.toml.example", "build.ps1", "build.sh",
                  "make_icons.py", "requirements.txt", "bulklogger.ico",
                  "bulklogger.icns", "bulklogger.png"]
        for name in needed:
            with self.subTest(name=name):
                self.assertTrue((bl.APP_DIR / name).exists(), f"{name} missing")
                # a bare filename on its own line would exclude it
                self.assertNotIn(f"\n{name}\n", self.rules)


class TestReleaseWorkflow(unittest.TestCase):
    def setUp(self):
        path = bl.APP_DIR / ".github" / "workflows" / "build.yml"
        self.assertTrue(path.exists(), "release workflow is missing")
        self.body = path.read_text(encoding="utf-8")

    def test_builds_on_both_platforms(self):
        self.assertIn("windows-latest", self.body)
        self.assertIn("macos-latest", self.body)

    def test_runs_only_when_triggered_by_hand(self):
        triggers = self.body.split("jobs:")[0]
        self.assertIn("workflow_dispatch", triggers)
        for automatic in ("on:\n  push", "pull_request", "schedule"):
            self.assertNotIn(automatic, triggers)

    def test_packaging_takes_repo_files_from_the_root_not_dist(self):
        """Regression: dist/ holds build output only, so dist/tickets.toml
        does not exist on a fresh checkout and packaging died on it."""
        for wrong in ("dist/tickets.toml", r"dist\tickets.toml",
                      "dist/credentials.toml.example",
                      r"dist\credentials.toml.example"):
            self.assertNotIn(wrong, self.body, f"packaging still reads {wrong}")

    def test_only_the_executable_comes_out_of_dist(self):
        # skip comment lines: prose like "comes out of dist/." is not a path
        commands = "\n".join(line for line in self.body.splitlines()
                             if not line.strip().startswith("#"))
        from_dist = re.findall(r"dist[/\\][^\s,\"']+", commands)
        self.assertTrue(from_dist, "packaging should copy the built binary")
        for path in from_dist:
            # The bundle's own Mach-O counts: packaging reads it to check the
            # build is for the architecture its runner claims. Still rejects
            # repo files such as dist/tickets.toml, which is the actual guard.
            self.assertRegex(
                path,
                r"Bulklogger\.exe$|Bulklogger\.app(/Contents/MacOS/Bulklogger)?$",
                f"{path} is not build output")

    def test_can_write_releases(self):
        self.assertIn("contents: write", self.body)
        self.assertIn("gh release create", self.body)

    def test_macos_uses_ditto_not_zip(self):
        """A plain zip breaks the bundle's symlinks and Gatekeeper rejects it."""
        self.assertIn("ditto -c -k", self.body)

    def test_packaging_only_copies_the_example_credentials(self):
        """Every copy command touching credentials.toml must mean .example."""
        checked = 0
        for line in self.body.splitlines():
            line = line.strip()
            if not (line.startswith("cp ") or line.startswith("Copy-Item ")):
                continue
            if "credentials.toml" not in line:
                continue
            checked += 1
            self.assertIn("credentials.toml.example", line,
                          f"packaging step would ship a real token: {line}")
        self.assertEqual(checked, 2, "expected one copy step per platform")


class TestCrossPlatformBuild(unittest.TestCase):
    """The macOS build cannot be produced here, so pin what can be checked."""

    def test_build_scripts_exist_for_both_platforms(self):
        self.assertTrue((bl.APP_DIR / "build.ps1").exists())
        self.assertTrue((bl.APP_DIR / "build.sh").exists())

    def test_shell_script_uses_lf_endings(self):
        """CRLF makes bash fail with a bare '\\r: command not found'."""
        self.assertNotIn(b"\r\n", (bl.APP_DIR / "build.sh").read_bytes())

    def test_data_separators_match_their_platform(self):
        # PyInstaller wants ';' on Windows and ':' elsewhere
        self.assertIn('"bulklogger.ico;."',
                      (bl.APP_DIR / "build.ps1").read_text(encoding="utf-8"))
        self.assertIn('"bulklogger.png:."',
                      (bl.APP_DIR / "build.sh").read_text(encoding="utf-8"))

    def test_both_scripts_strip_personal_files_from_dist(self):
        for script in ("build.ps1", "build.sh"):
            body = (bl.APP_DIR / script).read_text(encoding="utf-8")
            with self.subTest(script=script):
                self.assertIn("credentials.toml", body)
                self.assertIn("draft.json", body)
                self.assertIn("usage.json", body)


class TestJiraErrorMessages(unittest.TestCase):
    def test_403_mentions_permission(self):
        msg = bl.JiraClient._describe(403, {})
        self.assertIn("Work On Issues", msg)

    def test_401_mentions_token(self):
        self.assertIn("token", bl.JiraClient._describe(401, {}))

    def test_detail_from_body(self):
        msg = bl.JiraClient._describe(400, {"errorMessages": ["Bad started format"]})
        self.assertIn("Bad started format", msg)


class TestDryRunClient(unittest.TestCase):
    def test_fails_on_fail_comment(self):
        client = bl.DryRunClient(["QATT-85"])
        with self.assertRaises(bl.JiraError):
            client.add_worklog("QATT-85", 3600, "2026-08-07T11:00:00.000+0300",
                               "this should fail")

    def test_succeeds_otherwise(self):
        client = bl.DryRunClient(["QATT-85"])
        worklog_id = client.add_worklog("QATT-85", 3600,
                                        "2026-08-07T11:00:00.000+0300", "fine")
        self.assertTrue(worklog_id)


class TestJsonRoundTrip(unittest.TestCase):
    def test_write_then_read(self):
        path = Path(tempfile.mkdtemp()) / "draft.json"
        payload = {"date": "2026-08-07",
                   "rows": [{"key": "QATT-85", "comment": "a\nb", "time": "2h"}]}
        bl.write_json(path, payload)
        self.assertEqual(bl.read_json(path, {}), payload)

    def test_read_missing_returns_default(self):
        path = Path(tempfile.mkdtemp()) / "absent.json"
        self.assertEqual(bl.read_json(path, {"x": 1}), {"x": 1})

    def test_read_corrupt_returns_default(self):
        path = Path(tempfile.mkdtemp()) / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(bl.read_json(path, {}), {})


if __name__ == "__main__":
    unittest.main()
