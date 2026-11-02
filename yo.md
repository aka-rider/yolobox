# yo

Notes on the parts of `yo`, the host-side CLI, whose reasons are not obvious from reading the code. A short "why" lives in the code comment next to each piece. This file keeps the incidents, the evidence and how to recognise each failure. `yo --help`, `yo help` and `yo <command> --help` say what the commands do.

## The mirror

`yo enter`, `yo code` and `yo zed` land in the guest twin of the Mac's logical `$PWD`. That is the path relative to the Mac `$HOME`, grafted onto the guest `$HOME`, subdirectory included. Logical means `$PWD` as the shell spells it. A symlinked spelling is mirrored, never resolved, so the guest sees whatever name you `cd` through on the Mac. `yo link` maps a repo's toplevel the same way and refuses a repo outside `$HOME`.

The mapping is done by `mirror()`. The guest path it produces then goes through `guest_path()`, which is why no `yo` command can target a guest path outside the guest `$HOME`. The one exception is `yo cp`, which also accepts an absolute path under `/tmp` (see "yo cp" below).

When the mirrored path does not exist in the VM, the session lands at the deepest existing ancestor. When the Mac cwd is outside `$HOME`, it lands at the guest home. Both cases print `MISSING_PATH_NOTE` to stderr: `path "%s" does not exist in the yolobox you are in "%s"`. `yo bootstrap` needs no mirrored path, because it builds the VM straight from a flake ref and there is nothing of yolobox's own to locate inside the guest.

### The toplevel of a symlinked repo

`git rev-parse --show-toplevel` reports the repo root with symlinks resolved, but the mirror is spelled logically (say `~/code` is a link to `~/Developer`). `logical_toplevel()` therefore walks up from the cwd as spelled and takes the nearest ancestor whose `realpath` equals git's answer. When no ancestor matches, it falls back to the physical path.

It does not count the `../` that `--show-cdup` prints and apply that count to the logical cwd. That is wrong as soon as a symlink below the root makes the physical and logical depths differ, because the count then climbs to a different directory than the root. Recognise the shape by `yo link` mapping a repo to a parent of the one you stood in, or refusing it as `$HOME` itself.

## yo link

`yo link` does not refuse outright when a Mac repo already carries a `yolobox` remote. It reads the existing URL first, through `link_remote_action()`. An unrelated remote, one whose host is not `lima-yolobox`, is still refused. A `yolobox` remote whose host is `lima-yolobox` and whose path is merely stale is updated in place with `git remote set-url`, and `yo` prints the old and the new URL to stderr so the change is not silent.

A recreated box leaves exactly that stale shape behind. Every repo linked against a previous instance carries a `yolobox` URL whose path the new box does not have, something like `ssh://lima-yolobox/home/user.guest/Developer/myproject`. The update in place means no manual `git remote remove yolobox` is needed before `yo link` runs.

## yo cp

### Why scp, not `limactl cp`

`limactl cp` cannot move a file into a project the agent then has to write to. It copies as the operator (uid 501), so the file it writes is owned by an account `agent` cannot write as. A login shell that prints anything of its own (a prompt, a banner) writes onto the channel the copy's rsync or scp handshake reads, which breaks the transfer before a byte moves. `rsync` is not installed in the guest at all, so that was never a fallback.

`yo cp SRC... DST` replaces both with scp, run as the agent over the same `ssh_base` every other `yo` ssh goes through. The ownership problem never arises, and no guest shell runs, so the prompt problem cannot either.

### No shell runs, and why that holds

`yo` does not arrange the "no shell" property. The Mac ships OpenSSH 10.3, whose `scp` speaks the sftp protocol by default, so sshd answers a `yo cp` connection by spawning `sftp-server` directly. No login shell, the agent's or the operator's, runs at any point.

`nix/base.nix` leaves NixOS's default `allowSFTP = true` untouched, and that default is the whole reason this works. Turning sftp off would break `yo cp` with no other symptom to point at. The command also uses no `yolobox-guest` subcommand, so unlike the subcommands that do, it needs no box rebuild first and works against a box of any version.

### Operands

The guest side of an operand always starts with `:`, and `yo` never guesses which side is which. `yo cp --help` has the spellings. A copy has no existing-ancestor fallback the way a missing mirrored directory does, so `resolve_cp_operands()` requires every source on one side and the destination on the other. It refuses anything else outright: no colon anywhere, sources on both sides, or a copy entirely inside the guest. Each message names the remedy, plain `cp` or `yo enter`.

Two refusals are worth recognising on sight:

- A relative guest spelling (`:` or `:x`) issued from a Mac cwd outside `$HOME` has no mirror to extend. It is refused with `:~/...` named as the fix.
- A guest path that resolves outside `/home/agent` (or, for an absolute spelling, outside `/tmp`) is refused the same way `guest_path` refuses it everywhere else in `yo`.

### The `/tmp` exception

`/tmp` is accepted for absolute spellings only (`CP_GUEST_ROOTS`). Claude Code's scratchpad is `/tmp/claude-<uid>/...`, so an agent's scratch output, screenshots and exports are what a person most wants to pull to the Mac, and none of it is under the home. The relative, `:~` and bare `:` spellings keep the home-only check, so `:../../../tmp` is still refused.

Moving `TMPDIR` under `~` instead was rejected for two reasons. It changes the environment of every program in the box. And the two places age differently: `/tmp` has systemd's `q /tmp 1777 root root 10d`, which removes files untouched for ten days, while the home has no aging at all and would keep every scratch file until something deleted it. Letting scp reach `/tmp` widens nothing real, because scp runs as `agent` and that account's own permissions are the boundary, not `yo`'s path check.

### The multi-source case

scp itself requires the destination to be a directory once more than one source is given. That is why `cp_mkdir_target()` runs `mkdir -p` against the destination itself, rather than its parent, whenever there is more than one source, DST is `:`, or DST ends in `/`. The rule is scp's, not one `yo` invented.

### What reaches scp's argv

The aim is that neither a file name on the Mac nor one in the guest can steer scp.

- A Mac operand is made absolute by `local_cp_operand()`, which joins it onto the cwd and does not normalise it. That settles two cases at once: `a:b.png` is a path, not host `a`, and a name that starts with `-` is not an option. The operands also sit after a `--`. Normalising is wrong because collapsing a `..` lexically would change which file a symlinked spelling means.
- A guest source is glob-escaped by `scp_glob_escape()`, because scp expands wildcards in a remote source itself.
- A guest destination is deliberately not escaped. scp never globs a destination, so the backslashes would be taken literally and create a file named `a\[1\].txt`.
- An empty operand is refused outright, since joined onto the cwd it would mean the directory itself.

### Uploads refuse symlinks

`scp -r` follows symlinks and uploads what they point at. A link an agent left in a repo (`leak -> ~/.ssh/id_ed25519`) would therefore copy a file the user never named into the guest. `refuse_symlinks_in()` walks each source without following links before anything leaves, and the first symlink stops the copy, before even the `mkdir -p` in the guest. A source that cannot be read is refused by name too, not skipped.

The guest side is not checked. The test exists to keep the Mac's private files from entering the guest, and a guest source is something the agent could already read, with that account's own permissions as the boundary. A trailing slash on an upload source is stripped before the check, because `lstat("link/")` follows the link and would hide a symlink.

### Downloads stage, then merge

A download never lets scp write onto the Mac destination itself. scp writes through a symlink it finds there, and the agent controls what a repo it pushed contains. With `notes -> ../../.zshrc` committed, pulling the repo on the Mac and then running `yo cp :proj ~/Developer/` would overwrite `~/.zshrc` through the link.

`download_from_guest` instead has scp fill a `.yo-cp-*` staging directory beside the destination, inside it when it is an existing directory, so the final move is a rename on one filesystem. `plan_merge` then plans every entry against what is already on the Mac before anything moves, and `land_staged` renames the entries into place. The refusals you will see:

- `refusing to copy over symlink "<path>": the copy would overwrite its target "<target>", not the link`, for an existing symlink at any depth.
- A file meeting a directory, or the reverse, is refused the same way.
- `cannot place <staged> -> <target>: <reason> (N of M entries already landed)`, when `os.replace` fails, for example on a mount point or a read-only Mac directory. It is never a traceback.
- `destination "<path>/" is not a directory`, when DST ends in `/` and does not exist. scp itself says "No such file or directory" there, and an earlier `yo` created a file named `newdir`.

The staging directory is removed on every path, a refusal, an scp failure and a failed move included. `-p` keeps a guest directory's mode, and sftp-mode scp creates each local directory with that mode `| u+wx`. A read-only guest directory (a Go module cache) then refuses to lose its children, and an agent-side `chmod 300 ~/proj/sub` leaves a staged directory nobody can list. So `unlock_tree` adds `u+rwx` to each real directory before `shutil.rmtree`, and never follows a symlink, which would chmod a Mac file. If cleanup still fails, `download_from_guest` prints that failure on stderr and re-raises the original error. It never replaces it.

### Downloads never land control paths

A pulled guest tree is code the agent wrote, and some of its file names make the Mac execute it. A `.git/config` with `core.fsmonitor`, `core.pager` or `core.hooksPath` runs as the operator on the next `git` command in that directory, with no prompt, and `.git/hooks/*` does the same. `.envrc` runs under direnv, and `.vscode` and `.idea` carry editor tasks that auto-run. Without the filter, `yo cp :proj .` would lay the guest's `.git` over the Mac's, because `plan_merge` lets a file replace a file: a guest-to-host code execution path through the one command meant to be safe for pulling work back.

`drop_from_staging` runs once on the staging directory, after scp and before `plan_merge`. It removes every entry whose name is `.git`, `.hg`, `.svn`, `.envrc`, `.vscode` or `.idea` (`LANDING_CONTROL_NAMES`), at any depth, and prints `yo: skipped "<path>": ...` on stderr for each. The match uses Unicode case folding (`casefold()`), because APFS folds names that way, so `.vſcode` is skipped like `.vscode`. It prunes the staging tree instead of filtering the move list because a directory that does not exist on the Mac yet moves as one rename and never passes through `plan_merge`'s per-entry walk, so a `new/.git/config` would have slipped in. Everything else lands as before, ordinary overwrites included. The list is deliberately short. A name is on it only when the host runs what it holds with no prompt on the next `git` command, `cd` or editor open. Project files that other tools run only after a trust prompt (`.claude`, `.mcp.json`, `.devcontainer`, `.zed`) are not on it; a repo's content belongs on the Mac through `git pull`, not `yo cp`. If you do want a repo's history on the Mac, `git pull` is the path: `yo link` makes git the bridge.

### Downloads never land build output

A pulled tree also does not carry the directories the project's own tooling recreates: `node_modules`, `.venv`, a Rust `target` and a Next.js `.next`. They dwarf the source, and the host does not want the guest's compiled artifacts or interpreter links anyway. `drop_from_staging`, the same single walk that drops control paths, removes such a directory at any depth below the sources and prints `yo: skipped "<path>": build output is never copied to the host; the project's tooling recreates it` on stderr. A skipped directory is not entered, so a `node_modules/.git` inside it is covered by that one line.

`node_modules` and `.venv` are skipped by name. `target` is skipped only when a regular file `Cargo.toml` sits beside it, and `.next` only when `package.json` does. This is the reasoning behind the guest's `gc`: the name alone proves nothing, and a `target` with no `Cargo.toml` may be a directory of data. Only a directory counts, so a file or a symlink called `node_modules` lands, and a manifest that is itself a directory does not satisfy the rule.

The sources you name on the command line are exempt. `yo cp :~/proj/node_modules .` lands `node_modules`, because you asked for exactly that. Only what is found inside a named source is judged. Existing host content is untouched: the skip removes the staged copy before `plan_merge`, so a `node_modules` already on the host keeps every file and gains none.

The list is `BUILD_OUTPUT` in `yo`, a map from directory name to the manifest it needs, or `None`. `tests/test_consistency.py` keeps it equal to the arrays in `nix/guest/yolobox-guest.sh` (`BUILD_DIRS`, `REMOVED_OUTRIGHT`, `NEEDS_MANIFEST`) that `gc` uses. The skip happens in staging after scp has finished, so the bytes still cross the wire; `TODO.md` has the transport change that would stop that.

## Starting and unlocking

Starting the VM is more than one `limactl start`. The root disk is encrypted, and lima's own start blocks until its ssh works, which needs the disk unlocked. So `vm_up` derives the passphrase first (`luks_passphrase`, which asks 1Password for the `yolobox-disk` key and so shows a Touch ID prompt), starts `limactl start` in the background, and hands the passphrase to the initrd with `unlock_disk` once the initrd's sshd answers. It then waits for the stage-2 sshd to accept the operator. A VM that is running and already accepts the operator needs no passphrase and no prompt. `lima_stop()` is the one place that runs `limactl stop`, with a timeout and the recovery text for its known hang. Every pre-unlock probe goes through `initrd_ssh_argv()` and `sshd_answers`, never a pinned ssh role. The reasons, the protocol and the failure shapes are in [docs/disk-encryption.md](docs/disk-encryption.md).

## Several machines

`yo -m <name> <command>` (or `yo <command> -m <name>`) makes `yo` drive the lima instance `<name>` instead of `yolobox`. Any number of machines can exist and run side by side, each addressed by its own `-m`. Without the flag nothing changes. The flag is accepted before or after the command, and when it is given twice the last one wins.

It is a flag, not an exported variable, on purpose. An exported variable stayed set in the shell and carried a second machine's name into every later command, including ones meant for the default box. A flag sits on the line that acts on it. For `yo ssh`, a `-m` before the first guest word is `yo`'s; from the first non-option word on, everything reaches ssh untouched.

Every per-machine name derives from that one value through `set_instance()`, the one function that assigns them, so no name is left over from another machine:

- the lima directory `~/.lima/<name>` (ssh config, `known_hosts`, the multiplexing sockets), and every `limactl` call;
- the ssh host `lima-<name>`, and the herdr machine and ssh alias `<name>`;
- the `Include ~/.lima/<name>/ssh.config` line in `~/.ssh/config`.

A name follows lima's rule (letters and digits joined by single `.`, `_` or `-`) and must not start with `lima-`. Otherwise instance `lima-x` would name its herdr alias `lima-x`, which is the ssh host of instance `x`. `yo` refuses any other spelling while it parses the command line, with exit 2 and the usage text, before anything runs.

**The instance must exist.** `yo` creates only the default instance, from `lima/yolobox.yaml`. For any other it stops in `vm_up`, before it asks 1Password for the disk key and before it starts anything, and prints the command to run:

```sh
limactl create --name <name> --set '.ssh.localPort=<port>' lima/yolobox.yaml
```

Add `--cpus`, `--memory` and `--disk` to size it; `yo` never sizes an instance. Give it at least 8 GiB of memory: the system is evaluated and built inside the instance, and a smaller one gets the nix evaluator OOM-killed (the build dies with `Killed`, exit 137). Give it at least 40 GiB of disk: the box takes a fixed 16 GiB swapfile (`swapDevices` in `nix/base.nix`, whatever the disk size) plus the nix store of its first build, and a smaller disk fills up to 100 percent. The failure does not say "disk full". `dhcpcd` and `resolvconf` get "No space left on device" when they rewrite `/etc/resolv.conf`, which is left empty, so nothing resolves and a `tailscale up` retries forever with "failed to resolve". If a small instance is unavoidable, a `/etc/yolobox/local.nix` line `swapDevices = lib.mkForce [ { device = "/var/swapfile"; size = 2048; } ];` shrinks the swapfile; free the space first (`swapoff` and remove `/var/swapfile`), and run `resolvconf -u` once to refill `resolv.conf`. Give every instance its own free `<port>`. `yo` has to know the port that reaches an instance's initrd sshd before the first start, and `ssh_port()` is the one place that answers: 60022 for the default instance, and for any other the `ssh.localPort` that `limactl list <name> --json` reports. A port of 0, which makes lima pick a new one on every start, or a missing or malformed one, is an error. `yo` never guesses: the default box answers on 60022 and accepts the same lima key, so a fallback would probe, and unlock, the wrong machine.

**What bootstrap adds per instance**, for removing one by hand after `limactl delete <name>`:

- one `Include ~/.lima/<name>/ssh.config` line at the top of `~/.ssh/config`. `yo` asks first when it has a terminal, and otherwise prints the line;
- a herdr machine with the target `<name>` (`herdr machine list --json` shows it);
- inside `~/.lima/<name>/`, which `limactl delete` removes: the editor block `Match host lima-<name> user agent`, the host-key pin block and the herdr alias `Host <name>` (all re-applied to `ssh.config` on every `yo` run), and `known_hosts` with the guest's host keys.

Each instance's blocks live in that instance's own `ssh.config`, so writing one never rewrites another, and a second bootstrap of the same instance changes nothing. `TODO.md` lists the host-global state that several running boxes still share: the LUKS key, the AWS broker, the `yolobox` remote of `yo link` and the mirrored host ports.

## bootstrap --version

`yo bootstrap --version REF` chooses what the box builds from. Without it the box builds from this `yo`'s own release, `github:aka-rider/yolobox/v<version>`, so a `brew upgrade` moves the next bootstrap with it. The value is resolved while the command line is parsed, so a refused one (`yo bootstrap --version 1.7`) exits 2 with the usage line before `limactl` is looked for:

- `1.7.2` or `v1.7.2` is that release tag.
- `main`, `my-branch` or `v1.7.2-rc1` is a branch or tag on `aka-rider/yolobox`.
- A flake ref with a `:` in it, such as `github:you/yolobox/branch`, or an absolute guest path, such as `/home/agent/Developer/yolobox`, is passed through verbatim.

Anything else is refused before anything runs. A partial version such as `1.7` or `v2` is refused because a release is `X.Y.Z`. `latest` is refused because no such ref exists upstream, and this `yo`'s own release already is the latest one after an upgrade. An empty value or one with spaces is refused. So are `~` and `./` spellings: the ref is shell-quoted on its way over ssh, so nothing expands `~`, and `./` would resolve against the operator's working directory in the guest, where `nixos-rebuild` runs, rather than in the checkout. On an unreleased build (`YO_VERSION` is `dev`) `--version` is required, because there is no release to fall back on.

A host checkout cannot be fetched by the guest as a flake ref, so it goes through the repo's git bridge. Run `yo link` once inside the checkout, then `git push yolobox`. A checkout at `~/Developer/yolobox` lands at `/home/agent/Developer/yolobox` in the box, the path `mirror()` gives it under the guest home. Then run `yo bootstrap --version /home/agent/Developer/yolobox`. Nix reads the pushed tree, so commit first: uncommitted changes are invisible to it.

**The ref is remembered per machine.** A flag does not persist, so `yo bootstrap --version REF` writes the ref it built to `~/.local/state/yolobox/<name>.flake-ref` after `nixos-rebuild` succeeds. `rebuild_hint()` reads that file first, so a remedy printed for a box built from a fork keeps naming the fork's ref. Without the file it falls back to this `yo`'s release. A file that exists but is blank is an error that names the file, and it does not fall back: a silent fallback would switch a fork-built box to the upstream release on the next rebuild. The file holds only an explicit `--version`. A bootstrap without it means this `yo`'s release, so it deletes the file: otherwise the ref of an old fork build would pin the box against every later `brew upgrade`, and the next hint names this `yo`'s release again.

## The guest helper

`yo bootstrap` reads the profile and booted generations without the helper, over plain ssh (`guest_generations`), because the nixos-lima image a first bootstrap starts from has no `yolobox-guest` yet. Reading them through the helper would make that first bootstrap die with a version-skew error before its reboot into LUKS.

`guest_helper(role, sub, args)` runs a `yolobox-guest` subcommand over ssh as the operator or the agent, and turns a missing helper or subcommand into a version-skew note instead of a bare exit code. Nothing in it asks for root. The reasons are in [nix/guest/yolobox-guest.md](nix/guest/yolobox-guest.md#version-skew).

## The network line and the gateway-blocked marker

`yo status` prints one `network:` line, built by `network_status_line()` from the two results of `yolobox-guest net-check`: whether egress works and whether lima's gateway answers a DNS query. The pair tells a VPN on the host that owns its default route from one that blackholes the gateway (see [docs/host-network.md](docs/host-network.md#vpn-shapes)).

### The egress line

The `egress:` line above it comes from one `systemctl show -p LoadState -p ActiveState yolobox-egress`, because `systemctl is-active` prints `inactive` for a unit the box has never heard of and would make an old box look like it was in recovery mode (see [nix/egress.md](nix/egress.md)). `LoadState=not-found` prints `egress: box predates the egress table; run yo bootstrap`; a loaded unit that is `active` prints `egress: host and LAN blocked`; a loaded unit in any other state is the recovery-mode warning; `LoadState=masked` prints its own warning (`the egress table is masked`, with the `systemctl unmask` command), because a masked unit loads no table at the next boot. A probe that answered nothing (ssh failed) or an unrecognised `LoadState` prints `egress: not checked — the probe answered …` with the exit code and ssh's stderr, because a failed probe says nothing about the box and must not read as recovery mode.

When the unit is `active`, `yo` also reads the live table (`EGRESS_TABLE_PROBE`, `sudo -n nft list table inet yolobox-egress` over the operator channel), because a table that was flushed, replaced or emptied by hand leaves the unit `active` and `egress: host and LAN blocked` would then be a lie. `egress_table_holds()` checks structure only: an `output` hook chain, an `oifname { "lo", "tailscale0" ... } accept` rule, and `reject` rules for the private IPv4 and IPv6 ranges. It never compares rule content, because `lanAllow` and `tunnelInterfaces` boxes legitimately add accepts and a `postrouting` chain, and a content comparison would false-alarm on exactly those boxes (`tests/fixtures/egress-tunnel.nft` is the test's proof). A table that fails the check, or nft's "No such file or directory", prints `egress: WARNING unit active but table looks wrong`. Any other probe failure (ssh down, sudo asking for a password) says nothing about the table, so the line falls back to the unit-state message rather than guessing. The table probe runs only in the active case; the not-found, masked and recovery branches are unchanged.

The gateway result has a third value, `Gateway.SKIPPED`. A box that blocks lima's gateway by design creates `/etc/yolobox/gateway-blocked`; while that file exists `net-check` prints `gateway=skipped`, and `yo` says so in the line: `network: ok (lima's gateway probe skipped: this box blocks it by design)` when egress works, and `network: WARNING guest egress is dead (lima's gateway probe skipped: this box blocks it by design)` when it does not. Without the marker a refused probe would read as a VPN fault that is not there. The marker is the whole contract: `yo` never learns why the gateway is blocked, and the repo creates the file nowhere.

## One parser

`build_parser()` is the single declaration of `yo`'s command line: `add_subparsers` plus `set_defaults(func=cmd_x)` per subcommand. `main()` parses, runs the `limactl` preflight, then calls `args.func(args)`. Every command function takes the parsed `argparse.Namespace`.

`yo help [command]` is `yo [command] --help`: the same text, printed through `out()`. `yo help` with no topic prints the top-level help. It skips the `limactl` preflight, as `--help` does, so it works on a host without lima. It is a real subcommand, not a flag alias, so that `yo help <command>` is one word per command with no leading dashes, and so `yo help bogus` fails with argparse's usual `invalid choice` usage error.

One declaration means no second list can drift from it (a hand-written usage string beside a command table) and no command body carries its own copy of the arity check. Every command with no positional arguments rejects extra ones the same way, so `yo up bogus` fails like `yo down bogus`.

`disk-grow` validates its size through argparse's `type=` callable, which reaches `int()` first. A negative value therefore reports "must be greater than 0 GiB" and not "must be a whole number of GiB".

### `yo ssh` keeps `REMAINDER`

`yo ssh`'s trailing arguments are declared `nargs=argparse.REMAINDER`, and the choice is load-bearing. `rebuild_hint()` prints `yo ssh sudo YOLOBOX_USERNAME=$(id -un) nixos-rebuild switch --impure --flake '…'` as the documented remedy for a stale generation. If argparse ever parsed `--impure` as its own flag instead of passing it through, the tool's own printed advice would stop working.

Verified against real argparse: every invocation whose first token is not option-like passes through untouched, `yo ssh sudo … --impure --flake …` included. A leading-dash first token is handled by argparse before REMAINDER starts. `yo ssh --help` and `yo ssh -h` print `yo`'s own ssh help and exit 0. Any other leading option, such as `yo ssh -v`, exits 2 without reaching the VM. Nothing here changes what ssh receives, because REMAINDER only hands the tokens after the first non-option word to ssh as the remote command.

### `allow_abbrev` stays off

`allow_abbrev=False` is set on the top-level parser and on every subparser, so `yo gc --de` and `yo gc --y` are rejected as unknown arguments. argparse accepts prefixes by default. For a command whose job is deleting things, a flag that two keystrokes of a typo can reach is one too many. A new subparser added without it makes `--de` work again on that command, with no error anywhere.

### Empty string is not no argument

`enter`, `code` and `zed` take `project` as `nargs="?"`, so `yo enter ""` and `yo enter` produce different values, `project=''` against `project=None`, and they are different invocations. The first is an empty fzf query, the second mirrors the guest twin of the current directory. `target_dir` therefore branches on `project is not None`, never on truthiness, so the empty string does not collapse into the no-argument case.

## Output filter

`out()` and `err()` are the two functions every line `yo` prints goes through. They replace C0 and C1 control characters with `?`, except tab and newline (`TERMINAL_CONTROL_RE`). Guest output is attacker-controlled: ssh stderr, a path the helper reports, a project name, whatever a failing guest command says. It reaches the operator's terminal on the Mac, which acts on escape sequences. A title change, a cursor move that overwrites a line of the message above it, and an OSC 52 that sets the clipboard are all possible.

Filtering at one chokepoint, instead of at each call site that happens to print guest text, is what keeps a new message from forgetting. It is a display filter only. What `yo` runs and writes to files is never altered by it, and a command that must hand bytes through untouched (`yo ssh`, `yo enter`) does not print them via `out()` at all.

Output streamed live goes through it too. `yo gc` and `yo aws-check` show the guest tiers as they run, and the tiers print project paths and whatever a project's `make -n clean` or `make clean` recipe prints. So `run_filtered()` pumps their stdout and stderr line by line into `out()` and `err()` instead of handing ssh the terminal.

`pick_project()` runs each guest-listed project name through `TERMINAL_CONTROL_RE` before handing the list to `fzf --read0`, so the picker's safety does not depend on the installed fzf stripping control bytes. A name that contained one comes back from the picker with `?` in its place and so names no real directory.

## YO_VERSION

`YO_VERSION = "dev"` must survive as that exact literal assignment, unreformatted. `nix/pkgs/yolobox.nix` overwrites it with `--replace-fail` and `homebrew/yolobox.rb` with `inreplace`. Both match the literal text, and either patch is how a release build gets its real version baked in.

Reformat that line by reflowing it, requoting it or changing its text in any way, and the Nix build fails loudly at `--replace-fail`, which is the safer failure. The brew formula's `inreplace` can silently no-op and ship a binary that still reports `dev`. `TestYoVersionLiteral` fails when `yo` stops having exactly one line that is that literal, so the reformat is caught before the release, not by it.
