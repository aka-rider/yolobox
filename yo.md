# yo

Notes on the parts of `yo`, the Mac-side CLI, whose reasons are not obvious from reading the code. A short "why" lives in the code comment next to each piece. This file keeps the incidents, the evidence and how to recognise each failure. `yo --help` and `yo <command> --help` say what the commands do.

## The mirror

`yo enter`, `yo code` and `yo zed` land in the guest twin of the Mac's logical `$PWD`. That is the path relative to the Mac `$HOME`, grafted onto the guest `$HOME`, subdirectory included. Logical means `$PWD` as the shell spells it. A symlinked spelling is mirrored, never resolved, so the guest sees whatever name you `cd` through on the Mac. `yo link` maps a repo's toplevel the same way and refuses a repo outside `$HOME`.

The mapping is done by `mirror()`. The guest path it produces then goes through `guest_path()`, which is why no `yo` command can target a guest path outside the guest `$HOME`. The one exception is `yo cp`, which also accepts an absolute path under `/tmp` (see "yo cp" below).

When the mirrored path does not exist in the VM, the session lands at the deepest existing ancestor. When the Mac cwd is outside `$HOME`, it lands at the guest home. Both cases print `MISSING_PATH_NOTE` to stderr: `path "%s" does not exist in the yolobox you are in "%s"`. `yo bootstrap` needs no mirrored path, because it builds the VM straight from a flake ref and there is nothing of yolobox's own to locate inside the guest.

### The toplevel of a symlinked repo

`git rev-parse --show-toplevel` reports the repo root with symlinks resolved, but the mirror is spelled logically (`~/wrk` is a link to `~/Developer`). `logical_toplevel()` therefore walks up from the cwd as spelled and takes the nearest ancestor whose `realpath` equals git's answer. When no ancestor matches, it falls back to the physical path.

It used to count the `../` that `--show-cdup` printed and apply that count to the logical cwd. That is wrong as soon as a symlink below the root makes the physical and logical depths differ, because the count then climbs to a different directory than the root. Recognise the shape by `yo link` mapping a repo to a parent of the one you stood in, or refusing it as `$HOME` itself.

## yo link

`yo link` used to refuse outright whenever a Mac repo already carried a `yolobox` remote. It now reads the existing URL first, through `link_remote_action()`. An unrelated remote, one whose host is not `lima-yolobox`, is still refused. A `yolobox` remote whose host is `lima-yolobox` and whose path is merely stale is updated in place with `git remote set-url`, and `yo` prints the old and the new URL to stderr so the change is not silent.

A recreated box leaves exactly that stale shape behind. Every repo linked against a previous instance carries something like `ssh://lima-yolobox/home/alice.guest/wrk/rune`, from before the operator/agent split moved projects to `/home/agent`. Before this fix the remedy was a manual `git remote remove yolobox` in each repo before `yo link` would run at all.

## yo cp

### Why scp, not `limactl cp`

`limactl cp` cannot move a file into a project the agent then has to write to. It copies as the operator (uid 501), so the file it writes is owned by an account `agent` cannot write as. The operator's shell on this box is also unconfigured, and its default zsh prompt writes onto the channel the copy's own rsync or scp handshake reads, which breaks the transfer before a byte moves. `rsync` is not installed in the guest at all, so that was never a fallback.

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

The guest side is not checked. The test exists to keep the Mac's private files from entering the guest, and a guest source is something the agent could already read, with that account's own permissions as the boundary. A trailing slash on an upload source used to hide a symlink from the check, because `lstat("link/")` follows the link. The slash is now stripped first.

### Downloads stage, then merge

A download never lets scp write onto the Mac destination itself. scp writes through a symlink it finds there, and the agent controls what a repo it pushed contains. With `notes -> ../../.zshrc` committed, pulling the repo on the Mac and then running `yo cp :proj ~/wrk/` would overwrite `~/.zshrc` through the link.

`download_from_guest` instead has scp fill a `.yo-cp-*` staging directory beside the destination, inside it when it is an existing directory, so the final move is a rename on one filesystem. `plan_merge` then plans every entry against what is already on the Mac before anything moves, and `land_staged` renames the entries into place. The refusals you will see:

- `refusing to copy over symlink "<path>": the copy would overwrite its target "<target>", not the link`, for an existing symlink at any depth.
- A file meeting a directory, or the reverse, is refused the same way.
- `cannot place <staged> -> <target>: <reason> (N of M entries already landed)`, when `os.replace` fails, for example on a mount point or a read-only Mac directory. It is never a traceback.
- `destination "<path>/" is not a directory`, when DST ends in `/` and does not exist. scp itself says "No such file or directory" there, and an earlier `yo` created a file named `newdir`.

The staging directory is removed on every path, a refusal, an scp failure and a failed move included. `-p` keeps a guest directory's mode, and sftp-mode scp creates each local directory with that mode `| u+wx`. A read-only guest directory (a Go module cache) then refuses to lose its children, and an agent-side `chmod 300 ~/proj/sub` leaves a staged directory nobody can list. So `unlock_tree` chmods each real directory to 0700 before `shutil.rmtree`, and never follows a symlink, which would chmod a Mac file. If cleanup still fails, `download_from_guest` prints that failure on stderr and re-raises the original error. It never replaces it.

## Starting and unlocking

Starting the VM is no longer one `limactl start`. The root disk is encrypted, and lima's own start blocks until its ssh works, which needs the disk unlocked. So `vm_up` derives the passphrase first (`luks_passphrase`, which asks 1Password for the `yolobox-disk` key and so shows a Touch ID prompt), starts `limactl start` in the background, and hands the passphrase to the initrd with `unlock_disk` once the initrd's sshd answers. It then waits for the stage-2 sshd to accept the operator. A VM that is running and already accepts the operator needs no passphrase and no prompt. `lima_stop()` is the one place that runs `limactl stop`, with a timeout and the recovery text for its known hang. Every pre-unlock probe goes through `initrd_ssh_argv()` and `sshd_answers`, never a pinned ssh role. The reasons, the protocol and the failure shapes are in [docs/disk-encryption.md](docs/disk-encryption.md).

## The guest helper

`guest_helper(role, sub, args)` runs a `yolobox-guest` subcommand over ssh as the operator or the agent, and turns a missing helper or subcommand into a version-skew note instead of a bare exit code. Nothing in it asks for root. The reasons are in [nix/guest/yolobox-guest.md](nix/guest/yolobox-guest.md#version-skew).

## The network line and the gateway-blocked marker

`yo status` prints one `network:` line, built by `network_status_line()` from the two results of `yolobox-guest net-check`: whether egress works and whether lima's gateway answers a DNS query. The pair tells a VPN on the host that owns its default route from one that blackholes the gateway (see [docs/host-network.md](docs/host-network.md#vpn-shapes)).

The `egress:` line above it comes from one `systemctl show -p LoadState -p ActiveState yolobox-egress`, because `systemctl is-active` prints `inactive` for a unit the box has never heard of and would make an old box look like it was in recovery mode (see [nix/egress.md](nix/egress.md)). `LoadState=not-found` prints `egress: box predates the egress table; run yo bootstrap`; a loaded unit that is `active` prints `egress: host and LAN blocked`; a loaded unit in any other state is the recovery-mode warning; `LoadState=masked` prints its own warning (`the egress table is masked`, with the `systemctl unmask` command), because a masked unit loads no table at the next boot. A probe that answered nothing (ssh failed) or an unrecognised `LoadState` prints `egress: not checked — the probe answered …` with the exit code and ssh's stderr, because a failed probe says nothing about the box and must not read as recovery mode.

The gateway result has a third value, `Gateway.SKIPPED`. A box that blocks lima's gateway by design creates `/etc/yolobox/gateway-blocked`; while that file exists `net-check` prints `gateway=skipped`, and `yo` says so in the line: `network: ok (lima's gateway probe skipped: this box blocks it by design)` when egress works, and `network: WARNING guest egress is dead (lima's gateway probe skipped: this box blocks it by design)` when it does not. Without the marker a refused probe would read as a VPN fault that is not there. The marker is the whole contract: `yo` never learns why the gateway is blocked, and the repo creates the file nowhere.

## One parser

`build_parser()` is the single declaration of `yo`'s command line: `add_subparsers` plus `set_defaults(func=cmd_x)` per subcommand. `main()` parses, runs the `limactl` preflight, then calls `args.func(args)`. Every command function takes the parsed `argparse.Namespace`.

Before this there were two lists nothing kept in agreement, a hand-written `USAGE` string and a `COMMANDS` dict, plus 13 copies of the same arity check scattered through the command bodies. They had already drifted. `up`, `down` and `status` silently accepted and ignored extra arguments while the other nine zero-argument commands rejected them, because someone had copied the check into nine bodies and not the other three. `yo up bogus` used to succeed. It now fails like every other command with no positional arguments.

The same class of fix changed one message. `disk-grow`'s bespoke `.isdigit()` check used to reject `disk-grow -1` with "must be a whole number of GiB". argparse's `type=` callable reaches `int()` first, so a negative value now reports "must be greater than 0 GiB". That is a wording change, not a behaviour change, so do not mistake it for a new bug.

### `yo ssh` keeps `REMAINDER`

`yo ssh`'s trailing arguments are declared `nargs=argparse.REMAINDER`, and the choice is load-bearing. `rebuild_hint()` prints `yo ssh sudo YOLOBOX_USERNAME=$(id -un) nixos-rebuild switch --impure --flake '…'` as the documented remedy for a stale generation. If argparse ever parsed `--impure` as its own flag instead of passing it through, the tool's own printed advice would stop working.

Verified against real argparse: every invocation whose first token is not option-like passes through untouched, `yo ssh sudo … --impure --flake …` included. The one shape that changes is a leading-dash first token, `yo ssh -v` or `yo ssh --help`, which now exits 2 from argparse itself instead of reaching the VM. That is not a regression. REMAINDER, like the old hand-rolled passthrough, only ever hands the tokens to ssh as a remote command after the hostname, so `--help` used to fail inside the VM as `bash: --help: command not found`. `yo ssh -h` is the one case that improves, since it now prints `yo`'s own ssh help.

### `allow_abbrev` stays off

`allow_abbrev=False` is set on the top-level parser and on every subparser, so `yo gc --de` and `yo gc --y` are rejected as unknown arguments, as the old hand-rolled flag loop rejected any prefix. argparse accepts prefixes by default. For a command whose job is deleting things, a flag that two keystrokes of a typo can reach is one too many. A new subparser added without it makes `--de` work again on that command, with no error anywhere.

### Empty string is not no argument

`enter`, `code` and `zed` take `project` as `nargs="?"`, so `yo enter ""` and `yo enter` produce different values, `project=''` against `project=None`, and they are different invocations. The first is an empty fzf query, the second mirrors the guest twin of the current directory. `target_dir` therefore branches on `project is not None`, never on truthiness, so the empty string does not collapse into the no-argument case.

## Output filter

`out()` and `err()` are the two functions every line `yo` prints goes through. They replace C0 and C1 control characters with `?`, except tab and newline (`TERMINAL_CONTROL_RE`). Guest output is attacker-controlled: ssh stderr, a path the helper reports, a project name, whatever a failing guest command says. It reaches the operator's terminal on the Mac, which acts on escape sequences. A title change, a cursor move that overwrites a line of the message above it, and an OSC 52 that sets the clipboard are all possible.

Filtering at one chokepoint, instead of at each call site that happens to print guest text, is what keeps a new message from forgetting. It is a display filter only. What `yo` runs and writes to files is never altered by it, and a command that must hand bytes through untouched (`yo ssh`, `yo enter`) does not print them via `out()` at all.

Output streamed live goes through it too. `yo gc` and `yo aws-check` show the guest tiers as they run, and the tiers print project paths and whatever a project's `make -n clean` or `make clean` recipe prints. So `run_filtered()` pumps their stdout and stderr line by line into `out()` and `err()` instead of handing ssh the terminal.

## YO_VERSION

`YO_VERSION = "dev"` must survive as that exact literal assignment, unreformatted. `nix/pkgs/yolobox.nix` overwrites it with `--replace-fail` and `homebrew/yolobox.rb` with `inreplace`. Both match the literal text, and either patch is how a release build gets its real version baked in.

Reformat that line by reflowing it, requoting it or changing its text in any way, and the Nix build fails loudly at `--replace-fail`, which is the safer failure. The brew formula's `inreplace` can silently no-op and ship a binary that still reports `dev`. `TestYoVersionLiteral` fails when `yo` stops having exactly one line that is that literal, so the reformat is caught before the release, not by it.
