# dave-fork-ci

Reusable upstream maintenance for GitHub forks with Nix packaging and local changes.

The composite [action.yml](action.yml) handles Git preparation/promotion without executing candidate code. The reusable [reusable workflow](.github/workflows/upstream-sync.yml) connects it to isolated native builds, checks and Attic publication. A small generated caller is the only workflow each consuming fork needs.

## Behavior

1. Poll the configured public upstream branch, latest stable GitHub release, or both. A six-hour caller schedule works without upstream cooperation; an optional `repository_dispatch` can wake it sooner. A release in somebody else's repository does not directly trigger your fork's workflow. Drafts and prereleases are excluded.
2. Three-way merge the resolved upstream commits into the fork default branch in a scratch clone. Preserve fork commits and ancestry; never reset/rebase away local patches. Record resolved branch/release SHAs and publish an immutable `sync/upstream/<commit>` branch.
3. Scan incoming Git history with trusted Gitleaks rules, build selected packages/checks on each configured native platform, then run configured argument-vector test commands. Package builds do not receive Git write or Attic credentials. An unchanged upstream/release state uses no macOS build job.
4. After **every build row succeeds**, export only the selected runtime closures. Fresh Linux publisher jobs install a trusted pinned Attic client before importing NAR data and then upload to a cache-scoped candidate cache. Darwin packages can be uploaded from Linux without running them.
5. After every required cache upload succeeds, a fresh Git-only job checks the candidate SHA, ancestry, protected paths and current main. It then makes a normal fast-forward push to the fork's default branch. There is no force push, branch-protection bypass, package rebuild or candidate-script execution in this job. Cache failure prevents main promotion. Successful earlier cache rows may remain in the isolated candidate cache if a later row fails.

When `cache: false` is explicitly selected, builds/checks remain mandatory and cache publication is skipped. Automatic promotion additionally requires both `auto-merge: true` and repository variable `FORK_CI_AUTO_MERGE=true`. The entire workflow also requires `FORK_CI_ENABLED=true`. The reusable workflow is installed; consumers explicitly enable it using those variables.

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

Set `ATTIC_ENDPOINT` and `ATTIC_CACHE` as repository variables, and `ATTIC_TOKEN` as a secret scoped only to that isolated cache. Do not use a production/personal cache-wide credential. The installer is pinned `nixbuild/nix-quick-install-action` v35 with Nix 2.34.7. Trusted client/scanner installation is substitution-only from a pinned nixpkgs revision. A missing public substitute fails rather than executing build dependencies with publication credentials.

For warm builds, also set the public `ATTIC_SUBSTITUTER` URL and `ATTIC_PUBLIC_KEY`
verification key. Pass them through the corresponding reusable workflow inputs.
Package builds and Nix development-shell tests may read this public cache without
an upload token. Trusted scanner and publisher-client installation still uses only
the official Nix cache, before any candidate code or runtime closure is imported.

Enable `FORK_CI_ENABLED` for a supervised candidate-only run first; leave `FORK_CI_AUTO_MERGE` unset. Measure real cold/warm minutes, disk and artifact sizes. Closure exports default to a 2 GiB uncompressed limit per platform, configurable up to 4 GiB. Artifacts expire after one day. Test a real upload and independent consumer substitution. Once those pass and dependency/security findings are resolved, configure the repository's branch rules for the intended repository-scoped actor and enable `FORK_CI_AUTO_MERGE`. The action respects existing rules and fails if a direct push is forbidden; it does not modify them or request a bypass token.

`GITHUB_TOKEN` pushes normally do not trigger further push workflows. All required validation therefore runs in this same workflow before promotion; no unobserved downstream workflow is assumed. This action does not create GitHub releases or publish release binaries. GitHub may delay scheduled jobs or disable schedules in inactive public repositories, so this is polling, not a guaranteed immediate event subscription.

## Failures and review

Conflicts, upstream branch rewrites, moved release tags, changes to `.github`, `.forgejo`, `.gitmodules`, policy/state control files or configured protected paths stop automatic maintenance. New secret detections also stop builds. These cases require a local review and an ordinary reviewed commit; broad scanner exclusions and dropping local changes are not automatic repairs. A clean candidate branch remains available when a later build/cache/promotion fails. Conflicting merges do not publish a candidate. Concurrent operator work is preserved by checking the expected branch and using a normal fast-forward push.

Do not run this on persistent self-hosted workers: build/dependency code can modify its runner. Git-write jobs and cache publishers must stay on fresh GitHub-hosted runners. Candidate NARs are treated as untrusted data, and cache writes stay in the isolated candidate cache. Passing tests does not prove an upstream release harmless; you must trust the configured upstream and maintain meaningful policy tests.

## Local validation

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 ci/test-push-attic.py
PYTHONDONTWRITEBYTECODE=1 python3 ci/test-transfer-attic.py
actionlint -shellcheck= -pyflakes= .github/workflows/upstream-sync.yml
```

Tests include real local HTTP Git repositories for prepare → immutable candidate → promotion → unchanged/no-op, stable release publication over already merged source, rewound upstreams, conflicts, protected files, preserved history, explicit authorship and stale-main rejection. Local tests cannot prove GitHub permissions, hosted runner behavior, branch rules, cost or authenticated Attic uploads. Those remain unverified until the supervised pilot.

Reference: [GitHub reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows), [event behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows), [workflow security](https://docs.github.com/en/actions/reference/security/secure-use).

Every successful candidate build retains a Nix closure archive for one day, independently of Attic publication.
