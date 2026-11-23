# dave-fork-ci

Reusable upstream maintenance for GitHub forks with Nix packaging and local changes.

The composite [action.yml](action.yml) handles Git preparation/promotion without executing candidate code. The reusable [reusable workflow](.github/workflows/upstream-sync.yml) connects it to isolated native builds, checks and Niks3 publication. A small generated caller is the only workflow each consuming fork needs.

## Remotes and which is canonical

- **GitHub `dweee/dave-fork-ci` is canonical for this tree.** Its CI runs there (the ruleset requires the `test` check), and the generated fork callers reference `dweee/dave-fork-ci/.github/workflows/upstream-sync.yml` at a pinned commit, so releases are cut from here. Land changes through a GitHub PR.
- **Forgejo `d/dave-fork-ci` (remote `gmf`) is the private mirror used by the fleet repositories.** It runs no CI. The installer and the helper copies it generates for the Forgejo repositories are taken from a checkout of this tree. After a GitHub change lands, bring Forgejo in line with a Forgejo PR that merges GitHub `main` into Forgejo `main` with a merge commit. Never force-push either branch and never commit to Forgejo `main` first: the two histories would diverge again.

## Behavior

1. Poll the configured public upstream branch, latest stable GitHub release, or both. A six-hour caller schedule works without upstream cooperation; an optional `repository_dispatch` can wake it sooner. A release in somebody else's repository does not directly trigger your fork's workflow. Drafts and prereleases are excluded.
2. Three-way merge the resolved upstream commits into the fork default branch in a scratch clone. Preserve fork commits and ancestry; never reset/rebase away local patches. Keep the fork's control and protected paths, including when upstream only conflicts there. Record resolved branch/release SHAs and publish an immutable `sync/upstream/<commit>` branch.
3. Scan incoming Git history with trusted Gitleaks rules. Reject on-prem-only derivations before building. Then build selected packages/checks on each configured native platform and run configured argument-vector test commands. Package builds do not receive Git write or cache publication credentials. An unchanged upstream/release state uses no macOS build job.
4. Each cache-enabled row retains completed outputs from its selected package/check derivation graphs, including build-only dependencies and work completed before a later failure. Export their runtime closures as bounded data after enforcing the public policy; enforce that policy again after import. Fresh Linux publisher jobs install the trusted pinned Niks3 client before importing NAR data, then upload through a repository/ref/event-bound GitHub OIDC grant. Available partial artifacts remain reusable even when another row fails. Darwin packages can be uploaded from Linux without running them.
5. After **all required builds, checks and cache uploads succeed**, a fresh Git-only job checks the candidate SHA, ancestry, protected paths and current main. It opens or reuses a pull request for the exact candidate, verifies the live base and head, and requests a merge commit through GitHub's pull request API. There is no force push, branch-protection bypass, package rebuild or candidate-script execution in this job. Cache failure prevents main promotion; partial cache publication never turns a failed candidate into a release.

When `cache: false` is explicitly selected, builds/checks remain mandatory and cache publication is skipped. Automatic promotion additionally requires both `auto-merge: true` and repository variable `FORK_CI_AUTO_MERGE=true`. The entire workflow also requires `FORK_CI_ENABLED=true`. The reusable workflow is installed; consumers explicitly enable it using those variables. To merge upstream candidates, the caller grants `pull-requests: write` and the repository allows GitHub Actions to create pull requests and GitHub merge commits.

Each matrix row's `tests` must pass. Commands under `advisory_tests` (for example `cargo fmt --check` on upstream-owned code) run after them and only print a warning when they fail; the HEAD and tree-cleanliness checks still apply.

Builds reject lockfile updates, and tracked source/submodule changes or a changed Git HEAD during tests prevent a success manifest. Upstream dependency/hash changes that need packaging repair remain a review candidate; the workflow does not test an implicitly edited tree and then promote a different commit.

## Adopting a fork later

This tool manages the GitHub fork's default branch. Importing its outputs or updating downstream lockfiles is a separate integration step.

Publish a reviewed release of the controller repository. A reusable workflow must be under `.github/workflows` to be callable.

In each approved public fork, add a reviewed `.fork-ci.json` based on [the example policies](examples). Configure upstream, tracking mode, Git author, required custom files, native package/check attributes and regression tests that actually prove your changes. `required_paths` detects removal, not semantic correctness: tests must establish behavior. A passing package build alone is not a security review, and some package recipes deliberately disable tests. The examples are adoption drafts, not evidence of successful application builds.

For a fork whose upstream history contains private agent artifacts, configure
`history_exclude` as a list of literal repository-relative paths, such as
`[".claude", ".omx"]`. Apply the same filter to the initial imported history.
The controller deterministically filters incoming history before merging and
verifies that mapping again before promotion. Authors, dates, messages and source
changes remain; affected commit IDs change and invalidated signatures are removed.
Excluded files never enter the published candidate ancestry. Control files and
required source paths cannot be excluded. Changing the filter requires local
review and rebuilding the fork's filtered baseline.

Generate a staged caller with the actual published action repository and its exact release commit:

```sh
python3 bin/render_caller.py --tool-repository YOUR-OWNER/dave-fork-ci \
  --tool-revision YOUR-REVIEWED-40-CHARACTER-COMMIT --output /tmp/fork-sync.yml
```

The tool rejects mutable pins and refuses to write directly into live workflow directories. After reviewing the generated file, install it as `.github/workflows/fork-sync.yml` in the fork. The caller and `tool-revision` must identify the same reviewed action release. The default branch can be named `main` or another valid branch; it is taken from GitHub's repository metadata.

The cache administrator must register the owning repository, its trusted source
refs and allowed events with the Niks3 OIDC provider. No static fleet or cache
administrator credential belongs in GitHub. The generated caller grants
`id-token: write`; the publisher requests audience `https://niks3.monke.fail`.
GitHub documents the required [OIDC permission and reusable-workflow context](https://docs.github.com/en/actions/reference/security/oidc).

Unreviewed upstream candidate builds use public caches only. Approved default-branch
native and release jobs may separately configure the short-lived read-only audience
`https://niks3.monke.fail/read` through `ci/configure-niks3.py --oidc`. Its protected
netrc refreshes during long jobs and is removed before artifact export. PR events
receive no private-cache reader. Niks3 publication scope also implies reads; only
the dedicated reader grant is read-only.

The workflow installs pinned Determinate Nix 3.24.1. Trusted scanner installation
uses official public substitutes. The publisher client is installed from the
exact trusted source in `ci/install-niks3.sh` before candidate data is imported.
It may build that pinned public client if a substitute is unavailable; it never
runs candidate package programs in the publisher job.

The `cache-http2` input still controls Nix transport behavior and defaults to true.
Set it to false when a cache proxy cannot reliably serve HTTP/2; certificate and
Nix signature checks remain enabled.

## Failures and review

Conflicts outside the fork's control paths, upstream branch rewrites, and moved release tags stop automatic maintenance. Upstream edits to `.github`, `.forgejo`, `.gitmodules`, `.fork-ci`, `.fork-ci.json` and configured protected paths stay on the fork version, including modify/delete conflicts limited to those paths, so an upstream workflow tweak does not block the source update. A symlink planted on one of those paths is removed without being followed. New secret detections also stop builds. Remaining conflicts require a local review and an ordinary reviewed commit; broad scanner exclusions and dropping local changes are not automatic repairs. A clean candidate branch remains available when a later build/cache/promotion fails. Conflicting merges do not publish a candidate. Concurrent operator work is preserved by rechecking the expected base and passing the verified head SHA to GitHub’s merge API.

Do not run this on persistent self-hosted workers: build/dependency code can modify its runner. Git-write jobs and cache publishers must stay on fresh GitHub-hosted runners. Candidate NARs are treated as untrusted data, and cache writes stay in the isolated candidate cache. Passing tests does not prove an upstream release harmless; you must trust the configured upstream and maintain meaningful policy tests.

## Local validation

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 ci/test-push-niks3.py
PYTHONDONTWRITEBYTECODE=1 python3 ci/test-transfer-closures.py
PYTHONDONTWRITEBYTECODE=1 python3 ci/test-public-closure.py
actionlint -shellcheck= -pyflakes= .github/workflows/upstream-sync.yml
```

Tests include real local HTTP Git repositories for prepare → immutable candidate → promotion → unchanged/no-op, stable release publication over already merged source, rewound upstreams, conflicts, protected files, preserved history, explicit authorship and stale-main rejection. Local tests cannot prove GitHub permissions, hosted runner behavior, branch rules, cost or authenticated Niks3 OIDC uploads. Those remain unverified until the supervised pilot.

Reference: [GitHub reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows), [event behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows), [workflow security](https://docs.github.com/en/actions/reference/security/secure-use).

Every successful candidate build retains a Nix closure archive for one day, independently of Niks3 publication.

### Optional Determinate Nix on Forgejo runners

`ci/install-channel-automation.py` accepts two Forgejo-only policy fields:
`forgejoDeterminateImage`, a local `sha256:` image ID, and
`forgejoDeterminateLabels`, a list containing `nix` and/or `nix-light`. The
selected labels must be backed by a runner where that exact image is installed.
Generated jobs use their own ephemeral container store, start the matching
daemon and enable two parallel evaluation workers. Other labels retain their
existing setup. This option does not distribute images or migrate persistent
stores; arrange and verify those separately before enabling it.

Trusted owning-repository release channels also retain completed build-only
dependencies from each selected package/check derivation graph. JSON v4 fixed
output identities are resolved by Nix, then intersected with the local daemon’s
valid paths; unrelated store outputs and unfinished derivations are excluded.
These uploads happen even when the selected build fails later. A substituted
dependency can be omitted as an export root only when a listed public cache
serves a fresh narinfo with matching metadata and a valid signature from its
paired key, and serves the first NAR byte to a ranged GET on the same HTTPS origin.
The selector keeps content-addressed narinfo because Nix can accept it without
a signature. It also keeps outputs whose probes fail or exceed the deadline.
Some public caches evict old paths, so a later rebuild may have to build an
omitted dependency. This only affects build-time dependencies.
Successful package roots and retained roots still export their complete runtime
closures. This also applies to isolated upstream-candidate and migrated native
artifact workflows. Their separate cache manifests include completed selected graph
outputs after failure; archives keep the existing path/byte bounds and public
guards. Builds have no writer, and publisher jobs import data without executing
candidate programs. Channel promotion still requires every original build,
native check and signed cache verifier; upstream promotion still requires every
build/check row and cache job to pass.

Publisher installation uses `ci/install-niks3.sh --retain-bootstrap` to retain
the pinned public client's own completed derivation graph in
`niks3-bootstrap-outputs.txt`. A separate trusted publisher step uploads that
manifest before importing candidate artifacts. Installation strips writer,
forge and OIDC request credentials while preserving read-only substitution;
running the installer without the flag does not create a publication manifest.
Private Darwin jobs recheck host admission after graph evaluation. Publication
and credential cleanup disable source builds, so they can finish after the
host's build window closes. Read-only release-retention jobs gain no publisher.

### Native runner selection

A policy matrix row may set `"runner": "xcode-27"` for `aarch64-darwin`
when its owner requires the maintained macOS 27 / Xcode 27 route (for example,
Pared). Omitting the field preserves the existing platform defaults. The
controller accepts only its explicit per-platform route allowlist; arbitrary
labels, expressions and cross-platform overrides fail before preparation.
A label does not prove the toolchain version: retain the owning package's
native OS/SDK checks and treat an unavailable matching runner as pending.

### Forgejo private Darwin route and shared daemon

Both are explicit opt-ins. Without `forgejoSharedDaemon`, regeneration emits
exactly the previous workflows and helper set: plain `nixos/nix` containers, or
the per-job isolated Determinate daemon for `forgejoDeterminateLabels`.

`forgejoSharedDaemon: true` (Forgejo only) makes every generated Icicle `nix`,
`nix-light` and `darwin-tools` job connect through `ci/connect-shared-daemon.sh`
to the untrusted listener of Icicle's one shared store. Those jobs mount the
read-only aliases `flakey-ci-nix-pr-v2-ro:/nix:ro` and
`flakey-ci-nix-pr-socket-untrusted-ro` (so no job can replace the listener's
socket), never the store or socket volumes, the trusted socket or the fleet's
credential tmpfs; they start no private daemon or new store. As untrusted
clients they can build, add content-addressed paths and add signatures, but the
daemon ignores restricted client settings and trusts no Niks3 key: Niks3 signs
whatever any write-token holder pushes, so trusting it would let one repository
plant another's (or the fleet's) outputs. Substitution therefore comes only
from public caches, and everything else is built. Niks3 uploads and signed-cache
checks keep using the job's own credentials client-side. Two flows that import
Niks3 paths cannot run on the shared daemon: retained archives (the generator
refuses the opt-in for a repository with `ci/retained-archives.json`) and the
private Darwin retention pin (Darwin releases are published and verified but
not pinned, so Niks3 keeps them for its push-age window). The opt-in also
requires the shared daemon's exact image, since the connector pins its Nix. Opt in only after the owner
has deployed the matching runner/DinD/socket layout from Flakey; until then the
light runner does not admit those volumes and the connector times out. The
daemon alone writes store files; tool jobs are never trusted clients of it.

`privateDarwin` requires `forgejoSharedDaemon`. After host/runtime
qualification, set it with public `attestorPublicKeys` and exact `clientCommit`
and `providerCommit`. Promotion trusts exactly those attestor keys from the tool
repository's own reviewed `ci/channels.json`; changing them is a reviewed change
to that repository, and the host worker still only signs for admitted commits.
Darwin waits use the separate bounded `darwin-tools` label and a repository's
8 MiB metadata-only volume at `/run/darwin-tool-metadata`; that volume is not a
Nix store. The client stops waiting 45 minutes before the 6-hour job timeout so
its cancellation, verification and pinning always run. Provision
`PRIVATE_DARWIN_HMAC_KEY`, `FLAKEY_NIKS3_READ_NETRC` and the existing cache
publication grant in each tool repo. Host readers and HMACs are
repository-specific. Every changed tool commit requires an exact reviewed
Flakey admission PR, drain and reinstall before it can build. The host worker
never uses the shared store: it fetches, evaluates, admits, builds and hashes
each request in its own disposable per-request store, independently checks the
admitted source before fetching, fetches locked inputs with its scoped reader,
then evaluates offline without credentials/network. Promotion verifies signed
proof files and the exact selected cache closure; forge tokens never reach its
Nix verifier. GitHub counterpart workflows keep their existing route. Intel
requires qualified Rosetta/Nix compatibility. Flakey's
`docs/ci/private-tool-darwin.md` contains the installation, key validation,
runtime qualification and verified rollback steps.
