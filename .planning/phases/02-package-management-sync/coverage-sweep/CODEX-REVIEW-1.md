Missing branches

PKG-FR-APT-ORIGIN-DERIVED — No row covers an approved vendor install whose origin needs a pin: the pin must land without a separate or subsequent question.

PKG-FR-REPO-CONFLICT — No row proves that declining one conflicted repository fails multiple approved packages that depend on it.

PKG-FR-REPO-DELETE — No row covers a repository used only by a proposed removal that the user declines; the repository must remain unraised and undeleted.

PKG-FR-KEY-COPY — No row covers a target-missing key owned by packaging on the source; it must still be copied byte-for-byte.

PKG-FR-KEY-REFRESH — No row covers a differing target key owned by a non-distribution vendor package; the distribution-owned exception must not protect it.

PKG-FR-APTCONF / PKG-FR-MACHINE-SPECIFIC — No row covers permanently declining overwrite of a changed apt configuration file, including which holding machine records the mark and its later protection.

PKG-FR-COLLATERAL-MANUAL — No row covers newly discovered apply-time removal, downgrade, or upgrade collateral by asking the required three-way question before proceeding.

PKG-FR-SNAP-FAIL-ITEM — E50 covers an unavailable revision during change, but no row covers an unavailable revision for a source-only snap install.

PKG-FR-SNAP-HOLD — E60 covers a target orphan hold; no row covers the specified source-side hold recorded for a snap the source no longer has, which must produce no item.

PKG-FR-SNAP-DATA-BOUNDARY — No row excludes a source revision’s data after its install/change was declined or failed and the target’s snapd therefore never installed that revision.

PKG-FR-VERSION-FLOAT — No flatpak row asserts that installation names no commit/version and accepts the target repository’s offered build.

PKG-FR-FLATPAK-INSTALL-ORIGIN — No row covers the remote’s verification setting changing between the pre-install check and the post-install read-back.

PKG-FR-FLATPAK-MASK — No row covers approving an unmask and observing the target-only pattern actually removed.

PKG-FR-REGISTRY-CONSENT — No row covers several lost and changed target entries together and requires every affected entry to be named.

PKG-FR-REGISTRY-CONSENT — No unambiguous row covers an absent source registry with a non-empty target registry; that transfer would lose target entries and requires consent.

Wrong or non-observable Expected columns

PKG-FR-DEB-OWNERSHIP — A15 incorrectly calls target-only hand-`.deb` handling an article collision; the specific ownership rule and user intent require no apt item and no removal.

PKG-FR-MANUAL-SCOPE — G5 wrongly excludes an automatically marked hand-`.deb`; the article covers every installed version supplied by no configured repository, not only `apt-mark showmanual`.

PKG-FR-KEY-REFRESH — C3 says an identical repository file entails no key work, contradicting key rotation and C75; the key may differ independently.

PKG-FR-REPO-DELETE — C50 counts removal candidates rather than approved removals, permitting a repository deletion even when the package removal is declined.

PKG-FR-KEY-REFRESH — C81 exempts every dpkg-owned target key, while the article exempts only keys owned by the target’s distribution packaging.

PKG-FR-COLLATERAL-MANUAL — D39–D41 replace the required late consent question with an automatic refusal/failure.

PKG-FR-FLATPAK-INSTALL-ORIGIN — F79 says a verification mismatch names only the settings; the article requires either origin-check failure to name both URLs.

PKG-FR-FLATPAK-FILTER — F118 leaves a target-only filter untouched, contradicting the article’s unqualified “source does not restrict” branch; this also exposes a conflict with PKG-FR-FLATPAK-REMOTE-DERIVED that the criteria must resolve.

PKG-FR-MANUAL-RESOLUTION — G46 reports findings as unanswered, creating a fourth state despite the article requiring exactly snippet, machine-specific, or skipped-for-this-run.

PKG-FR-MANUAL-RESOLUTION — G48 permits an answered run to retain an unanswered item, again contradicting the exhaustive three-state rule.

PKG-FR-REGISTRY-CONSENT — G72 treats changed label/authoring data as unchanged even though the target’s entry is being changed.

PKG-FR-READ-FAILS-JOB / PKG-FR-REGISTRY-CONSENT — G79 treats an unreadable target registry as empty and overwrites it without consent instead of refusing to infer that it holds nothing.

PKG-FR-APT-SCOPE / PKG-FR-APT-ORIGIN-VERIFY — A18, A26, A45 and A66–A74 prescribe probe counts, parser algorithms, and exact commands rather than observable required outcomes.

PKG-FR-APT-HOLD-ITEM / PKG-FR-APT-HELD-TARGET — B8, B20–B21, B30, B40, B48 and B51 specify simulations, command selection, and read strategy rather than article outcomes.

PKG-FR-REPO-DELETE / PKG-FR-KEY-COPY / PKG-FR-PIN-ALWAYS / PKG-FR-APT-CONFIG-ATOMIC — C6, C9, C39, C53–C54, C59–C60, C75–C76, C83, C89, C91, C107, C109, C126, C151, C155 and C158 are implementation mechanics not imposed by their articles.

PKG-FR-COLLATERAL-MANUAL / PKG-FR-COLLATERAL-ATTRIBUTION — D8’s comparison-command clause, D38’s rehearsal flag, D52–D53’s rehearsal counts, and D62–D63’s simulation counts are implementation details.

PKG-FR-SNAP-SCOPE / PKG-FR-SNAP-REFRESH-PAUSE — E22–E23, E29, E74, E88, E93–E94 and E99 specify parsing, privilege mechanism, clock calculation, or cleanup internals rather than observable outcomes.

PKG-FR-FLATPAK-SCOPE / PKG-FR-FLATPAK-REMOTE-DERIVED / PKG-FR-FLATPAK-REMOTE-TRUST — F2, F8, F29, F32, F36, F59, F74, F107, F114, F120 and F132 specify digest reads, staging, command counts, mutation annotations, comparison, or parsing internals.

PKG-FR-MANUAL-SCOPE / PKG-FR-SNIPPET-VERBATIM / PKG-FR-REGISTRY-SYNCS — G7, G14, G20, G52, G61, G63–G65, G76, G84, G89 and G91–G92 specify exact scans, witnesses, stamps, storage mechanics, plumbing, or UI material not required by the articles.

Duplicate branches

PKG-FR-DISTRO-ORIGIN / PKG-FR-APT-ORIGIN-DIFF — A23 and A61 duplicate the different-Ubuntu-mirrors/no-divergence branch.

PKG-FR-APT-ORIGIN-DISCLOSURE — A27 and A39 duplicate the distribution-origin install with no origin text.

PKG-FR-REPO-DERIVED — A28 and C21 duplicate the already-served-origin/no-repository-write branch.

PKG-FR-REPO-DERIVED — A29 and C22 duplicate the source-only vendor-origin/derived-repository branch.

PKG-FR-REPO-DERIVED — A36 and C24 duplicate the unreproducible-origin/empty-derived-set branch.

PKG-FR-APT-ORIGIN-UNREPLICABLE — A34 and C86 duplicate the missing-key report-only branch; A35 and C87 duplicate its one-sound-file exception.

PKG-FR-APT-ORIGIN-DERIVED / PKG-FR-KEY-COPY — A51 and C1 duplicate approval carrying a repository/key without another question.

PKG-FR-REPO-DERIVED / PKG-NG-APT-IDENTICAL — A65 and C2 duplicate the unused-source-repository/no-sync branch.

PKG-FR-APT-REMOVE / PKG-FR-COLLATERAL-MANUAL — A55 and D36 duplicate the cascade onto another approved removal.

PKG-FR-APT-HOLD-ITEM — B3 and B12 duplicate the both-machines-hold/no-item branch.

PKG-FR-APT-HELD-TARGET — B11 and B15 duplicate suppression of a held target package’s version-difference item.

PKG-FR-FLATPAK-REMOTE-DERIVED / PKG-FR-FLATPAK-REPOINT — F20 and F45 duplicate silent repointing of a differing URL with no protected application.

PKG-FR-FLATPAK-SCOPE / PKG-FR-FLATPAK-MASK — F3 duplicates the add/remove mask branches later stated separately by F122 and F123.

PKG-NG-MANUAL-REMOVE — G22 duplicates G88’s no-removal outcome and G89’s no-target-query outcome.
tokens used
154,407
