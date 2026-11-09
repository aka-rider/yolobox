# Constitution

The rules for changing this repo. Each rule is followed by the reason it exists, so you can tell when it stops applying. Read this before your first change, after `AGENTS.md`.

## Before you start

- ALWAYS find out whether you run on the host or in the guest before you run anything. `uname` says `Linux` on a Linux host and in the guest alike; only the guest has `yolobox-guest` and `/etc/yolobox/version`, and only a host has `yo`.

## Versions and design

- ALWAYS treat the latest `yo` and the box it builds as the only version that exists. A box is disposable: any incompatibility between `yo`, the guest, a Lima instance, a libvirt domain or the broker is solved by `yo destroy --yes && yo bootstrap`, never by compatibility code, because every branch for an older box is a state nobody tests.
- ALWAYS bump the major version for a breaking change. The release note is where an operator learns that the cure is a new box.
- ALWAYS keep the seed image's first bootstrap working: a plaintext root, no `yolobox-guest`, no agent account, no version stamp. That is a box not yet bootstrapped, not an older version, and every new box passes through it.
- ALWAYS make `yo` define the box, not judge and repair it. Declare the desired state idempotently, or refuse with one message that names the one command to run; inferring a state from exit codes and picking among repairs multiplies the states that can go wrong.
- ALWAYS decide a state only from an answer that names it: a banner word, a stamp, one word of a closed set the probe prints. NEVER from an absence, a bare exit code or an error other failures share, and give every classifier an outcome for an answer it has no name for, shown in that answer's words. A missing mark, ssh's silent 255, a failing `cat`, `dig`'s 0 and `sudo test -e`'s 1 each also mean something else. The one state read off an absence is an ssh banner with neither mark, and only in the safe direction: it gets the operator's login alone, never `root` or the passphrase.
- ALWAYS run every check that can refuse a command before the command's first step that changes the box or the host. A refusal after a step leaves a half-done state nobody chose.
- ALWAYS build each remedy `yo` prints in one place, and test it by running the printed command against fakes from every state that prints it. A remedy that does not run is a second refusal.
- ALWAYS write a file another process or a later run reads atomically: a sibling temporary file, fsync, rename, fsync the directory, and remove the temporary file on failure. A reader must never see half a file.
- ALWAYS judge a name, path, address or rule by fullmatch, whole words or parsed membership, NEVER by substring, prefix or `re.search`. A search accepts whatever merely contains the name.
- ALWAYS prefer the trivial implementation, and delete when in doubt. Fewer states, branches and parsers leave less to break.
- ALWAYS spell a literal that more than one language reads (uids, account names, socket paths, the LUKS names, exit codes, ports) once, in `facts.json`. nix reads it with `fromJSON`, Python with `json`, and the shell scripts source the `/etc/yolobox/facts.sh` nix renders from it; two spellings drift.
- ALWAYS route repeated logic through one chokepoint, push branching up into the caller and hand helpers whole batches. A fix then lands once.
- NEVER write dead code, a wall-clock sleep that orders events, or a sentence-long comment that justifies a workaround. A sleep is a race with a timeout, and a workaround that needs a paragraph is wrong code.

## Documentation and comments

- ALWAYS keep the documentation to three files: `README.md` for the operator, `AGENTS.md` for a developer, this file for the rules. `CLAUDE.md` only names them, and `TODO.md` is not documentation but the list of upstream changes being waited on. A change updates the three in the same commit, because a doc that lags the code teaches the wrong thing.
- NEVER point at another document from code, a comment, a runtime message or an assertion message. State the fact in place: the reader of an error has no checkout, and a pointer rots when its target moves.
- NEVER write a `file:line` reference or an `X.md#anchor` link anywhere. Lines and headings move with every edit.
- NEVER write one operator's machine, network, vaults, providers or projects into a tracked file. The repo is generic; a box's own setup lives in its `/etc/yolobox/local.nix`.
- NEVER cite a commit hash, or a past tag or version, as provenance or as an era boundary in a tracked file. History gets rewritten and tags get deleted. Hashes of other repos and pinned action SHAs are exempt.
- ALWAYS keep `TODO.md` to open items that wait on an upstream change, each with its link. A bug or debt found in this repo is fixed in the change that finds it.

## Tests

- ALWAYS test behaviour through a public interface: a function, a command, a script run under bash with fakes. A test that restates the code (a constant equals its literal, an argv equals the concatenation the function does) proves nothing and is deleted.
- ALWAYS test the extremes (empty, zero, negative, huge, malformed) and the happy path once.
- NEVER let a test reach the operator's real home. Every test module imports `tests/shell_harness.py`, which points `$HOME` at a scratch directory and fails a test that opens, lists or connects to anything under the real one; otherwise a test talks to the operator's 1Password agent, ssh config and boxes, and passes or fails by what they hold.
- NEVER let a unit test depend on the host OS, a hypervisor or a running box. Skip cleanly when a tool is missing, never assert that it is present, so the suite runs on a Mac, a Linux PC and CI alike; and CI fails a test that skips although its runner has the tool, because there the skip hides a broken probe.
- ALWAYS flip each new test and watch it fail, run every live check beside a positive control through the same path, and make a probe ask a question only its target can answer, one layer at a time. A check that cannot fail, or that fails for reasons of its own, proves nothing.

## The VM

- ALWAYS keep the VM rebuildable from this repo alone, plus `/etc/yolobox/local.nix` and what it imports. That file is the one per-box input; any other hand setup after creation is lost with the box.
- ALWAYS keep the mount layout identical to the shipped nixos-lima image (`/boot` is the ESP), except `/` on LUKS. Any other divergence makes every fresh instance migrate mounts before its first switch.
- ALWAYS keep `boot.loader.grub.configurationLimit = 1`. The ESP is 249 MiB and GRUB copies the incoming kernel before it prunes, so two retained kernels plus one incoming do not fit.
- ALWAYS keep `restartIfChanged` false on `lima-init` and `lima-guestagent`, until the `nixos-lima` input sets it itself. Restarting the guest agent breaks every port Lima forwards until the VM restarts.
- ALWAYS treat `lima/yolobox.yaml` as read once, at creation. An existing instance changes only through `limactl edit` while stopped, or by being recreated.
- NEVER let Lima mirror a guest UDP listener, a guest port outside 1024-49151, or a known OAuth callback port onto the host. A mirrored tailscaled port took a host's own Tailscale port, port 68 intercepts the host's DHCP, and a squatted callback port receives a host login.
- ALWAYS keep `vmType`, `propagateProxyEnv: false`, `ssh.loadDotSSHPubKeys: false`, `ssh.overVsock: false` and `user.uid: 501` pinned in `lima/yolobox.yaml`. Lima's defaults leak the Mac's proxy variables and every `~/.ssh/*.pub` into the guest, let ssh ride vsock where the initrd cannot answer, and give the operator the Mac's uid.

## Disk encryption

- ALWAYS keep `/` on LUKS2, unlocked only by `yo` through the initrd's sshd. The image file on the host is otherwise plaintext in every backup and copy.
- NEVER use the 1Password CLI (`op`) or any 1Password interface but its SSH agent socket. The CLI adds a session token any process can reuse; the agent hands out signatures, never keys, and names the asking application.
- NEVER store a secret on disk, on the host or in the guest: no key file, passphrase, signature, token or initrd host key. Secrets travel over sockets and stdin and live in memory; the passphrase is derived on demand and the initrd host key is generated per boot.
- NEVER change `LUKS_SIGN_DATA`, `LUKS_KDF_INFO` or `derive_luks_passphrase`. Together they are the passphrase of every existing disk, and there is no recovery slot.
- NEVER approve a 1Password prompt for `yolobox-disk` outside your own `yo up`, `yo bootstrap` or `yo disk-grow`. The same agent is forwarded into every agent session, and one approved signature is the disk passphrase.
- ALWAYS grow the encrypted root with `yo disk-grow`, never with `autoResize`. `cryptsetup resize` needs the passphrase, which only `yo` holds.
- ALWAYS list an initrd script's tools in `inputs` in `apps.nix`, whose bin directories the initrd's `storePaths` copies. The initrd builder follows ELF dependencies only, and a missing tool shows up as a box stuck in its initrd.
- NEVER create or chmod anything under `/run/yolobox` from the initrd. The directory survives switch-root and holds the agent's sockets.
- ALWAYS label the disk last when the in-place encryption finishes, and encrypt only a plain ext4 root. The label is the proof the disk is done, and a LUKS disk without it is someone else's or half-done, which must never be touched.
- ALWAYS keep `PerSourcePenalties no` ahead of `Match` in the initrd's sshd config. Lima's readiness loop fails as an invalid user every 3 s from `yo`'s own address, and a global keyword after `Match` makes sshd refuse its config.

## Accounts

- NEVER give the agent uid 501, `wheel`, `nix.settings.trusted-users`, a sudoers rule, or the role of Lima's cidata account. 501 is the operator's, and `lima-init` adds its cidata account to `wheel` on every boot, after activation.
- NEVER give `agent` or `root` a usable password. `passwd -l` runs on every activation, because `hashedPassword` applies only when an account is created.
- NEVER grant `CAP_SYS_PTRACE` to anything the agent can run. It ignores uids, so it attaches to root's processes; same-uid debugging comes from `kernel.yama.ptrace_scope = 0`.
- NEVER enable a setuid or capability wrapper, or give the agent a group, beyond the fixed lists in `hardening.nix`. Every wrapper is a path to root, and the journal groups read the operator's `sudo` command lines; the eval-time assertion fails any other.
- ALWAYS keep `security.sudo.execWheelOnly = true`. The agent should not even be able to exec the setuid `sudo` wrapper.
- NEVER add a direnv hook or an `.envrc` loader to the box. An `.envrc` is code the agent writes, and the operator would run it with `sudo` on the first `cd`.
- ALWAYS run the agent's user units as the agent's own user units, never system units with `User=agent`. A system unit logs to the system journal, which the agent must not read.

## SSH and identities

- ALWAYS give each ssh role a `ControlPath` of its own that no other tool uses (Lima's hostagent keeps `ssh.sock`). OpenSSH never re-checks the login user on a multiplexed connection, so `-o User=agent` over the operator's master runs as the operator, and a master another tool opened carries none of `yo`'s forwards.
- ALWAYS reach 1Password in the guest through a reverse unix-socket forward proxied to an agent-owned socket, never through `ForwardAgent` in a shared config. A forward in the shared file would reach every role `yo` spawns, editors included.
- ALWAYS limit `yo ssh`'s `ForwardAgent` (and the `IdentityAgent` ssh needs before it forwards anything) to its own command line over `ControlPath=none`. A forward on a shared master outlives its pane.
- ALWAYS verify the guest's host key from the instance's `known_hosts`, with `NoHostAuthenticationForLocalhost no` first in the ssh config. A changed key must surface as ssh's own refusal, never as a silent rewrite, which would hand the forwarded agent to whatever holds the port.
- ALWAYS offer `root`, and the passphrase, only through `unlock.initrd_endpoint()`, under the box's lock: it proves the listener the VM's (Lima's hostagent, or passt's account), then reads the banner, and lets through only one that carries the initrd's word `yolobox-initrd`; `unlock.root_argv()` proves the listener again right before each `root` login, since a port can be taken after the gate. Every other sshd (stage 2, the seed image's unmarked one, no banner) penalises a `root` login and must never see the passphrase, and a missing word proves nothing, since the seed image's stage 2 has none either. A `yo` that cannot take the lock, `yo status` while another `yo` acts on the box, asks `root` nothing.
- ALWAYS keep the gateway in stage 2's `PerSourcePenaltyExemptList` and admit both accounts only from the gateway, with the anti-spoof rule off `enp0s1`. Every host connection comes from that one address, and the agent holds a 1Password agent that serves the operator's key.
- ALWAYS let the guest's ssh identity setup live in the guest. A host that reads paths the guest can influence once followed a symlink to a private key.
- NEVER set `IdentityAgent` unconditionally in the guest's shared ssh config, and NEVER let an rc file replace an inherited `SSH_AUTH_SOCK`. Either takes the forwarded agent away.
- NEVER copy a private key into the VM.

## Network

- ALWAYS keep the egress table, one constant file, loaded before the network, reloaded in place on a switch, with `dhcpcd` and `tailscaled` requiring its unit and no stop hook. It is what keeps the guest off the host and the LAN, and a box without it must have no network at all.
- ALWAYS judge destinations in the egress table, never output interfaces. The output hook sees the interface of the route chosen before a DNAT, so `oifname` cannot say where a packet leaves.
- ALWAYS keep `--netfilter-mode=off`, `--ssh=false` and `--accept-dns=false` in tailscale's `extraSetFlags`, and NEVER answer a refused `tailscale up` with `--reset`. nixos-fw owns the firewall, the repo's keys own ssh, the guest's resolver stays its own, and `--reset` clears the pinned flags.
- ALWAYS keep `tailscale0` trusted and `enp0s1` open on port 22 only. The tailnet ACL decides who reaches a service, and Lima's start-up wait dials port 22 over TCP, where a dropped packet is a 600 s timeout.
- ALWAYS keep `RemainAfterExit = true` on `tailscaled-set`. A switch restarts a changed oneshot only while it is active.
- ALWAYS put a box-local route in `networking.dhcpcd.runHook`. A dhcpcd restart flushes routes a oneshot added once.

## The Linux host

- ALWAYS run a box on `qemu:///system` with a passt interface, never `qemu:///session`, a bridge or libvirt's `default` network. Only the system connection gives each domain its own sVirt category and a static DAC account, and a bridge gives the guest a layer 2 path to the LAN.
- NEVER add graphics, vsock, filesystem, channel, hostdev or tpm devices, or a second interface, to `domain.xml`, and ALWAYS redefine a stopped domain from it. Each device is a path to the host that no table sees, and the template is the only shape a domain may have.
- NEVER act on a domain that lacks `yo`'s `yolobox:instance` tag for that machine. `yo destroy --yes` must not remove another tool's domain that happens to carry a box's name.
- ALWAYS keep the `yolobox-host` nft table, `RequiredBy=virtqemud.service` on its unit and the account `yolobox-vm`, and NEVER give the unit an `ExecStop`. passt turns every guest connection into a host `connect()`, so the table, keyed on that uid, is the only layer the guest cannot undo.
- ALWAYS give a `portForward` the address `127.0.0.1`, and take host ports only from above the kernel's ephemeral range. Without an address passt binds every host address; above the range nothing else picks the port.
- NEVER pass the host OS into the guest. Gate each setting on the fact it needs, because the OS is wrong at the edges: an x86_64 guest runs under Lima on an Intel Mac and under libvirt on a PC.
- NEVER write the operator's private key to a file on a Linux host. The operator is the 1Password item `yolobox-operator`, and `yo` stores only its public half.

## Kernel attack surface

- NEVER unblock a kernel module without saying, in a comment beside its entry, what needs it. The agent autoloads a protocol module with one `socket()` call, and the comment is how the next person learns why it was blocked.
- NEVER block `vsock`, `virtiofs` or `binfmt_misc`, and keep `overlay`, `fuse`, `tun`, `bridge`, `veth`, `dm_crypt`, `af_alg` and `sch_fq_codel` loadable. Lima, rosetta, podman, tailscale and the LUKS root need them.
- NEVER turn off unprivileged user namespaces. The Chromium sandbox, rootless podman and the nix build sandbox all create them.
- ALWAYS mount `/proc` with `hidepid=invisible,gid=wheel` and keep `systemd-logind` in `wheel`. Otherwise the agent reads the operator's `sudo` command lines, and without the group logind stops tracking sessions.

## yo internals

- ALWAYS keep `YO_VERSION = "dev"` and `YO_SOURCE_REV = "dev"` in `config.py` as those exact lines, once each. The nix build rewrites both and the brew formula rewrites the first; a miss ships a `yo` that reports `dev`.
- NEVER import `lima` or `libvirt` anywhere but `cli.driver_for`, and ship each host only its own driver. A host runs one driver.
- ALWAYS set `allow_abbrev=False` on every parser. Otherwise a typo's prefix reaches `--yes`.
- ALWAYS print through `ui.out()` and `ui.err()`, and pass guest text only through `ui.shown()` or `ui.quoted_block()`. Guest output is attacker-controlled text that reaches the host's terminal. Only a guest session's output is quoted; a host tool's stderr is printed inline, so the reader can tell whose words they are.
- NEVER disable `services.openssh.allowSFTP`. `yo cp` uploads with scp's sftp mode, served by `internal-sftp` so no agent login shell runs for an upload.
- NEVER upload a symlink with `yo cp`. `scp -r` follows it and copies the target.
- ALWAYS unpack a `yo cp` download into a staging directory beside the destination, let only regular files and directories land, drop control paths there, and merge only then. The guest's `tar` runs through the agent's login shell, so the agent controls the stream, and a pulled tree never lands `.git`, `.hg`, `.svn`, `.jj`, `.husky`, `.envrc`, `.vscode` or `.idea`, matched case-insensitively, because the host would run them as the operator.
- ALWAYS derive a guest path by mirroring the host's logical `$PWD` under `$HOME`, never `realpath`. Physical resolution loses the spelling the user stands in.
- NEVER let a `yo` command target a guest path outside the guest `$HOME`, except `yo cp` under `/tmp`, where agents keep scratch output.
- ALWAYS read, decide and act under one lock when two `yo` processes can reach the same box, master, forward, broker or host port, and read again after taking it. What another `yo` did while this one waited is in no earlier read.
- ALWAYS take the box lock once, at the top of a command that creates, starts, unlocks, stops, rebuilds, grows or destroys the box, hold it to the command's end, and hand its `HeldBox` to every step that must run under it. `flock` is not re-entrant across two descriptors of one process, so a nested `box_lock()` would wait on itself; it raises instead.

## Distribution and release

- NEVER hand-edit `.version`. The release workflow writes it from the release tag.
- ALWAYS publish a release from the tip of `main`. The workflow force-moves the tag onto its stamp commit.
- NEVER let a VM hold a checkout of this repo. `yo bootstrap` builds as root from a published flake ref, and a guest checkout is a tree the agent could edit before root builds it.
- ALWAYS keep `aka-rider/yolobox` public. Every box fetches its flake ref anonymously.
- ALWAYS pin every GitHub Action to a full commit SHA with its full tag in a comment, keep workflow permissions at `contents: read` except the release job, and `persist-credentials: false` on every checkout that does not push. The release job holds writable tokens.
- ALWAYS re-run only a release's failed jobs, and NEVER turn on immutable releases. A full re-run replays against the moved tag, and the job re-uploads and moves the tag.

## Building

- ALWAYS `git add` new files before nix evaluates a local clone. Nix ignores untracked files.
- ALWAYS run `nixos-rebuild` unpiped and read its exit code. A pipe reports a failed bootloader install as success.
- ALWAYS run shellcheck at its default severity, as `writeShellApplication` does. A finding that passes at `-S warning` fails the box build at bootstrap.
- NEVER pipe into `grep -q` under `set -o pipefail`. The producer dies of SIGPIPE and a present thing reads as missing.
- ALWAYS assign a `mktemp` result on a line of its own, and delete only a variable the script set, after checking it is not empty. Inside another command or after `local` its failure is lost, and the empty path is deleted from.
- ALWAYS run every tool whose words you match or whose sort order you compare under `LC_ALL=C`, host or guest: `yo` runs its host tools through `proc` and every guest command it builds through `sshx.ssh_run` that way, and a script that matches a tool's words exports it at its top level. A translated tool says something else, and nix and every byte comparison sort bytewise. Only an interactive session keeps the operator's locale.

## Harnesses, herdr and the display

- NEVER let anything but the box own `~/.local/bin/claude`, and NEVER give the launcher `runtimeInputs`. The launcher exports `HERDR_AGENT`, and `runtimeInputs` would shadow every tool a session runs.
- ALWAYS watch the claude version directory with `PathChanged`, never `PathModified`, and set `StartLimitIntervalSec = 0` on the service it triggers. One download is thousands of writes.
- NEVER let a vendor installer edit an rc file, and keep `~/.claude/settings.json` a regular file and pi's settings pi's own. An rc line changes `PATH` behind the box's back, and a symlinked settings file sends writes elsewhere or drops them.
- NEVER drop a retired `L+` link from the config without an `r` rule. A rebuild does not remove what it stops declaring.
- ALWAYS keep the VM free of a C and Python toolchain. It keeps native npm modules from compiling at install time.
- ALWAYS keep herdr in `environment.systemPackages` and `HERDR_AGENT=claude` exported before the launcher's `exec`. `herdr machine add` adopts the system binary, and herdr classifies an agent by process name.
- NEVER add a tmpfiles rule that deletes a file herdr installs. `r` rules run on every switch.
- NEVER add a tmpfiles rule for `/tmp/.X11-unix`. systemd ships one, and a duplicate logs errors every boot.

## Disk and gc

- ALWAYS gate deleting a project directory on `git check-ignore`. Deleting a tracked directory dirties the checkout and refuses every later push.
- NEVER delete a directory because of its name alone. A `target` can hold code; the project's own clean command, or its manifest beside a git-ignored directory, decides.
- ALWAYS limit gc's cache list to caches a tool refetches by itself, and NEVER prune podman volumes. Model weights and volumes cost data the box may not get back.

## AWS credentials

- ALWAYS choose an AWS profile per process with `AWS_PROFILE`, never one switchable global profile. botocore fixes its credential source at first use.
- ALWAYS run one broker per box, its sockets, `yo`'s lock on it and its log in that box's own directory, and tie its lifetime to a host process that lives exactly as long as the box (Lima's hostagent, or the operator's ssh master on Linux). Reach it over a reverse unix socket on `yo`'s own master, never TCP. A broker that outlives its box serves nobody, one shared by all boxes is re-pinned to whichever box last ran a session command, and a host VPN can blackhole the gateway's TCP while egress works.
- NEVER guard the reversed socket with a bearer token, and NEVER write a secret to stdout after the handshake or to a log. File permissions are the boundary, and logs are read later by anyone.
- ALWAYS require a `region`, refuse a response without `SessionToken`, answer every failure with the same generic 500, and bound connections, request size and time. The guest is the untrusted side.
