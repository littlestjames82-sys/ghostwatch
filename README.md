# GhostWatch

**Is that saved GhostSlice bundle still trustworthy, yes or no?**

A GhostSlice bundle is a snapshot in time. The project keeps moving:
a bundled file is edited, a symbol the bundle shipped is renamed or
deleted, a bundled file disappears. `ghostslice check` still passes,
because it only proves the bundle on disk matches its own manifest.
It says nothing about whether the bundle still matches the *project*.

GhostWatch closes that gap. At export time it snapshots the project's
source hashes and symbol outlines. Later, `check` diffs the live
project against that baseline and gives a verdict by file and by
symbol: `fresh`, `stale`, or `unknown`.

By Ghost Developer Studio. Working name, Wave 3 companion #20 to
GhostSlice. Full plan: [PLAN.md](PLAN.md).

## Status

v0.1 built and verified (Oct 14, 2026; 41 tests at build), with a
polish round the same day (see [POLISH.md](POLISH.md)). Nothing is
published, pushed, or priced, and no paid layer is decided.

## Not GhostSlice `watch`

GhostSlice already has a `watch` command. That is a live session
tool: it polls one project you have open, streams token deltas, and
optionally re-exports that project's bundle.

GhostWatch is different in the ways that matter:

- It checks **saved artifacts**: an exported bundle, a
  `.claude_context` folder, or a GhostPacks Pack, possibly weeks old.
- It answers one question with a verdict tied to a bundle manifest
  hash, not a live token stream.
- It is built to run across projects: CI, a nightly sweep, a
  pre-flight step before reusing an old Pack on a new machine.
- Its baseline is source hashes + symbols stored next to the bundle
  in `.ghostwatch/snapshot.json`. A bundle manifest alone cannot be
  the baseline: its per-file `sha256` is a hash of the *sliced* text,
  not of the source file, so it can never prove the source has not
  moved for non-`full` modes.

If a future Slice release folds a bundle verdict into its own
watcher, GhostWatch should be retired in favour of it.

## Hard rules (v0.1)

- **Local only, stdlib only, no network.** No daemon, no service, no
  dashboard, no telemetry, no accounts.
- **Hashes decide, not mtimes.** An mtime-only `touch` with identical
  bytes is reported as touched, not stale.
- **Never confident without a baseline.** With no snapshot, Watch
  falls back to a clearly labelled mtime heuristic against the
  manifest. That path can report `stale` on a definite finding
  (a bundled file missing on disk, a shipped symbol gone), but
  otherwise the verdict is `unknown`, never a confident `fresh`.
  The report always states which baseline was used.
- **Watch never slices.** Re-export is opt-in (`--reexport`) and is
  delegated to the GhostSlice CLI as a subprocess. If GhostSlice is
  not available, Watch reports `reexport: unavailable` with the
  reason, keeps the stale verdict, and exits 1. No traceback, no
  fake re-export, no built-in fallback slicer.
- **Watch never edits project source.** The only writes are the
  snapshot file (on `snapshot`, or after a successful opt-in
  re-export) and the report files at the paths you choose.
- **No code execution.** Watch does not load Slice plugins, does not
  execute project or Pack code, and stores hashes and symbol names
  only, never file bodies, in its snapshot.

## Install / run (from this checkout)

No dependencies beyond Python 3.10+.

```bash
export PYTHONPATH=src
python -m ghostwatch --help
# or, once installed: ghostwatch ...
```

## Usage

### 1. Snapshot at bundle time

```bash
ghostwatch snapshot <project> [--bundle <path>] [-o <snapshot.json>]
```

Walks the project (default ignore dirs such as `.git`,
`__pycache__`, `.claude_context`, `.ghostwatch`, `node_modules`,
build/dist dirs, plus a lightweight `.gitignore` reader: plain
names, directory prefixes, and `*` globs, with leading-`/`
root-anchored and trailing-`/` directory-only patterns honoured
and `*` never crossing a directory boundary; negations are
ignored and `**` is collapsed to `*`, labelled lite in the
snapshot). For each visible
file it records relative path, size, `mtime_ns`, a sha256 of the
source bytes, and a symbol outline:

- Python: `ast`, top-level and nested `def` / `async def` / `class`,
  qualified names.
- Other text files: a labelled parser-lite line scan.
- Binary or unreadable files: hash/size only, symbols empty,
  labelled as such.

If a bundle is given or found (default
`<project>/.claude_context`), the snapshot also records the bundle
path, the sha256 of its `manifest.json` bytes, its `generated_at`,
and its file list, so a later check can also detect the bundle
itself being replaced.

Default snapshot location: `<project>/.ghostwatch/snapshot.json`
(see `ghostwatch snapshot --help` for the exact flags Agent A's CLI
ships; the tests in `tests/conftest.py` are the executable record).

### 2. Check once

```bash
ghostwatch check <project> [--bundle <path>] [--snapshot <path>] [--json]
ghostwatch check <project> --strict-added
ghostwatch check <project> --reexport
```

Re-snapshots the project in memory and diffs against the baseline.
Every finding is classified:

| Class | Meaning | Makes the bundle stale? |
|---|---|---|
| `changed_in_bundle` | A bundled file's content changed | Yes |
| `changed_not_in_bundle` | An unbundled file changed | No, context only |
| `added_not_in_bundle` | A new file the bundle never included | No, unless `--strict-added` |
| `removed_from_project` | A file present at snapshot is gone | Yes if it was bundled |
| `missing_bundle_file` | A file named in the manifest is not on disk | Yes |
| `stale_symbols` | A symbol the bundle shipped was removed/renamed | Yes |

Symbol staleness compares baseline symbols (or the manifest's
`symbols_included`) with current symbols per bundled file and
reports added / removed symbols by file.

Pack staleness: if the checked path is a GhostPacks Pack that
carries or points at a bundle, the same verdict is reported as the
Pack verdict, so a saved Pack and a plain bundle get one consistent
answer.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | `fresh` |
| 1 | `stale` |
| 2 | `unknown` / could not determine (no bundle, unreadable manifest, missing project, no baseline and no manifest timestamp) |

Reports: pass `--json-out <watch-report.json>` and/or
`--md-out <WATCH.md>` to `check` (or to `watch` together with
`--write-report`) to write the machine-readable report (for CI and
GhostBench) and the human report at caller-chosen paths; `--json`
prints the full JSON report to stdout instead. Every verdict is
reproducible from `watch-report.json` + snapshot + bundle manifest
hash alone.

### 3. Polling watch (convenience loop, no daemon)

```bash
ghostwatch watch <project> [--bundle <path>] [--interval 2] [--max-polls N] [--reexport]
```

A plain stdlib polling loop over `check`: snapshot, sleep
`interval` seconds, re-check, print each settled verdict change. A
change must be seen in two consecutive polls before it is reported,
so a mid-save read does not flap the verdict. `--max-polls` bounds a
run for scripts and tests; without it the loop runs until Ctrl-C,
which exits cleanly with a session summary (exit 0 if no stale
verdict was ever reported, else 1).

There is no requirement to run `watch` at all. `snapshot` + `check`
are the v0.1 product; `watch` is a convenience loop over `check`.

## A bundle and a snapshot

```
my-project/
  src/app.py
  src/util.py
  .claude_context/          a GhostSlice bundle
    manifest.json
    PROMPT.md
    files/...
  .ghostwatch/
    snapshot.json           GhostWatch baseline (hashes + symbols only)
```

Snapshot shape (versioned JSON, abbreviated):

```json
{
  "tool": "ghostwatch",
  "version": "0.1.0",
  "snapshot_version": 1,
  "created_at": "2026-10-14T00:00:00+00:00",
  "root": "/path/to/my-project",
  "ignore": "defaults",
  "files_hashed": 2,
  "files_reused": 0,
  "bundle": {
    "path": ".../.claude_context",
    "manifest_sha256": "...",
    "generated_at": "2026-10-14T00:00:00+00:00",
    "files": [{"path": "src/app.py", "mode": "full", "sha256_sliced": "...", "symbols_included": []}]
  },
  "files": {
    "src/app.py": {"sha256": "...", "size": 212, "mtime_ns": 0, "symbols": ["compute_total"], "symbol_method": "ast"}
  }
}
```

`files` is an object keyed by relative path. See
`tests/conftest.py` for the shapes the test suite pins down.

## Out of scope for v0.1

- A daemon, system service, OS file notifications, or a dashboard.
- Slicing or editing any project or bundle file. Re-export is
  delegated to GhostSlice, opt-in, or it does not happen.
- Judging whether a bundle is sufficient for a task (GhostVerify)
  or whether a patch is correct.
- Symbol analysis beyond outline level: no call graph, no type
  checking, no cross-file reachability.
- Cloud, accounts, telemetry, paid layer, network access.

## Honest notes

- Parser-lite limits: non-Python symbol outlines are line-pattern
  level. Reports label the per-file extraction method (`ast` vs
  `parser-lite` vs `none`) so a reader can judge confidence.
- Polling cost: `check` reuses the baseline hash for files whose
  size AND `mtime_ns` are both unchanged (the report counts them
  as `files_reused`) and only re-hashes files whose stat moved, so
  an unchanged tree costs a walk + stats, not a full re-read. The
  `snapshot` command always hashes everything: a baseline must be
  authoritative. Trade-off, stated plainly: a content change that
  preserves both size and `mtime_ns` exactly would be missed by
  the reuse path (the same trade-off git's index makes); any stat
  change at all forces a full hash, which is what keeps
  touched-only files correctly non-stale. Keep
  constantly-changing build outputs out with ignore rules.
- `.gitignore`-lite is not full gitignore semantics, and the
  snapshot records that the lite reader was used.

## Tests

```bash
PYTHONPATH=src python3 -m pytest tests -q
```

See [REPORT_B.md](REPORT_B.md) for the test inventory and results,
and [examples/](examples/) for a runnable fixture walkthrough.
