# Reading Sync Logs

When you set `log.file: FULL`, pc-switcher records one line per changed item during a folder sync. Each line looks like this:

```
/home/user: <f+++++++++ Documents/notes.txt
/home/user: cd+++++++++ Documents/
/home/user: <f.st...... Pictures/photo.jpg
/home/user: cL+++++++++ .bashrc -> /etc/skel/.bashrc
/home/user: *deleting   Downloads/old.iso
```

Reading left to right: the synced folder, then an 11-character change code, then the item's path (with ` -> target` shown for symlinks). The change code tells you what happened to that item and why it was copied.

## The change code

The code has the form `YXcstpoguax`. The first two characters describe the item; the remaining nine describe which of its attributes differed from the target.

### Character 1 — what happened

| Char | Meaning |
| ---- | ------- |
| `<`  | the item was **copied to the target** |
| `c`  | a directory, symlink, device, or special file was **created** on the target |
| `h`  | the item is a **hard link** to another item |
| `.`  | nothing was copied — only attributes changed |
| `*`  | a message follows instead of a code (you'll see `*deleting` when an item is removed from the target) |

pc-switcher only ever copies **from this machine to the target**, so you will never see `>` (which would mean the reverse).

### Character 2 — the kind of item

| Char | Meaning |
| ---- | ------- |
| `f`  | regular file |
| `d`  | directory |
| `L`  | symlink |
| `D`  | device |
| `S`  | special file (socket, pipe) |

### Characters 3–11 — what differed

For a **newly created** item there is nothing on the target to compare against, so every slot shows `+` (that's why new files read `<f+++++++++`). Otherwise each slot shows its letter if that attribute differed, or `.` if it matched:

| Char | Attribute |
| ---- | --------- |
| `s`  | size |
| `t`  | modification time |
| `p`  | permissions |
| `o`  | owner |
| `g`  | group |
| `a`  | access-control list (ACL) |
| `x`  | extended attributes |

(The two remaining slots — position 3 and position 9 — stay `.` for ordinary files in pc-switcher's sync.)

## Worked examples

- `cd+++++++++ Documents/` — a **new directory** that didn't exist on the target.
- `<f+++++++++ notes.txt` — a **new file** copied to the target.
- `<f.st...... photo.jpg` — an **existing file re-copied** because its **size** (`s`) and **modification time** (`t`) differed.
- `.f...p..... script.sh` — the file's contents matched; only its **permissions** (`p`) changed, so nothing was transferred.
- `.d..t...... Pictures/` — an existing directory whose **timestamp** was adjusted.
- `cL+++++++++ .bashrc -> /etc/skel/.bashrc` — a **new symlink** pointing at the shown target.
- `*deleting old.iso` — the item was **removed from the target** because it no longer exists here.

## More detail

These codes come from `rsync`. Their full specification is in the [rsync manual page](https://download.samba.org/pub/rsync/rsync.1), under `--itemize-changes`.
