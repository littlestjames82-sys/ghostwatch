"""GhostWatch core: snapshot, bundle reading, staleness check.

Stdlib only. No network, no subprocess (re-export delegation lives in
cli.py), no execution of project code: Python symbols come from ast
parsing, other languages from a labelled parser-lite line scan.

Key fact this module is built around: a GhostSlice manifest's per-file
sha256 is a hash of the SLICED text, not the source file. So a manifest
alone cannot prove a source file has not changed for sliced modes.
GhostWatch therefore snapshots SOURCE hashes + symbols at bundle time
and diffs later snapshots against that baseline. Without a baseline it
falls back to a labelled mtime heuristic and never claims fresh.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

SNAPSHOT_NAME = "snapshot.json"
WATCH_DIR = ".ghostwatch"

DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".claude_context", ".ghostwatch", ".ghostslice-cache", ".venv", "venv",
    "env", "node_modules", "build", "dist", ".idea", ".vscode", ".tox",
    ".eggs", "htmlcov",
}
DEFAULT_IGNORE_FILES = {".ghostslice-history.jsonl"}

_MAX_SYMBOL_FILE_BYTES = 2_000_000

_PY_SUFFIXES = {".py"}
_LITE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java",
                  ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".kt",
                  ".swift", ".scala", ".sh", ".lua"}

_LITE_PATTERNS = [
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\(|async|function)"),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*[<(]"),
    re.compile(r"^\s*def\s+([A-Za-z_]\w*)\s*[(:]"),
    re.compile(r"^\s*(?:public|private|protected|static|final|\s)+[\w<>\[\]]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{?\s*$"),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- ignore handling (.gitignore-lite) ----------

def _parse_pattern(raw: str):
    """Parse one .gitignore-lite line into (body, anchored, dir_only)
    or None. Git semantics kept: a leading '/' anchors the pattern to
    the project root, a trailing '/' makes it directory-only, and any
    pattern containing a '/' is root-anchored. Negations (!) are
    ignored and '**' is collapsed to '*' (both labelled lite)."""
    line = raw.strip()
    if not line or line.startswith("#") or line.startswith("!"):
        return None
    dir_only = line.endswith("/")
    if dir_only:
        line = line.rstrip("/")
    anchored = line.startswith("/")
    if anchored:
        line = line.lstrip("/")
    body = line.replace("**", "*")
    if not body:
        return None
    if "/" in body:
        anchored = True
    return (body, anchored, dir_only)


def _load_gitignore_lite(root: Path) -> list:
    """Parsed patterns from <root>/.gitignore (see _parse_pattern)."""
    patterns: list = []
    try:
        text = (root / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return patterns
    for line in text.splitlines():
        pat = _parse_pattern(line)
        if pat is not None:
            patterns.append(pat)
    return patterns


def _path_match(candidate: str, body: str) -> bool:
    """Segment-wise match: '*' never crosses a '/' boundary."""
    cand_parts = candidate.split("/")
    pat_parts = body.split("/")
    if len(cand_parts) != len(pat_parts):
        return False
    return all(fnmatch.fnmatchcase(c, p) for c, p in zip(cand_parts, pat_parts))


def _ignored(rel: str, patterns: list) -> bool:
    parts = rel.split("/")
    # Only directory components count for the default ignore dirs: a
    # top-level FILE named e.g. 'build' is a file, not a build dir.
    if any(p in DEFAULT_IGNORE_DIRS for p in parts[:-1]):
        return True
    if parts[-1] in DEFAULT_IGNORE_FILES:
        return True
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for body, anchored, dir_only in patterns:
        if anchored or "/" in body:
            cands = prefixes[:-1] if dir_only else prefixes
            for cand in cands:
                if _path_match(cand, body):
                    return True
                # '**/' collapsed to '*/': also try every suffix, so
                # '*/foo' behaves like git's '**/foo' (matches 'foo'
                # at the root and at any depth).
                if body.startswith("*/"):
                    rest = body[2:]
                    cand_segs = cand.split("/")
                    for k in range(1, len(cand_segs)):
                        if _path_match("/".join(cand_segs[k:]), rest):
                            return True
                    if _path_match(cand, rest):
                        return True
        else:
            comps = parts[:-1] if dir_only else parts
            if any(fnmatch.fnmatchcase(c, body) for c in comps):
                return True
    return False


# ---------- symbols ----------

def extract_symbols(path: Path, data: bytes) -> tuple[list[str], str]:
    """Return (sorted qualified symbol names, method).
    method: 'ast' | 'parser-lite' | 'none'."""
    suffix = path.suffix.lower()
    if len(data) > _MAX_SYMBOL_FILE_BYTES:
        return [], "none"
    if suffix in _PY_SUFFIXES:
        try:
            text = data.decode("utf-8")
            tree = ast.parse(text)
        except (UnicodeDecodeError, SyntaxError, ValueError):
            return [], "none"
        names: list[str] = []

        def walk(node, prefix=""):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    q = f"{prefix}{child.name}" if prefix else child.name
                    names.append(q)
                    walk(child, q + ".")
                else:
                    walk(child, prefix)

        walk(tree)
        return sorted(set(names)), "ast"
    if suffix in _LITE_SUFFIXES:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return [], "none"
        names = []
        for line in text.splitlines():
            for pat in _LITE_PATTERNS:
                m = pat.match(line)
                if m:
                    names.append(m.group(1))
                    break
        return sorted(set(names)), "parser-lite"
    return [], "none"


# ---------- snapshot ----------

def snapshot_project(root: str | Path, bundle: str | Path | None = None,
                     extra_ignore: list[str] | None = None,
                     baseline_files: dict | None = None) -> dict:
    """Snapshot source hashes + symbols.

    baseline_files (a previous snapshot's files object) enables the
    stat pre-filter: a file whose size AND mtime_ns are both unchanged
    since the baseline reuses the baseline sha256/symbols without being
    read or re-hashed. Any stat change falls back to a full read+hash,
    so touched-only vs content-changed is still decided by the hash.
    The plain `snapshot` command never passes a baseline: a baseline
    must always be authoritative. Only `check` reuses, against the
    baseline it is diffing anyway."""
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"project not found or not a directory: {root}")
    patterns = _load_gitignore_lite(root) + [
        pat for pat in (_parse_pattern(x) for x in (extra_ignore or []))
        if pat is not None]
    files: dict[str, dict] = {}
    hashed = 0
    reused = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(root).as_posix()
        if _ignored(rel, patterns):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        base = (baseline_files or {}).get(rel)
        if isinstance(base, dict) and isinstance(base.get("sha256"), str) \
                and base.get("size") == st.st_size \
                and base.get("mtime_ns") == st.st_mtime_ns:
            reused += 1
            files[rel] = {
                "sha256": base["sha256"],
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "symbols": list(base.get("symbols") or []),
                "symbol_method": base.get("symbol_method", "none"),
            }
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        hashed += 1
        symbols, method = extract_symbols(p, data)
        files[rel] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "symbols": symbols,
            "symbol_method": method,
        }
    snap: dict = {
        "tool": "ghostwatch",
        "version": __version__,
        "snapshot_version": 1,
        "created_at": utc_now(),
        "root": str(root.resolve()),
        "ignore": "gitignore-lite" if (root / ".gitignore").exists() else "defaults",
        "files_hashed": hashed,
        "files_reused": reused,
        "bundle": None,
        "files": files,
    }
    bundle_path = resolve_bundle(root, bundle)
    if bundle_path is not None:
        snap["bundle"] = read_bundle_info(bundle_path)
    return snap


def save_snapshot(snap: dict, out: str | Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def _validate_snapshot(data, source: str) -> dict:
    """Structural validation shared by load_snapshot and check_project,
    so a corrupt snapshot is a clean ValueError (exit 2), never an
    AttributeError traceback deep in the diff."""
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ValueError(f"{source}: snapshot is not an object with a 'files' object")
    for rel, entry in data["files"].items():
        if not isinstance(entry, dict):
            raise ValueError(f"{source}: snapshot entry for {rel!r} is not an object")
        if "symbols" in entry and entry["symbols"] is not None \
                and not isinstance(entry["symbols"], list):
            raise ValueError(f"{source}: snapshot entry for {rel!r} has a non-list 'symbols'")
        if "sha256" in entry and entry["sha256"] is not None \
                and not isinstance(entry["sha256"], str):
            raise ValueError(f"{source}: snapshot entry for {rel!r} has a non-string 'sha256'")
    bundle = data.get("bundle")
    if bundle is not None and not isinstance(bundle, dict):
        raise ValueError(f"{source}: snapshot 'bundle' is not an object")
    return data


def load_snapshot(path: str | Path) -> dict:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"{path}: cannot read snapshot ({exc})") from exc
    except ValueError as exc:
        raise ValueError(f"{path}: invalid snapshot JSON ({exc})") from exc
    return _validate_snapshot(data, str(path))


def default_snapshot_path(root: str | Path) -> Path:
    return Path(root) / WATCH_DIR / SNAPSHOT_NAME


# ---------- bundles / packs ----------

def resolve_bundle(root: str | Path, bundle: str | Path | None) -> Path | None:
    """Explicit bundle wins; else <root>/.claude_context if it has a manifest."""
    if bundle is not None:
        return Path(bundle)
    cand = Path(root) / ".claude_context"
    return cand if (cand / "manifest.json").exists() else None


def read_bundle_info(bundle: str | Path) -> dict:
    """Read a GhostSlice bundle (or Pack carrying a bundle) manifest.
    Raises ValueError with a clear message on any problem."""
    bundle = Path(bundle)
    manifest_path = bundle / "manifest.json"
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{manifest_path}: cannot read manifest ({exc})") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{manifest_path}: invalid manifest JSON ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise ValueError(f"{manifest_path}: manifest is not an object with a 'files' list")
    entries = []
    for f in data["files"]:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            continue
        entries.append({
            "path": f["path"],
            "mode": f.get("mode") if isinstance(f.get("mode"), str) else "",
            "language": f.get("language") if isinstance(f.get("language"), str) else "",
            "sha256_sliced": f.get("sha256") if isinstance(f.get("sha256"), str) else None,
            "symbols_included": list(f["symbols_included"]) if isinstance(f.get("symbols_included"), list) else [],
        })
    info = {
        "path": str(bundle),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "generated_at": data.get("generated_at") if isinstance(data.get("generated_at"), str) else None,
        "tool": data.get("tool"),
        "version": data.get("version"),
        "files": entries,
    }
    try:
        info["manifest_mtime_ns"] = manifest_path.stat().st_mtime_ns
    except OSError:
        info["manifest_mtime_ns"] = None
    return info


# ---------- check ----------

def _parse_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def check_project(root: str | Path, bundle: str | Path | None = None,
                  snapshot: dict | None = None,
                  strict_added: bool = False) -> dict:
    """Return the watch report dict. Verdict: fresh|stale|unknown."""
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"project not found or not a directory: {root}")
    if snapshot is not None:
        snapshot = _validate_snapshot(snapshot, "<snapshot>")
    bundle_path = resolve_bundle(root, bundle)
    if bundle_path is None:
        raise ValueError("no bundle found (no --bundle given and no .claude_context/manifest.json)")
    info = read_bundle_info(bundle_path)
    bundle_files = {e["path"]: e for e in info["files"]}
    current = snapshot_project(
        root, bundle=bundle_path,
        baseline_files=(snapshot.get("files") if snapshot is not None else None))

    findings: dict[str, list] = {
        "changed_in_bundle": [], "changed_not_in_bundle": [],
        "added_not_in_bundle": [], "removed_from_project": [],
        "missing_bundle_file": [], "stale_symbols": [],
        "touched_only": [],
    }
    stale = False
    baseline = "none"

    if snapshot is not None:
        baseline = "snapshot"
        base_files: dict = snapshot.get("files") or {}
        # bundle replaced since snapshot?
        base_bundle = snapshot.get("bundle") or {}
        bundle_replaced = bool(base_bundle.get("manifest_sha256")
                               and base_bundle["manifest_sha256"] != info["manifest_sha256"])
        for rel in sorted(set(base_files) | set(current["files"])):
            in_base, in_cur = rel in base_files, rel in current["files"]
            in_bundle = rel in bundle_files
            if in_base and not in_cur:
                (findings["removed_from_project"] if in_bundle else findings["changed_not_in_bundle"]).append(
                    {"path": rel, "reason": "deleted since snapshot"})
                if in_bundle:
                    stale = True
                continue
            if in_cur and not in_base:
                (findings["added_not_in_bundle"]).append({"path": rel, "reason": "added since snapshot"})
                if strict_added and in_bundle:
                    stale = True
                continue
            b, c = base_files[rel], current["files"][rel]
            if b.get("sha256") != c.get("sha256"):
                (findings["changed_in_bundle"] if in_bundle else findings["changed_not_in_bundle"]).append(
                    {"path": rel, "reason": "content changed since snapshot"})
                if in_bundle:
                    stale = True
            elif b.get("mtime_ns") != c.get("mtime_ns"):
                findings["touched_only"].append({"path": rel, "reason": "mtime moved, content identical"})
            # symbols
            removed_syms = sorted(set(b.get("symbols") or []) - set(c.get("symbols") or []))
            added_syms = sorted(set(c.get("symbols") or []) - set(b.get("symbols") or []))
            if (removed_syms or added_syms) and in_bundle:
                findings["stale_symbols"].append({
                    "path": rel, "removed": removed_syms, "added": added_syms,
                    "method": c.get("symbol_method", "none")})
                if removed_syms:
                    stale = True
        if bundle_replaced:
            stale = True
            findings["changed_in_bundle"].append(
                {"path": "manifest.json", "reason": "bundle manifest changed since snapshot"})
    else:
        baseline = "manifest_heuristic"
        gen_ts = _parse_ts(info.get("generated_at"))
        ref_ns = None
        if gen_ts is not None:
            ref_ns = int(gen_ts * 1_000_000_000)
        elif info.get("manifest_mtime_ns"):
            ref_ns = info["manifest_mtime_ns"]
        for rel, entry in sorted(bundle_files.items()):
            cur = current["files"].get(rel)
            if cur is None:
                findings["missing_bundle_file"].append({"path": rel, "reason": "in bundle, missing on disk"})
                stale = True
                continue
            if ref_ns is not None and cur["mtime_ns"] > ref_ns:
                findings["changed_in_bundle"].append(
                    {"path": rel, "reason": "source mtime newer than bundle (heuristic, no snapshot baseline)"})
            inc = entry.get("symbols_included") or []
            if inc:
                missing = sorted(set(inc) - set(cur["symbols"]))
                if missing:
                    findings["stale_symbols"].append({
                        "path": rel, "removed": missing, "added": [],
                        "method": cur.get("symbol_method", "none")})
                    stale = True

    if baseline == "snapshot":
        verdict = "stale" if stale else "fresh"
    else:
        verdict = "stale" if stale else "unknown"

    return {
        "tool": "ghostwatch", "version": __version__, "checked_at": utc_now(),
        "project": str(root.resolve()), "bundle": info["path"],
        "bundle_manifest_sha256": info["manifest_sha256"],
        "bundle_generated_at": info["generated_at"],
        "baseline": baseline,
        "verdict": verdict,
        "stale": verdict == "stale",
        "pack_stale": verdict == "stale",
        "strict_added": strict_added,
        "files_hashed": current["files_hashed"],
        "files_reused": current.get("files_reused", 0),
        "bundle_file_count": len(bundle_files),
        "findings": findings,
        "reexport": None,
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# GhostWatch report", "",
        f"- Verdict: **{report['verdict']}** (pack_stale: {report['pack_stale']})",
        f"- Project: `{report['project']}`",
        f"- Bundle: `{report['bundle']}`",
        f"- Bundle manifest sha256: `{report['bundle_manifest_sha256']}`",
        f"- Bundle generated at: {report['bundle_generated_at']}",
        f"- Baseline: {report['baseline']}",
        f"- Checked at: {report['checked_at']}",
        f"- Files hashed this check: {report['files_hashed']}",
        f"- Files reused from baseline (size + mtime unchanged): {report.get('files_reused', 0)}",
        "",
    ]
    titles = {
        "changed_in_bundle": "Changed since baseline, in bundle (stale-making)",
        "missing_bundle_file": "In bundle, missing on disk (stale-making)",
        "removed_from_project": "Removed from project, was in bundle (stale-making)",
        "stale_symbols": "Stale symbols in bundled files",
        "changed_not_in_bundle": "Changed, not in bundle (context only)",
        "added_not_in_bundle": "Added, not in bundle (context only)",
        "touched_only": "Touched only, content identical (not stale)",
    }
    for key, title in titles.items():
        items = report["findings"].get(key) or []
        lines.append(f"## {title} ({len(items)})")
        for it in items:
            if key == "stale_symbols":
                lines.append(f"- `{it['path']}` removed: {it['removed'] or '-'} added: {it['added'] or '-'} (via {it['method']})")
            else:
                lines.append(f"- `{it['path']}`: {it['reason']}")
        lines.append("")
    if report.get("reexport"):
        lines.append(f"## Re-export\n\n{json.dumps(report['reexport'], indent=2)}\n")
    return "\n".join(lines)


def exit_code_for(report: dict) -> int:
    return {"fresh": 0, "stale": 1, "unknown": 2}[report["verdict"]]
