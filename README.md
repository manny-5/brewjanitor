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

1. **install** it via Homebrew,
2. **verify** the install actually placed a matching app (by bundle id or
   `.app` name),
3. **only then** remove the old bundle.

If an install or a verification fails, the old app is **left untouched**. It
also refuses to remove anything outside `/Applications` or `~/Applications`.

> ⚠️ `--apply` changes your system (runs `brew install` and deletes old `.app`
> bundles). Read the dry-run output first.

### Fast check (no network)

The default run matches casks by name, which is fast. Under `--apply`,
brewjanitor verifies each match with a `brew info` call (which can hit the
network). To skip that verification even under `--apply`:

```bash
brewjanitor --offline --apply
```

`--offline` is faster but every candidate is `unverified`, so use it only if
you trust the name matches.

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
3. **Brew search** (`brewsearch.py`) — for each unmanaged app, runs `brew search`
   to decide whether Homebrew *could* install it. A dry run matches by name
   (fast, no `brew info`); verification happens only under `--apply`. Read-only.
4. **Replace** (`replace.py`) — the only step that mutates. Under `--apply` it
   installs the brew item, verifies the install, then removes the old bundle. On
   any failure the old bundle is left untouched.
5. **Report** (`report.py`) — writes the couldn't-be-replaced apps to a CSV.
6. **CLI** (`cli.py`) — wires it all together behind the `brewjanitor` command,
   plus an `autoupdate` subcommand (see below) for scheduled upgrades.

You can also run any piece directly, e.g.:

```bash
python3 -m brewjanitor.inventory      # just list apps
python3 -m brewjanitor.brewcheck     # which does Homebrew manage
python3 -m brewjanitor.brewsearch    # which could Homebrew install
```

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
