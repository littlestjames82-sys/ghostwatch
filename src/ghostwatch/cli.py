"""GhostWatch CLI: snapshot, check, watch. Stdlib only, local only.

Re-export delegation: on a stale verdict with --reexport, GhostWatch
calls the GhostSlice CLI as a subprocess if available (ghostslice on
PATH, else python -m ghostslice). If unavailable or failing, it reports
that plainly, keeps the stale verdict, and exits 1. It never slices
anything itself.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .core import (check_project, default_snapshot_path, exit_code_for,
                   load_snapshot, render_markdown, save_snapshot,
                   snapshot_project)


def _find_snapshot(root: Path, explicit: str | None) -> tuple[dict | None, Path | None]:
    if explicit:
        p = Path(explicit)
        return load_snapshot(p), p
    p = default_snapshot_path(root)
    if p.exists():
        return load_snapshot(p), p
    return None, None


def try_reexport(root: Path, bundle: Path) -> dict:
    """Attempt one GhostSlice re-export. Never raises."""
    exe = shutil.which("ghostslice")
    if exe:
        cmd = [exe, "export", str(root), "-o", str(bundle)]
    else:
        # Probe the module without side effects.
        try:
            probe = subprocess.run([sys.executable, "-c", "import ghostslice"],
                                   capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"status": "unavailable", "reason": f"ghostslice probe failed: {exc}"}
        if probe.returncode != 0:
            return {"status": "unavailable",
                    "reason": "ghostslice CLI not on PATH and ghostslice module not importable"}
        cmd = [sys.executable, "-m", "ghostslice", "export", str(root), "-o", str(bundle)]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "command": cmd, "reason": str(exc)}
    if run.returncode != 0:
        return {"status": "failed", "command": cmd, "returncode": run.returncode,
                "reason": (run.stderr or run.stdout or "").strip()[-500:]}
    return {"status": "ok", "command": cmd}


def _write_reports(report: dict, out_json: str | None, out_md: str | None) -> None:
    if out_json:
        Path(out_json).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if out_md:
        Path(out_md).write_text(render_markdown(report), encoding="utf-8")


def _print_summary(report: dict, flush: bool = False) -> None:
    f = report["findings"]
    print(f"[ghostwatch] verdict: {report['verdict']} (baseline: {report['baseline']})", flush=flush)
    for key in ("changed_in_bundle", "missing_bundle_file", "removed_from_project",
                "stale_symbols", "changed_not_in_bundle", "added_not_in_bundle", "touched_only"):
        if f.get(key):
            print(f"  {key}: {len(f[key])}", flush=flush)
            for it in f[key][:20]:
                label = it["path"]
                detail = it.get("reason") or (f"removed symbols: {it.get('removed')}" if it.get("removed") else "symbols changed")
                print(f"    - {label}: {detail}", flush=flush)
    if report.get("reexport"):
        print(f"  reexport: {report['reexport'].get('status')} {report['reexport'].get('reason', '')}".rstrip(), flush=flush)


def cmd_snapshot(args) -> int:
    root = Path(args.project)
    try:
        snap = snapshot_project(root, bundle=args.bundle)
    except ValueError as exc:
        print(f"ghostwatch snapshot: {exc}", file=sys.stderr)
        return 2
    out = Path(args.output) if args.output else default_snapshot_path(root)
    save_snapshot(snap, out)
    print(f"[ghostwatch] snapshot: {len(snap['files'])} file(s) hashed -> {out}")
    if snap.get("bundle"):
        print(f"  bundle: {snap['bundle']['path']} (manifest {snap['bundle']['manifest_sha256'][:12]}...)")
    else:
        print("  bundle: none found (snapshot is project-only)")
    return 0


def _run_check(args, root: Path) -> tuple[dict | None, int]:
    try:
        snap, _ = _find_snapshot(root, args.snapshot)
        report = check_project(root, bundle=args.bundle, snapshot=snap,
                               strict_added=args.strict_added)
    except ValueError as exc:
        print(f"ghostwatch check: {exc}", file=sys.stderr)
        return None, 2
    if args.reexport and report["verdict"] == "stale":
        bundle_path = Path(report["bundle"])
        result = try_reexport(root, bundle_path)
        report["reexport"] = result
        if result["status"] == "ok":
            # Fresh baseline for the new bundle; verdict stays stale for
            # THIS run (the bundle the caller asked about was stale).
            try:
                save_snapshot(snapshot_project(root, bundle=bundle_path),
                              default_snapshot_path(root))
                report["reexport"]["snapshot_refreshed"] = True
            except ValueError:
                report["reexport"]["snapshot_refreshed"] = False
    return report, exit_code_for(report)


def cmd_check(args) -> int:
    root = Path(args.project)
    report, code = _run_check(args, root)
    if report is None:
        return code
    _write_reports(report,
                   args.json_out if args.json_out else None,
                   args.md_out if args.md_out else None)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_summary(report)
    return code


def cmd_watch(args) -> int:
    root = Path(args.project)
    if not root.is_dir():
        print(f"ghostwatch watch: project not found: {root}", file=sys.stderr)
        return 2
    print(f"[ghostwatch] watching {root} every {args.interval:g}s "
          f"(two consecutive identical verdicts required; Ctrl-C to stop)", flush=True)
    ever_stale = False
    last_key: str | None = None
    pending_key: str | None = None
    polls = 0
    try:
        while True:
            polls += 1
            report, _code = _run_check(args, root)
            if report is None:
                return 2
            key = json.dumps([report["verdict"], report["findings"]], sort_keys=True)
            if key == pending_key and key != last_key:
                last_key = key
                _print_summary(report, flush=True)
                if args.write_report:
                    _write_reports(report, args.json_out, args.md_out)
            pending_key = key
            if report["verdict"] == "stale":
                ever_stale = True
            if args.max_polls is not None and polls >= args.max_polls:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    print(f"[ghostwatch] stopped: {polls} poll(s), stale seen: {ever_stale}", flush=True)
    return 1 if ever_stale else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ghostwatch",
                                description="Standalone staleness checker for saved GhostSlice bundles and Packs")
    p.add_argument("--version", action="version", version=f"ghostwatch {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, need_bundle=False):
        sp.add_argument("project")
        sp.add_argument("--bundle", default=None, help="bundle/Pack dir (default: <project>/.claude_context)")
        sp.add_argument("--snapshot", default=None, help="snapshot file (default: <project>/.ghostwatch/snapshot.json)")

    sp = sub.add_parser("snapshot", help="snapshot project source hashes + symbols (at bundle time)")
    common(sp)
    sp.add_argument("-o", "--output", default=None, help="snapshot output path")
    sp.set_defaults(fn=cmd_snapshot)

    sp = sub.add_parser("check", help="check once: is the saved bundle/Pack still fresh?")
    common(sp)
    sp.add_argument("--strict-added", action="store_true", help="treat added files that are in the bundle list as stale-making")
    sp.add_argument("--reexport", action="store_true", help="on stale, try one GhostSlice re-export (fails gracefully if unavailable)")
    sp.add_argument("--json", action="store_true", help="print the full JSON report")
    sp.add_argument("--write-report", action="store_true", help="(watch mode helper) write report files")
    sp.add_argument("--json-out", default=None)
    sp.add_argument("--md-out", default=None)
    sp.set_defaults(fn=cmd_check)

    sp = sub.add_parser("watch", help="simple polling loop over check (no daemon)")
    common(sp)
    sp.add_argument("--interval", type=float, default=2.0)
    sp.add_argument("--max-polls", type=int, default=None)
    sp.add_argument("--strict-added", action="store_true")
    sp.add_argument("--reexport", action="store_true")
    sp.add_argument("--write-report", action="store_true")
    sp.add_argument("--json-out", default=None)
    sp.add_argument("--md-out", default=None)
    sp.set_defaults(fn=cmd_watch)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
