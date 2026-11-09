"""The argument parser, driver choice and the process entry point."""

from __future__ import annotations

import argparse
import functools
import os
import re
import signal
from typing import Callable, Sequence

from . import (
    config,
    cp,
    driver,
    forwards,
    proc,
    release,
    session,
    sshx,
    state,
    status,
    ui,
    vm,
)
from .config import (
    DEFAULT_INSTANCE,
    MAX_PORT,
    MIN_DISK_GIB,
    MIN_DISK_WHY,
    MIN_MEMORY_GIB,
    MIN_MEMORY_WHY,
)
from .driver import Driver
from .ui import YoError


EXIT_BROKEN_PIPE = 128 + signal.SIGPIPE

# Lima's own rule for an instance name (identifiers.Validate): it becomes a
# directory under ~/.lima, an ssh host name and an ssh config pattern. instance_name()
# narrows it to what also makes a hostname.
INSTANCE_NAME_RE = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*")


def instance_name(value: str) -> str:
    """argparse `type=` of -m/--machine: a lima instance name `yo` can drive,
    whose instance directory on this host holds the box's unix sockets: a
    name too long for them is refused before any command runs."""
    if (
        not INSTANCE_NAME_RE.fullmatch(value)
        or value.startswith("lima-")
        or not config.HOSTNAME_LABEL_RE.fullmatch(state.hostname_for(value))
    ):
        # "lima-" is excluded because instance "lima-x" would give its herdr alias
        # the name of instance "x"'s ssh host. The hostname rule excludes '.' and
        # '_' (no DNS label has them) and names over 55 characters, because the
        # box's hostname, yolobox-<name>, is also its tailnet node name.
        raise argparse.ArgumentTypeError(
            "%r is not a usable machine name (letters and digits, joined by "
            "single '-', at most 55 characters, not starting with 'lima-')" % value
        )
    directory = state.instance_dir_of(
        driver_for(config.HOST_PLATFORM).instances_dir, value
    )
    refusal = state.sockets_refusal(directory, config.HOST_PLATFORM)
    if refusal is not None:
        raise argparse.ArgumentTypeError(refusal)
    return value


def driver_for(platform: str) -> Driver:
    """The driver for a host OS, spelled as sys.platform does.

    Each driver module is imported here and nowhere else, because a host ships
    only its own: lima.py is not installed on Linux, libvirt.py not on macOS.
    """
    if platform == "darwin":
        from . import lima

        return lima.LIMA
    if platform == "linux":
        from . import libvirt

        return libvirt.LIBVIRT
    raise YoError("yo drives the box from macOS or Linux, not %r" % platform)


def guest_port_number(value: str) -> int:
    if not proc.is_ascii_number(value) or not 0 < int(value) <= MAX_PORT:
        raise argparse.ArgumentTypeError(
            "guest port must be a number from 1 to %d, got '%s'" % (MAX_PORT, value)
        )
    return int(value)


def whole_gib(label: str, value: str) -> int:
    if not proc.is_ascii_number(value) or int(value) == 0:
        raise argparse.ArgumentTypeError(
            "%s must be a whole number of GiB greater than 0, got '%s'" % (label, value)
        )
    return int(value)


def box_gib(label: str, floor: int, why: str) -> Callable[[str], int]:
    """argparse `type=` of a box size: whole GiB, and at least `floor` because `why`."""

    def parse(value: str) -> int:
        parsed = whole_gib(label, value)
        if parsed < floor:
            raise argparse.ArgumentTypeError(
                "%s must be at least %d GiB, got %d: %s" % (label, floor, parsed, why)
            )
        return parsed

    return parse


def box_cpus(value: str) -> int:
    """argparse `type=` of --cpus: a whole number from 1 to one less than the
    host's CPU count, so the host keeps a CPU."""
    try:
        host = driver.host_cpu_count()
    except YoError as unknown:
        raise argparse.ArgumentTypeError(
            "--cpus cannot be checked, %s" % unknown.message
        )
    if host < 2:
        raise argparse.ArgumentTypeError(
            "--cpus cannot be used: this host has only 1 CPU, and a box needs at "
            "least 1 and must leave the host 1"
        )
    if not proc.is_ascii_number(value) or not 1 <= int(value) < host:
        raise argparse.ArgumentTypeError(
            "--cpus must be a whole number of CPUs from 1 to %d (this host has %d "
            "CPUs and keeps at least 1), got '%s'" % (host - 1, host, value)
        )
    return int(value)


def add_size_arguments(parser: argparse.ArgumentParser) -> None:
    """--memory, --disk and --cpus, for the commands that create a box."""
    parser.add_argument(
        "--memory",
        metavar="GiB",
        type=box_gib("--memory", MIN_MEMORY_GIB, MIN_MEMORY_WHY),
        default=None,
        help="the memory of the box this command creates (at least %d); a box "
        "that exists is refused the flag" % MIN_MEMORY_GIB,
    )
    parser.add_argument(
        "--disk",
        metavar="GiB",
        type=box_gib("--disk", MIN_DISK_GIB, MIN_DISK_WHY),
        default=None,
        help="the disk of the box this command creates (at least %d); an existing "
        "box's disk grows with disk-grow" % MIN_DISK_GIB,
    )
    parser.add_argument(
        "--cpus",
        metavar="N",
        type=box_cpus,
        default=None,
        help="the CPU count of the box this command creates, from 1 to one less "
        "than the host's",
    )


DIRECTORY_RESOLUTION_EPILOG = (
    "enter, code and zed resolve the directory the same way: with a project\n"
    "argument, pick among the VM's git repos with fzf; without one, the guest\n"
    "mirror of the current directory (its deepest existing ancestor when the\n"
    "mirror is missing, the guest home when the cwd is outside $HOME)."
)


def build_parser() -> argparse.ArgumentParser:
    # One line serves every machine on both hosts, so the help can name it.
    include_line = '"%s"' % config.SSH_CONFIG_INCLUDE_LINE
    parser = argparse.ArgumentParser(
        prog="yo",
        description="yo — thin host-side CLI for the NixOS-VM yolobox.",
        epilog=DIRECTORY_RESOLUTION_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--version", "-V", action="version", version="yolobox %s" % config.YO_VERSION
    )
    parser.add_argument(
        "-m",
        "--machine",
        metavar="NAME",
        type=instance_name,
        default=DEFAULT_INSTANCE,
        help="machine to drive (default: %s); accepted before or after "
        "the command" % DEFAULT_INSTANCE,
    )
    # session: the command talks to the guest, so main() first brings up the
    # operator's ssh master and the reverse forwards it carries, and says so
    # when it cannot.
    parser.set_defaults(needs_driver=True, session=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, description: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(
            name,
            help=help_text,
            description=description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            allow_abbrev=False,
        )
        # SUPPRESS: a subparser default would overwrite a -m given before the command.
        sub.add_argument(
            "-m",
            "--machine",
            metavar="NAME",
            type=instance_name,
            default=argparse.SUPPRESS,
            help="machine to drive (default: %s)" % DEFAULT_INSTANCE,
        )
        return sub

    up = add(
        "up",
        "Start the VM.",
        "Start the VM and unlock its encrypted disk. When the box's initrd asks for\n"
        "the passphrase, asks 1Password (Touch ID) for the yolobox-disk key; a box\n"
        "that is already up, or whose disk is not encrypted yet, needs no prompt.\n"
        "Safe to run again.\n"
        "--memory, --disk and --cpus size the box this command creates; a box that\n"
        "exists is refused them (yo destroy && yo up to resize it, yo disk-grow to\n"
        "grow its disk).",
    )
    add_size_arguments(up)
    up.set_defaults(func=vm.cmd_up)

    down = add("down", "Stop the VM.", "Stop the VM.")
    down.set_defaults(func=vm.cmd_down)

    destroy = add(
        "destroy",
        "Delete the VM and everything on it.",
        "Delete the VM, its disk and the files yo keeps for it on this host, and\n"
        "remove its herdr machine. Everything on the box is lost, and a running\n"
        "box is powered off without asking, and its AWS broker stopped. Without\n"
        "--yes it only lists what would be removed. It leaves what the host\n"
        "shares between machines: the Include line in ~/.ssh/config and the\n"
        "downloaded base image.",
    )
    destroy.add_argument(
        "--yes",
        action="store_true",
        help="delete instead of only listing what would be deleted",
    )
    destroy.set_defaults(func=vm.cmd_destroy)

    status_parser = add(
        "status",
        "Describe the VM.",
        "Describe the VM. Also reports disk usage on both sides and warns\n"
        "when this host's yo version differs from the box's. Exits 1 when any\n"
        "check found something wrong (an unreachable VM, an unencrypted disk,\n"
        "egress rules not in force, a dead forward or broker, version drift).",
    )
    status_parser.set_defaults(func=status.cmd_status)

    bootstrap = add(
        "bootstrap",
        "Create and build the VM from the pinned flake ref.",
        "Create the VM and build/(if needed) reboot it into the NixOS\n"
        "configuration from the pinned yolobox flake ref. Safe to run again: every step checks before it acts.\n"
        "--memory, --disk and --cpus size the box as they do for yo up.\n"
        "--version picks which flake ref to build from (e.g. a branch), and is\n"
        'required on an unreleased ("dev") build.\n'
        "The reboot into the first encrypted generation encrypts the disk in\n"
        "place: the box is unusable until it finishes, which can take many\n"
        "minutes on a large disk, and its first minutes (an offline\n"
        "filesystem shrink) must not be interrupted.",
    )
    bootstrap.add_argument(
        "--version",
        metavar="REF",
        type=release.bootstrap_version,
        default=None,
        help="what the box builds from: a release (1.7.2), a branch or tag on "
        "aka-rider/yolobox (main) or github:OWNER/REPO/REF "
        "(github:you/yolobox/x); default: this yo's own release",
    )
    add_size_arguments(bootstrap)
    bootstrap.set_defaults(func=vm.cmd_bootstrap)

    gc = add(
        "gc",
        "Report and reclaim the VM's disk usage.",
        "Report what is using the VM's disk. --yes reclaims the machine's own\n"
        "caches: journald, systemd coredumps, nix garbage plus a store\n"
        "optimise, root's and the operator's nix download caches, npm, podman,\n"
        "the tool caches under ~/.cache (go-build, gopls, goimports,\n"
        "golangci-lint, staticcheck, uv, pip, pip-tools, pnpm, zig,\n"
        "cargo-zigbuild, opengrep, nix), cargo's registry and git checkouts,\n"
        "and every Go module and build cache under the guest home. Each of\n"
        "those is rebuilt or refetched on demand, so the next build is slower\n"
        "and needs network. Model weights and other app data are never\n"
        "touched. /tmp scratchpads are reported only. --deep adds per-project\n"
        "build output under the guest home, cleaned by the project's own tools\n"
        "run in its devbox environment: make clean where the Makefile\n"
        "has a clean target, cargo clean for a target beside a Cargo.toml, go\n"
        "clean beside a go.mod. node_modules, .venv and a .next beside a\n"
        "package.json are removed outright. Those removals and cargo clean\n"
        "happen only where git ignores the directory; make clean and go clean\n"
        "are the project's own definition of clean and run unguarded. A target\n"
        "without its Cargo.toml or a .next without its package.json is kept and\n"
        "named, because the name alone does not prove it is build\n"
        "output. A removed .venv comes back with the project's own uv sync or\n"
        "devbox, and until then basedpyright has no environment for it. Run this\n"
        "from the host; in-VM tools die first.",
    )
    gc.add_argument(
        "--deep", action="store_true", help="also reclaim per-project build output"
    )
    gc.add_argument(
        "--yes", action="store_true", help="apply the reclaim instead of only reporting"
    )
    gc.set_defaults(func=vm.cmd_gc)

    disk_grow = add(
        "disk-grow",
        "Raise the VM's disk ceiling.",
        "Raise the VM's disk ceiling (grow-only; the backing file is sparse, so\n"
        "a large ceiling costs nothing until used), then grow the encrypted root\n"
        "and its filesystem into it. Stops and restarts the VM when the ceiling\n"
        "moves. Needs the disk passphrase (1Password, Touch ID). Safe to repeat:\n"
        "at the ceiling it only grows what is still short.",
    )
    disk_grow.add_argument(
        "size", type=functools.partial(whole_gib, "disk-grow size"), metavar="GiB"
    )
    disk_grow.set_defaults(func=vm.cmd_disk_grow)

    ssh = add(
        "ssh",
        "Plain shell in the VM as the operator account.",
        "Plain interactive shell in the VM as the operator account (the one with\n"
        "sudo), or run one command there.",
    )
    ssh.add_argument("ssh_cmd", nargs=argparse.REMAINDER, metavar="cmd...")
    ssh.set_defaults(func=session.cmd_ssh, session=True)

    enter = add(
        "enter",
        "Shell in the VM as the agent account.",
        "Shell in the VM as the unprivileged agent account, landing in the guest\n"
        "twin of the current project. 1Password and the AWS credentials broker\n"
        "ride the operator's ssh master, which this command opens if it is not up;\n"
        "set AWS_PROFILE in the shell to pick an allow-listed profile. Inside a\n"
        "herdr pane, prints a note that an agent started here is invisible to\n"
        "herdr — run agents in a workspace on the yolobox herdr machine instead.",
    )
    enter.add_argument("project", nargs="?")
    enter.set_defaults(func=session.cmd_enter, session=True)

    cp_parser = add(
        "cp",
        "Copy files between the host and the agent account.",
        "Copy files between the host and the VM's agent account, as scp. A guest\n"
        'path is any operand starting with ":": ":" alone is the guest twin of\n'
        'the current directory, ":rel/path" extends it, ":~" and ":~/x" are the\n'
        'guest home and a path under it, and ":/abs" is an absolute guest path\n'
        "(refused unless it is under the guest home or under /tmp, where agents'\n"
        "scratch output lives). Every source must be on\n"
        "the host or every source must be in the guest, never mixed, and the\n"
        "destination must be on the other side.",
    )
    cp_parser.add_argument("paths", nargs="+", metavar="path")
    cp_parser.set_defaults(func=cp.cmd_cp, session=True)

    code = add(
        "code",
        "Open VS Code Remote-SSH in the VM.",
        "Open VS Code Remote-SSH in the VM, as the agent account. Needs\n"
        "%s in\n"
        "~/.ssh/config (yo code offers to add it otherwise); also needs the\n"
        "ms-vscode-remote.remote-ssh extension." % include_line,
    )
    code.add_argument("project", nargs="?")
    code.set_defaults(func=session.cmd_code, session=True)

    zed = add(
        "zed",
        "Open Zed in the VM.",
        "Open Zed in the VM, as the agent account. Needs\n"
        "%s in\n"
        "~/.ssh/config (yo zed offers to add it otherwise)." % include_line,
    )
    zed.add_argument("project", nargs="?")
    zed.set_defaults(func=session.cmd_zed, session=True)

    port = add(
        "port",
        "Forward a guest port onto the host's loopback (Linux hosts).",
        "Forward a guest TCP port onto this host's 127.0.0.1, over the operator's\n"
        "ssh connection, and print the address, 127.0.0.1:HOST_PORT. HOST_PORT is\n"
        "the lowest free port above the kernel's ephemeral range\n"
        "(/proc/sys/net/ipv4/ip_local_port_range). The forward lasts as long as the\n"
        "ssh connection. On macOS lima mirrors guest ports by itself, so this\n"
        "command exits 64 there.",
    )
    port.add_argument("guest_port", type=guest_port_number, metavar="GUEST_PORT")
    port.set_defaults(func=session.cmd_port)

    aws_check = add(
        "aws-check",
        "Heal the AWS broker and check every allow-listed profile from the guest.",
        "Start (or heal) the host AWS credentials broker, push its per-profile\n"
        "config into the VM, then run the guest's own check against every\n"
        "allow-listed profile: AWS_PROFILE=<P> aws sts get-caller-identity.",
    )
    aws_check.set_defaults(func=session.cmd_aws_check)

    # Added last: its choices are every command above, and itself.
    help_ = add(
        "help",
        "Print the help of a command, or of yo itself.",
        "Print the help of a command, or of yo itself. yo help gc is yo gc --help.",
    )
    help_.add_argument("topic", nargs="?", choices=list(subparsers.choices))

    def cmd_help(args: argparse.Namespace) -> None:
        target = parser if args.topic is None else subparsers.choices[args.topic]
        target.print_help()

    help_.set_defaults(func=cmd_help, needs_driver=False)

    return parser


def main(argv: Sequence[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.needs_driver:
        state.set_box(driver_for(config.HOST_PLATFORM), args.machine)
        # Refused here, before any tool runs: the name reaches the box's cidata
        # and its sshd config, and nix/base.nix would refuse it only at the
        # rebuild, after the box was created.
        config.operator_account(release.host_user())
        state.driver().require_tool()
        if args.session:
            # The services `yo up` declares, declared again: any session command
            # restores a master, a forward or a broker that died since. A box
            # not bootstrapped yet has none to serve. A session reaches the box
            # without them too: `yo ssh` is the way into a box whose forwards
            # are broken.
            try:
                if sshx.bootstrapped(mux=True):
                    forwards.vm_services()
            except sshx.Unreachable as lost:
                # Not up, still in its initrd, or refusing the operator's key:
                # ssh's own words below say which.
                ui.err(
                    "yo: %s does not take the operator's login, so nothing is "
                    "forwarded into this session: %s" % (state.box.name, lost.message)
                )
            except YoError as failure:
                ui.err(
                    "yo: this session runs without what is not up: %s" % failure.message
                )
    args.func(args)
    return 0


def entry(argv: Sequence[str]) -> int:
    """Run `yo` with `argv` and return its exit code: the launcher's whole job."""
    try:
        return main(argv)
    except YoError as failure:
        if failure.message:
            ui.err("yo: %s" % failure.message)
        return failure.code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # Stdout is gone, so the interpreter's own flush at exit would fail
        # again. 141 is what a shell reports for a process killed by SIGPIPE.
        os._exit(EXIT_BROKEN_PIPE)
