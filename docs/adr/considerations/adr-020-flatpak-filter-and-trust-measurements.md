# Flatpak ref filters and libostree trust anchors, measured

The premises `flatpak_sync` rests on, measured rather than reasoned about. All hold.

Everything below ran in a throwaway `ubuntu:24.04` container: flatpak `1.14.6-1ubuntu0.1`, `libostree-1-1` `2024.5-1build2`.

## Does flatpak refuse to install a ref its own remote filter excludes?

Yes. That is what `PKG-FR-FLATPAK-FILTER`'s ordering — remote, filter, install, with nothing cleared for the installs' benefit — has to reckon with: a filter narrower than the set being replicated blocks the very installs it travels with.

Setup: `flatpak remote-add --system flathub https://dl.flathub.org/repo/flathub.flatpakrepo`, then `flatpak remote-modify --system --filter=/etc/flatpak/filters/f1 flathub`.

A filter defaults to allowing everything, so an allow-only file narrows nothing. With `f1` holding only `allow runtime/org.gtk.Gtk3theme.Yaru-dark/*/*`, `flatpak remote-ls --system flathub` listed the whole remote and `flatpak install --system --noninteractive flathub runtime/org.gtk.Gtk3theme.Adwaita-dark/x86_64/3.22` exited 0 and landed the ref. A filter file must carry a `deny` line to exclude anything, which is what flatpak's own `--filter` documentation says.

With `f1` holding `deny *` then `allow runtime/org.gtk.Gtk3theme.Yaru-dark/*/*`:

| Command | Result |
| - | - |
| `flatpak remote-ls --system flathub` | one line, `Yaru-dark` |
| `flatpak install --system --noninteractive flathub runtime/org.gtk.Gtk3theme.Adwaita-dark/x86_64/3.22` | exit 1, `error: Nothing matches org.gtk.Gtk3theme.Adwaita-dark in remote flathub`; nothing landed |
| the same install of the allowed `runtime/org.gtk.Gtk3theme.Yaru-dark/x86_64/3.22` | exit 0, landed |

The refusal is the filter and not the ref: the two commands differ only in which ref the filter names.

An application already installed when the filter arrives is untouched by it. Adwaita-dark installed with no filter, then the denying `f1` applied: `remote-modify` exited 0, `flatpak list --system` still showed the ref, `flatpak info` exited 0. `flatpak update --system --noninteractive` exited 0, removed nothing, and printed `F: Warning: Treating remote fetch error as non-fatal since runtime/org.gtk.Gtk3theme.Adwaita-dark/x86_64/3.22 is already installed: No entry for runtime/org.gtk.Gtk3theme.Adwaita-dark/x86_64/3.22 in remote 'flathub' summary flatpak cache` followed by `Nothing to do.`. Only a fresh install is refused: `flatpak install --reinstall` of the denied ref exited 1 with the same `Nothing matches` error.

So a filter arriving over an application that already landed costs that application nothing, and only the applications still to install can be blocked by one.

## Can flatpak itself be asked what a filter denies?

It can be asked what a remote OFFERS under its filter, and no more than that. `flatpak remote-ls <name>` applies the named remote's filter to its own listing, which is what `_abort_on_a_source_filter_that_denies_its_own_apps` rests on instead of transcribing `flatpak_filter_glob_to_regexp` into Python. What no question reaches is why a ref is missing from that listing: flatpak 1.14.6 offers no unfiltered view of the same remote to compare it against.

Rig: a local `archive-z2` repository holding `app/org.test.One/x86_64/{stable,beta}`, `app/org.test.Two/x86_64/stable` and `app/org.test.Nine/i386/stable`, indexed with `flatpak build-update-repo`, served over `file://` and (the same repository again) over `http://127.0.0.1:8099/`; `flatpak remote-add --system --no-gpg-verify r <url>`.

| Filter on `r` | `flatpak remote-ls --system --arch='*' --columns=ref r` |
| - | - |
| none | One/beta, One/stable, Two |
| `deny *` + `allow app/org.test.One/*/*` | One/beta, One/stable |
| `deny *` + `allow app/org.test.One` (the id alone) | One/beta, One/stable — a glob naming the id covers every arch and branch of it |

The same filter over `http://` gave the same listing, and `flatpak install r app/org.test.Two/x86_64/stable` exited 1 with `Nothing matches org.test.Two in remote r`, so the listing and the install agree about what the filter withholds.

The listing prints the ref with its kind — `app/<id>/<arch>/<branch>`, and `runtime/…` for a runtime — which is exactly what `flatpak list --columns=ref` prints without the kind, so `app/` in front of an installed ref is the string to look for.

### No unfiltered second opinion exists

`flatpak remote-ls`'s help documents a `[REMOTE or URI]` argument, but only a `file://` URI is accepted as one:

| Argument | Result |
| - | - |
| `file:///srv/repo` | listed, unfiltered — the URI form ignores the configured remote's filter |
| `http://127.0.0.1:8099/`, with or without the trailing slash | exit 1, `error: Remote "http://127.0.0.1:8099/" not found in the system installation` |
| `https://dl.flathub.org/repo/` | exit 1, same error, against real flathub |
| `http://127.0.0.1:8099/t.flatpakrepo` | exit 1, same error |
| `/srv/repo` (a local path, no scheme) | exit 1, same error |

`flatpak remote-info <name> <ref>` is filtered too — for a denied ref it exits 1 with `No entry for app/org.test.Two/x86_64/stable in remote 'r' summary flatpak cache`, and for an allowed one it exits 0 — and `flatpak search` matched nothing. So for the remotes that matter (`https://`), the only unfiltered answer would come from reconfiguring the source's own remote, which no plan-time read may do.

### A missing ref names no cause

Two states other than a `deny` line take a ref out of the listing:

- **Delisted.** With the filter allowing both applications, `ostree refs --delete app/org.test.Two/x86_64/stable` followed by `flatpak build-update-repo` took Two out of the listing.
- **Not listable for its architecture.** `--arch='*'` is required — without it only the running machine's architecture is listed, and an `aarch64` ref appeared only with it — and it is still not complete. `app/org.test.Nine/i386/stable` sat in the local repository's summary (`ostree summary --view` shows it) and was listed by neither `--arch='*'` nor `--arch=i386` on an x86_64 host; real flathub answered `--arch='*'` with 10178 refs, not one of them `i386`, while `flatpak --supported-arches` printed `x86_64` and `i386`.

So "absent from the listing" is all that can be established, and the abort states exactly that rather than naming a culprit.

### When flatpak declines to answer

It declines rather than answering wrongly:

| Situation | `remote-ls` |
| - | - |
| filter file holds a line flatpak cannot parse | exit 1, `error: Failed to parse filter '/etc/flatpak/filters/bad': Unexpected word 'bogus' on line 2` |
| filter path never existed | exit 1, `error: Failed to load filter '/var/lib/flatpak/repo/fresh.filter': … No such file or directory` |
| filter path is a directory | exit 1, `error: Failed to load filter … Is a directory` |
| remote's URL serves no summary | exit 1, `error: Unable to load summary from remote gone: …` |
| remote unreachable (`https://127.0.0.1:9/repo`) | exit 1, `… Couldn't connect to server` |

`flatpak remote-modify --filter=<path>` copies the file to `<installation>/repo/<remote>.filter`, headed `# backup copy of <path>, do not edit!`, and re-reads the configured path on every use: editing the file in place changed the next listing with no `remote-modify` at all, and deleting it fell back to the backup copy. A path that never existed has no backup to fall back on, which is the second row above.

A listing is an ordinary read. An unprivileged user with no TTY listed a `--system` remote, exit 0, and the only thing the read created was that user's own `~/.cache/flatpak`. It took 7 ms against a `file://` remote and 0.3 s against real flathub.

## Does libostree read any keyring directory beyond `/usr/share/ostree/trusted.gpg.d`?

No — one directory, plus an environment override that replaces it rather than adding to it.

Rig: a gpg key `K1`; `ostree init --repo=/srv/ost --mode=archive-z2`; `ostree commit --repo=/srv/ost --branch=test/ref --gpg-sign=K1`; `ostree summary --repo=/srv/ost -u --gpg-sign=K1`. Each measurement used a fresh client repo, `ostree remote add --repo=/srv/client test file:///srv/ost` (signature verification on by default), then `ostree pull --repo=/srv/client test test/ref`.

| Where `K1` is | pull |
| - | - |
| nowhere | exit 1, `Can't check signature: public key not found` |
| `/usr/share/ostree/trusted.gpg.d/` | exit 0 |
| `/etc/ostree/trusted.gpg.d/` | exit 1, same error |
| a directory named by `OSTREE_GPG_HOME` | exit 0 |
| the remote's own `/srv/client/test.trustedkeys.gpg`, via `--gpg-import`, no anchor anywhere | exit 0 |

`OSTREE_GPG_HOME` replaces the anchor directory: with `K1` in `/usr/share/ostree/trusted.gpg.d` and `OSTREE_GPG_HOME` pointed at an empty directory the pull exited 1, and exited 0 with the override unset.

A remote's own keyring suppresses the anchor directory outright. A remote whose `trustedkeys.gpg` held only a second key `K2`, with `K1` in the anchor directory and the repo signed by `K1`, exited 1. So an anchor is consulted only for a remote holding no keyring of its own, which is the one case `_anchors_to_import` acts on.

libostree's own source at `v2024.5` says the same: `_ostree_gpg_verifier_add_global_keyring_dir` in `src/libostree/ostree-gpg-verifier.c` takes `OSTREE_GPG_HOME` and otherwise falls back to `DATADIR "/ostree/trusted.gpg.d/"`, and the keyring lookup in `ostree-repo.c` sets `add_global_keyrings = FALSE` once it finds a per-remote keyring. `SYSCONFDIR` appears only for `/etc/ostree/remotes.d`, which holds remote configuration rather than keys. Ubuntu's `2024.5-1build2` carries three test-skip patches and none touching gpg. flatpak 1.14.6 adds no keyring directory of its own; `--gpg-import` calls `ostree_repo_remote_gpg_import`.

Confirmed at the flatpak level. A flathub ref mirrored with `ostree pull-local`, re-signed with `K1` and indexed by `flatpak build-update-repo --gpg-sign=K1`, served at `file:///srv/fprepo` and added with `flatpak remote-add --system localtest file:///srv/fprepo` — no key of its own, verification on. `flatpak install --system --noninteractive localtest org.gtk.Gtk3theme.Adwaita-dark` exited 1 with `Unable to load summary from remote localtest: ... Can't check signature: public key not found` while the anchor directory was empty, and exited 0 once `K1` was in it.

`flatpak remote-add --gpg-import` is repeatable and merges, which is what lets one remote carry several anchor files. Two files imported in one `remote-add` left both fingerprints in `/var/lib/flatpak/repo/lt2.trustedkeys.gpg` and the install exited 0 with no anchor anywhere. `ostree remote add` keeps only the last `--gpg-import`; flatpak's does not.

## Which anchor does a keyless verified remote rest on?

All of them, and nothing records which one verified anything — read from libostree's source at `v2024.5` rather than measured. `_ostree_gpg_verifier_add_keyring_dir_at` in `src/libostree/ostree-gpg-verifier.c` walks the directory and takes every regular file whose name ends in `.gpg`, skipping exactly `trustdb.gpg` and `secring.gpg` (gpg's own database files); each surviving file's bytes are appended to one `keyring_data` array, imported into a single GPGME context by `_ostree_gpg_verifier_import_keys`, and `gpgme_op_verify` then accepts a signature any key in that merged keyring validates. No per-remote state records which key it was.

So "the anchor a remote rests on" is not a question configuration can answer: for a remote holding no keyring of its own it is the whole merged set, and replicating all of it reproduces exactly the trust the source's remote had. `_anchors_to_import` therefore carries every anchor the target lacks, and the only narrowing available is libostree's own file filter, which the anchor read applies (`*.gpg` in the glob, `trustdb.gpg`/`secring.gpg` dropped after it).

One key source stays outside both: the ostree per-remote configuration option `gpgkeypath`. Setting `remote "r1"`'s `gpgkeypath` to a key file made a pull exit 0 with no anchor and no keyring. flatpak never writes that option, so it can only reach a machine through a hand-edited repo config, and `_stage_source_keys` does not read it.
