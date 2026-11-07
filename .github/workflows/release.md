# Release workflow

`release.yml` runs when a release is published. It stamps the version into the tree, moves the tag onto the stamp, uploads a source tarball and renders the Homebrew formula into the tap. `verify.yml` is shared with `ci.yml` and gates all of it. The step-level reasoning is in the comments of the workflow files. This page keeps the failure shapes and the mechanics that cut across jobs. For why boxes build from the tag at all, see [Distribution](../../docs/distribution.md).

## Moving the tag

The release workflow takes the published release's tag (`github.event.release.tag_name`) as the single source of truth. It writes `.version` from the tag, commits the stamp to `main`, and force-moves the tag onto the stamp commit.

Moving the tag is the part worth understanding. Both channels read `.version`, but at different moments. Homebrew reads it from the `git archive` tarball that CI builds, there and then. Nix reads it from whatever tree the tag points at, whenever a guest later fetches the flake ref. Writing the stamp into the tarball alone would leave every nix fetch on a stale version. The stamp has to be inside the tagged tree, and moving the tag puts it there.

A guard makes that safe. The workflow refuses unless the release tag is already the tip of `main`, because force-moving a tag cut from an older commit would republish different content under a name someone may already have fetched. It accepts one other shape, the state its own failed run leaves behind: the stamp commit pushed and the tag not yet moved. That is `HEAD` being a `release: <version> [skip ci]` commit that changes only `.version`, directly on top of the commit the tag still names. Without that exception, a run that died between the push and the tag move could never be re-run, and the only way out would be to hand-move a published tag.

Recognise a release created from a feature branch by a workflow failure with an explicit refusal message, before any tag move or archive.

The workflow fires on the `released` event only, not `published`, so a prerelease or a draft does not start a tag move. The `release` job's first step (after `verify`) validates the tag against `^v[0-9]+\.[0-9]+\.[0-9]+$`. The version is spliced into the tarball name, the formula URL and the tap commit message, so a tag with a slash, a quote or a newline in it must be refused before a tag moves.

### Immutable releases

Immutable releases must stay off. This is a deliberate cost, not an open problem: the tag is the pin every box builds from, and the release process owns moving it. `release` re-uploads the tarball with `--clobber` and force-moves the tag, and an immutable release refuses both.

## bump-tap and actions/checkout

Symptom: `verify` and `release` both succeed, then `bump-tap`'s bare `actions/checkout@v6` dies on the tag that `release` just moved, with `The ref 'refs/tags/<tag>' does not point to the expected commit '<sha>…'. The ref may have been updated after the workflow was triggered.`

With no `ref:`, checkout resolves `github.ref` (the release tag) and asserts that the tag still points at `github.sha`, where it pointed when the release was published (`testRef` in `src/ref-helper.ts`). Two jobs earlier, `release` had moved the tag onto its own stamp commit, so the assertion fails by construction. This is actions/checkout's behaviour from v6.0.2 on (`actions/checkout#2356`, "Fix tag handling"; v7.0.1 keeps it). It fetches a tag by name and compares it, so a moved tag is noticed, and no input turns the comparison off.

The check is skipped only when `ref:` names an explicit branch or tag, because `commit` is then never assigned. That is why `release`'s own `ref: main` checkout was never at risk. It is also why `bump-tap` now pins `ref: ${{ github.event.release.tag_name }}`, the moved tag, whose tree is exactly what the tarball was built from.

Two rules follow from the same mechanism:

- After a release failure, re-run only the failed jobs, never the whole run. `verify.yml` would otherwise replay against the already-moved tag, and its checkout stays bare on purpose because `ci.yml` shares it.
- A re-run always executes the workflow file from the triggering commit, so a fix to the workflow itself can only be proven by cutting a new release.

### The formula

`homebrew/yolobox.rb` is rendered into the tap from the same tarball, hashed locally rather than re-downloaded, so the tap never holds a hand-edited formula and the hash never rests on GitHub's auto-generated tarballs, which are not stable. The `release` job builds the tarball once, names it in a step output that the upload reads too, and refuses a hash that is not 64 hex digits before it is written. `bump-tap` fills the holes with `sed`, then greps the result for a leftover `@HOLE@`. A hole `sed` was not told about would otherwise ship a formula with a literal `@SOMETHING@` in it. `grep` exits 1 for "no hole left", which is the good case, so the step handles three exit codes. Any status above 1 is a real error and must not read as success.

Before any of that, the `brew` job builds the same formula on a macOS runner from its own archive of `main`, served to brew over a `file://` URL through a local tap (`brew tap-new`), runs `brew install --build-from-source`, `brew test` and `yo --version`, and `release` needs it: a formula that does not install, or a `yo` that still reports `dev`, stops the tag from moving. The tarball differs from the release's only by the `.version` stamp, and the version brew splices comes from the file name either way.

The formula carries `depends_on :macos`, so a Linux machine is refused at install with brew's own message. It pins no architecture: the guest runs the Mac's own CPU, so Intel and Apple silicon Macs install the same formula.

## Early refusals

`bump-tap` runs after the tag has moved, so everything it needs is checked before. The `release` job checks that `HOMEBREW_TAP_TOKEN` is non-empty. It then does a dry-run `git push` of a per-run branch name to the tap, so a token that is set but read-only fails before the tag moves, not after. The dry run sits after the checkout because it needs a local repo. The REST `permissions.push` field was dropped as the probe, because it reports the token owner's role and not a fine-grained token's own grant, so it can say `true` for a read-only token.

### Permissions

Workflow permissions are `contents: read` at the top and `contents: write` only on the `release` job. Every checkout that does not push has `persist-credentials: false`, so a step that runs a script from the tree holds no writable token.

## verify.yml

`verify.yml` is the one workflow that `ci.yml` and `release.yml` share, and `release` declares `needs: verify`, so whatever it checks gates a tag move. It has three jobs:

- `lint` runs `ruff check`, `ruff format --check`, the `yo --help` smoke test, and shellcheck at warning severity on every tracked `*.sh` (the guest helper, the initrd and egress scripts, the recorder, `linux/host-setup.sh`, the libvirt hook `linux/yolobox-libvirt-hook.sh` and the scripts under `tests/`), listed once in `verify.yml`; `CLAUDE.md` carries the same command, and `TestShellcheckListsAgree` keeps both lists equal to `git ls-files '*.sh'`. The shellcheck binary is `shellcheck-py`, installed at a pinned version, so a runner image's own shellcheck never decides. The job also runs `./yo --help` as a smoke test.
- `unit` runs `./yo --help` and the unit tests on Python 3.9 and the newest 3.x, so the smoke test runs under the floor interpreter too. 3.9 is the floor `yo` promises, because the Mac's Xcode Python is that. The newest catches the other direction. `yo` is stdlib Python, so the Python version is the only axis: both legs run on one runner, `ubuntu-24.04`, because setup-python has no 3.9 for `ubuntu-latest`'s 26.04 image; [TODO.md](../../TODO.md) says what replaces the pin. No OS axis: what differs between hosts (vz, libvirt) cannot run in CI.
- `eval` is a two-leg matrix on `cachix/install-nix-action@v31`, one leg per guest CPU: `--system x86_64-linux` (a Linux host's box, or an Intel Mac's) and `--system aarch64-linux` (an Apple-silicon Mac's box). The guest is never told its host OS, so the CPU is the only axis a box varies on. The job sets `YOLOBOX_USERNAME` and `YOLOBOX_FLAKE_REF`, which `flake.nix` requires. Each leg runs `nix eval --impure --raw --option allow-import-from-derivation false .#nixosConfigurations.yolobox.config.system.build.toplevel.drvPath` and then every other step of the job; the step names in `verify.yml` are the list, each with a comment saying which story it holds. Two kinds of step: the ones that read a setting back (the podman log driver, the hardening settings, the rendered sysctl file, the swap option, `AllowUsers` and the gateway DROP rule, the stage-2 banner mark, the github.com host keys) and the ones that prove a refusal or an invariant (the flake throws without `YOLOBOX_FLAKE_REF` or with `path:/x`, and with the agent's or root's name as the operator; the box is the same derivation whatever `YOLOBOX_HOST_OS` says; a docs-only change keeps the generation; rosetta is on exactly for the aarch64 guest; `lima-guestagent` gates on its binary; `lanAllow`, the wrapper allowlist and the agent's groups reject what they must). The x86_64 leg also builds the `yo` package and checks that `yo --version` reports `.version`. `nft --check` parses the egress rule files, and the fixture diff keeps `tests/fixtures/` equal to a fresh render. The rule files are `builtins.toFile`, so they print on any system without a builder. Both legs must produce the same rule files, because the fixtures are one set.

`eval` evaluates the whole box configuration without building it, so an invalid option fails CI before a tag is moved, not on a user's `yo bootstrap`. An option that no nixpkgs branch defines, such as a misspelled `programs.<name>.enable`, would otherwise make a release that cannot be rebuilt. It needs no aarch64 builder because the derivation path is computed and never realised, and forbidding import-from-derivation keeps it so.

## Pinned actions

Every action in the three workflows is pinned to a full commit SHA, with the tag it was resolved from in a trailing comment. The pinned actions are `actions/checkout`, `ruff-action`, `setup-python` and `install-nix-action`, and the comment is the full `X.Y.Z` tag, which `TestPinnedActionsNameTheirFullTag` in `tests/test_consistency.py` enforces, so a comment that says `v7` for a `v7.0.1` SHA cannot stay. A tag is a name the action's owner can move, while a SHA is the content, and the release job holds a writable token and the tap's. Bumping one means resolving the new tag to its SHA and changing both. `ruff-action` v4 has no floating major tag in any case, because its releases are immutable.
