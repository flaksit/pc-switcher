# Flatpak ref filters and libostree trust anchors, measured

Two premises `flatpak_sync` rests on, measured rather than reasoned about. Both hold.

Everything below ran in a throwaway `ubuntu:24.04` container: flatpak `1.14.6-1ubuntu0.1`, `libostree-1-1` `2024.5-1build2`.

## Does flatpak refuse to install a ref its own remote filter excludes?

Yes. `PKG-FR-FLATPAK-FILTER`'s ordering — clear the target's filter before the installs, re-apply the source's after — is necessary, not merely harmless.

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

So the late re-apply costs the target nothing that has already landed, and the early clear is what lets the approved applications land at all.

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

One key source stays outside both: the ostree per-remote configuration option `gpgkeypath`. Setting `remote "r1"`'s `gpgkeypath` to a key file made a pull exit 0 with no anchor and no keyring. flatpak never writes that option, so it can only reach a machine through a hand-edited repo config, and `_stage_source_keys` does not read it.
