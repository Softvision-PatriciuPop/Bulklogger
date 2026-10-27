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
        self.fill(0, "QATT-85", "line one\nline two", "2h")
        self.fill(1, "QATT-86", "half typed", None)
        self.app.save_draft()

        self.app.destroy()
        self.app = self.make_app()

        self.assertEqual(self.app.rows[0].key, "QATT-85")
        self.assertEqual(self.app.rows[0].comment.get("1.0", "end-1c"),
                         "line one\nline two")
        self.assertEqual(self.app.rows[0].time_text, "2h")
        self.assertEqual(self.app.rows[1].key, "QATT-86")
        self.assertEqual(self.app.rows[1].state, bl.PARTIAL)

    def test_a_restart_returns_to_today(self):
        """Back-dating is per-session; a stale date must not come back."""
        self.app.shift_date(-2)
        self.fill(0, "QATT-85", "work from two days ago", "2h")
        self.app.save_draft()

        self.app.destroy()
        self.app = self.make_app()

        self.assertEqual(self.app.selected_date, bl.date.today())
        self.assertEqual(self.app.rows[0].key, "QATT-85")

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


class TestWeekField(AppTestCase):
    """The review tab picks weeks, so every date it is handed snaps to Monday."""

    def field(self):
        return self.app.review_week

    def test_starts_on_the_current_week(self):
        self.assertEqual(self.field().value, bl.monday_of(date.today()))

    def test_any_day_snaps_to_its_monday(self):
        self.field().set(date(2026, 8, 6))  # a Thursday
        self.assertEqual(self.field().value, date(2026, 8, 3))
        self.assertEqual(self.field().end, date(2026, 8, 9))

    def test_shift_moves_whole_weeks(self):
        self.field().set(date(2026, 8, 6))
        self.field().shift(-1)
        self.assertEqual(self.field().value, date(2026, 7, 27))
        self.field().shift(2)
        self.assertEqual(self.field().value, date(2026, 8, 10))

    def test_this_week_returns_to_today(self):
        self.field().set(date(2020, 1, 1))
        self.field().today()
        self.assertEqual(self.field().value, bl.monday_of(date.today()))

    def test_shows_the_span_it_will_fetch(self):
        self.field().set(date(2026, 8, 6))
        self.assertEqual(self.field().var.get(), "2026-08-03 → 2026-08-09")

    def test_committing_its_own_rendering_is_a_no_op(self):
        """FocusOut fires on the unedited text, which must not be an error."""
        self.field().set(date(2026, 8, 6))
        self.field().commit()
        self.assertEqual(self.field().value, date(2026, 8, 3))
        self.assertNotIn("YYYY", self.field().note.cget("text"))

    def test_typing_a_mid_week_date_selects_that_week(self):
        self.field().var.set("2026-08-06")
        self.field().commit()
        self.assertEqual(self.field().value, date(2026, 8, 3))

    def test_garbage_is_rejected_and_the_span_restored(self):
        self.field().set(date(2026, 8, 6))
        self.field().var.set("not a date")
        self.field().commit()
        self.assertEqual(self.field().value, date(2026, 8, 3))
        self.assertEqual(self.field().var.get(), "2026-08-03 → 2026-08-09")
        self.assertIn("YYYY-MM-DD", self.field().note.cget("text"))

    def test_the_note_names_the_iso_week(self):
        self.field().set(date(2026, 8, 6))
        self.assertIn("W32", self.field().note.cget("text"))


class TestLoggedWorkTab(AppTestCase):
    """A week at a time: issues down the side, days across the top."""

    def cells(self):
        """Every piece of text the table drew, flattened."""
        out = []
        for widget in _walk(self.app.review_grid):
            try:
                text = widget.cget("text")
            except bl.tk.TclError:
                continue
            if text:
                out.append(str(text))
        return out

    def shown(self):
        return "\n".join(self.cells())

    def grid(self):
        return self.app.review_grid_model

    def load(self, day=None):
        if day is not None:
            self.app.review_week.set(day)
        self.app.load_logged_work()
        wait_for_review(self.app)

    def test_starts_with_an_invitation_not_a_fetch(self):
        self.assertIn("Show work logged", self.shown())
        self.assertIsNone(self.grid())

    def test_columns_are_the_working_days_of_the_week(self):
        self.load(date(2026, 8, 6))  # a Thursday, so the week of the 3rd
        self.assertEqual(self.grid().days,
                         [date(2026, 8, d) for d in range(3, 8)])
        for header in ("Mon 03", "Tue 04", "Wed 05", "Thu 06", "Fri 07"):
            self.assertIn(header, self.cells())

    def test_rows_are_issues_and_cells_carry_time_and_comment(self):
        self.load(date(2026, 8, 6))
        shown = self.shown()
        self.assertIn("QATT-85", shown)
        self.assertIn("2h", shown)
        self.assertIn("Reviewed the auth PR.", shown)

        row = next(r for r in self.grid().rows if r.key == "QATT-85")
        monday = date(2026, 8, 3)
        self.assertEqual(row.cells[monday].seconds, 7200)
        self.assertIn("Paired with Anna", row.cells[monday].comment)

    def test_shows_a_total_per_day_and_for_the_week(self):
        self.load(date(2026, 8, 6))
        grid = self.grid()
        self.assertEqual(grid.day_totals[date(2026, 8, 3)], 12600)  # 3h 30m
        self.assertEqual(grid.total, sum(grid.day_totals.values()))
        self.assertEqual(
            self.app.review_total.cget("text"),
            f"Week total: {bl.format_duration(grid.total)}")
        self.assertIn("Day total", self.cells())

    def test_shows_a_total_per_issue(self):
        self.load(date(2026, 8, 6))
        row = next(r for r in self.grid().rows if r.key == "QATT-85")
        self.assertEqual(row.total, sum(c.seconds for c in row.cells.values()))
        self.assertIn(bl.format_duration(row.total), self.cells())

    def test_days_without_work_are_marked_not_blank(self):
        self.load(date(2026, 8, 6))
        self.assertIn("—", self.cells())

    def test_weekend_columns_appear_only_when_worked(self):
        monday = bl.monday_of(date.today())
        self.load(monday)
        self.assertEqual(len(self.grid().days), 5)

        saturday = monday + timedelta(days=5)
        self.app.client._logged.append(
            {"key": "QATT-91", "summary": "Weekend", "seconds": 3600,
             "comment": "pipeline wedged",
             "started": f"{saturday.isoformat()}T10:00:00.000+0300"})
        self.load(monday)
        self.assertEqual(self.grid().days[-1], saturday)
        self.assertIn(saturday.strftime("%a %d"), self.cells())

    def test_reports_an_empty_week(self):
        self.app.client.fetch_week_worklogs = lambda *a, **k: []
        self.load(date(2026, 8, 6))
        self.assertIn("Nothing logged", self.shown())
        self.assertEqual(self.app.review_total.cget("text"), "Week total: 0m")

    def test_surfaces_errors_instead_of_crashing(self):
        def boom(*_args, **_kwargs):
            raise bl.JiraError("Jira returned a server error (500).", status=500)

        self.app.client.fetch_week_worklogs = boom
        self.load()
        self.assertIn("server error", self.shown())
        self.assertEqual(str(self.app.review_btn.cget("state")), "normal")

    def test_selecting_a_cell_shows_its_full_comment(self):
        """Cells clamp to fit the column; the whole comment lives below."""
        self.load(date(2026, 8, 6))
        row = next(r for r in self.grid().rows if r.key == "QATT-85")
        monday = date(2026, 8, 3)
        self.app._show_cell(row, monday, row.cells[monday])

        detail = self.app.detail_text()
        self.assertIn("QATT-85", detail)
        self.assertIn("Mon 03 Aug", detail)
        self.assertIn("Paired with Anna on retry logic.", detail)

    def test_a_long_comment_is_clamped_in_the_cell_but_not_in_the_detail(self):
        monday = bl.monday_of(date.today())
        long_comment = "wedged on a stuck runner " * 12
        self.app.client._logged.append(
            {"key": "QATT-91", "summary": "Long one", "seconds": 3600,
             "comment": long_comment,
             "started": f"{monday.isoformat()}T10:00:00.000+0300"})
        self.load(monday)

        self.assertFalse(any(long_comment.strip() == c for c in self.cells()))
        row = next(r for r in self.grid().rows if r.key == "QATT-91")
        self.app._show_cell(row, monday, row.cells[monday])
        self.assertIn(long_comment.strip(), self.app.detail_text())

    def test_the_detail_strip_is_selectable_and_copyable(self):
        """Its whole point is pasting a comment into a standup or a ticket."""
        self.load(date(2026, 8, 6))
        row = next(r for r in self.grid().rows if r.key == "QATT-85")
        monday = date(2026, 8, 3)
        self.app._show_cell(row, monday, row.cells[monday])

        detail = self.app.review_detail
        detail.tag_add("sel", "1.0", "end-1c")
        self.assertEqual(detail.get("sel.first", "sel.last"),
                         self.app.detail_text())

        self.app.clipboard_clear()
        detail.event_generate("<<Copy>>")
        self.app.update()
        self.assertIn("Paired with Anna", self.app.clipboard_get())

    def test_ctrl_a_selects_the_whole_comment(self):
        self.load(date(2026, 8, 6))
        row = next(r for r in self.grid().rows if r.key == "QATT-85")
        monday = date(2026, 8, 3)
        self.app._show_cell(row, monday, row.cells[monday])

        self.app._select_all_detail()
        detail = self.app.review_detail
        self.assertEqual(detail.get("sel.first", "sel.last"),
                         self.app.detail_text())

    def test_the_detail_strip_cannot_be_edited(self):
        self.load(date(2026, 8, 6))
        before = self.app.detail_text()
        self.app.review_detail.event_generate("<Key>", keysym="x")
        self.app.update()
        self.assertEqual(self.app.detail_text(), before)

    def test_the_detail_strip_grows_for_a_long_comment(self):
        monday = bl.monday_of(date.today())
        self.app.client._logged.append(
            {"key": "QATT-91", "summary": "Long one", "seconds": 3600,
             "comment": "\n".join(f"line {n}" for n in range(30)),
             "started": f"{monday.isoformat()}T10:00:00.000+0300"})
        self.load(monday)
        short = self.app.review_detail.cget("height")

        row = next(r for r in self.grid().rows if r.key == "QATT-91")
        self.app._show_cell(row, monday, row.cells[monday])
        self.assertGreater(self.app.review_detail.cget("height"), short)
        self.assertLessEqual(self.app.review_detail.cget("height"),
                             bl.DETAIL_MAX_LINES)

    def test_its_week_is_independent_of_the_log_tab(self):
        self.app.shift_date(-4)
        self.assertEqual(self.app.review_week.value, bl.monday_of(date.today()))

    def test_newly_submitted_work_shows_up(self):
        self.fill(0, "QATT-87", "just logged this", "25m")
        self.app.submit()
        wait_for_submit(self.app)

        self.load(self.app.selected_date)
        self.assertIn("just logged this", self.shown())

    def test_a_narrower_week_does_not_leave_a_phantom_column(self):
        """Column weights outlive their widgets unless they are cleared."""
        monday = bl.monday_of(date.today())
        saturday = monday + timedelta(days=5)
        self.app.client._logged.append(
            {"key": "QATT-91", "summary": "Weekend", "seconds": 3600,
             "comment": "pipeline wedged",
             "started": f"{saturday.isoformat()}T10:00:00.000+0300"})
        self.load(monday)
        self.assertEqual(len(self.grid().days), 6)

        self.app.client._logged.clear()
        self.load(monday)
        self.assertEqual(len(self.grid().days), 5)
        # Columns 0..6 are issue + five days + week total. Anything configured
        # beyond that is left over from the six-day week and would reserve
        # dead space on the right.
        grid = self.app.review_grid
        for column in range(7, grid.grid_size()[0]):
            config = grid.columnconfigure(column)
            self.assertEqual(config.get("weight"), 0, f"column {column}")
            self.assertEqual(config.get("minsize"), 0, f"column {column}")

    def test_a_reload_replaces_the_table_rather_than_appending(self):
        self.load(date(2026, 8, 6))
        first = len(self.cells())
        self.load(date(2026, 8, 6))
        self.assertEqual(len(self.cells()), first)


def wait_for_update(app, timeout=10):
    end = time.time() + timeout
    while app._update_job and time.time() < end:
        app.update()
        time.sleep(0.01)
    app.update()


class TestUpdateCheck(AppTestCase):
    """Startup asks GitHub whether this build is current, quietly."""

    def setUp(self):
        super().setUp()
        # Point at a throwaway credentials.toml: the check is gated on one
        # existing, and the real file must not be touched by a test run.
        self._orig_creds = bl.CREDENTIALS_PATH
        bl.CREDENTIALS_PATH = Path(tempfile.mkdtemp()) / "credentials.toml"
        self.addCleanup(lambda: setattr(bl, "CREDENTIALS_PATH",
                                        self._orig_creds))

    def sign_in(self):
        bl.store_credentials("me@mozilla.com", "tok")

    def banner(self):
        return self.app.update_label.cget("text")

    def is_shown(self):
        return self.app.update_label.winfo_manager() == "pack"

    def rebuild(self, tag, dry_run=False, signed_in=True):
        """A fresh app whose background check returns `tag`."""
        if signed_in:
            self.sign_in()
        self._orig_fetch = bl.fetch_latest_release
        bl.fetch_latest_release = lambda *a, **k: tag
        self.addCleanup(lambda: setattr(bl, "fetch_latest_release",
                                        self._orig_fetch))
        self.app.destroy()
        self.app = self.make_app(dry_run=dry_run)
        wait_for_update(self.app)

    def test_a_newer_release_raises_a_banner(self):
        self.rebuild("v99.9")
        self.assertTrue(self.is_shown())
        self.assertIn("v99.9", self.banner())
        self.assertIn("Update available", self.banner())

    def test_being_current_shows_nothing_at_all(self):
        """An up-to-date install gets no furniture it has to look past."""
        self.rebuild(f"v{bl.VERSION}")
        self.assertFalse(self.is_shown())
        self.assertEqual(self.banner(), "")

    def test_an_unreachable_github_is_silent(self):
        self.rebuild("")
        self.assertFalse(self.is_shown())

    def test_an_older_tag_does_not_offer_a_downgrade(self):
        self.rebuild("v0.1")
        self.assertFalse(self.is_shown())

    def test_the_banner_opens_the_releases_page(self):
        opened = []
        orig = bl.webbrowser.open
        bl.webbrowser.open = opened.append
        self.addCleanup(lambda: setattr(bl.webbrowser, "open", orig))

        self.rebuild("v99.9")
        self.app.update_label.event_generate("<Button-1>")
        self.app.update()
        self.assertEqual(opened, [bl.RELEASES_URL])

    def test_dry_run_shows_the_banner_without_a_network(self):
        """No network in dry run, but the banner still has to be demonstrable."""
        def boom(*_a, **_k):
            raise AssertionError("dry run must not touch the network")

        orig = bl.fetch_latest_release
        bl.fetch_latest_release = boom
        self.addCleanup(lambda: setattr(bl, "fetch_latest_release", orig))

        self.app.destroy()
        self.app = self.make_app(dry_run=True)
        wait_for_update(self.app)
        self.assertTrue(self.is_shown())

    def test_the_check_is_skipped_without_a_stored_token(self):
        """A first-run user is mid-sign-in and has no use for release news."""
        called = []
        orig = bl.fetch_latest_release
        bl.fetch_latest_release = lambda *a, **k: called.append(True) or "v99.9"
        self.addCleanup(lambda: setattr(bl, "fetch_latest_release", orig))

        self.app.destroy()
        self.app = self.make_app(dry_run=False)  # no credentials.toml written
        wait_for_update(self.app)
        self.assertEqual(called, [])
        self.assertFalse(self.is_shown())

    def test_the_version_is_in_the_title_bar(self):
        """So "what version are you on?" has an answer without a rebuild."""
        self.assertIn(f"v{bl.VERSION}", self.app.title())


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

    def write_credentials(self, created):
        bl.CREDENTIALS_PATH.write_text(
            bl.CREDENTIALS_TEMPLATE.format(app=bl.APP_NAME,
                                           email='"me@mozilla.com"',
                                           token='"tok"',
                                           created=f'"{created}"'),
            encoding="utf-8")
        self.addCleanup(bl.CREDENTIALS_PATH.unlink, True)

    def test_expired_token_is_called_out_in_red(self):
        created = date.today() - timedelta(days=400)
        self.write_credentials(created.isoformat())
        self.app.destroy()
        self.app = self.make_app(dry_run=False)

        text = self.app.token_label.cget("text")
        self.assertIn("expired", text)
        self.assertIn(bl.token_expiry(created.isoformat()).isoformat(), text)
        self.assertEqual(str(self.app.token_label.cget("foreground")),
                         bl.theme.ERROR)

    def test_ageing_token_is_amber_not_red(self):
        created = date.today() - timedelta(days=bl.TOKEN_STALE_DAYS + 1)
        self.write_credentials(created.isoformat())
        self.app.destroy()
        self.app = self.make_app(dry_run=False)

        self.assertIn("expires", self.app.token_label.cget("text"))
        self.assertEqual(str(self.app.token_label.cget("foreground")),
                         bl.theme.WARNING)

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
