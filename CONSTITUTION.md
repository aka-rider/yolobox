# Constitution

The rules for changing this repo. Read this before your first change.

Every rule was paid for by an outage or a near miss. The sentence after the
rule says why, so you can tell when it stops applying. The link after that
is the story: what happened, the evidence, and how to recognise it again.
Stories live beside the code they explain (`nix/base.md` beside
`nix/base.nix`) or, when they span several files, in `docs/`.
[docs/README.md](docs/README.md) is the index.

## Before you start

Identify whether you are running on the host (macOS or Linux) or in the guest (Linux). `uname` cannot tell a Linux host from the guest: the guest alone has `yolobox-guest` and `/etc/yolobox/version`, and has no `yo`.

## Writing docs and comments

- NEVER write one operator's machine, network, vaults, providers or projects into a tracked file, as a default or as a fact. The repo is generic; a box's own setup lives in its `/etc/yolobox/local.nix`, and an incident story keeps the failure and how to recognise it, not whose machine it was. [story](docs/README.md#no-operator-specifics)
- NEVER cite a commit hash, or a past tag or version, as provenance in a tracked file. History gets rewritten and tags get deleted, so the pointer rots and leaves the reader with nothing. State the fact itself, in place. Hashes of other repos and pinned action SHAs are exempt. [story](docs/README.md#no-revision-pointers)

## The VM

- ALWAYS keep the VM rebuildable from this repo alone, plus `/etc/yolobox/local.nix` and what it imports, when the box has one. That file is the one sanctioned per-box input and is optional. It has no snapshots and is meant to be thrown away, so any other hand setup after creation is a defect. [story](docs/distribution.md#flake-ref-not-a-checkout)
- ALWAYS keep `nix/base.nix`'s mount layout identical to the shipped nixos-lima image (`/boot` is the ESP), except `/` on LUKS, which the initrd migrates in place on the first boot. Any other divergence means every fresh instance needs a manual mount migration before its first switch. [story](nix/base.md#the-esp-holds-one-kernel)
- ALWAYS keep `boot.loader.grub.configurationLimit = 1`. The ESP is 249 MiB and GRUB copies the incoming kernel before it prunes old ones, so two retained kernels plus one incoming do not fit. [story](nix/base.md#the-esp-holds-one-kernel)
- ALWAYS keep `restartIfChanged = false` on `lima-init` and `lima-guestagent`. Restarting the guest agent kills every port lima forwards until the VM is stopped and started again. [story](lima/yolobox.md#an-empty-page-means-limas-forwarder-died)
- ALWAYS declare the operator's account in `nix/base.nix` as `users.users.${username}`, fed by `YOLOBOX_USERNAME=$(id -un)` and `--impure`. A hardcoded name creates a second account with the same uid and splits state across two homes. [story](docs/accounts.md#two-accounts)
- ALWAYS treat `lima/yolobox.yaml` as read once, at creation. An existing instance changes only through `limactl edit` while stopped. [story](lima/yolobox.md#read-once)
- ALWAYS restate `portForwards` in full, with the command `yo up` prints, never a hand-copied one. Lima's yq cannot read the repo file, and a stale copy drops the socket rules. [story](lima/yolobox.md#checking-a-running-instance)
- ALWAYS keep the socket-forward rules in `lima/yolobox.yaml` equal to `yo`'s `LIMA_PORT_FORWARDS`. The constant, not the yaml, builds the restate command, and `TestLimaYamlMatchesPortForwardsConstant` in `tests/test_consistency.py` enforces it. [story](lima/yolobox.md#checking-a-running-instance)
- NEVER let lima mirror a guest UDP listener onto the Mac. `lima/yolobox.yaml` ends its UDP handling with one `proto: udp`, `ignore: true` rule that also names `guestIP: 0.0.0.0` and `guestIPMustBeZero: false`, because lima's default `guestIP` skips wildcard listeners. A mirrored tailscaled port took 41641 from a host's own Tailscale, and port 68 intercepts the host's DHCP. [story](lima/yolobox.md#guest-udp-is-never-mirrored)
- NEVER let lima mirror a guest port outside 1024–49151, or a known OAuth callback port, onto the host. `lima/yolobox.yaml` orders its TCP rules deny-list, range, catch-all, because lima takes the first match, and its default rule would otherwise mirror every port. [story](lima/yolobox.md#which-guest-ports-reach-the-host)
- ALWAYS keep `vmType`, `propagateProxyEnv: false`, `ssh.loadDotSSHPubKeys: false`, `ssh.overVsock: false` and `user.uid: 501` pinned in `lima/yolobox.yaml`. Lima's defaults leak the Mac's proxy variables and every `~/.ssh/*.pub` into the guest, let ssh ride vsock where the initrd that unlocks the disk cannot answer, and give the operator the Mac account's uid where `nix/base.nix` declares 501. [story](lima/yolobox.md#pinned-settings)
- ALWAYS keep the `user@.service` `OOMScoreAdjust` override an `asDropin`. Replacing upstream's unit text loses the rest of it, and the default of 100 makes the user manager the kernel's preferred victim. [story](nix/base.md#memory-pressure)

## Disk encryption

- ALWAYS keep `/` on LUKS2 (`boot.initrd.luks.devices.root` over `/dev/vda2`, `fileSystems."/".device = "/dev/mapper/root"`), unlocked only by `yo` through the initrd sshd. The image file on the Mac is otherwise plaintext in every backup and copy. [story](docs/disk-encryption.md#what-is-encrypted-and-what-is-not)
- NEVER use the 1Password CLI (`op`) or any 1Password interface but its SSH agent socket, on the Mac or in the guest. The CLI adds a session token any process can reuse, while the agent hands out signatures, never keys, and names the asking application in its prompt. [story](docs/disk-encryption.md#the-key)
- NEVER store sensitive information on disk, on the Mac or in the guest: no key file, passphrase, signature, token, recovery code or initrd host key. Secrets travel over sockets and stdin and live in memory, the disk passphrase is derived on demand, and the initrd host key is generated per boot in RAM. What predates the rule (lima's instance key under `~/.lima/_config`, credentials vendor tools keep under the agent's home, the stage-2 sshd host keys) is listed in the story, not grandfathered. [story](docs/disk-encryption.md#secrets-that-still-live-on-disk)
- NEVER change `LUKS_SIGN_DATA`, `LUKS_KDF_INFO` or `derive_luks_passphrase`. Together they are the passphrase of every existing disk, and there is no recovery slot. [story](docs/disk-encryption.md#the-key)
- NEVER approve a 1Password prompt for the `yolobox-disk` key outside `yo up`, `yo bootstrap` or `yo disk-grow`. The same agent is forwarded into every agent session, and one approved signature is the disk passphrase. [story](docs/disk-encryption.md#what-is-encrypted-and-what-is-not)
- ALWAYS keep `ssh.overVsock: false` pinned on the instance, and its gap fatal in `vm_up` (`LIMA_FATAL_GAPS`). Lima picks vsock or TCP for port 60022 once per start, and the initrd only speaks TCP. A vsock peer also has no IP address, so the guest's `AllowUsers`, which admits the operator only from 192.168.5.2, would lock the operator out. [story](docs/disk-encryption.md#how-yo-reaches-the-initrd)
- ALWAYS grow an encrypted root with `yo disk-grow`, never with `autoResize`. `cryptsetup resize` needs the passphrase, which only `yo` holds. [story](docs/disk-encryption.md#growing-an-encrypted-disk)
- ALWAYS list a shell script's tools in `boot.initrd.systemd.storePaths` when the script runs in the initrd. The initrd builder follows ELF dependencies only, and a missing tool shows up as a box stuck in its initrd. [story](docs/disk-encryption.md#how-yo-reaches-the-initrd)
- NEVER create or chmod anything under `/run/yolobox` from the initrd. The directory survives switch-root and holds the agent's sockets. [story](docs/disk-encryption.md#encrypting-in-place)
- ALWAYS keep `PerSourcePenalties no` ahead of the `Match` block in the initrd's `sshd_config`. Lima's readiness loop fails as an invalid user from `yo`'s own source address every 3 s, and OpenSSH would block that address for up to 600 s, while a global keyword after `Match` makes sshd refuse its config and leaves the initrd unreachable. [story](docs/disk-encryption.md#how-yo-reaches-the-initrd)

## Accounts

- NEVER give the agent account uid 501. It is lima's own cidata uid, reserved for the operator, and a second account sharing it splits state across two homes with `whoami` unable to tell them apart. [story](docs/accounts.md#why-a-new-account-not-a-stripped-one)
- NEVER add the agent to `wheel`, to `nix.settings.trusted-users` or to any sudoers rule, and NEVER make the agent lima's cidata account. `lima-init` runs `usermod -a -G wheel $LIMA_CIDATA_USER` after activation on every boot, so stripping `wheel` from that account is silently undone. [story](docs/accounts.md#why-a-new-account-not-a-stripped-one)
- NEVER give `agent` or `root` a usable password. Both stay locked, and `passwd -l` runs on every activation, because `hashedPassword` applies only when an account is created. [story](docs/accounts.md#locked-passwords)
- NEVER point the agent's `AuthorizedKeysFile` at a file lima-init writes. sshd opens it with the agent's uid, which gets `EACCES`, so the agent uses the root-run `AuthorizedKeysCommand` instead. [story](docs/accounts.md#authorizedkeyscommand)
- NEVER grant `CAP_SYS_PTRACE`, through `security.wrappers`, `setcap` or ambient capabilities, to any binary the agent can run. The capability ignores uids, so it attaches to root's processes and hands the agent root. Same-uid debugging comes from `kernel.yama.ptrace_scope = 0`. [story](nix/base.md#same-uid-ptrace)
- ALWAYS point `homeTmpfiles`, every systemd `User=` and `ConditionUser=` at `agentUser`, never at `${username}`. The one deliberate exception is `opProxy` in `nix/base.nix`, whose proxy services run as the operator because the forwarded 1Password and AWS broker sockets they proxy are operator-owned (`docs/ssh.md`). No module but `nix/base.nix` may name the `username` variable, and `TestOnlyBaseNixSeesTheOperator` enforces it. [story](docs/accounts.md#two-accounts)
- NEVER add a direnv hook or an `.envrc` auto-loader to the box. An `.envrc` is code the agent writes, and the operator would run it on the first `cd` after a pull, with the operator's `sudo`. A project's environment is entered explicitly with `devbox shell` or `devbox run`, and editor language servers launch through `lspWrapper` in `nix/lsp.nix` (`devbox run -q -- <server>` when the project has a `devbox.json`). [story](nix/lsp.md#no-direnv)
- ALWAYS keep `security.sudo.execWheelOnly = true`, so the non-wheel agent cannot exec the setuid `sudo` wrapper. The agent cannot authenticate anyway, but the wrapper should not even be reachable. [story](nix/base.md#sudo-is-wheel-only)
- NEVER enable a setuid or capability wrapper outside `keptWrappers` in `nix/hardening.nix`, which spells the allowlist and journals each entry. Every wrapper is a path to root for the agent, and an eval-time assertion fails the rebuild on any other, and on a kept one that gains setuid, another owner or program, or capabilities. A box that needs one, such as a VPN client's split-tunnel helper, names it and its capabilities in `yolobox.hardening.extraWrappers` in its own `/etc/yolobox/local.nix`. [story](nix/hardening.md#setuid-wrappers)
- NEVER put the agent in `systemd-journal`, `adm` or any other group that reads the system journal. It holds the operator's `sudo ... COMMAND=<argv>` lines, which is why the display units are the agent's own user units. [story](docs/accounts.md#no-journal-group)

## Identities and SSH

- ALWAYS let an ssh role, not a bare `User=` override, own the `ControlPath`. Lima's `ControlPath` has no `%r`, so OpenSSH never re-checks the login user of a new connection, and `-o User=agent` over the operator's live master silently runs as the operator. [story](docs/ssh.md#one-controlpath-per-role)
- ALWAYS reach the agent's 1Password through the lima reverse socket forward (`/run/yolobox-op/agent.sock`, proxied by `systemd-socket-proxyd` to the agent-owned `/run/yolobox/op-agent.sock`), never through `ForwardAgent`. The forward is set up once per hostagent start and lives with the VM, not with any one pane. On a Linux host there is no hostagent: `yo` makes the same reverse forward, to the same guest socket, over the operator's ssh master and heals it in `vm_services()`, which `main()` runs before `ssh`, `enter`, `cp`, `code` and `zed` and which `up`, `bootstrap` and `disk-grow` run themselves. [story](docs/ssh.md#healing-reverse-forwards)
- NEVER put a `ForwardAgent` other than `no` in `~/.lima/yolobox/ssh.config`. Every ssh role `yo` spawns reads that file, so a forward there would reach `yo code` and `yo zed`. The owned agent `Match` block and the herdr alias say `no`, so the operator's own `ForwardAgent yes` cannot apply to them. On a Linux host the file is `~/.local/state/yolobox/instances/NAME/ssh.config`, and the same holds. [story](docs/ssh.md#the-owned-block-in-sshconfig)
- ALWAYS limit `yo ssh`'s `ForwardAgent` to its own command line over `ControlPath=none`, pointing at the 1Password socket. The default forwards `$SSH_AUTH_SOCK`, which on macOS is Apple's empty agent, and a forward on a shared master would outlive its pane. [story](docs/ssh.md#yo-ssh-forwards-per-session)
- ALWAYS verify the guest's host key from `~/.lima/yolobox/known_hosts`, with `NoHostAuthenticationForLocalhost no` in the owned head block of `ssh.config`. Lima's own block sets it to yes, which disables verification for 127.0.0.1, and ssh keeps the first value. Only the fetch of the keys (`OPERATOR_FIRST_CONTACT`) may opt out, on its command line. On a Linux host the pin is `~/.local/state/yolobox/instances/NAME/known_hosts`, and the `ssh.config` that `yo` writes there carries the same two lines. [story](docs/ssh.md#host-key-pin)
- ALWAYS let the guest's ssh identity setup (`~/.ssh/config`, the `*.pub` selectors) live in the guest, never by the Mac reading files and pushing them in. The Mac reading paths the guest can influence is how a repo-held symlink once pointed at a private key. [story](docs/ssh.md#git-identities-in-the-guest)
- NEVER set `IdentityAgent` unconditionally in the shared ssh config. It goes only under `Match exec "test -S <1Password socket>"`, and the VM has no such socket, so an unconditional one would take the forwarded agent away. [story](docs/ssh.md#git-identities-in-the-guest)
- NEVER let an rc file replace an inherited `SSH_AUTH_SOCK`. `/etc/zshenv` sets it before `~/.zshrc` runs, and an `eval "$(ssh-agent -s)"` there swapped in an empty agent in the first shells after boot. [story](docs/ssh.md#an-rc-file-took-the-agent-away)
- ALWAYS spell git `includeIf` paths exactly as the project path is spelled on the Mac. `yo` mirrors the logical spelling into the VM verbatim, so any other spelling matches nothing there. [story](docs/ssh.md#git-identities-in-the-guest)
- NEVER let `yo` offer the box's stage-2 sshd a user it rejects, and ALWAYS keep the gateway 192.168.5.2 in its `PerSourcePenaltyExemptList`. Every host connection comes from that one address, so one penalty locks every `yo` command out; `yo` tells stage 2 from the initrd by the banner word `yolobox-stage2`, and a command that must log in checks the login key is on offer first. [story](nix/base.md#sshd-penalties)
- NEVER copy a private key into the VM. [story](docs/ssh.md#git-identities-in-the-guest)

## Network and Tailscale

- ALWAYS keep the egress table: no guest-originated traffic to the host or a private network except DNS to the Lima gateway, DHCP to the broadcast and the gateway, `lanAllow` and the tailnet, whose access policy is the gate; no stop hook. [story](nix/egress.md)
- ALWAYS keep `--netfilter-mode=off` in tailscale's `extraSetFlags`, and NEVER add firewall rules with `iptables -I INPUT 1`. `nixos-fw` owns the rules, and by default tailscaled inserts its own jump ahead of it and re-inserts it at the top whenever it restarts. [story](nix/tailscale.md#firewall)
- ALWAYS keep `--ssh=false` and `--accept-dns=false` in `extraSetFlags`. The first stops tailscaled authorising ssh by tailnet ACL instead of this repo's keys, and the second keeps MagicDNS from taking over the guest's resolver. [story](nix/tailscale.md#dns-and-logging)
- NEVER pass `--hostname` to `tailscale up`, and NEVER answer a refused `tailscale up` with `--reset`. `extraSetFlags` pins the name and the other non-default flags, and `--reset` clears them. Restart `tailscaled-set` instead. [story](nix/tailscale.md#login)
- ALWAYS keep `tailscale0` trusted, so the tailnet ACL decides who reaches the box, and ALWAYS keep `enp0s1` at port 22 only, whatever `ssh.overVsock` says. A service on the tailnet address must answer with no further setup, and lima's start-up wait and its usernet fallback dial 192.168.5.15:22 over TCP, where a dropped packet shows as a 600 s timeout. [story](nix/tailscale.md#sshd-on-two-interfaces)
- ALWAYS keep `RemainAfterExit = true` on `tailscaled-set`. A `Type=oneshot` unit that has exited is inactive, and a switch restarts a changed unit only if it is active, so flag changes would never apply. [story](nix/tailscale.md#tailscaled-set)
- ALWAYS put a box-local route in `networking.dhcpcd.runHook`, never in `networking.interfaces.<if>.ipv4.routes`. A dhcpcd restart flushes the route and the oneshot that added it never runs again. [story](nix/tailscale.md#box-local-routes)

## The Linux host

- ALWAYS run a box on `qemu:///system` with a passt interface of type `user`, and NEVER on `qemu:///session`, a bridge or libvirt's `default` network. Only the system connection gives each domain its own sVirt category and a static DAC account, and a bridge gives the guest a layer 2 path to the host and the LAN. [story](docs/linux-host.md#why-qemusystem-and-passt)
- NEVER add graphics, vsock, filesystem, channel, hostdev or tpm to `libvirt/domain.xml`, or any second interface. Each is a path from the guest to the host that no table sees, and `config_gaps()` refuses a live domain that has one. [story](docs/linux-host.md#config-gaps-are-fatal)
- ALWAYS keep every config gap fatal on Linux (`LibvirtDriver.fatal_gaps`). A libvirt domain that is not what the template made may hold a path to the host, so `vm_up` refuses before it asks for the passphrase. [story](docs/linux-host.md#config-gaps-are-fatal)
- ALWAYS keep the `yolobox-host` nft table, `RequiredBy=virtqemud.service` on its unit and the static DAC account `yolobox-vm`, and NEVER give the unit an `ExecStop`. passt turns every guest connection into a host `connect()` and has no filter, so the table is the only layer the guest cannot undo, and it can tell the guest's traffic only by that uid. [story](docs/linux-host.md#the-host-table)
- ALWAYS give the domain's `portForward` the address `127.0.0.1`. Without an address passt binds every host address and the box's sshd lands on the LAN. [story](docs/linux-host.md#host-ports)
- ALWAYS take host ports only from the window above the second field of `ip_local_port_range`, never from a fixed list or a deny-list. The kernel never uses that window as a source port and no registered service lives there, so a port picked in it collides with nothing. [story](docs/linux-host.md#host-ports)
- NEVER pass the host OS into the guest, as a nix variable, a stamp or a module argument; gate each setting on the fact it needs. The host OS is a proxy that is wrong at the edges (an x86_64 box runs under Lima on an Intel Mac and under libvirt on a Linux PC), so the guest agent gates on its binary in Lima's cidata and rosetta on an aarch64 CPU, and the operator's `AllowUsers` holds on every box. `TestTheGuestNeverLearnsTheHostOs` guards it. [story](nix/base.md#no-host-os-in-the-guest)
- ALWAYS read the 1Password socket through the driver (`state.driver().op_sock()`), never a hard-coded path. The Mac's lives under Group Containers and a Linux host's is `~/.1password/agent.sock`. [story](docs/linux-host.md#the-operators-key)
- ALWAYS keep `AllowUsers = [ "${username}@192.168.5.2" agentUser ]` and the `! -i enp0s1 -s 192.168.5.2 -j DROP` rule in `nixos-fw`, on every box. The agent holds the forwarded 1Password agent that serves the operator's key, and without both it could log in to its own box as the operator. `tests/box-hardening-check.sh` tries it. [story](docs/linux-host.md#the-operators-key)
- NEVER write a private key file for the operator on Linux. The operator is the 1Password item `yolobox-operator`, and `yo` stores only its public half in the instance directory. [story](docs/linux-host.md#the-operators-key)

## Kernel attack surface

- NEVER drop a name from `blockedModules` in `nix/hardening.nix` without a journal line in `nix/hardening.md` saying what needs it. The agent can autoload any protocol module with one `socket()` call, and the journal is how the next person learns why a name was ever blocked. [story](nix/hardening.md#kernel-modules)
- NEVER block `vsock`, `virtiofs` or `binfmt_misc`, and keep `overlay`, `fuse`, `tun`, `bridge`, `veth` and `dm_crypt` loadable. The lima guest agent rides on vsock, rosetta mounts over virtiofs and registers through `binfmt_misc`, and podman, tailscale and the LUKS root need the rest. [story](nix/hardening.md#kernel-modules)
- NEVER turn off unprivileged user namespaces to harden the box. The chromium sandbox, rootless podman and the nix build sandbox all create them. [story](nix/hardening.md#sysctls)

## yo internals

- ALWAYS keep `YO_VERSION = "dev"` in `yolobox_cli/config.py` as that exact literal, unreformatted. The Nix build and the brew formula match its text in that file, and the formula's `inreplace` can silently no-op. `TestYoVersionLiteral` guards it. [story](yo.md#yo_version)
- ALWAYS define the control-byte filter, the profile-name pattern and the pid-exit watch once, in `yolobox_cli/common.py`, which `aws-broker` imports too. Two spellings drift, and the broker's log and yo's terminal output are read by the same person. [story](docs/aws.md#the-profile-name-pattern)
- NEVER import `lima` or `libvirt` anywhere but `cli.driver_for`, and ALWAYS ship a host only its own driver module. A host runs one driver, and the other one's module and files are dead weight on it. `TestDriversAreImportedOnlyByCli` and `TestEachPlatformShipsItsOwnFiles` guard it. [story](docs/distribution.md#each-platform-ships-its-own-files)
- NEVER let a guest subcommand's own stderr contain `unknown subcommand`. `yo` reads that marker, with exit 64, as "the box is older than this `yo`". [story](nix/guest/yolobox-guest.md#version-skew)
- ALWAYS set `allow_abbrev=False` on the top-level parser and on every subparser. Otherwise two keystrokes of a typo reach `yo gc --deep` and `--yes`. [story](yo.md#one-parser)
- ALWAYS keep `yo ssh`'s trailing arguments `nargs=argparse.REMAINDER`. The printed `nixos-rebuild --impure` remedy only works if argparse passes `--impure` through. [story](yo.md#one-parser)
- ALWAYS branch on `project is not None`, never on truthiness, for the optional `project` of `enter`, `code` and `zed`. `yo enter ""` and `yo enter` are different invocations. [story](yo.md#one-parser)
- ALWAYS print through `out()` and `err()`, never `print` or a raw stream. They replace control characters, and guest output is attacker-controlled text that reaches the Mac's terminal. [story](yo.md#output-filter)
- NEVER disable `services.openssh.allowSFTP`. The transfer in `yo cp` is scp, which speaks sftp, so sshd runs `sftp-server` and no login shell for it. [story](yo.md#yo-cp)
- ALWAYS treat what a guest `find` prints as untrusted, and validate every record in `list_guest`: a clean path strictly under a named source, well-formed fields, no contradiction. A download runs `find` through the agent's login shell, which the agent owns, so a forged path could otherwise steer scp out of the staging directory. [story](yo.md#a-guest-listing-is-untrusted)
- ALWAYS glob-escape a guest source in `yo cp`, and NEVER escape a guest destination. scp expands wildcards in remote sources only, so escaping a destination creates a file named with backslashes. [story](yo.md#yo-cp)
- NEVER upload a symlink with `yo cp`. `scp -r` follows it and copies the target, which can be a private file the user never named. [story](yo.md#yo-cp)
- ALWAYS stage a `yo cp` download beside its destination and plan the merge before anything moves. scp writes through a symlink at the destination, and the agent controls what a pulled repo contains. [story](yo.md#yo-cp)
- NEVER let a `yo cp` download land a version-control or auto-exec control path. `.git`, `.hg`, `.svn`, `.envrc`, `.vscode` and `.idea`, matched case-insensitively at any depth, are skipped and reported, because the agent writes them and the host would run them as the operator on the next git command, `cd` or editor open. [story](yo.md#downloads-never-land-control-paths)
- ALWAYS have `yo status` check the live egress table's structure, not the unit state alone. When the unit is active it asserts the `output` hook, the `lo`/`tailscale0` accept and the private-range rejects, never the full rule content, which `lanAllow` and `tunnelInterfaces` vary per box. [story](yo.md)
- ALWAYS build a printed yo remedy with `yo_cmd()`. A bare `yo up` sends the user of a second machine to the default box. [story](yo.md#several-machines)

## Paths

- ALWAYS derive a guest path by mirroring the Mac's logical `$PWD` (or a repo's logical toplevel) relative to `$HOME` onto the guest `$HOME`. Use the logical path, never `realpath` or `cd -P`: physical resolution loses the spelling the user stands in. [story](yo.md#the-mirror)
- NEVER let a `yo` command target a guest path outside the guest `$HOME`. A path the mirror cannot place lands at the guest home with a loud stderr note. The one exception is `yo cp`, which also takes an absolute path under `/tmp`, where agents' scratch output lives. [story](yo.md#yo-cp)

## Distribution and release

- NEVER hand-edit `.version`. The release workflow writes it from the release tag, and `yo` derives its flake ref from it, so a wrong version builds the wrong system with no error. [story](docs/distribution.md#flake-ref-not-a-checkout)
- ALWAYS publish a release from a commit that is the tip of `main`. The workflow force-moves the tag onto its stamp commit, and moving a tag cut elsewhere republishes different content under a name someone may have fetched. [story](.github/workflows/release.md#moving-the-tag)
- NEVER let a VM hold a checkout of yolobox's own repo. `yo bootstrap` builds from a flake ref, and a guest checkout would reopen the push-to-checkout hazard for a repo nothing in the VM reads, and a path in `--version` would build as root from a tree the agent owns. `--version` accepts a release, a branch or tag of the upstream repo, or `github:OWNER/REPO/REF`, nothing else. [story](docs/distribution.md#flake-ref-not-a-checkout)
- ALWAYS keep `aka-rider/yolobox` public. `nixos-rebuild` fetches the flake ref anonymously, so a private repo fails every `yo bootstrap` and every `local.nix` rebuild at once. [story](docs/distribution.md#failure-shapes)
- ALWAYS validate the release tag against `^v[0-9]+\.[0-9]+\.[0-9]+$` as the first step of the workflow. The version is spliced into the tarball name, the formula URL and a commit message before any tag moves. [story](.github/workflows/release.md#early-refusals)
- ALWAYS pin every GitHub Action to a full commit SHA, with the tag in a comment. A tag is a name its owner can move, and the release job holds a writable token and the tap's. [story](.github/workflows/release.md#pinned-actions)
- ALWAYS keep workflow `permissions` at `contents: read`, with `contents: write` only on the `release` job, and `persist-credentials: false` on every checkout that does not push. A step that runs a script from the tree should not hold a writable token. [story](.github/workflows/release.md#early-refusals)
- ALWAYS give `bump-tap`'s checkout `ref:` the release tag name. A bare checkout asserts the tag still points at the commit that triggered the run, which `release` has just moved. [story](.github/workflows/release.md#bump-tap-and-actionscheckout)
- ALWAYS re-run only the failed jobs after a release failure, never the whole run. `verify.yml` would replay against the already-moved tag. [story](.github/workflows/release.md#bump-tap-and-actionscheckout)
- NEVER turn on immutable releases. `release` re-uploads the tarball with `--clobber` and force-moves the tag, and both are refused on an immutable release. [story](.github/workflows/release.md#moving-the-tag)

## Building

- ALWAYS `git add` new files before nix evaluates a local clone. Nix reads the git tree, so an untracked file does not exist to it. [story](docs/distribution.md#flake-ref-not-a-checkout)
- ALWAYS run `nixos-rebuild switch` unpiped, and read its exit code. A pipe through `tail` reports a failed bootloader install as exit 0. [story](nix/base.md#the-esp-holds-one-kernel)
- ALWAYS check `df /boot`, and compare `readlink /run/current-system` with `readlink /nix/var/nix/profiles/system`, when a switch behaves strangely. Disagreement means the box runs one generation and the profile claims another, so do not garbage-collect. [story](nix/base.md#the-esp-holds-one-kernel)
- ALWAYS use the `path` option, not `environment.PATH`, in a systemd unit. NixOS already defines the latter for every service, so a second definition conflicts.

## Display and browsers

- ALWAYS write `${pkgs.xvfb}/bin/Xvfb` explicitly. `pkgs.xorg.xvfb` is deprecated and `lib.getExe pkgs.xvfb` names a binary that does not exist. [story](nix/display.md#learned-the-hard-way)
- NEVER add a tmpfiles rule for `/tmp/.X11-unix`. systemd ships one, and a duplicate logs errors every boot. [story](nix/display.md#learned-the-hard-way)
- ALWAYS make `openbox` wait for the X socket in `ExecStartPre`, never in a `Restart=` loop. The socket lags Xvfb's start, and a restart loop trips systemd's start limit and stays failed. [story](nix/display.md#learned-the-hard-way)

## Harnesses and installs

- NEVER let anything but the box own `~/.local/bin/claude`. The launcher exports `HERDR_AGENT`, and the agent's shell rc files may prepend that directory to `PATH`, so nothing on the system `PATH` can win. It is a tmpfiles `L+` link to `/etc/yolobox/bin/claude`, re-asserted by `yolobox-claude-launcher`. [story](nix/harnesses.md#the-launcher)
- NEVER follow `claude update`'s "Fix" line about removing `~/.local/bin/claude`. It is the installer noticing the box's launcher, and removing the link runs claude unwrapped, with no `HERDR_AGENT`. [story](nix/harnesses.md#recognising-a-broken-launcher)
- NEVER give the launcher `runtimeInputs`. It would prepend the store's coreutils, findutils and git to `PATH`, and every tool a session runs would be shadowed by the box's. [story](nix/harnesses.md#the-launcher)
- ALWAYS set `StartLimitIntervalSec = 0` on a service a path unit triggers. One download writes thousands of times, and the default limit trips in the first second and leaves the path unit failed. [story](nix/harnesses.md#the-version-keeper)
- NEVER pipe into `grep -q` in a `set -o pipefail` script. `grep -q` exits at its first match, the producer dies of SIGPIPE, and an installed package reads as missing. Capture the output first. [story](nix/harnesses.md#install-phases)
- ALWAYS start `yolobox-harness-install` from `yolobox-harness-install.path`, which waits on the flag `yolobox-network-ready` creates once dhcpcd has a lease, and never wire the service to `default.target`. The agent's user manager starts in parallel with the network, so an install wired there fails on DNS first and waits out `RestartSec`. [story](nix/harnesses.md#install-phases)
- ALWAYS run each install phase in its own errexit subshell through `phase()`. One vendor's outage then cannot block the others, and the script still exits 1 at the end. [story](nix/harnesses.md#install-phases)
- NEVER let a vendor installer edit an rc file. pi's installer has no `--no-modify-path` flag. The install service runs it with no tty, so it takes the defaults, and if you run it by hand, answer `n` to its PATH prompt. An rc line changes `PATH` behind the box's back. [story](nix/harnesses.md#pi)
- ALWAYS keep `~/.claude/settings.json` a regular file, never a symlink into a version-controlled directory. claude writes its plugin installs there, and they would land in the checkout. [story](nix/harnesses.md#plugins-not-mcp-files)
- ALWAYS let pi own `~/.pi/agent/settings.json`, and NEVER add a tmpfiles rule for it. pi rewrites it and only logs a failed write, so a symlink silently drops every package, and an unconditional `r` rule deletes the real file. [story](nix/harnesses.md#pi)
- ALWAYS install pi packages with `pi install`, never by editing its settings file. [story](nix/harnesses.md#pi)
- NEVER drop a retired `L+` link or file from the config without an `r` rule or a hand delete. A rebuild does not remove what it stops declaring, and the dangling link breaks its owner. [story](nix/harnesses.md#pi)
- ALWAYS keep the VM free of a C and Python toolchain. It keeps native npm modules from compiling at install time. [story](nix/harnesses.md#install-phases)

## herdr

- ALWAYS start an agent in a herdr pane on the saved `yolobox` machine, never expect `yo enter`'s plain landing shell to report to herdr. Only a real pty opened against the VM's own herdr server reaches its manifest. [story](nix/herd-server.md#machine-panes)
- ALWAYS treat herdr compatibility as capability-gated, not version-equal. A saved machine needs the guest's `endpoint_protocol_generation` to equal the client's exactly, so bump `yolobox.harness.herdr.version`/`.hashes` when it does not. [story](nix/herd-server.md#capability-gating)
- ALWAYS keep `herdrPkg` in `environment.systemPackages`. `herdr machine add` adopts a compatible binary on its probe list, which includes `/run/current-system/sw/bin/herdr`, and otherwise tries an install that a non-interactive run refuses. [story](nix/herd-server.md#capability-gating)
- ALWAYS keep `HERDR_AGENT=claude` exported in the claude launcher ahead of its `exec`. herdr classifies by process name, and the launcher execs a binary named after its version directory, so without the hint a session reports nothing. [story](nix/herd-server.md#herdr_agent)
- NEVER add a tmpfiles rule that deletes a herdr-installed integration file (`~/.pi/agent/extensions/herdr-agent-state.ts`, for one). `r` rules rerun on every switch, so the rule would silently un-install the integration. [story](nix/harnesses.md#herdrs-pi-integration)

## Disk and gc

- ALWAYS diagnose a full disk from the Mac with `yo gc`. In-VM tools die first, while `yo ssh` still works and the operator's `sudo` can use ext4's root reserve (5% of the filesystem unless tuned). A dying agent session proves nothing, because the agent has no `sudo`. [story](docs/disk.md#recognising-a-full-disk)
- ALWAYS reclaim before growing. Build output and caches are the usual cause of a full disk, so free those first. [story](docs/disk.md#incidents)
- ALWAYS gate the deletion of a project directory on `git check-ignore`. Deleting a tracked directory dirties the checkout and silently refuses every later push. [story](nix/guest/yolobox-guest.md#gc-tiers)
- NEVER delete a directory because its name is in `BUILD_DIRS`. A name proves nothing, since a `target` can hold code or data. The project's own clean command, or a manifest beside a git-ignored directory, decides. [story](nix/guest/yolobox-guest.md#build_dirs-and-local_caches)
- ALWAYS limit `LOCAL_CACHES` to caches a tool rebuilds or refetches by itself. Model weights and app data (`huggingface`, `ms-playwright`) cost a download the box may not repeat, or break a tool. [story](nix/guest/yolobox-guest.md#build_dirs-and-local_caches)
- NEVER give `podman system prune` the `--volumes` flag, in `gc` or in the prune timer. Volumes hold database state. [story](nix/guest/yolobox-guest.md#gc-tiers)
- NEVER let `gc --deep` touch `.devbox/virtenv`. It is devbox's toolchain, not build output, and the walk prunes hidden directories so it stays out of reach. [story](nix/guest/yolobox-guest.md#gc-tiers)
- ALWAYS switch to a generation carrying `boot.growPartition` before `limactl edit --disk`. Otherwise the backing file grows and the guest partition stays where it was, with no error. [story](nix/base.md#growing-the-root-disk)

## AWS credentials

- ALWAYS fix a process's AWS profile with `AWS_PROFILE` against the guest's rendered `AWS_CONFIG_FILE`, never one global, switchable profile. botocore fixes its credential source at first use, so flipping a shared profile leaves a running process on the old account for tens of minutes. [story](docs/aws.md#design)
- ALWAYS tie the AWS broker's lifetime to lima's hostagent (`--watch-pid`, read from `limactl list yolobox --json`'s `hostAgentPID`), never to a per-session forward or an idle timeout, and never by opening lima's `ha.pid`. Profiles are chosen per process, so no session bounds the broker, and lima rewrites `ha.pid` on every start. On a Linux host there is no hostagent, so `--watch-pid` is the pid of the operator's ssh ControlMaster (`ssh -O check`), and `vm_services()` re-creates the master and re-ensures the broker, as it heals the reverse sockets, on the commands that heal (`yo.md#which-commands-heal`). [story](docs/aws.md#broker-lifetime)
- ALWAYS reach the AWS broker the way 1Password is reached: a lima reverse unix-socket forward (`/run/yolobox-op/aws-broker.sock`, proxied to the agent-owned `/run/yolobox/aws-broker.sock`), never a forwarded TCP port. A host-side VPN can blackhole every guest connection to lima's gateway address while general egress kept working. [story](docs/aws.md#transport)
- NEVER guard a reversed unix socket with a bearer token. Filesystem permissions on the agent-owned socket are the whole authorization boundary, and a token is a second mechanism to keep in sync. [story](docs/aws.md#transport)
- ALWAYS keep the agent's environment (`SSH_AUTH_SOCK`, `AWS_CONFIG_FILE`, `PI_CLIPBOARD_OSC52`) declared once, in `nix/lib/agent-env.nix`, applied identically to the herdr server and every agent login shell. `awsBrokerSock` there is the one name of the guest-side broker socket, and `TestAgentEnvMatchesYoSockets` checks it against `yo`. [story](docs/aws.md#guest-config)
- NEVER let the broker write a secret to stdout after its handshake line, or to any log. [story](docs/aws.md#broker-lifetime)

### Broker hardening

- ALWAYS spell the profile-name pattern identically in `yo`, `aws-broker` and `yolobox-guest`, and match it with `fullmatch`. A name goes into a `credential_process` argument and a URL path, and `$` matches before a trailing newline. [story](docs/aws.md#allowlist)
- NEVER allow-list a profile without a valid `region`. A region-less profile makes the CLI target the global STS endpoint, which a DNS filter on the host may sinkhole, and the region lands in the rendered config file. [story](docs/aws.md#allowlist)
- ALWAYS answer every failure of a request the broker reads with the same generic 500, and write the detail only to `broker.log`. A connection over the bound, or a request head over the cap, is closed with no answer at all. The guest is the untrusted side and the response is the one channel it reads. [story](docs/aws.md#minting)
- NEVER hand long-lived IAM keys to the guest. A response without a `SessionToken` is refused at request time. [story](docs/aws.md#minting)
- ALWAYS compare socket identity with `lstat`, never `stat`. A path replaced by a symlink to the old socket would otherwise report the target's identity and be deleted as ours. [story](docs/aws.md#broker-lifetime)
- ALWAYS bound the broker: at most 64 open connections on the guest socket, closing the next at accept, an 8 KiB request head, at most 32 concurrent `/creds/` requests, answering the next with `503`, a 60 s wall-clock deadline per connection, and a 15 s wait for a profile's lock before a `503`; and answer yo's `/health` on a host-only control socket. A mint can take 30 s, a silent or trickling client must free its slot, and a guest flood must not make yo think the broker is gone. [story](docs/aws.md#bounds-on-one-request)
- ALWAYS write the guest AWS config atomically, with `mktemp` and `mv`. A `credential_process` call must never read a half-written file. [story](docs/aws.md#guest-config)

## Project repos in the VM

- ALWAYS keep a project's VM checkout clean before pushing to it. The push target is `updateInstead` (set system-wide in `nix/base.nix`), which refuses when the worktree or index differs from HEAD. [story](nix/base.md#pushing-into-a-vm-checkout)
- ALWAYS pull the VM's commits (it commits `devbox.lock`) before pushing again, or the push lands on a diverged branch and is refused. The pull is also the sandbox's trust boundary, so review agent-authored commits before running anything in them. [story](nix/base.md#pushing-into-a-vm-checkout)
