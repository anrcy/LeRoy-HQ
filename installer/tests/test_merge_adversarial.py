#!/usr/bin/env python3
"""Adversarial edge-case tests for merge.py — the ones a happy-path suite misses."""
import json, os, shutil, stat, subprocess, sys, tempfile
from pathlib import Path

MERGE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (
    Path(__file__).resolve().parent.parent / "merge.py")
PY = sys.executable
fails, passes = [], []


def check(name, cond, detail=""):
    (passes if cond else fails).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + str(detail)[:200]}")


def run(src, dst, *extra, expect_ok=True):
    r = subprocess.run([PY, str(MERGE), "--source", str(src), "--dest", str(dst), *extra],
                       capture_output=True, text=True)
    if expect_ok and r.returncode != 0:
        print("    STDOUT:", r.stdout[-600:])
        print("    STDERR:", r.stderr[-600:])
    return r


def mk(root, files):
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


base = Path(tempfile.mkdtemp(prefix="leroy-adv-"))
print(f"scratch: {base}\n")

# ============================================================ 1. read-only target
print("[1] a READ-ONLY file in the user's config (Windows: very common)")
src, dst = base / "s1", base / "d1"
mk(src, {"a.md": "v1\n", "b.md": "b1\n"})
run(src, dst)
src2 = base / "s2"; shutil.copytree(src, src2)
(src2 / "a.md").write_text("v2\n", encoding="utf-8")
(src2 / "b.md").write_text("b2\n", encoding="utf-8")
target = dst / "a.md"
os.chmod(target, stat.S_IREAD)
r = run(src2, dst, expect_ok=False)
check("does not crash with a raw traceback", "Traceback" not in r.stderr,
      r.stderr[-300:])
# whatever it does, the OTHER file must still get updated (no partial abort)
check("an unrelated file still updated despite the locked one",
      (dst / "b.md").read_text() == "b2\n", (dst / "b.md").read_text())
try:
    os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
except OSError:
    pass

# ==================================================== 2. manifest corrupt/truncated
print("\n[2] corrupt .leroy-overlay.json")
src, dst = base / "s3", base / "d3"
mk(src, {"x.md": "x1\n"})
run(src, dst)
(dst / ".leroy-overlay.json").write_text("{not json", encoding="utf-8")
(dst / "x.md").write_text("USER EDIT\n", encoding="utf-8")
src4 = base / "s4"; shutil.copytree(src, src4)
(src4 / "x.md").write_text("x2 upstream\n", encoding="utf-8")
r = run(src4, dst, "--keep-local", expect_ok=False)
check("survives a corrupt manifest", r.returncode == 0, r.stderr[-300:])
check("corrupt manifest does NOT eat the user's edit",
      (dst / "x.md").read_text() == "USER EDIT\n", (dst / "x.md").read_text())

# ============================================= 3. manifest claims a file we removed
print("\n[3] manifest lists a file the user already deleted themselves")
src, dst = base / "s5", base / "d5"
mk(src, {"keep.md": "k\n", "gone.md": "g\n"})
run(src, dst)
(dst / "gone.md").unlink()          # user deleted it
src6 = base / "s6"; shutil.copytree(src, src6)
(src6 / "gone.md").unlink()          # upstream also retired it
r = run(src6, dst, expect_ok=False)
check("no crash when a to-remove file is already absent", r.returncode == 0, r.stderr[-300:])

# ==================================================== 4. nested new dirs are created
print("\n[4] deeply nested NEW file")
src7 = base / "s7"; shutil.copytree(src6, src7)
mk(src7, {"deep/a/b/c/new.md": "deep\n"})
run(src7, dst)
check("nested dirs created", (dst / "deep/a/b/c/new.md").exists())

# ============================================ 5. .leroy-new for a nested conflict
print("\n[5] conflict inside a nested dir")
(dst / "deep/a/b/c/new.md").write_text("MINE\n", encoding="utf-8")
src8 = base / "s8"; shutil.copytree(src7, src8)
(src8 / "deep/a/b/c/new.md").write_text("theirs\n", encoding="utf-8")
run(src8, dst)
check("nested user edit preserved", (dst / "deep/a/b/c/new.md").read_text() == "MINE\n")
check("nested .leroy-new written", (dst / "deep/a/b/c/new.md.leroy-new").exists())

# ================================================ 6. binary files aren't corrupted
print("\n[6] binary file round-trip")
src9 = base / "s9"; shutil.copytree(src8, src9)
blob = bytes(range(256)) * 40
(src9 / "bin.dat").write_bytes(blob)
dst9 = base / "d9"
run(src9, dst9)
check("binary copied byte-exact", (dst9 / "bin.dat").read_bytes() == blob)

# ============================================== 7. two runs in the SAME second
print("\n[7] two writing runs back-to-back (the FileExistsError bug)")
srcA = base / "sA"; shutil.copytree(src9, srcA)
dstA = base / "dA"
run(srcA, dstA)
for i in range(3):
    srcN = base / f"sA{i}"; shutil.copytree(srcA, srcN)
    (srcN / "keep.md").write_text(f"rev{i}\n", encoding="utf-8")
    r = run(srcN, dstA, expect_ok=False)
    if r.returncode != 0:
        check(f"rapid run #{i} ok", False, r.stderr[-300:]); break
else:
    check("3 rapid writing runs, no collision crash", True)
check("last rapid run applied", (dstA / "keep.md").read_text() == "rev2\n")

# ==================================== 8. no-op run creates NO backup dir (cost)
print("\n[8] no-op run must not back up")
before = {p.name for p in base.iterdir() if p.name.startswith("dA.backup")}
run(srcA.parent / "sA2" if (srcA.parent / "sA2").exists() else srcA, dstA)
r = run(srcA, dstA)  # source sA is now older than dstA content; may write.
# Deterministic no-op: re-run with the exact same source twice
srcZ = base / "sZ"; shutil.copytree(srcA, srcZ)
dstZ = base / "dZ"
run(srcZ, dstZ)
n_before = len([p for p in base.iterdir() if p.name.startswith("dZ.backup")])
run(srcZ, dstZ)
n_after = len([p for p in base.iterdir() if p.name.startswith("dZ.backup")])
check("second identical run made no new backup", n_after == n_before, f"{n_before}->{n_after}")

# ================================= 9. manifest never records a hash for a conflict
print("\n[9] manifest integrity after a conflict")
man = json.loads((dst / ".leroy-overlay.json").read_text())["files"]
key = "deep/a/b/c/new.md"
recorded = man.get(key)
# The correct invariant is NOT "must be the sentinel" — retaining the ORIGINAL
# installed hash is equally safe and more informative. What must never happen is
# recording the hash of the version we did NOT install, which would make the next
# run believe the user's file is ours and overwrite it.
import hashlib
not_installed = hashlib.sha256((src8 / key).read_bytes()).hexdigest()
check("manifest does NOT claim we installed the new version",
      recorded != not_installed, f"{key} -> {recorded}")
# And prove the consequence directly: another round must still not clobber them.
src10 = base / "s10"; shutil.copytree(src8, src10)
(src10 / key).write_text("theirs v3\n", encoding="utf-8")
run(src10, dst)
check("user's edit still safe after a THIRD round",
      (dst / key).read_text() == "MINE\n", (dst / key).read_text())


# ==================================================================
# Regressions for the 2026-08-23 audit findings. Every one of these
# shipped green through the original suite; none were caught by it.
# ==================================================================

print("\n[10] a file the USER authored, later shipped by the overlay (finding 1)")
sA, dA2 = base / "sA2", base / "dA2"
mk(sA, {"agents/stock.md": "stock\n"})
run(sA, dA2)
(dA2 / "agents/mine.md").write_text("MY OWN WORK\n", encoding="utf-8")
sB = base / "sB"; shutil.copytree(sA, sB)
mk(sB, {"agents/mine.md": "upstream ships it now\n"})
run(sB, dA2)
check("user-authored file NOT overwritten when the overlay adopts the path",
      (dA2 / "agents/mine.md").read_text() == "MY OWN WORK\n",
      (dA2 / "agents/mine.md").read_text())
check("its new version surfaced as .leroy-new",
      (dA2 / "agents/mine.md.leroy-new").exists())

print("\n[11] retired-but-edited file keeps its user-owned marking (finding 3)")
sC, dC = base / "sC", base / "dC"
mk(sC, {"agents/a.md": "a1\n", "agents/k.md": "k\n"})
run(sC, dC)
(dC / "agents/a.md").write_text("MY EDIT\n", encoding="utf-8")
sD = base / "sD"; shutil.copytree(sC, sD); (sD / "agents/a.md").unlink()
run(sD, dC)
man2 = json.loads((dC / ".leroy-overlay.json").read_text())["files"]
check("orphan stays in the manifest as user-owned",
      man2.get("agents/a.md") == "user-modified", man2.get("agents/a.md"))
sE = base / "sE"; shutil.copytree(sD, sE); mk(sE, {"agents/a.md": "upstream re-adds\n"})
run(sE, dC)
check("still not clobbered when the overlay re-adds that path",
      (dC / "agents/a.md").read_text() == "MY EDIT\n", (dC / "agents/a.md").read_text())

print("\n[12] a run that only rewrites settings.json still takes a backup (finding 5)")
sF, dF = base / "sF", base / "dF"
_hooks = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 x.py"}]}]}}
mk(sF, {"hooks/settings.hooks.json": json.dumps(_hooks), "agents/z.md": "z\n"})
run(sF, dF)
n0 = len([q for q in base.iterdir() if q.name.startswith("dF.backup")])
run(sF, dF)
n1 = len([q for q in base.iterdir() if q.name.startswith("dF.backup")])
check("settings-only rewrite is backed up (reversibility holds)", n1 > n0, f"{n0}->{n1}")

print("\n[13] the plan is printed BEFORE anything is written (finding 12)")
sG, dG = base / "sG", base / "dG"
mk(sG, {"a.md": "1\n"})
run(sG, dG)
sH = base / "sH"; shutil.copytree(sG, sH); (sH / "a.md").write_text("2\n", encoding="utf-8")
out = run(sH, dG).stdout
i_plan = out.find("to UPDATE"); i_done = out.find("Merge complete")
check("plan appears before the completion line", i_plan != -1 and i_plan < i_done,
      f"plan@{i_plan} done@{i_done}")

print("\n[14] CLAUDE.md is never blind-overwritten (finding 11)")
sI, dI = base / "sI", base / "dI"
mk(sI, {"CLAUDE.md": "hub v1\n"})
run(sI, dI)
(dI / "CLAUDE.md").write_text("hub v1\nMY REMEMBERED NOTE\n", encoding="utf-8")
sJ = base / "sJ"; shutil.copytree(sI, sJ); (sJ / "CLAUDE.md").write_text("hub v2\n", encoding="utf-8")
run(sJ, dI)
check("user's CLAUDE.md preserved", "MY REMEMBERED NOTE" in (dI / "CLAUDE.md").read_text())
check("improved version still surfaced as .leroy-new", (dI / "CLAUDE.md.leroy-new").exists())

print("\n[15] case-only rename does not delete what it just installed (finding 7)")
sK, dK = base / "sK", base / "dK"
mk(sK, {"skills/Index.md": "idx\n"})
run(sK, dK)
sL = base / "sL"; shutil.copytree(sK, sL)
(sL / "skills/Index.md").unlink(); mk(sL, {"skills/index.md": "idx renamed\n"})
run(sL, dK)
left = [q.name for q in (dK / "skills").glob("*")] if (dK / "skills").exists() else []
check("skills/ is not left empty by a case-only rename", len(left) > 0, left)

print("\n[16] hostile manifest keys never reach unlink (finding 13)")
sM, dM = base / "sM", base / "dM"
mk(sM, {"ok.md": "ok\n"})
run(sM, dM)
mp = dM / ".leroy-overlay.json"
_d = json.loads(mp.read_text())
_d["files"]["../../ESCAPED.md"] = "x" * 64
_d["files"]["C:/Windows/System32/drivers/etc/hosts"] = "y" * 64
mp.write_text(json.dumps(_d), encoding="utf-8")
outside = base / "ESCAPED.md"
outside.write_text("must survive\n", encoding="utf-8")
_r = run(sM, dM)
check("traversal key rejected; file outside dest untouched",
      outside.exists() and _r.returncode == 0, f"rc={_r.returncode}")

print(f"\n{len(passes)} passed, {len(fails)} failed")
if fails:
    print("FAILED: " + "; ".join(fails))
shutil.rmtree(base, ignore_errors=True)
sys.exit(1 if fails else 0)
