# Hosted Nix cleanup trial

This records the initial opt-in evaluation of `nothing-but-nix`'s `cleave` profile
on disposable GitHub-hosted runners. During that experiment the production
upstream-sync workflow, channel generator and consumer callers were unchanged.
The subsequent rollout adds cleanup to hosted native build jobs only; see the
README for its boundaries. The experiment workflows are manual-only. Nothing
runs on persistent or Forgejo runners, and the trial neither publishes caches
nor promotes a branch/channel.

The smoke matrix compares the current manual Linux SDK cleanup (and unchanged
macOS image) with `cleave` on Ubuntu x86, Ubuntu ARM and Apple silicon macOS.
Both sides install the same Determinate Nix, compile and execute a small native
C program, export its closure outside `/nix`, and verify the output. Linux
`cleave` builds use `/nix/build`; macOS keeps the installer's APFS store and
normal build directory. Foreground cleanup provides a complete purge duration
before the ready measurement. This deliberately measures setup overhead without
overlapping deletion and compilation. Production background mode needs separate
qualification if adopted.

Reserve 16 GiB on Linux outside the new store for closure exports, upload staging,
checkouts and logs. The trial does not claim the maximum store sizes from the
action's README. Measurements include image identity, initial/ready/final disk
headroom and elapsed time, retained as per-job JSON artifacts. Compare only like
runner images; timings also include installer/network variability. A small
compilation validates operation, not heavy-build throughput or peak disk use.

After every smoke job passes, the fork matrix runs each selected native row's
complete packages, checks and required test commands through `bin/build.py`.
Sources are pinned to the audited public default-branch revisions in
`hosted-nix-trial.json`. The catalog builds Atuin on both Linux architectures.
All builds use public cache policy only, with no OIDC grant, cache credentials,
Git write token or publisher. A failed dependency download, missing private
substitute, pre-existing source failure, unavailable runner or timeout must be
reported separately from cleanup incompatibility. One selected platform per
fork is not complete native-platform coverage.

## Consumer audit (2026-11-25)

Seventeen forks have a pinned caller of the shared upstream-sync workflow:
Handy, larperCord, niks3, paneru, pared, rekordbox-mcp, schildi-revenge, tabrix,
kopuz, opencode-free-router, kuna, ha-mcp, Patchbay, ghidra-mcp,
codexbar-plasmoid, tuwunel and postmerkos-ui. Exact source revisions and selected
policies are recorded in the JSON inventory. Sixteen get a runtime trial.
Ghidra MCP selects `retained-java`, which must use its existing owning artifact
and cache route; this trial does not grant those credentials or rebuild a
retained artifact from another source.

The shared native build job uses JavaScript checkout/upload actions and invokes
Nix and Python directly. It has no container action, `container`, `services`,
Docker CLI or Docker daemon requirement. The selected fork policies use Nix
packages and Nix development environments. They do not require the preinstalled
SDKs that `cleave` removes. Pared retains its explicit `xcode-27` runner so its
native SDK checks remain meaningful; default Xcode survives `cleave`.

Repositories may also have unrelated upstream workflows. For example, Niks3's
Docker-image release workflow has registry login and provenance steps. It is
outside this trial, as are Handy's upstream Bun/Playwright workflows. Do not
insert cleanup before every Nix installer indiscriminately. Generated release
builders and isolated cache publishers require their own staging-space and
credential-boundary review before adoption.
