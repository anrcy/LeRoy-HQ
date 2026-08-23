# Upgrading LeRoy

**Already have LeRoy installed? Re-run the installer. That's the whole upgrade.**

```powershell
irm https://raw.githubusercontent.com/Zeekeey-jpeg/LeRoy-HQ/main/install.ps1 | iex
```

It notices your existing checkout, pulls the latest `main`, and updates your
`~/.claude` in place. Your memory, your settings, and anything you've edited
yourself are left alone.

---

## What actually happens to your files

Every file is sorted into one of these. The full plan — and the backup location
— is printed **before** anything is written:

| | What it means |
|---|---|
| **new** | A file LeRoy didn't ship before. Added. |
| **update** | A LeRoy file you never edited, with a newer version available. **Replaced.** |
| **you changed this** | You edited it. **Yours is kept.** The new version is written next to it as `<name>.leroy-new` so you can diff and merge by hand. |
| **remove** | A file LeRoy no longer ships and you never touched. Deleted, so you don't accumulate dead agents and skills. |
| **retired, but you edited it** | Dropped from LeRoy, but it's yours now. Left alone. |
| **protected** | `settings.json`, `SOUL.md`, `USER.md`, `MEMORY.md`. Never overwritten, ever. |
| **your surface** | `CLAUDE.md`. LeRoy ships it, but Claude Code's `#` shortcut writes to it too, so it's treated as yours: never overwritten, and the new version arrives as `.leroy-new`. |

`settings.json` is never replaced — it's merged key by key. Your values always
win, and LeRoy's hooks are appended to yours rather than replacing them.

Your `memory/` vault is not part of the overlay and is never touched by an
upgrade.

### How it knows what you edited

At install time LeRoy records a hash of every file it placed, in
`~/.claude/.leroy-overlay.json`. On the next run it compares that record against
what's actually on disk. A file that still matches its hash is one you never
touched, so it's safe to update. A file that doesn't match is yours, and LeRoy
won't overwrite it.

**Keep that file.** Deleting it doesn't protect anything — it erases the record
of what LeRoy installed, so the next upgrade can no longer recognise your edits
and falls back to refreshing changed files (keeping a `.leroy-bak` beside each).
If you want to review changes yourself instead, use `--keep-local` below.

---

## Preview it first

```powershell
python installer/merge.py --dry-run
```

Writes nothing. Prints exactly what would be added, updated, kept, and removed.

## The first upgrade is the one special case

If you installed LeRoy **before** this release, there's no manifest yet, so
LeRoy can't prove whether a changed file is your edit or just an old version.
Rather than skip the update (which is what left people stranded on old
versions), it refreshes those files and keeps the previous copy beside each one
as `<name>.leroy-bak`.

If you'd rather keep your versions and review the new ones yourself:

```powershell
python installer/merge.py --keep-local
```

Then each new version arrives as `<name>.leroy-new` and nothing of yours is
replaced.

This only applies once. After that, the manifest exists and your edits are
detected precisely every time.

---

## Undo

Every run that writes anything first backs up your entire `~/.claude` to
`~/.claude.backup-<timestamp>/`.

```powershell
leroy reset     # restore the most recent backup
```

Per-file undo, from the first upgrade: the `.leroy-bak` file next to anything
that was replaced.

A run that changes nothing doesn't make a backup, so re-running to check for
updates costs you nothing.

## Cleaning up

Once you've merged what you want, the leftovers are safe to delete:

```powershell
Get-ChildItem ~/.claude -Recurse -Include *.leroy-new,*.leroy-bak | Remove-Item
```

---

## If something goes wrong

**"git pull failed (local changes in the way?)"** — you've edited files inside
the LeRoy *checkout* itself (not `~/.claude`). Stash or discard them there, or
delete the checkout and re-run; your `~/.claude` is separate and unaffected.

**An update overwrote something you wanted** — it's in
`~/.claude.backup-<timestamp>/`, and if it was the first upgrade, also in the
`.leroy-bak` beside it.

**You want to start completely clean** — `python installer/merge.py --fresh`
moves your existing config aside as a backup and installs into an empty
directory. Nothing is merged or overwritten; the original stays intact.
