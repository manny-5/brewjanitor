# brewjanitor

**brewjanitor** brings the software on your Mac into the fold of [Homebrew](https://brew.sh).

It scans your Mac for installed apps, figures out which ones Homebrew already
manages, and — for the rest — checks whether Homebrew *could* install them. When
it can, brewjanitor offers to replace the app with a Homebrew-managed copy
(install the new one first, verify it, then remove the old one). Apps that
Homebrew can't install are left alone and listed in a report.

It is **safe by default**: with no flags it only inspects your machine and prints
a plan. An explicit `--apply` is required to actually install or delete
anything.

---

## Requirements

- A Mac (it reads `.app` bundles from `/Applications` and `~/Applications`).
- [Homebrew](https://brew.sh) installed and on your `PATH` (the `brew` command).
- Python 3.9 or newer.

You can check both with:

```bash
python3 --version
brew --version
```

---

## Install

From a clone of this repo:

```bash
git clone https://github.com/manny222manny/brewjanitor
cd brewjanitor
python3 -m pip install -e .
```

This installs a `brewjanitor` command you can run from anywhere. (The `-e`
installs it "editable", so pulling new code with `git pull` updates it without
reinstalling.)

---

## How to run it

### 1. Preview first (does nothing) — always start here

```bash
brewjanitor
```

This prints what brewjanitor *would* do: how many apps it found, how many
Homebrew already manages, which of the rest could be installed via Homebrew, and
which can't. It changes nothing on your machine and writes no files.

To see each app as it's checked (useful if you have many apps), add `--verbose`:

```bash
brewjanitor --verbose
```

### 2. Save a report of the apps it can't replace

```bash
brewjanitor --report my-report.csv
```

This writes a CSV (`name, path, bundle_id, brew_name, brew_kind, reason`) of
the apps Homebrew can't manage, so you have a durable list. (Without `--report`
or `--apply`, a dry run writes no files.)

### 3. Actually do it (installs + removes) — only when you're ready

```bash
brewjanitor --apply
```

For each app that Homebrew can install, this will:

1. run `brew install --cask --adopt <name>`,
2. **verify** the install actually placed a matching app (by bundle id or
   `.app` name),
3. **only then**, and only if Homebrew installed a *separate* copy somewhere
   else, remove the old bundle.

`--adopt` is what makes this work. Your unmanaged app sits on exactly the path
the cask installs to, and a plain `brew install --cask` refuses to overwrite it.
With `--adopt`, Homebrew checks that the bundle already on disk matches the cask
and takes ownership of it **in place** — so the usual outcome deletes nothing at
all. The removal step only runs in the genuine leftover case: your copy is in
`~/Applications` while the cask installs to `/Applications`.

If an install or a verification fails, the old app is **left untouched**. It
also refuses to remove anything outside `/Applications` or `~/Applications`.

One limitation worth knowing: Homebrew will only adopt a bundle whose version
matches the cask's current version. If yours is out of date, the adopt fails
with a message telling you to update the app first, and nothing is changed.

> ⚠️ `--apply` changes your system (it runs `brew install`, and in the leftover
> case deletes an old `.app` bundle). Read the dry-run output first.

### Fast check (no network)

The default run matches casks by name, which is fast. Under `--apply`,
brewjanitor verifies each match with a `brew info` call (which can hit the
network). To skip that verification even under `--apply`:

```bash
brewjanitor --offline --apply
```

`--offline` is faster but every candidate is `unverified`, so use it only if
you trust the name matches.

### If you interrupted an `--apply` run

If you stopped `brewjanitor --apply` mid-way (e.g. at a password prompt or with
Ctrl-C), your apps are safe: nothing is deleted until after an install + verify
succeeds, so an interruption at most leaves a cask *installed but the old bundle
not yet removed*. brewjanitor never deletes first.

To find and clean up those leftover old bundles, run:

```bash
brewjanitor --reconcile              # show what it would remove (dry-run)
brewjanitor --reconcile --apply       # remove the leftover old bundles
```

`--reconcile` detects apps that are now brew-managed (the new copy) but where
the *old* bundle is still on disk at a different path, and removes only the old
bundle. It **never re-installs** anything — it's a pure cleanup of a leftover.
The same path guard applies (only removes inside `/Applications` or
`~/Applications`).

Matching is on **bundle identifier only**, never on the `.app` filename. Two
apps sharing a name are not the same app: if you keep a beta or a pinned old
build in `~/Applications` alongside a cask-installed copy in `/Applications`,
a filename rule would call your second copy a leftover and delete it.

---

## Scheduled upgrades (autoupdate)

brewjanitor can install a daily `brew update && brew upgrade` job using macOS's
built-in scheduler (`launchd`). It is fully user-level and auditable:

- The job lives in **`~/Library/LaunchAgents/`** (your own home folder), never
  the system folder, so it runs as **you** with **your** permissions and never
  needs `sudo`.
- The file is **plain XML** — open it and read the whole thing before you trust
  it. The only command it ever runs is `brew` (update, upgrade, optionally
  `--greedy`/`--cleanup`). Nothing else is downloaded or executed.
- The absolute path to **your** `brew` is baked in, so it runs the same brew you
  use interactively.
- Installing it does **not** run an upgrade immediately (`RunAtLoad` is off);
  it only fires at the scheduled time.

### Install the daily upgrade

```bash
brewjanitor autoupdate --install              # daily at 08:00
brewjanitor autoupdate --install --hour 7 --minute 15   # at 07:15
brewjanitor autoupdate --install --greedy     # also upgrade self-updating casks
brewjanitor autoupdate --install --cleanup     # also run `brew cleanup`
```

After install, read the plist to confirm what it does:
```bash
cat ~/Library/LaunchAgents/com.manny.brewjanitor.autoupdate.plist
```

### Check / stop it

```bash
brewjanitor autoupdate --status    # show schedule + command
brewjanitor autoupdate --remove     # unload and delete the job (no sudo)
```

Logs of each run go to `~/Library/Logs/brewjanitor/autoupdate.log`.

> ⚠️ `--greedy` upgrades casks that auto-update themselves (browsers, VS Code,
> etc.) and may quit a running app to replace it. Omit it for a gentler daily
> upgrade. To protect a specific package from ever being upgraded, pin it:
> `brew pin <name>`.

---

## What each command does, in plain terms

| Command | What it does | Changes your machine? |
| --- | --- | --- |
| `brewjanitor` | Prints a plan | No |
| `brewjanitor --verbose` | Prints a plan + per-app progress | No |
| `brewjanitor --report X.csv` | Prints a plan + writes the can't-replace list to a CSV | No (only writes the CSV you named) |
| `brewjanitor --apply` | Installs via brew, verifies, removes old bundles | **Yes** |
| `brewjanitor --offline` | Skips `brew info` verification (faster, less sure) | Only with `--apply` |
| `brewjanitor --reconcile` | Finds leftover old bundles from an interrupted run | No |
| `brewjanitor --reconcile --apply` | Removes those leftover old bundles | Yes (deletes old bundles) |
| `brewjanitor autoupdate --install` | Schedules a daily `brew upgrade` (user-level launchd) | Yes (writes one plist to ~/Library/LaunchAgents) |
| `brewjanitor autoupdate --remove` | Removes the scheduled job | Yes (deletes that plist) |

---

## How it works (the six pieces)

brewjanitor is built in stages, each safe to run on its own:

1. **Inventory** (`inventory.py`) — finds every `.app` bundle in `/Applications`
   and `~/Applications` (never `/System/Applications`) and reads each bundle's
   `CFBundleIdentifier` from its `Info.plist`. Read-only.
2. **Brew check** (`brewcheck.py`) — labels each app as Homebrew-managed or not,
   using `brew info --json=v2 --installed` to map brew items to the `.app` paths
   they own. Read-only.
3. **Brew search** (`brewsearch.py`) — for each unmanaged app, runs
   `brew search --casks` to decide whether Homebrew *could* install it. The
   `--casks` scope matters: an unscoped `brew search` groups results under
   `==> Formulae` / `==> Casks` headers that brew prints only to a terminal, so
   parsing them from a pipe silently classified every cask as a formula.
   Casks only — a formula never provides a `.app`. A dry run matches by name
   (fast, no `brew info`); verification happens only under `--apply`. Read-only.
4. **Replace** (`replace.py`) — the only step that mutates. Under `--apply` it
   runs `brew install --cask --adopt`, verifies the install, and removes the old
   bundle only if Homebrew installed a separate copy elsewhere. On any failure
   the old bundle is left untouched.
5. **Report** (`report.py`) — writes the couldn't-be-replaced apps to a CSV.
6. **CLI** (`cli.py`) — wires it all together behind the `brewjanitor` command,
   plus an `autoupdate` subcommand (see below) for scheduled upgrades and a
   `formulae` subcommand for the read-only command-line-tool survey.
7. **Binaries** (`binaries.py`) — the formula-side survey. Report-only by
   design: it never installs and never deletes. Read-only.
8. **Streams** (`streams.py`) — ordered writes to stdout and stderr. Results go
   to stdout so `brewjanitor | grep ...` works; progress and summaries go to
   stderr. The two buffer differently, so each write flushes the other stream
   first — otherwise, whenever both land in the same terminal or file, the
   lines arrive out of order (a summary once printed *above* the results it
   summarised).

You can also run any piece directly, e.g.:

```bash
python3 -m brewjanitor.inventory      # just list apps
python3 -m brewjanitor.brewcheck     # which does Homebrew manage
python3 -m brewjanitor.brewsearch    # which could Homebrew install
```

---

## Command-line tools (`brewjanitor formulae`)

```bash
brewjanitor formulae                    # report only; changes nothing
brewjanitor formulae --report tools.csv # also write the list to a CSV
brewjanitor formulae --all              # sweep every directory on PATH
```

Reports which of your manually installed command-line tools Homebrew has a
formula for. **It never installs and never deletes**, and there is no flag that
makes it.

That asymmetry with the app pipeline is deliberate. Replacing an app is safe
because three things line up: an app has a `CFBundleIdentifier` that Homebrew
records, a cask maps to exactly one `.app`, and `--adopt` lets brew take
ownership of the bundle already on disk. None of that holds for formulae:

- A command-line tool has **no identity**. A file named `python3` is just a
  name, and name matching is exactly what produced the `R.app` → `r` false
  match the cask path had to stop making.
- A formula owns **hundreds of files** across `bin/`, `lib/`, `include/` and
  `share/`, so "replace this binary" is not a well-defined operation.
- There is **no `--adopt` for formulae**. Homebrew installs into its own prefix,
  so a "replacement" would leave both copies on disk with `PATH` order silently
  deciding which one runs.

So it reports, and you decide. The output ends with the exact `brew install`
commands, to run yourself if you want them.

### What gets filtered out

A raw listing of `PATH` is almost entirely things that are already managed, so
the filtering is most of the value. Ruled out automatically:

| Rule | Why |
|---|---|
| Already inside Homebrew's prefix | brew manages it already |
| Under `/usr/bin`, `/System`, … | part of macOS |
| Resolves into a `.app` or `.framework` | belongs to a GUI app — that's the cask pipeline's business |
| Under `.pyenv`, `.nvm`, `.cargo`, `conda`, … | a version manager is deliberately managing it |
| Broken symlink | points at nothing |

A non-standard directory holding more than 25 executables is reported as one
line rather than 25 candidates, since that is one installed product (a
scientific suite, a vendored toolchain) rather than that many separate tools.

On the development machine this took a `--all` sweep from 838 raw executables
down to zero false candidates: 1266 macOS files, 473 Homebrew-owned, 52 from a
version manager, 17 app shims, and one 387-binary product tree collapsed to a
single line.

---

## Running on a pre-release macOS

Homebrew prints `We do not provide support for this pre-release version` when
you are on a developer beta. brewjanitor detects this (it asks Homebrew itself,
rather than keeping a version table that would go stale every autumn) and says
so once at the top of a run.

The warning is broader than its actual effect here:

- **Casks are unaffected.** A cask ships a prebuilt `.app`; it does not depend
  on the OS build. `brew search --casks`, `brew info --json=v2` and cask
  installs were all checked on a beta and behave identically, with clean stdout
  and no warning.
- **Formulae are the part that a beta breaks** — they are built per-OS, and
  bottles for a brand-new major version often do not exist yet, so brew warns
  and may build from source. brewjanitor installs no formulae, which is why the
  warning does not apply to it.

Two related hardening measures, useful on a beta and harmless otherwise:

- Read-only calls run with `HOMEBREW_NO_AUTO_UPDATE=1`, so a long scan cannot
  stall partway through on an unannounced `brew update`. Installs keep
  auto-update, since they genuinely want fresh cask metadata.
- Under `--apply`, brew's output is **shown, not swallowed**. Previously both
  streams were captured, so a cask whose installer asks for a password looked
  like a hang — the prompt went into a pipe nobody displayed. stdout and stdin
  are now inherited and stderr is teed, so you see warnings and progress live
  and can still get a parsed reason if something fails.

---

## Tests

The suite is stdlib `unittest` — no dependencies to install, nothing on disk is
touched, and no `brew` is required (the command line is faked, and the removal
step is injected).

```bash
python3 -m unittest discover -s tests -t .
```

The destructive paths carry the most coverage. Every test that exercises
`--apply` or `--reconcile` injects a recording stub in place of the real
remover, so a regression shows up as an unexpected call rather than as a
deleted application.

---

## Safety guarantees

- **Dry-run by default.** No flags = no changes, no files written.
- **Install before delete.** It never deletes an old app until the new one is
  installed and verified.
- **Verify before delete.** The new install must match the old app by bundle id
  or `.app` name; otherwise the old app stays.
- **Path guard.** It will only remove bundles inside `/Applications` or
  `~/Applications` — never elsewhere on disk.
- **Timeouts.** Every `brew` call has a timeout, so a hung network call fails
  gracefully instead of hanging the tool forever.

---

## Troubleshooting

- **"Homebrew manages 0" but you know some apps came from `brew install --cask`:**
  this was a bug in earlier versions (cask paths were matched wrong). Make sure
  you're on the latest `main` (`git pull` + `pip install -e .`). If it still
  shows 0, run this and check the output looks like the documented JSON shape:
  ```bash
  brew info --json=v2 --cask --installed $(brew list --cask -1 | head -1) | python3 -m json.tool
  ```
- **It seems stuck:** add `--verbose` to see per-app progress, and `--offline`
  to skip slow `brew info` network calls.
- **`brew` not found:** install Homebrew from https://brew.sh and ensure it's on
  your `PATH`.
