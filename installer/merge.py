#!/usr/bin/env python3
"""
LeRoy — merge.py
================
Safe, backup-first, ADDITIVE merge of the LeRoy overlay (`core/`) into the
user's Claude Code config directory (`~/.claude`).

Two ironclad rules:
  1. NEVER overwrite the user's own work. We back the whole `~/.claude` tree up
     first, then use the overlay manifest (`.leroy-overlay.json`) to tell files
     the user edited apart from files that are simply out of date: untouched
     files are UPDATED, edited files are kept and the new version is written
     beside them as `<name>.leroy-new`. `settings.json` is MERGED key-by-key
     (hooks appended, never replaced) and is never overwritten.
  2. Everything is reversible. The backup lives at
     `~/.claude.backup-<YYYY-MM-DD-HHMMSS>/` and `leroy reset` restores it.

Re-running the installer is therefore a real UPDATE, not just an add: fixes to
existing files land, and files dropped from LeRoy are removed (only when the
user never touched them).

Usage:
    python installer/merge.py                 # install or update
    python installer/merge.py --dry-run       # show what WOULD happen, touch nothing
    python installer/merge.py --keep-local    # first upgrade: keep your versions
    python installer/merge.py --source PATH   # override the core/ source dir
    python installer/merge.py --dest PATH     # override ~/.claude (testing)

The dry-run prints a plan: which files are new (copied), which already exist
(skipped), and how settings.json would be merged — without writing anything.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

# See installer/doctor.py for why this exists: a glyph-probe fallback alone
# can miss a non-ASCII character that only appears later in the file,
# crashing print() on a strict cp1252 console. Reconfiguring is the
# whole-class fix.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(errors="replace")

# --- pretty marks (ASCII fallback) ------------------------------------------
_UTF_OK = True
try:
    "✅＋≈↷".encode(sys.stdout.encoding or "utf-8")
except (UnicodeEncodeError, LookupError):
    _UTF_OK = False

NEW = "＋" if _UTF_OK else "[new] "
SKIP = "·" if _UTF_OK else "[skip]"
MERGE = "≈" if _UTF_OK else "[merge]"
OK = "✅" if _UTF_OK else "[ OK ]"
UPD = "↑" if _UTF_OK else "[upd] "
CONF = "!" if _UTF_OK else "[keep]"
DEL = "−" if _UTF_OK else "[del] "


def default_source() -> Path:
    """core/ sits next to installer/ in the repo."""
    return (Path(__file__).resolve().parent.parent / "core").resolve()


def default_dest() -> Path:
    """The live Claude Code config dir — always derived from HOME, never hardcoded."""
    return (Path.home() / ".claude").resolve()


# --- the overlay manifest ---------------------------------------------------
# WHY THIS EXISTS
#     Before it, this merger was additive-only: `if target.exists(): skip`. That
#     made re-running the installer useless for anyone who already had LeRoy —
#     new files landed, but every FIXED file was skipped and every REMOVED file
#     stayed, leaving mixed-version installs.
#     The manifest records the hash of each file LeRoy installed, so on the next
#     run we can tell "the user edited this" apart from "this is just old" — and
#     update only what is genuinely untouched.
MANIFEST_NAME = ".leroy-overlay.json"
MANIFEST_VERSION = 1

# Never updated or removed by an upgrade, whatever the manifest says. These are
# the user's own identity/config surfaces (settings.json is handled separately
# by the key-by-key merger below).
PROTECTED = {"settings.json", "SOUL.md", "USER.md", "MEMORY.md"}

# Recorded instead of a real hash when we leave a user-modified file in place.
# Never equals a genuine digest, so the file stays flagged as theirs forever.
USER_OWNED = "user-modified"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_path(dest: Path) -> Path:
    return dest / MANIFEST_NAME


def load_manifest(dest: Path) -> dict:
    """Return {relative-posix-path: hash}. Missing/corrupt manifest -> {}."""
    p = manifest_path(dest)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}  # unreadable manifest is treated as "no baseline", never fatal
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def write_manifest(dest: Path, files: dict) -> None:
    payload = {
        "version": MANIFEST_VERSION,
        "updated": _dt.datetime.now().isoformat(timespec="seconds"),
        "note": (
            "Hashes of the files LeRoy installed. This is how the installer tells "
            "YOUR edits apart from an out-of-date file, so re-running it can update "
            "safely. Delete this only if you want the next update to treat every "
            "file as yours and touch nothing."
        ),
        "files": dict(sorted(files.items())),
    }
    manifest_path(dest).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# --- settings.json merge ----------------------------------------------------
def merge_settings(user: dict, overlay: dict) -> dict:
    """
    Additive deep merge that NEVER clobbers a user value.

    - Scalars/lists the user already set win (overlay is ignored for them).
    - Keys the user doesn't have are taken from the overlay.
    - `hooks` is special: it's a dict of event -> list; we append overlay hook
      entries that the user doesn't already have (dedup by JSON identity).
    - Nested dicts recurse with the same rules.
    """
    result = dict(user)  # start from the user's settings — they always win
    for key, ov_val in overlay.items():
        if key == "hooks" and isinstance(ov_val, dict):
            result["hooks"] = _merge_hooks(user.get("hooks", {}), ov_val)
        elif key not in result:
            result[key] = ov_val
        elif isinstance(result[key], dict) and isinstance(ov_val, dict):
            result[key] = merge_settings(result[key], ov_val)
        # else: user already has a non-dict value here -> keep it, drop overlay
    return result


def _merge_hooks(user_hooks: dict, overlay_hooks: dict) -> dict:
    """Append overlay hooks additively, deduping by serialized identity."""
    merged = {k: list(v) for k, v in user_hooks.items()}
    for event, entries in overlay_hooks.items():
        existing = merged.setdefault(event, [])
        seen = {json.dumps(e, sort_keys=True) for e in existing}
        for entry in entries:
            sig = json.dumps(entry, sort_keys=True)
            if sig not in seen:
                existing.append(entry)
                seen.add(sig)
    return merged


# --- hook wiring (settings.hooks.json -> settings.json) ---------------------
def hooks_overlay_path(source: Path) -> Path:
    """The overlay's hook-wiring file, which ships next to the hook scripts."""
    return source / "hooks" / "settings.hooks.json"


def rewrite_hook_command(cmd: str, interpreter: str | None, claude_home: Path) -> str:
    """
    Make a shipped hook command runnable on THIS machine:
      * `python3`/`python` -> the absolute interpreter that ran the installer
        (Windows usually has no `python3` on PATH), quoted for spaces.
      * `~/.claude` / `~\\.claude` -> the absolute config dir (Claude Code does
        not expand `~` inside a hook command string).
    Deterministic, so re-running the merge produces byte-identical commands and
    _merge_hooks dedups instead of duplicating.
    """
    if not isinstance(cmd, str):
        return cmd
    out = cmd
    if interpreter:
        out = re.sub(
            r"^(\s*)(?:python3|python)(\s+)",
            lambda m: f'{m.group(1)}"{interpreter}"{m.group(2)}',
            out,
            count=1,
        )
    ch = str(claude_home)
    out = out.replace("~/.claude", ch).replace("~\\.claude", ch)
    return out


def rewrite_hooks(hooks_dict: dict, interpreter: str | None, claude_home: Path) -> dict:
    """Return a copy of the overlay hooks with every command rewritten."""
    result: dict = {}
    for event, entries in hooks_dict.items():
        new_entries = []
        for entry in entries if isinstance(entries, list) else []:
            e = dict(entry) if isinstance(entry, dict) else entry
            if isinstance(e, dict) and isinstance(e.get("hooks"), list):
                e["hooks"] = [
                    {**h, "command": rewrite_hook_command(h["command"], interpreter, claude_home)}
                    if isinstance(h, dict) and isinstance(h.get("command"), str)
                    else h
                    for h in e["hooks"]
                ]
            new_entries.append(e)
        result[event] = new_entries
    return result


def _count_hook_entries(hooks_dict: dict) -> int:
    return sum(len(v) for v in hooks_dict.values() if isinstance(v, list))


# --- the merge engine -------------------------------------------------------
class MergePlan:
    def __init__(self) -> None:
        self.new_files: list[Path] = []   # not on disk, never installed -> copy
        self.readded: list[Path] = []     # we installed it, user deleted it -> restore
        self.updated: list[Path] = []     # ours, untouched by user, newer -> overwrite
        self.conflicts: list[Path] = []   # user edited it -> keep theirs, drop .leroy-new
        self.unchanged: list[Path] = []   # already byte-identical -> no-op
        self.kept: list[Path] = []        # PROTECTED -> never touched
        self.removed: list[Path] = []     # dropped from overlay, untouched -> delete
        self.orphan_kept: list[Path] = []  # dropped from overlay but user edited -> keep
        self.settings_merge: str | None = None  # human summary of settings action
        self.hooks_merge: str | None = None  # human summary of hook-wiring action
        self.had_manifest: bool = False   # False on the first upgrade of an old install
        self.keep_local: bool = False
        self.source_hashes: dict[str, str] = {}
        # (rel, reason) for files we could not write — read-only, locked by
        # another process, permissions. Collected, never fatal: one bad file
        # must not abort an otherwise good update and leave a half-written config.
        self.failed: list[tuple] = []

    @property
    def skipped(self) -> list[Path]:
        """Back-compat: everything deliberately left as-is."""
        return self.unchanged + self.conflicts + self.kept + self.orphan_kept

    @property
    def writes(self) -> int:
        return (
            len(self.new_files) + len(self.readded) + len(self.updated)
            + len(self.conflicts) + len(self.removed)
        )


def build_plan(source: Path, dest: Path, keep_local: bool = False) -> MergePlan:
    """
    Classify every overlay file against what's on disk AND against the manifest
    of what we installed last time. Three-way, so a fix can land without ever
    clobbering something the user wrote.
    """
    plan = MergePlan()
    if not source.exists():
        raise FileNotFoundError(f"source overlay not found: {source}")

    manifest = load_manifest(dest)
    plan.had_manifest = bool(manifest)
    plan.keep_local = keep_local
    seen: set[str] = set()

    for path in sorted(source.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(source)
        # Skip Python bytecode caches — build artifacts, never part of the overlay.
        if "__pycache__" in rel.parts or rel.suffix == ".pyc":
            continue
        # settings.json is handled by the merger, not the copier.
        if rel.name == "settings.json" and rel.parent == Path("."):
            continue
        # Defensive: the manifest lives in dest and must never be shipped IN the
        # overlay. If one ever were, copying it would clobber the real record of
        # what the user has — and the next run would misjudge every file.
        if rel.name == MANIFEST_NAME:
            continue

        key = rel.as_posix()
        seen.add(key)
        src_hash = _sha256(path)
        plan.source_hashes[key] = src_hash
        target = dest / rel

        if not target.exists():
            # Restore anything we own; a file the user deleted comes back on
            # update (a broken overlay is worse than an unwanted file).
            (plan.readded if key in manifest else plan.new_files).append(rel)
            continue

        # PROTECTED holds root-level names, and `key` is the relative posix
        # path, so this matches exactly ~/.claude/SOUL.md and never a nested
        # file that happens to share the name.
        if key in PROTECTED:
            plan.kept.append(rel)
            continue

        tgt_hash = _sha256(target)
        if tgt_hash == src_hash:
            plan.unchanged.append(rel)
            continue

        base = manifest.get(key)
        if base is None:
            # No baseline: an install from before manifests existed. We cannot
            # prove the file is untouched, so this is the one judgement call.
            # Default is to update (that IS the upgrade), with a per-file
            # .leroy-bak plus the full backup as the undo. --keep-local flips it.
            (plan.conflicts if keep_local else plan.updated).append(rel)
        elif tgt_hash == base:
            plan.updated.append(rel)     # exactly what we installed -> safe
        else:
            plan.conflicts.append(rel)   # user changed it -> theirs wins

    # Files we installed that the overlay no longer ships -> propagate removal,
    # but only when the user never touched them.
    for key, base in manifest.items():
        if key in seen or base == USER_OWNED:
            continue
        target = dest / key
        if not target.exists():
            continue
        if key in PROTECTED:
            continue
        if _sha256(target) == base:
            plan.removed.append(Path(key))
        else:
            plan.orphan_kept.append(Path(key))

    # Plan the settings.json action.
    src_settings = source / "settings.json"
    dst_settings = dest / "settings.json"
    if src_settings.exists():
        if dst_settings.exists():
            plan.settings_merge = "deep-merge into existing settings.json (user values win, hooks appended)"
        else:
            plan.settings_merge = "create settings.json from overlay (no existing file)"

    # Plan the hook wiring: settings.hooks.json is merged INTO settings.json so
    # Claude Code actually loads LeRoy's hooks (copying it to hooks/ alone does
    # nothing - Claude Code only reads settings.json).
    hooks_src = hooks_overlay_path(source)
    if hooks_src.exists():
        try:
            ov = json.loads(hooks_src.read_text(encoding="utf-8"))
            n = _count_hook_entries(ov.get("hooks", {}))
            plan.hooks_merge = (
                f"wire {n} hook group(s) from hooks/settings.hooks.json into "
                "settings.json (interpreter + ~/.claude paths rewritten; your own hooks preserved)"
            )
        except Exception:
            plan.hooks_merge = "hooks/settings.hooks.json present but unreadable - will skip (hooks stay inert)"
    return plan


def backup(dest: Path, dry_run: bool, needed: bool = True) -> Path | None:
    """
    Back up the entire existing ~/.claude before touching it.

    `needed` is False when the plan writes nothing: now that re-running the
    installer is the normal way to UPDATE, a no-op run would otherwise copy the
    whole config tree every time and pile up identical backups.
    """
    if not dest.exists():
        return None  # nothing to back up (fresh install)
    if not needed:
        return None  # nothing will be written, so there is nothing to undo
    stamp = _dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    backup_dir = dest.parent / f"{dest.name}.backup-{stamp}"
    # The stamp only resolves to the second, so two runs inside the same second
    # collided and crashed on FileExistsError. Rare when installing once; not
    # rare at all once updating is routine.
    suffix = 2
    while backup_dir.exists():
        backup_dir = dest.parent / f"{dest.name}.backup-{stamp}-{suffix}"
        suffix += 1
    if dry_run:
        return backup_dir
    shutil.copytree(dest, backup_dir, dirs_exist_ok=False)
    return backup_dir


def apply_plan(source: Path, dest: Path, plan: MergePlan, interpreter: str | None) -> None:
    """Execute the merge for real (caller must have backed up first)."""
    dest.mkdir(parents=True, exist_ok=True)

    # 1. files we've never placed, plus ones we own that went missing
    for rel in list(plan.new_files) + list(plan.readded):
        try:
            tgt = dest / rel
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, tgt)
        except OSError as e:
            plan.failed.append((rel, _why(e)))

    # 2. THE UPDATE: overwrite files that are ours and untouched. On the first
    #    upgrade of a pre-manifest install we also drop a per-file .leroy-bak,
    #    so the undo is local as well as in the whole-tree backup.
    for rel in plan.updated:
        tgt = dest / rel
        if not plan.had_manifest and tgt.exists():
            try:
                shutil.copy2(tgt, tgt.with_name(tgt.name + ".leroy-bak"))
            except OSError:
                pass  # a backup we couldn't write must never block the update
        try:
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, tgt)
        except OSError as e:
            plan.failed.append((rel, _why(e)))

    # 3. user-modified: never overwrite. Park the new version alongside.
    for rel in plan.conflicts:
        try:
            side = (dest / rel).with_name((dest / rel).name + ".leroy-new")
            side.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, side)
        except OSError as e:
            plan.failed.append((rel, _why(e)))

    # 4. propagate removals (only files proven untouched in build_plan)
    for rel in plan.removed:
        try:
            (dest / rel).unlink()
        except FileNotFoundError:
            pass  # already gone; nothing to do
        except OSError as e:
            plan.failed.append((rel, _why(e)))

    # settings.json - start from the user's existing settings (or empty), then
    # apply the root overlay (if any) and the hook wiring. Written once at the
    # end so a hooks-only overlay still produces a settings.json.
    dst_settings = dest / "settings.json"
    user = _load_json(dst_settings) if dst_settings.exists() else {}
    changed = False

    src_settings = source / "settings.json"
    if src_settings.exists():
        user = merge_settings(user, _load_json(src_settings))
        changed = True

    hooks_src = hooks_overlay_path(source)
    if hooks_src.exists():
        overlay_hooks = _load_json(hooks_src).get("hooks", {})
        if isinstance(overlay_hooks, dict) and overlay_hooks:
            rewritten = rewrite_hooks(overlay_hooks, interpreter, dest)
            user = merge_settings(user, {"hooks": rewritten})
            changed = True

    if changed:
        dst_settings.write_text(
            json.dumps(user, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # Record what we now own. Files we deliberately did NOT install (conflicts,
    # PROTECTED) keep their old baseline — or are marked USER_OWNED — so they
    # stay the user's on every future run.
    prior = load_manifest(dest)
    # A file we failed to write is NOT installed. Recording our hash for it
    # would make the next run believe it is current and skip it forever.
    hands_off = ({r.as_posix() for r in plan.conflicts}
                 | {r.as_posix() for r in plan.kept}
                 | {r.as_posix() for r, _ in plan.failed})
    new_manifest: dict[str, str] = {}
    for key, digest in plan.source_hashes.items():
        if key in hands_off:
            new_manifest[key] = prior.get(key, USER_OWNED)
        else:
            new_manifest[key] = digest
    write_manifest(dest, new_manifest)


def _why(e: OSError) -> str:
    """Plain-language reason, so a user sees the cause and not a stack trace."""
    import errno
    if isinstance(e, PermissionError) or getattr(e, "errno", None) == errno.EACCES:
        return "permission denied (file is read-only, or open in another program)"
    if getattr(e, "errno", None) == errno.ENOSPC:
        return "no space left on device"
    return f"{type(e).__name__}: {e}"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise SystemExit(f"  {'!'}  could not read {path}: {e}")


# --- rendering --------------------------------------------------------------
def print_plan(plan: MergePlan, source: Path, dest: Path, dry_run: bool, backup_dir: Path | None) -> None:
    print()
    dash = "—" if _UTF_OK else "-"
    mode = f"DRY RUN {dash} nothing will be written" if dry_run else "APPLYING merge"
    print(f"  LeRoy merge {dash} {mode}")
    print(f"  source: {source}")
    print(f"  dest:   {dest}")
    print("  " + "-" * 60)
    if backup_dir:
        verb = "would back up" if dry_run else "backed up"
        print(f"  {'↷'if _UTF_OK else '[bak]'}  {verb} existing config -> {backup_dir}")
    else:
        print(f"  (fresh install — no existing {dest.name} to back up)")
    print()
    def _bucket(mark: str, items: list, caption: str, limit: int = 20) -> None:
        if not items:
            return
        print(f"  {mark} {len(items)} {caption}")
        for rel in items[:limit]:
            print(f"      {mark} {rel.as_posix()}")
        if len(items) > limit:
            print(f"      ... and {len(items) - limit} more")
        print()

    if not plan.had_manifest and (plan.updated or plan.conflicts):
        print("  (first update of an existing install: no record of what we")
        print("   installed before, so changed files are refreshed and the old")
        print("   copy is kept beside each as <name>.leroy-bak.")
        print("   Prefer to keep your versions? re-run with --keep-local.)")
        print()

    _bucket(NEW, plan.new_files, "new file(s) to add:", 40)
    _bucket(NEW, plan.readded, "file(s) to restore (installed by LeRoy, now missing):")
    _bucket(UPD, plan.updated, "file(s) to UPDATE (unchanged by you since install):", 40)
    _bucket(CONF, plan.conflicts, "file(s) YOU changed - kept as-is, new version written beside as .leroy-new:")
    _bucket(DEL, plan.removed, "file(s) to remove (no longer part of LeRoy):")
    _bucket(SKIP, plan.orphan_kept, "retired file(s) you had edited - left alone:")
    _bucket(SKIP, plan.kept, "protected file(s) - never touched:")
    if plan.unchanged:
        print(f"  {SKIP} {len(plan.unchanged)} file(s) already up to date.")
        print()
    if plan.writes == 0:
        print("  Nothing to do - this install is already current.")
        print()
    if plan.settings_merge:
        print(f"  {MERGE} settings.json: {plan.settings_merge}")
    else:
        print("  (no settings.json in overlay)")
    if plan.hooks_merge:
        print(f"  {MERGE} hooks: {plan.hooks_merge}")
    print("  " + "-" * 60)
    if plan.failed:
        print()
        print(f"  {CONF} {len(plan.failed)} file(s) COULD NOT be written:")
        for rel, why in plan.failed[:20]:
            print(f"      {CONF} {rel.as_posix()} - {why}")
        if len(plan.failed) > 20:
            print(f"      ... and {len(plan.failed) - 20} more")
        print()
        print("  Everything else applied. These files are NOT recorded as updated,")
        print("  so re-running the installer retries them once the cause is cleared")
        print("  (close the program holding them, or clear the read-only flag).")
        print("  " + "-" * 60)

    if dry_run:
        print("  Dry run complete. Re-run without --dry-run to apply.\n")
    elif plan.failed:
        print(f"  Merge finished with {len(plan.failed)} problem(s) - see above.")
        print()
    else:
        print(f"  {OK}  Merge complete.\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe additive merge of LeRoy core/ into ~/.claude")
    parser.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    parser.add_argument(
        "--keep-local",
        action="store_true",
        help="on the first update of a pre-manifest install, do NOT refresh files "
        "that differ from the shipped version - keep yours and write the new one "
        "beside it as <name>.leroy-new. Later runs never need this: once a manifest "
        "exists, your edits are detected precisely and always preserved.",
    )
    parser.add_argument("--source", type=Path, default=None, help="override the core/ source dir")
    parser.add_argument("--dest", type=Path, default=None, help="override the ~/.claude target dir")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="start clean: move any existing dest aside to a timestamped backup, then "
        "install the overlay into an empty dir (instead of merging into what's there). "
        "The original is preserved intact as the backup — never merged into or overwritten.",
    )
    args = parser.parse_args(argv)

    source = (args.source or default_source()).resolve()
    dest = (args.dest or default_dest()).resolve()

    # --fresh: preserve the user's whole existing config AS the backup by MOVING
    # it aside, so the install below lands in a clean dir. Nothing of theirs is
    # merged into or overwritten — their original sits intact next to the new one
    # and `leroy reset` restores it.
    fresh_backup = None
    if args.fresh and dest.exists():
        stamp = _dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
        fresh_backup = dest.parent / f"{dest.name}.backup-{stamp}"
        if args.dry_run:
            print(f"\n  [fresh] would move existing {dest} aside to {fresh_backup}, then install clean\n")
        else:
            dest.rename(fresh_backup)  # move, not copy — original preserved as the backup

    try:
        plan = build_plan(source, dest, keep_local=args.keep_local)
    except FileNotFoundError as e:
        print(f"\n  merge aborted: {e}\n")
        return 1

    # The interpreter that ran this installer is the one we wire hooks to run
    # under - most robust (no dependency on `python3` being on PATH).
    interpreter = sys.executable or None

    # In --fresh mode the move-aside above already produced the backup; otherwise
    # take the usual copy-based backup of the existing config before merging.
    backup_dir = fresh_backup if args.fresh else backup(
        dest, dry_run=args.dry_run, needed=plan.writes > 0
    )
    if not args.dry_run:
        apply_plan(source, dest, plan, interpreter)
    print_plan(plan, source, dest, args.dry_run, backup_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
