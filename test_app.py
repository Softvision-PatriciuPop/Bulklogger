"""
Acceptance tests for Bulklogger's window, driven against the dry-run client.

These build a real Tk window (withdrawn) and exercise the row state machine,
submit flow and persistence. Run: python -m unittest -v test_app
"""

import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

import bulklogger as bl


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _inside(widget, container_path):
    """Tk paths nest by dots, and ".!frame" is a prefix of ".!frame2"."""
    return str(widget).startswith(f"{container_path}.")


def pump(app, seconds=0.05):
    """Let Tk process pending events, including due after() callbacks."""
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.005)
    app.update()


def wait_for_submit(app, timeout=30):
    end = time.time() + timeout
    while app.submitting and time.time() < end:
        app.update()
        time.sleep(0.01)
    app.update()
    if app.submitting:
        raise AssertionError("submit did not finish within the timeout")


class AppTestCase(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.draft_path = tmp / "draft.json"
        self.usage_path = tmp / "usage.json"
        self._orig = (bl.DRAFT_PATH, bl.USAGE_PATH)
        bl.DRAFT_PATH, bl.USAGE_PATH = self.draft_path, self.usage_path

        self.prompts = []
        self.warnings = []
        self._orig_ask = bl.messagebox.askyesno
        self._orig_warn = bl.messagebox.showwarning
        bl.messagebox.askyesno = self._fake_ask
        bl.messagebox.showwarning = self._fake_warn
        self.answer_yes = True

        self.app = self.make_app()

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        bl.DRAFT_PATH, bl.USAGE_PATH = self._orig
        bl.messagebox.askyesno = self._orig_ask
        bl.messagebox.showwarning = self._orig_warn

    def _fake_ask(self, *args, **kwargs):
        self.prompts.append(args)
        return self.answer_yes

    def _fake_warn(self, *args, **kwargs):
        self.warnings.append(args)

    def make_app(self, dry_run=True):
        cfg = bl.sample_config()
        client = bl.DryRunClient(cfg.all_keys)
        titles = client.fetch_summaries(cfg.all_keys)
        usage = bl.read_json(bl.USAGE_PATH, {})
        catalog = bl.Catalog(cfg, titles, usage)
        app = bl.App(cfg, client, catalog, usage,
                     account="me@mozilla.com", dry_run=dry_run)
        app.withdraw()
        app.update()
        return app

    # -- helpers -----------------------------------------------------------

    def fill(self, index, key=None, comment=None, time_text=None):
        row = self.app.rows[index]
        if key is not None:
            row.picker.set_key(key)
        if comment is not None:
            row.comment.delete("1.0", "end")
            row.comment.insert("1.0", comment)
        if time_text is not None:
            row.time_var.set(time_text)
        self.app.on_row_change()
        self.app.update()
        return row

    def states(self):
        return [row.state for row in self.app.rows]

    def button(self):
        return self.app.submit_btn.cget("text")


class TestRowLifecycle(AppTestCase):
    def test_opens_with_four_rows(self):
        self.assertEqual(len(self.app.rows), bl.MIN_ROWS)
        self.assertTrue(all(row.is_blank for row in self.app.rows))

    def test_filling_last_row_appends_exactly_one_blank(self):
        self.fill(3, "QATT-85", "did a thing", "2h")
        self.assertEqual(len(self.app.rows), 5)
        self.assertTrue(self.app.rows[4].is_blank)

    def test_surplus_blanks_collapse_back_to_one(self):
        self.fill(3, "QATT-85", "did a thing", "2h")
        self.fill(4, "QATT-86", "another thing", "1h")
        self.assertEqual(len(self.app.rows), 6)
        # empty row 5 back out again
        self.fill(4, comment="", time_text="")
        self.app.rows[4].picker.clear()
        self.app.on_row_change()
        self.app.update()
        self.assertEqual(len(self.app.rows), 5)
        self.assertTrue(self.app.rows[-1].is_blank)

    def test_never_shrinks_below_four(self):
        self.fill(0, "QATT-85", "x", "1h")
        self.fill(0, comment="", time_text="")
        self.app.rows[0].picker.clear()
        self.app.on_row_change()
        self.app.update()
        self.assertEqual(len(self.app.rows), bl.MIN_ROWS)

    def test_no_add_entry_button(self):
        """Auto-append covers it; the button was dead weight."""
        self.assertFalse(hasattr(self.app, "add_row_manual"))
        labels = [w.cget("text") for w in _walk(self.app)
                  if isinstance(w, bl.ttk.Button)]
        self.assertNotIn("+ Add entry", labels)

    def test_a_blank_row_is_always_available_to_type_into(self):
        for index in range(6):
            self.fill(index, "QATT-85", f"entry {index}", "30m")
            self.assertTrue(self.app.rows[-1].is_blank)

    def test_remove_button_drops_the_row(self):
        self.fill(0, "QATT-85", "x", "1h")
        self.fill(1, "QATT-86", "y", "2h")
        self.app.remove_row(self.app.rows[0])
        self.app.update()
        self.assertEqual(self.app.rows[0].key, "QATT-86")


class TestValidation(AppTestCase):
    def test_ticket_and_time_without_comment_blocks_submit(self):
        self.fill(0, "QATT-85", "", "2h")
        self.assertEqual(self.app.rows[0].state, bl.PARTIAL)
        self.app.submit()
        self.app.update()
        self.assertEqual(len(self.warnings), 1)
        self.assertEqual(self.app.rows[0].state, bl.PARTIAL)

    def test_comment_only_blocks_submit(self):
        self.fill(0, comment="just a note")
        self.assertEqual(self.app.rows[0].state, bl.PARTIAL)
        self.app.submit()
        self.app.update()
        self.assertEqual(len(self.warnings), 1)

    def test_partial_row_blocks_the_whole_batch(self):
        self.fill(0, "QATT-85", "good row", "2h")
        self.fill(1, "QATT-86", "", "1h")  # partial
        self.app.submit()
        wait_for_submit(self.app)
        self.assertEqual(self.app.rows[0].state, bl.READY)  # nothing was sent
        self.assertEqual(len(self.warnings), 1)

    def test_blank_rows_are_silently_skipped(self):
        self.fill(0, "QATT-85", "real work", "2h")
        self.assertEqual(self.button(), "Submit 1 entry")
        self.assertEqual(self.app.total_label.cget("text"), "Total: 2h")
        self.app.submit()
        wait_for_submit(self.app)
        self.assertEqual(self.app.rows[0].state, bl.LOGGED)
        self.assertEqual(len(self.warnings), 0)

    def test_unparseable_time_is_partial(self):
        self.fill(0, "QATT-85", "work", "1d")
        self.assertEqual(self.app.rows[0].state, bl.PARTIAL)
        self.assertIn("ambiguous", " ".join(self.app.rows[0].missing()))

    def test_running_total(self):
        self.fill(0, "QATT-85", "a", "2h")
        self.fill(1, "QATT-86", "b", "45m")
        self.fill(2, "QATT-87", "c", "4h30m")
        self.assertEqual(self.app.total_label.cget("text"), "Total: 7h 15m")

    def test_button_pluralisation(self):
        self.assertEqual(self.button(), "Submit")
        self.fill(0, "QATT-85", "a", "2h")
        self.assertEqual(self.button(), "Submit 1 entry")
        self.fill(1, "QATT-86", "b", "1h")
        self.assertEqual(self.button(), "Submit 2 entries")


class TestSubmitFlow(AppTestCase):
    def test_partial_failure_leaves_three_logged_and_one_editable(self):
        self.fill(0, "QATT-85", "first", "1h")
        self.fill(1, "QATT-86", "this one will fail", "2h")
        self.fill(2, "QATT-87", "third", "30m")
        self.fill(3, "QATT-91", "fourth", "45m")
        self.assertEqual(self.button(), "Submit 4 entries")

        self.app.submit()
        wait_for_submit(self.app)

        logged = [r for r in self.app.rows if r.state == bl.LOGGED]
        failed = [r for r in self.app.rows if r.state == bl.FAILED]
        self.assertEqual(len(logged), 3)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].key, "QATT-86")
        self.assertFalse(failed[0].is_locked)
        self.assertIn("Work On Issues", failed[0].error)
        self.assertEqual(self.button(), "Submit 1 entry")

    def test_resubmitting_after_partial_failure_creates_no_duplicates(self):
        self.fill(0, "QATT-85", "first", "1h")
        self.fill(1, "QATT-86", "this one will fail", "2h")
        self.app.submit()
        wait_for_submit(self.app)

        first_id = self.app.rows[0].worklog_id
        self.assertEqual(self.button(), "Submit 1 entry")

        # fix the failing row and resubmit
        self.fill(1, comment="corrected")
        self.app.submit()
        wait_for_submit(self.app)

        self.assertEqual(self.app.rows[0].worklog_id, first_id)  # not re-sent
        self.assertEqual(self.app.rows[1].state, bl.LOGGED)
        self.assertEqual(self.button(), "Submit")

    def test_logged_rows_collapse_and_lock(self):
        self.fill(0, "QATT-85", "some work\nsecond line", "90m")
        self.app.submit()
        wait_for_submit(self.app)
        row = self.app.rows[0]
        self.assertTrue(row.is_locked)
        self.assertIn("QATT-85", row.summary.cget("text"))
        self.assertIn("1h 30m", row.summary.cget("text"))
        self.assertFalse(row.comment.winfo_ismapped())

    def test_success_records_usage_for_picker_ordering(self):
        self.fill(0, "QATT-91", "work", "1h")
        self.app.submit()
        wait_for_submit(self.app)
        self.assertIn("QATT-91", bl.read_json(self.usage_path, {}))


class TestDateHandling(AppTestCase):
    def test_defaults_to_today(self):
        self.assertEqual(self.app.selected_date, date.today())

    def test_arrows_step_a_day(self):
        self.app.shift_date(-1)
        self.assertEqual(self.app.selected_date, date.today() - timedelta(days=1))
        self.app.goto_today()
        self.assertEqual(self.app.selected_date, date.today())

    def test_typed_date_is_accepted(self):
        self.app.date_var.set("2026-12-24")
        self.app.commit_date()
        self.assertEqual(self.app.selected_date, date(2026, 12, 24))

    def test_bad_date_reverts(self):
        self.app.date_var.set("not a date")
        self.app.commit_date()
        self.assertEqual(self.app.selected_date, date.today())
        self.assertEqual(self.app.date_var.get(), date.today().isoformat())

    def test_change_preserves_typed_rows_and_drops_logged_ones(self):
        self.fill(0, "QATT-85", "already logged", "1h")
        self.app.submit()
        wait_for_submit(self.app)
        self.fill(1, "QATT-86", "still typing", "2h")
        self.fill(2, "QATT-87", "half done", None)

        self.app.shift_date(-1)
        self.app.update()

        keys = [row.key for row in self.app.rows if row.key]
        self.assertNotIn("QATT-85", keys)          # logged row removed
        self.assertIn("QATT-86", keys)             # ready row kept
        self.assertIn("QATT-87", keys)             # partial row kept
        self.assertEqual(
            self.app.rows[0].comment.get("1.0", "end-1c"), "still typing")

    def test_started_uses_the_selected_dates_offset(self):
        tz = self.app.cfg.tz
        self.assertTrue(bl.build_started(date(2026, 8, 7), tz).endswith("+0300"))
        self.assertTrue(bl.build_started(date(2026, 12, 7), tz).endswith("+0200"))


class TestClear(AppTestCase):
    def test_clears_silently_when_empty(self):
        self.app.clear_all()
        self.app.update()
        self.assertEqual(self.prompts, [])
        self.assertEqual(len(self.app.rows), bl.MIN_ROWS)

    def test_prompts_when_rows_hold_content(self):
        self.fill(0, "QATT-85", "work", "1h")
        self.app.clear_all()
        self.app.update()
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(len(self.app.rows), bl.MIN_ROWS)
        self.assertTrue(all(row.is_blank for row in self.app.rows))

    def test_declining_the_prompt_keeps_everything(self):
        self.fill(0, "QATT-85", "work", "1h")
        self.answer_yes = False
        self.app.clear_all()
        self.app.update()
        self.assertEqual(self.app.rows[0].key, "QATT-85")

    def test_date_is_left_alone(self):
        self.app.shift_date(-3)
        target = self.app.selected_date
        self.app.clear_all()
        self.assertEqual(self.app.selected_date, target)


class TestDraftPersistence(AppTestCase):
    def test_draft_survives_a_restart(self):
        self.app.shift_date(-2)
        self.fill(0, "QATT-85", "line one\nline two", "2h")
        self.fill(1, "QATT-86", "half typed", None)
        self.app.save_draft()

        expected_date = self.app.selected_date
        self.app.destroy()
        self.app = self.make_app()

        self.assertEqual(self.app.selected_date, expected_date)
        self.assertEqual(self.app.rows[0].key, "QATT-85")
        self.assertEqual(self.app.rows[0].comment.get("1.0", "end-1c"),
                         "line one\nline two")
        self.assertEqual(self.app.rows[0].time_text, "2h")
        self.assertEqual(self.app.rows[1].key, "QATT-86")
        self.assertEqual(self.app.rows[1].state, bl.PARTIAL)

    def test_logged_rows_are_not_persisted(self):
        self.fill(0, "QATT-85", "committed work", "1h")
        self.app.submit()
        wait_for_submit(self.app)
        self.app.save_draft()
        self.assertEqual(bl.read_json(self.draft_path, {})["rows"], [])

    def test_debounced_save_happens_without_an_explicit_call(self):
        self.fill(0, "QATT-85", "typed and left alone", "3h")
        pump(self.app, seconds=(bl.DRAFT_DEBOUNCE_MS / 1000) + 0.4)
        rows = bl.read_json(self.draft_path, {}).get("rows", [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "QATT-85")


class TestRowsScrolling(AppTestCase):
    """Regression: the rows could be scrolled downwards off the top of the list.

    Everything here asserts on canvasy(0) — the canvas coordinate showing at
    the top of the widget — and NOT on yview(). When the scrollregion is
    smaller than the widget, Tk lets the canvas scroll above the content while
    yview() keeps reporting (0.0, 1.0), so a yview-based assertion passes right
    through the bug.

    A real viewport is needed, so the window is mapped for the duration.
    """

    def setUp(self):
        super().setUp()
        self.app.deiconify()
        self.app.geometry("1020x700")
        self.app.update()

    def tearDown(self):
        try:
            self.app.withdraw()
        except Exception:
            pass
        super().tearDown()

    def fill_past_the_bottom(self, count=12):
        for index in range(count):
            self.fill(index, "QATT-85", f"entry {index}", "1h")
        self.app.update()

    def wheel(self, times, delta):
        for _ in range(times):
            self.app._on_wheel(_Wheel(delta))
        self.app.update()

    def origin(self):
        """Canvas y showing at the top of the widget. 0 means anchored."""
        return self.app.canvas.canvasy(0)

    def lowest_origin(self):
        bbox = self.app.canvas.bbox("all")
        return max(bbox[3] - self.app.canvas.winfo_height(), 0)

    def assert_anchored(self, why):
        self.assertAlmostEqual(self.origin(), 0.0, delta=0.5, msg=why)

    def assert_within_content(self):
        self.assertGreaterEqual(self.origin(), -0.5, "content pushed below the top")
        self.assertLessEqual(self.origin(), self.lowest_origin() + 0.5,
                             "scrolled past the end of the content")

    def test_fresh_app_cannot_be_scrolled_above_the_first_row(self):
        self.assertEqual(self.lowest_origin(), 0, "expected a short list here")
        self.wheel(8, 120)
        self.assert_anchored("wheeling up on a short list pushed the rows down")

    def test_short_list_ignores_the_wheel_in_both_directions(self):
        self.wheel(8, -120)
        self.assert_anchored("wheeling down on a short list moved the rows")
        self.wheel(8, 120)
        self.assert_anchored("wheeling up on a short list moved the rows")

    def test_long_list_scrolls_and_comes_back_to_the_top(self):
        self.fill_past_the_bottom()
        self.wheel(40, -120)
        self.assertGreater(self.origin(), 0.0)
        self.assert_within_content()
        self.wheel(80, 120)
        self.assert_anchored("could not get back to the first row")

    def test_long_list_cannot_be_scrolled_above_the_first_row(self):
        self.fill_past_the_bottom()
        self.wheel(80, 120)
        self.assert_anchored("scrolled above the first row")

    def test_clear_returns_the_list_to_the_top(self):
        self.fill_past_the_bottom()
        self.wheel(40, -120)
        self.assertGreater(self.origin(), 0.0)

        self.app.clear_all()
        self.app.update()
        self.assert_anchored("blank space left above the first row after Clear")

    def test_removing_rows_keeps_the_view_inside_the_content(self):
        self.fill_past_the_bottom()
        self.wheel(40, -120)
        for row in list(self.app.rows[1:9]):
            self.app.remove_row(row)
        self.app.update()
        self.assert_within_content()

    def test_submitting_collapses_rows_without_stranding_the_view(self):
        self.fill_past_the_bottom(10)
        self.wheel(40, -120)
        self.app.submit()
        wait_for_submit(self.app)
        self.app.update()
        self.assert_within_content()

    def test_scrollregion_is_never_smaller_than_the_viewport(self):
        """The root cause: a short region lets Tk scroll outside it silently."""
        region = [float(n) for n in self.app.canvas.cget("scrollregion").split()]
        self.assertGreaterEqual(region[3], self.app.canvas.winfo_height())


class TestStickyNote(AppTestCase):
    """Current work is a scratchpad: it outlives Clear, date changes and restarts."""

    def note(self):
        return self.app.sticky.get()

    def write(self, text):
        self.app.sticky.set(text)
        self.app.sticky._modified()
        self.app.update()

    def test_survives_clear(self):
        self.write("chasing the flaky login test")
        self.fill(0, "QATT-85", "work", "1h")
        self.app.clear_all()
        self.app.update()
        self.assertEqual(self.note(), "chasing the flaky login test")
        self.assertTrue(all(r.is_blank for r in self.app.rows))

    def test_survives_a_date_change(self):
        self.write("still on the OIDC migration")
        self.app.shift_date(-1)
        self.app.update()
        self.assertEqual(self.note(), "still on the OIDC migration")

    def test_survives_a_restart(self):
        self.write("tomorrow: finish the retry logic")
        self.app.save_draft()
        self.app.destroy()
        self.app = self.make_app()
        self.assertEqual(self.note(), "tomorrow: finish the retry logic")

    def test_is_never_submitted(self):
        self.write("private notes")
        self.assertEqual(self.button(), "Submit")  # nothing submittable
        self.fill(0, "QATT-85", "real work", "1h")
        self.app.submit()
        wait_for_submit(self.app)
        logged = [e for e in self.app.client._logged]
        self.assertEqual([e["comment"] for e in logged], ["real work"])

    def test_does_not_count_towards_the_total(self):
        self.write("a note mentioning 5h of stuff")
        self.assertEqual(self.app.total_label.cget("text"), "Total: 0m")

    def test_has_no_ticket_or_time_field(self):
        self.assertFalse(hasattr(self.app.sticky, "picker"))
        self.assertFalse(hasattr(self.app.sticky, "time_var"))

    def test_lives_in_its_own_tab_not_the_log_tab(self):
        tabs = [self.app.notebook.tab(i, "text").strip()
                for i in range(self.app.notebook.index("end"))]
        self.assertEqual(tabs, ["Log work", "Current work", "Logged work"])
        # the note must not be a descendant of the Log work tab
        self.assertFalse(_inside(self.app.sticky, str(self.app.notebook.tabs()[0])))

    def test_fills_its_tab(self):
        self.assertTrue(_inside(self.app.sticky, str(self.app.notebook.tabs()[1])))
        info = self.app.sticky.pack_info()
        self.assertEqual(info["fill"], "both")
        self.assertEqual(str(info["expand"]), "1")

    def test_long_notes_scroll(self):
        self.write("\n".join(f"line {n}" for n in range(200)))
        self.assertTrue(self.app.sticky.view.scrollbar.winfo_exists())
        self.assertEqual(self.note().splitlines()[-1], "line 199")


def wait_for_review(app, timeout=30):
    end = time.time() + timeout
    while app.reviewing and time.time() < end:
        app.update()
        time.sleep(0.01)
    app.update()
    if app.reviewing:
        raise AssertionError("review fetch did not finish within the timeout")


class TestLoggedWorkTab(AppTestCase):
    def shown(self):
        return self.app.review_view.text.get("1.0", "end-1c")

    def test_starts_with_an_invitation_not_a_fetch(self):
        self.assertIn("Show work logged", self.shown())

    def test_lists_ticket_time_and_comment(self):
        self.app.review_date.set(date(2026, 8, 6))  # a Thursday
        self.app.load_logged_work()
        wait_for_review(self.app)
        shown = self.shown()
        self.assertIn("QATT-85", shown)
        self.assertIn("2h", shown)
        self.assertIn("Reviewed the auth PR.", shown)
        self.assertIn("Paired with Anna on retry logic.", shown)

    def test_shows_a_total(self):
        self.app.review_date.set(date(2026, 8, 6))
        self.app.load_logged_work()
        wait_for_review(self.app)
        self.assertEqual(self.app.review_total.cget("text"), "Total: 3h 30m")

    def test_reports_an_empty_day(self):
        self.app.review_date.set(date(2026, 8, 8))  # a Saturday
        self.app.load_logged_work()
        wait_for_review(self.app)
        self.assertIn("Nothing logged", self.shown())
        self.assertEqual(self.app.review_total.cget("text"), "Total: 0m")

    def test_surfaces_errors_instead_of_crashing(self):
        def boom(*_args, **_kwargs):
            raise bl.JiraError("Jira returned a server error (500).", status=500)

        self.app.client.fetch_day_worklogs = boom
        self.app.load_logged_work()
        wait_for_review(self.app)
        self.assertIn("server error", self.shown())
        self.assertEqual(str(self.app.review_btn.cget("state")), "normal")

    def test_view_is_read_only(self):
        self.app.review_date.set(date(2026, 8, 6))
        self.app.load_logged_work()
        wait_for_review(self.app)
        self.assertEqual(str(self.app.review_view.text.cget("state")), "disabled")

    def test_its_date_is_independent_of_the_log_tab(self):
        self.app.shift_date(-4)
        self.assertEqual(self.app.review_date.value, date.today())

    def test_newly_submitted_work_shows_up(self):
        self.fill(0, "QATT-87", "just logged this", "25m")
        self.app.submit()
        wait_for_submit(self.app)

        self.app.review_date.set(self.app.selected_date)
        self.app.load_logged_work()
        wait_for_review(self.app)
        self.assertIn("just logged this", self.shown())


class TestSignOut(AppTestCase):
    """The token expires roughly yearly, so this must work without a restart."""

    def setUp(self):
        super().setUp()
        self.app.destroy()
        self.app = self.make_app(dry_run=False)  # sign-out is hidden in dry run
        self.cleared = []
        self._orig_clear = bl.clear_credentials
        self._orig_auth = bl.authenticate
        bl.clear_credentials = lambda: self.cleared.append(True)

    def tearDown(self):
        bl.clear_credentials = self._orig_clear
        bl.authenticate = self._orig_auth
        super().tearDown()

    def stub_auth(self, account="new@mozilla.com", cancelled=False):
        def fake(parent, cfg, message=""):
            self.auth_message = message
            if cancelled:
                return None, None, None
            return bl.DryRunClient(cfg.all_keys), {"timeZone": "Europe/Bucharest"}, account
        bl.authenticate = fake

    def app_is_gone(self):
        # the App is the root Tk, so destroying it takes the interpreter with it
        try:
            return not self.app.winfo_exists()
        except bl.tk.TclError:
            return True

    def test_shows_when_the_token_was_added(self):
        bl.store_credentials("me@mozilla.com", "tok")
        self.addCleanup(bl.CREDENTIALS_PATH.unlink, True)
        self.app.destroy()
        self.app = self.make_app(dry_run=False)
        self.assertIn(f"token added {date.today().isoformat()}",
                      self.app.token_label.cget("text"))

    def test_token_date_is_blank_when_there_is_no_credentials_file(self):
        self.assertEqual(self.app.token_label.cget("text"), "")

    def test_button_is_shown_and_labelled_with_the_account(self):
        # winfo_ismapped is 0 for the whole window under test (it is withdrawn),
        # so ask the geometry manager whether the button was placed at all
        self.assertEqual(self.app.signout_btn.winfo_manager(), "pack")
        self.assertEqual(self.app.account_label.cget("text"), "me@mozilla.com")

    def test_hidden_in_dry_run(self):
        self.app.destroy()
        self.app = self.make_app(dry_run=True)
        self.assertEqual(self.app.signout_btn.winfo_manager(), "")

    def test_declining_the_prompt_changes_nothing(self):
        self.answer_yes = False
        self.stub_auth()
        self.fill(0, "QATT-85", "work", "1h")
        self.app.sign_out()
        self.assertEqual(self.cleared, [])
        self.assertEqual(self.app.account, "me@mozilla.com")

    def test_clears_the_token_and_adopts_the_new_account(self):
        self.stub_auth(account="new@mozilla.com")
        self.app.sign_out()
        self.app.update()
        self.assertEqual(self.cleared, [True])
        self.assertEqual(self.app.account, "new@mozilla.com")
        self.assertEqual(self.app.account_label.cget("text"), "new@mozilla.com")

    def test_typed_rows_survive(self):
        self.stub_auth()
        self.fill(0, "QATT-85", "half written note", "2h")
        self.app.sign_out()
        self.app.update()
        self.assertEqual(self.app.rows[0].key, "QATT-85")
        self.assertEqual(self.app.rows[0].comment.get("1.0", "end-1c"),
                         "half written note")
        self.assertEqual(self.app.rows[0].state, bl.READY)

    def test_window_comes_back(self):
        self.stub_auth()
        self.app.deiconify()
        self.app.sign_out()
        self.app.update()
        self.assertEqual(self.app.state(), "normal")

    def test_explains_why_re_authentication_is_needed(self):
        self.stub_auth()
        self.app.sign_out()
        self.assertIn("Signed out", self.auth_message)

    def test_cancelling_sign_in_closes_the_app(self):
        self.stub_auth(cancelled=True)
        self.app.sign_out()
        self.assertTrue(self.app_is_gone())

    def test_disabled_while_submitting(self):
        self.app.submitting = True
        self.app.refresh()
        self.assertEqual(str(self.app.signout_btn.cget("state")), "disabled")


class TestCredentialDialog(unittest.TestCase):
    """Regression: the dialog is created while the root window is withdrawn.

    Tk on Windows withdraws a Toplevel that is transient to a withdrawn master,
    so the app would sit running with no visible window on first launch.
    """

    def setUp(self):
        self.root = bl.tk.Tk()
        self.root.withdraw()
        bl.theme.apply_theme(self.root)

    def tearDown(self):
        self.root.destroy()

    def make_dialog(self, verify=None):
        verify = verify or (lambda email, token: ("client", {"timeZone": "UTC"}))
        dialog = bl.CredentialDialog(self.root, "https://example.atlassian.net",
                                     verify)
        self.root.update()
        self.addCleanup(self._dispose, dialog)
        return dialog

    @staticmethod
    def _dispose(dialog):
        try:
            dialog.grab_release()
            dialog.destroy()
        except Exception:
            pass

    def test_dialog_is_actually_visible(self):
        dialog = self.make_dialog()
        self.assertTrue(dialog.winfo_viewable(),
                        "sign-in dialog exists but is not mapped")

    def test_dialog_reports_both_fields_required(self):
        dialog = self.make_dialog()
        dialog._ok()
        self.assertIsNone(dialog.result)
        self.assertIn("required", dialog.error.cget("text"))

    def test_dialog_verifies_before_closing(self):
        dialog = self.make_dialog()
        dialog.email.insert(0, "me@mozilla.com")
        dialog.token.insert(0, "tok")
        dialog._ok()
        self.assertEqual(dialog.result[2:], ("me@mozilla.com", "tok"))

    def test_rejected_token_keeps_the_dialog_open(self):
        def verify(email, token):
            raise bl.JiraError("Authentication failed - token invalid.", status=401)

        dialog = self.make_dialog(verify)
        dialog.email.insert(0, "me@mozilla.com")
        dialog.token.insert(0, "expired")
        dialog._ok()
        self.assertIsNone(dialog.result)
        self.assertTrue(dialog.winfo_exists())
        self.assertIn("Authentication failed", dialog.error.cget("text"))


class TestPickerWidget(AppTestCase):
    def test_selection_shows_key_and_live_title(self):
        row = self.fill(0, "QATT-85")
        self.assertTrue(row.picker.entry.get().startswith("QATT-85 · "))
        self.assertIn(self.app.catalog.title("QATT-85"), row.picker.entry.get())

    def test_popup_opens_and_closes(self):
        picker = self.app.rows[0].picker
        picker._open()
        self.app.update()
        self.assertIsNotNone(picker._popup)
        self.assertTrue(any(kind == bl.ITEM for kind, _t, _p in picker._rows))
        picker._close()
        self.app.update()
        self.assertIsNone(picker._popup)

    def test_keyboard_navigation_skips_headers(self):
        picker = self.app.rows[0].picker
        picker._open()
        self.app.update()
        self.assertEqual(picker._rows[picker._active][0], bl.ITEM)
        picker._move(1)
        self.assertEqual(picker._rows[picker._active][0], bl.ITEM)
        picker._close()

    def test_ctrl_enter_binding_exists_on_comment(self):
        self.assertIn("<Control-Key-Return>",
                      self.app.rows[0].comment.bind())

    def test_wheel_over_the_popup_does_not_scroll_the_row_area(self):
        """Regression: one wheel notch used to move both lists at once."""
        picker = self.app.rows[0].picker
        picker._open()
        self.app.update()

        # the popup's own binding must claim the event
        self.assertIn("<MouseWheel>", picker._listbox.bind())
        self.assertEqual(picker._on_wheel(_Wheel(-120)), "break")
        self.assertTrue(picker.is_open())
        picker._close()

    def test_wheel_over_the_row_area_closes_an_open_popup(self):
        """An absolutely-positioned popup would otherwise hang over the scroll."""
        picker = self.app.rows[0].picker
        picker._open()
        self.app.update()
        self.assertTrue(picker.is_open())

        self.app._on_wheel(_Wheel(-120))
        self.app.update()
        self.assertFalse(picker.is_open())


class _Wheel:
    """Stand-in for a Tk MouseWheel event."""

    def __init__(self, delta):
        self.delta = delta


if __name__ == "__main__":
    unittest.main()
