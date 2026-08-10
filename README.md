# Bulklogger

Fill in a day's worth of Jira worklogs in one window and submit them with one
click, instead of opening the Log Work dialog on six issues in turn.

Built to the specification in
[documentation/jira-bulk-worklog-design.md](documentation/jira-bulk-worklog-design.md).

---

## For users — running the app

You need two files in the same folder, and nothing else installed:

```
Bulklogger.exe        (Windows)        Bulklogger.app        (macOS)
tickets.toml                           tickets.toml
```

Open it. The first time, it asks for your email and a Jira API token and writes
`credentials.toml` beside itself. After that it goes straight to the main
window.

On macOS the app is unsigned, so the first launch needs **right-click → Open**
rather than a double-click; after that it opens normally.

Create your API token at **id.atlassian.com → Security → API tokens**. That is
not your account password. See [Jira permissions](#jira-permissions) below for
what your account needs to be able to do.

Everyone runs their own copy with their own token — nothing is shared between
people except `tickets.toml`.

## For developers — running from source

Python 3.11 or newer. Tkinter ships with the official Python installer on
Windows and macOS; on Debian/Ubuntu, `apt install python3-tk`.

```
pip install -r requirements.txt
python bulklogger.py
python bulklogger.py --dry-run            # simulated Jira, no network, no token
python bulklogger.py --reset-credentials  # delete credentials.toml
```

Tests: `python -m unittest test_bulklogger test_app` (81 tests, no network).

## Building

**PyInstaller cannot cross-compile.** Each binary has to be built on the OS it
targets — you cannot produce the macOS app from Windows or vice versa.

| Target | Where to build | Command | Output |
|---|---|---|---|
| Windows | Windows | `.\build.ps1` | `dist\Bulklogger.exe` (~14 MB) |
| macOS | macOS | `./build.sh` | `dist/Bulklogger.app` |
| Both, without those machines | GitHub Actions | push, or run the **build** workflow | zips on the run summary |

```
pip install -r requirements-dev.txt
```

### Cutting a release

[`.github/workflows/build.yml`](.github/workflows/build.yml) runs the tests,
builds both platforms on their own runners, and attaches the zips to a GitHub
Release. Two ways to trigger it:

```
git tag v1.0.0
git push origin v1.0.0
```

or **Actions → build → Run workflow**, filling in the version box (e.g.
`v1.0.0`). Leave the box blank to just build and get downloadable artifacts
without publishing anything.

Each release carries `Bulklogger-<version>-windows.zip` and
`Bulklogger-<version>-macos.zip`, each containing the executable, `tickets.toml`,
`credentials.toml.example` and this README. The macOS zip is made with `ditto`
rather than `zip`, which preserves the bundle's symlinks and executable bits —
a plain zip mangles them and Gatekeeper then refuses to open the app.

Nothing personal ever reaches a release: the build scripts strip
`credentials.toml`, `draft.json` and `usage.json` from `dist/`, the packaging
step lists files explicitly rather than copying a directory, and a CI runner
never signs in anyway.

### macOS notes

The `.app` is **unsigned**, so Gatekeeper blocks a plain double-click on first
run. Recipients should right-click → **Open** once, or:

```
xattr -dr com.apple.quarantine /path/to/Bulklogger.app
```

Config lives *beside* `Bulklogger.app`, not inside it — put `tickets.toml` in
the same folder as the bundle and `credentials.toml` is written there too.

macOS ships its own IANA time zone database, so `tzdata` is redundant there; it
is bundled anyway so the same requirements file works everywhere.

Icons are committed so a normal build needs no image library. Regenerate them
from the artwork in [documentation/](documentation/) with:

```
python make_icons.py
```

`tickets.toml` and `credentials.toml` are deliberately **not** bundled — the exe
reads them from its own directory at runtime. That way each person keeps their
own token beside their own copy, and rebuilding never overwrites either.

> ### ⚠️ Ship two files, and only two
>
> ```
> Bulklogger.exe
> tickets.toml
> ```
>
> If you run the exe from `dist\`, it writes **your** `credentials.toml`,
> `draft.json` and `usage.json` right next to it — and `dist\` is the folder you
> would naturally zip. `credentials.toml` contains your API token in plain text.
>
> `build.ps1` deletes those three from `dist\` on every build and tells you when
> it did, but check before you send anything. If a build did go out with your
> token in it, revoke it at id.atlassian.com → Security → API tokens and let
> Bulklogger prompt you for a new one.

---

## Configuration

### tickets.toml — shared, safe to commit

Ticket keys only. Titles come live from Jira on every start, so nothing here can
go stale.

```toml
base_url = "https://mozilla-hub.atlassian.net"
timezone = "Europe/Bucharest"
retired  = ["QATT-40"]          # still selectable, collapsed under "Past goals"

[groups]
"QATT · Test automation" = ["QATT-85", "QATT-86"]
```

Group labels are yours to choose and need not mirror Jira's hierarchy — they
only control how the picker groups things.

### credentials.toml — personal, never commit

```toml
email = "you@mozilla.com"
api_token = "..."
```

Written for you on first run, or copy [credentials.toml.example](credentials.toml.example)
and fill it in. It is gitignored.

> **This file holds an API token in plain text.** For API purposes that is
> equivalent to your password. Anyone who can read the file can act as you in
> Jira. Don't commit it, don't put it on a shared drive, and revoke the token at
> id.atlassian.com if it is ever exposed. This was a deliberate trade for
> per-machine portability over the OS keyring the design doc originally called
> for.

---

## Jira permissions

Bulklogger authenticates with **HTTP Basic + an API token**, not OAuth. Which
permissions matter depends on which kind of token you create.

### Classic API token (the default, and what this app expects)

Classic tokens carry **no scopes at all** — they inherit every permission your
Jira account already has. There is nothing to configure on the token itself.

What your *account* needs, per project you log against:

| Jira project permission | Why | Symptom if missing |
|---|---|---|
| **Browse Projects** | Read the issue and let it come back from the JQL search that fetches titles at startup | Startup aborts: "these keys do not exist, or are not visible to this account" |
| **Work On Issues** | Create the worklog | The row fails at submit with `403 — the account lacks 'Work On Issues'` |

That's the whole list. Bulklogger never edits or transitions issues, and since
per-row undo was dropped it never deletes worklogs either — so **Edit Issues**,
**Delete Own Worklogs** and **Administer Projects** are all unnecessary.

The Logged work tab needs no extra permission — reading worklogs comes with
**Browse Projects**. It only ever shows projects you can already see, so if time
was logged somewhere you have since lost access to, it will not appear.

One global setting also has to be on: **Settings → Issues → Time tracking**. If
time tracking is disabled Jira-wide, every worklog POST fails regardless of
permissions.

### Scoped API token, or OAuth 2.0 (3LO)

If your org requires scoped tokens, the app needs read access to your own user
record, read access to issues, and write access to worklogs. In classic scope
terms that is:

```
read:jira-user      GET /rest/api/2/myself
read:jira-work      GET /rest/api/2/search/jql   (issue keys and titles)
write:jira-work     POST /rest/api/2/issue/{key}/worklog
```

Atlassian is migrating to finer-grained scopes (`read:issue:jira`,
`write:issue-worklog:jira`, `read:issue-worklog:jira` and similar), and the
exact set the token picker offers has been changing. If you go this route, grant
those three capabilities — read user, read issues, write worklogs — and check
against Atlassian's current list rather than treating the names above as fixed.
The project permissions in the table above still apply on top of any scope.

Bulklogger has not been tested against a scoped token. The classic path is the
supported one.

---

## Using it

Two tabs: **Log work** to enter a day's worklogs, **Logged work** to see what is
already in Jira.

### Log work

| | |
|---|---|
| Current work | A scratchpad at the top. Kept across days, survives Clear, and is **never sent to Jira** — somewhere to park what you are in the middle of so tomorrow's entry is easy to write. |
| Ticket picker | Click and type. Matches on key, title and group — `85`, `oidc` and a group name all work. Most recently logged appears first. Past goals are collapsed at the bottom. |
| Comment | Multi-line. Required — the tool will not log time without a description. |
| Time | `2h`, `90m`, `2h 30m`, `1.5h`. Days and weeks are refused as ambiguous. |
| Date | One date for the whole batch. `◀ ▶` step a day, `Today` resets. Switching days keeps everything you have typed. |
| `Ctrl+Enter` | Submit. Plain `Enter` inserts a newline in the comment box. |
| Sign out | Top right, next to the account you are signed in as. Use it when your API token expires. |

Blank rows are ignored. Half-filled rows block the submit entirely and nothing
is sent — deliberately, so a row you meant to log never gets silently dropped.

If some rows fail, the ones that succeeded stay locked and the button re-counts
to only what is left, so pressing it again cannot create duplicates.

Your remaining estimates are never touched: every call sends
`adjustEstimate=leave`.

### Logged work

Pick a date, press **Show work logged**, and you get back everything *you*
logged that day — ticket, time and comment — with a running total. Useful for
spotting a day you forgot, or an entry that went in wrong.

This reads from Jira live and is **not** limited to the tickets in
`tickets.toml`. That is deliberate: time logged against something unexpected is
exactly what you would want to catch. Other people's worklogs on the same issues
are filtered out.

Its date is independent of the Log work tab, so checking last Tuesday does not
disturb what you are typing.

### When your token expires

Atlassian API tokens expire — annually by default. Two things happen, and both
lead to the same place:

- **You know in advance.** Hit **Sign out** in the top right. It deletes
  `credentials.toml` and asks for a new token straight away. Anything you have
  typed stays on screen; anything already logged stays in Jira. Cancelling the
  sign-in closes the app, and your draft is restored next time you open it.
- **You find out the hard way.** The rows fail with a 401, the stale token is
  deleted automatically, and the next launch asks for a new one.

The rest of the batch is abandoned on a 401 rather than fired off to fail one by
one, so you never end up with a half-logged day you have to reconcile.

## Dry-run mode

`--dry-run` fakes the whole Jira side: invented titles, a simulated delay, and
no network or token access. Any row whose **comment contains the word "fail"**
is rejected, which is how you exercise the partial-failure path. It works with
no `tickets.toml` present.

## Files

| | |
|---|---|
| `bulklogger.py` | the application |
| `theme.py` | Mono Industrial dark theme |
| `bulklogger.ico` / `.icns` / `.png` | app icons for Windows / macOS / Tk — regenerate with `make_icons.py` |
| `tickets.toml` | shared config — safe to commit |
| `credentials.toml` | **your API token — gitignored, never commit** |
| `draft.json` | unsubmitted rows and your Current work note, restored on restart — gitignored |
| `usage.json` | last-logged time per ticket, drives picker ordering — gitignored |
