# Retaining historical cache variants

A release proof binds the exact metadata and NAR bytes of every closure member.
Different valid builds of an input-addressed store path can have different bytes.
Keep the canonical Niks3 entry intact when migrating such an older release.

An optional `ci/retained-archives.json` selects data archives already published to
Niks3, by original release commit and system:

```json
{"version":1,"releases":{"<40-character-commit>":{"x86_64-linux":"/nix/store/<hash>-retained-release"}}}
```

An archive contains `manifest.json` and a `cache/` binary-cache directory. The
manifest records version 1, commit, policy digest, system, sorted roots, complete
closure metadata, and `archivedPaths`. The cache contains `nix-cache-info` and
only those paths' original narinfos and NAR files. NAR URLs must be local
`nar/<store-hash>.nar` paths. This is data; no historical helper or program runs.

The verifier freshly reads and verifies the archive NAR from Niks3 before
copying it. Its complete metadata must reproduce the existing immutable release
proof before any variant is selected. Every unchanged member is still read from
its normal cache. Every archived member is content/signature checked against its
original metadata, with the original Attic signer trusted only for that local
archive. Canonical variants cannot be adopted during this check. The final proof
must still equal the original proof. No Attic network access is needed in CI.

Keep migration archives pinned in Niks3 while their releases remain retained.
The archive and its required cache objects must not disappear during GC.
