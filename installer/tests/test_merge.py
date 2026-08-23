#!/usr/bin/env python3
"""Behavioural test for the manifest-backed merge.py. Scratch dirs only."""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path

MERGE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (
    Path(__file__).resolve().parent.parent / "merge.py")
PY = sys.executable
fails, passes = [], []


def check(name, cond, detail=""):
    (passes if cond else fails).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + detail}")


def run(src, dst, *extra):
    r = subprocess.run(
        [PY, str(MERGE), "--source", str(src), "--dest", str(dst), *extra],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        raise SystemExit(f"merge.py exited {r.returncode}")
    return r.stdout


def mkoverlay(root, files):
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


V1 = {
    "agents/scout.md": "scout v1\n",
    "hooks/gate.py": "gate v1\n",
    "skills/meta/index.md": "index v1\n",
    "agents/janitor.md": "janitor v1\n",
    "SOUL.md": "TEMPLATE SOUL\n",
}

base = Path(tempfile.mkdtemp(prefix="leroy-merge-test-"))
print(f"scratch: {base}\n")

# ---------------------------------------------------------------- 1. install
src1, dest = base / "src1", base / "dest"
mkoverlay(src1, V1)
run(src1, dest)
print("[1] fresh install")
check("all files installed", all((dest / f).exists() for f in V1))
man = json.loads((dest / ".leroy-overlay.json").read_text())
check("manifest written", man.get("version") == 1 and len(man["files"]) == 5,
      str(man.get("files", {}).keys()))

# ------------------------------------------------------------ 2. idempotence
out = run(src1, dest)
print("\n[2] re-run, nothing changed")
check("reports nothing to do", "already current" in out, out[-300:])

# ------------------------------------------- 3. upstream fix reaches the user
src2 = base / "src2"
shutil.copytree(src1, src2)
(src2 / "hooks/gate.py").write_text("gate v2 FIXED\n", encoding="utf-8")
out = run(src2, dest)
print("\n[3] upstream fixes an untouched file")
check("file actually updated", (dest / "hooks/gate.py").read_text() == "gate v2 FIXED\n",
      (dest / "hooks/gate.py").read_text())
check("no .leroy-bak (manifest existed)", not (dest / "hooks/gate.py.leroy-bak").exists())

# --------------------------------------------------- 4. user edit is defended
(dest / "agents/scout.md").write_text("MY OWN SCOUT\n", encoding="utf-8")
src3 = base / "src3"
shutil.copytree(src2, src3)
(src3 / "agents/scout.md").write_text("scout v3 upstream\n", encoding="utf-8")
out = run(src3, dest)
print("\n[4] user edited a file, upstream also changed it")
check("user's version preserved", (dest / "agents/scout.md").read_text() == "MY OWN SCOUT\n",
      (dest / "agents/scout.md").read_text())
check(".leroy-new written beside", (dest / "agents/scout.md.leroy-new").read_text() == "scout v3 upstream\n")

# --- 4b. that file must STAY the user's on later runs, even if they revert it
src4 = base / "src4"
shutil.copytree(src3, src4)
(src4 / "agents/scout.md").write_text("scout v4 upstream\n", encoding="utf-8")
run(src4, dest)
check("still not clobbered on a later run",
      (dest / "agents/scout.md").read_text() == "MY OWN SCOUT\n")

# ------------------------------------------------------ 5. deletion propagates
src5 = base / "src5"
shutil.copytree(src4, src5)
(src5 / "agents/janitor.md").unlink()
run(src5, dest)
print("\n[5] file dropped from the overlay")
check("untouched retired file removed", not (dest / "agents/janitor.md").exists())

# --- 5b. a retired file the user EDITED must survive
(dest / "skills/meta/index.md").write_text("my index\n", encoding="utf-8")
src6 = base / "src6"
shutil.copytree(src5, src6)
(src6 / "skills/meta/index.md").unlink()
run(src6, dest)
check("retired-but-edited file kept", (dest / "skills/meta/index.md").exists())

# ------------------------------------------------ 6. PROTECTED never touched
src7 = base / "src7"
shutil.copytree(src6, src7)
(dest / "SOUL.md").write_text("BRIAN'S REAL SOUL\n", encoding="utf-8")
(src7 / "SOUL.md").write_text("TEMPLATE SOUL v2\n", encoding="utf-8")
run(src7, dest)
print("\n[6] PROTECTED files")
check("SOUL.md untouched", (dest / "SOUL.md").read_text() == "BRIAN'S REAL SOUL\n",
      (dest / "SOUL.md").read_text())

# ------------------------- 7. pre-manifest install (today's real-world users)
old = base / "old-install"
old.mkdir()
mkoverlay(old, {"agents/scout.md": "scout v1\n", "hooks/gate.py": "gate v1\n"})
src8 = base / "src8"
mkoverlay(src8, {"agents/scout.md": "scout NEW\n", "hooks/gate.py": "gate NEW\n"})
out = run(src8, old)
print("\n[7] existing install with NO manifest (the upgrade everyone hits)")
check("updates land", (old / "agents/scout.md").read_text() == "scout NEW\n",
      (old / "agents/scout.md").read_text())
check(".leroy-bak left behind", (old / "agents/scout.md.leroy-bak").read_text() == "scout v1\n")
check("manifest now exists", (old / ".leroy-overlay.json").exists())

# -------------------------------------------------- 8. --keep-local opt-out
old2 = base / "old-install-2"
old2.mkdir()
mkoverlay(old2, {"agents/scout.md": "scout MINE\n"})
run(src8, old2, "--keep-local")
print("\n[8] --keep-local on a pre-manifest install")
check("local version kept", (old2 / "agents/scout.md").read_text() == "scout MINE\n")
check(".leroy-new written", (old2 / "agents/scout.md.leroy-new").exists())

# ------------------------------------------------------------ 9. dry-run
before = (dest / "hooks/gate.py").read_text()
src9 = base / "src9"
shutil.copytree(src7, src9)
(src9 / "hooks/gate.py").write_text("gate v9\n", encoding="utf-8")
run(src9, dest, "--dry-run")
print("\n[9] --dry-run")
check("wrote nothing", (dest / "hooks/gate.py").read_text() == before)

print(f"\n{len(passes)} passed, {len(fails)} failed")
if fails:
    print("FAILED: " + ", ".join(fails))
shutil.rmtree(base, ignore_errors=True)
sys.exit(1 if fails else 0)
