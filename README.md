# brewjanitor

Tool that brings the software on your Mac into the fold of Homebrew. It scans
installed apps, checks which Homebrew already manages, and replaces the rest
with brew-managed installs where possible — leaving the rest in a report.

**Safe by default.** With no flags, `brewjanitor` only inspects and prints a
plan. An explicit `--apply` is required to actually install or delete anything,
and the install-before-delete order is mandatory: it installs via Homebrew,
verifies the install, and only then removes the old bundle.

## Install

```bash
git clone https://github.com/manny222manny/brewjanitor
cd brewjanitor
python3 -m pip install -e .
```

Requires Python 3.9+ and [Homebrew](https://brew.sh).

## Usage

```bash
brewjanitor                      # dry-run: print the plan, change nothing
brewjanitor --apply              # install via brew, verify, remove old bundles
brewjanitor --report PATH.csv    # write the couldn't-be-replaced apps to PATH
```

## How it works (piece by piece)

1. **Inventory** — `inventory.py`: finds every `.app` bundle in `/Applications`
   and `~/Applications` (never `/System/Applications`), reading each bundle's
   `CFBundleIdentifier` from `Info.plist`. Read-only.
2. **Brew check** — `brewcheck.py`: labels each app as Homebrew-managed or not,
   using `brew list --formula`/`--cask` and `brew info --json=v2 --installed` to
   map brew items to the `.app` paths they own. Read-only.
3. **Brew search** — `brewsearch.py`: for each unmanaged app, runs `brew search`
   and `brew info` to decide whether Homebrew *could* install it, with a
   `verified` flag when the candidate matches the app by bundle id or `.app`
   path. Read-only.
4. **Replace** — `replace.py`: the only step that mutates. Under `--apply` it
   installs the brew item, verifies the install placed a matching app, then
   removes the old bundle. On any failure the old bundle is left untouched.
   Refuses to remove anything outside the scan directories.
5. **Report** — `report.py`: writes the couldn't-be-replaced apps to a CSV
   (`name, path, bundle_id, brew_name, brew_kind, reason`).
6. **CLI** — `cli.py`: wires the above together behind the `brewjanitor` command
   with `--apply` and `--report`.

Each piece is also runnable on its own, e.g. `python -m brewjanitor.inventory`.
