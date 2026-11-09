"""yo cp: scp up, a tar stream down, with the refusals, staging and merge."""

from __future__ import annotations

import argparse
import os
import posixpath
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import paths, proc, sshx, state, ui
from .config import AGENT_HOME, FACTS
from .sshx import AGENT
from .ui import YoError


@dataclass(frozen=True)
class CpPlan:
    direction: str
    local_paths: List[str]
    guest_paths: List[str]


def operand_is_guest(operand: str) -> bool:
    return operand.startswith(":")


CP_GUEST_ROOTS = (AGENT_HOME, "/tmp")


def resolve_guest_operand(operand: str, cwd_mirror: Optional[str]) -> str:
    spelling = operand[1:]
    if spelling == "~" or spelling.startswith("~/"):
        return paths.guest_path(AGENT_HOME + spelling[1:])
    if spelling.startswith("/"):
        return paths.guest_path(spelling, CP_GUEST_ROOTS)
    if cwd_mirror is None:
        raise YoError(
            'guest path "%s" is relative, and the host cwd is outside $HOME; '
            'spell it as ":~/..." instead' % operand
        )
    if spelling == "":
        return paths.guest_path(cwd_mirror)
    return paths.guest_path(cwd_mirror + "/" + spelling)


def resolve_cp_operands(operands: List[str], cwd_mirror: Optional[str]) -> CpPlan:
    if len(operands) < 2:
        raise YoError("yo cp needs at least one source and a destination")
    if "" in operands:
        raise YoError("yo cp got an empty operand")
    sources = operands[:-1]
    dest = operands[-1]
    if not any(operand_is_guest(operand) for operand in operands):
        raise YoError(
            'no guest path given (an operand must start with ":"); use plain "cp" for a local copy'
        )
    source_guest_flags = [operand_is_guest(source) for source in sources]
    dest_is_guest = operand_is_guest(dest)
    if len(set(source_guest_flags)) > 1:
        raise YoError(
            "sources are split between the host and the guest; every source must be on one side"
        )
    sources_are_guest = source_guest_flags[0]
    if sources_are_guest and dest_is_guest:
        raise YoError(
            'sources and destination are both in the guest; use "%s" and a plain cp there'
            % state.yo_cmd("enter")
        )
    if sources_are_guest:
        guest_paths = guest_sources(
            [resolve_guest_operand(source, cwd_mirror) for source in sources]
        )
        return CpPlan(
            direction="from_guest", local_paths=[dest], guest_paths=guest_paths
        )
    guest_paths = [resolve_guest_operand(dest, cwd_mirror)]
    return CpPlan(
        direction="to_guest", local_paths=list(sources), guest_paths=guest_paths
    )


def guest_sources(resolved: List[str]) -> List[str]:
    """The sources of a download, each once, in the order given. Two sources
    with one name would meet in the staging directory and merge silently, and
    APFS folds case, so two names that differ only in case would meet too."""
    sources = list(dict.fromkeys(resolved))
    by_name: Dict[str, str] = {}
    for source in sources:
        name = posixpath.basename(source).casefold()
        if name in by_name:
            raise YoError(
                'sources "%s" and "%s" would land under one name; copy them one at a time'
                % (ui.shown(by_name[name]), ui.shown(source))
            )
        by_name[name] = source
    return sources


def cp_mkdir_target(dest_operand: str, resolved_dest: str, source_count: int) -> str:
    # A spelling whose last component is empty (":", ":x/"), "." or ".." names
    # a directory, as it does for cp; so does any destination of two sources.
    names_a_directory = posixpath.basename(dest_operand[1:]) in ("", ".", "..")
    if names_a_directory or source_count > 1:
        return resolved_dest
    return os.path.dirname(resolved_dest)


def local_cp_operand(operand: str) -> str:
    # Absolute, so a Mac file "a:b.png" is not read as host "a" and a name
    # starting with "-" is not read as an option. Joined, not normalised: a
    # lexical ".." collapse would change which file a symlinked path means.
    return os.path.join(os.getcwd(), operand)


def walk_failure(failure: OSError) -> None:
    raise YoError(
        "cannot read %s: %s" % (ui.shown(failure.filename), ui.os_error_reason(failure))
    )


def lstat_mode(path: str) -> int:
    try:
        return os.lstat(path).st_mode
    except OSError as failure:
        raise YoError(
            "cannot read %s: %s" % (ui.shown(path), ui.os_error_reason(failure))
        )


def refuse_symlinks_in(operand: str) -> None:
    # scp -r follows symlinks and uploads what they point at, so a link the
    # agent pushed into a repo (leak -> ~/.ssh/id_ed25519) would copy a file
    # the user never named into the guest.
    def refuse(link: str) -> None:
        raise YoError(
            'refusing to upload symlink "%s": scp would copy its target "%s", not the link'
            % (ui.shown(link), ui.shown(os.readlink(link)))
        )

    # Each component the user typed is judged on its own, because lstat judges
    # only the last one: "link/", "link/.", "link/file" and "link/../x" all
    # resolve the link. The cwd itself is not judged, only what lies below it.
    path = "/" if os.path.isabs(operand) else os.getcwd()
    for name in operand.split("/"):
        if name in ("", "."):
            continue
        path = os.path.join(path, name)
        if stat.S_ISLNK(lstat_mode(path)):
            refuse(path)
    if not stat.S_ISDIR(lstat_mode(path)):
        return
    walk = os.walk(path, onerror=walk_failure, followlinks=False)
    for directory, subdirs, files in walk:
        for name in subdirs + files:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                refuse(path)


def cmd_cp(args: argparse.Namespace) -> None:
    sshx.require_agent_account()
    cwd_mirror = paths.mirror(paths.logical_cwd())
    plan = resolve_cp_operands(args.paths, cwd_mirror)
    guest_operands = [operand for operand in args.paths if operand_is_guest(operand)]
    for operand, resolved in zip(guest_operands, plan.guest_paths):
        ui.err("yo: guest path %s -> %s" % (operand, resolved))
    local_paths = [local_cp_operand(path) for path in plan.local_paths]
    if plan.direction == "to_guest":
        for operand in plan.local_paths:
            refuse_symlinks_in(operand)
        target = cp_mkdir_target(
            args.paths[-1], plan.guest_paths[0], len(plan.local_paths)
        )
        sshx.ssh_run(AGENT, ["mkdir", "-p", target], check=True)
        scp_transfer(local_paths, "%s:%s" % (state.box.vm_host, plan.guest_paths[0]))
    else:
        download_from_guest(plan.guest_paths, local_paths[0])


def scp_transfer(sources: Sequence[str], dest: str) -> None:
    # -s: sftp, which sshd serves without a login shell. Older scp defaulted to
    # the legacy protocol, which runs `scp` in the agent's shell.
    argv = (
        ["scp", "-r", "-p", "-s"]
        + sshx.ssh_options(AGENT)
        + ["--"]
        + list(sources)
        + [dest]
    )
    proc.run(argv, capture=False, check=True)


def plan_merge(staged: str, target: str, moves: List[Tuple[str, str]]) -> None:
    # Where the staged tree meets what is already on the Mac: directories merge
    # (as scp -r would merge them), files replace files, anything else, and any
    # symlink, is refused. scp itself writes through a symlink it finds at the
    # destination, and the agent controls what a pulled repo's links say.
    try:
        existing = os.lstat(target).st_mode
    except FileNotFoundError:
        moves.append((staged, target))
        return
    except OSError as failure:
        raise YoError(
            "cannot read %s: %s" % (ui.shown(target), ui.os_error_reason(failure))
        )
    if stat.S_ISLNK(existing):
        raise YoError(
            'refusing to copy over symlink "%s": the copy would overwrite its target "%s", not the link'
            % (ui.shown(target), ui.shown(os.readlink(target)))
        )
    staged_is_dir = stat.S_ISDIR(os.lstat(staged).st_mode)
    if staged_is_dir and stat.S_ISDIR(existing):
        for name in sorted(os.listdir(staged)):
            plan_merge(os.path.join(staged, name), os.path.join(target, name), moves)
    elif staged_is_dir or stat.S_ISDIR(existing):
        raise YoError(
            'cannot copy over "%s": it is a directory on one side and not on the other'
            % ui.shown(target)
        )
    else:
        moves.append((staged, target))


def unlock_tree(path: str) -> None:
    # tar keeps a guest directory's mode (less the umask). This adds u+rwx,
    # top down so each directory is readable before it is entered, so the tree
    # can be walked and removed whatever mode it came with (a read-only Go
    # module cache refuses to lose its children). Group and other bits stay as
    # tar made them. Symlinks are never followed: chmod would reach the Mac
    # file they point at. A stack, not recursion: a pulled tree can be deeper
    # than the interpreter's frame limit.
    pending = [path]
    while pending:
        directory = pending.pop()
        mode = os.lstat(directory).st_mode
        os.chmod(directory, stat.S_IMODE(mode) | 0o700)
        with os.scandir(directory) as entries:
            pending += [e.path for e in entries if e.is_dir(follow_symlinks=False)]


def remove_tree(path: str) -> None:
    unlock_tree(path)
    shutil.rmtree(path)


def remove_staging(path: str) -> None:
    # Any failure, not only an OSError: shutil.rmtree recurses, and a tree too
    # deep for it raises RecursionError, which must not replace the copy's own
    # error with a traceback.
    try:
        remove_tree(path)
    except Exception as failure:
        reason = (
            ui.os_error_reason(failure)
            if isinstance(failure, OSError)
            else str(failure)
        )
        raise YoError(
            'cannot remove the staging directory "%s": %s; delete it by hand'
            % (ui.shown(path), reason)
        )


# Names that make git, jj, an editor or direnv execute what a tree says the
# next time the operator touches it. A pulled guest tree never lands these: the
# agent writes them.
# Compared with casefold(), because APFS folds names with Unicode case folding
# (.GIT and .vſcode are .git and .vscode).
LANDING_CONTROL_NAMES = frozenset(
    {".git", ".hg", ".svn", ".jj", ".husky", ".envrc", ".vscode", ".idea"}
)


# Build output and dependency installs a pulled tree never lands: the project's
# own tooling recreates them, and they dwarf the source. The names the guest's
# gc knows. One with a manifest is build output only beside that manifest,
# because the name alone proves nothing: a `target` with no Cargo.toml may
# hold code.
BUILD_OUTPUT = tuple(
    dict.fromkeys(FACTS["gc_build_dirs"] + FACTS["gc_removed_outright"])
)
BUILD_OUTPUT_MANIFESTS: Dict[str, str] = FACTS["build_output_manifests"]
BUILD_OUTPUT_NOTE = (
    "build output is never copied to the host; the project's tooling recreates it"
)

CONTROL_PATH_NOTE = (
    "version-control and editor/direnv control paths are never copied to the "
    "host, because they run code as you"
)


def refuse_unlandable(staging: str, path: str) -> None:
    # Only plain files and directories land: a symlink, FIFO, device or socket,
    # or a setuid/setgid file, is the agent's way to reach past the tree.
    mode = os.lstat(path).st_mode
    plain = stat.S_ISDIR(mode) or (
        stat.S_ISREG(mode) and not mode & (stat.S_ISUID | stat.S_ISGID)
    )
    if not plain:
        raise YoError(
            'refusing to land "%s": the guest sent %s, and only plain files and '
            "directories land on the host"
            % (ui.shown(os.path.relpath(path, staging)), staged_kind(mode))
        )


def staged_kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "a symlink"
    if stat.S_ISREG(mode):
        return "a setuid or setgid file"
    return "a special file (mode %o)" % stat.S_IFMT(mode)


def drop_control_paths(staging: str) -> None:
    # The one place a staged tree is judged, entry by entry: control paths are
    # dropped and anything but a plain file or directory is refused. It prunes
    # the staging tree, not the move list, because a directory with no
    # counterpart on the Mac moves as one rename and never reaches plan_merge's
    # per-entry walk. A dropped directory is never entered, so a control path
    # inside another is not reported on its own. The guest cannot do this
    # judging: APFS case folding is the Mac's.
    skipped: List[str] = []

    unlock_tree(staging)
    for directory, subdirs, files in os.walk(staging, onerror=walk_failure):
        judged = sorted(
            name for name in subdirs + files if name.casefold() in LANDING_CONTROL_NAMES
        )
        for name in subdirs + files:
            if name not in judged:
                refuse_unlandable(staging, os.path.join(directory, name))
        for name in judged:
            path = os.path.join(directory, name)
            skipped.append(os.path.relpath(path, staging))
            try:
                if os.path.islink(path) or not os.path.isdir(path):
                    os.unlink(path)
                else:
                    remove_tree(path)
            except OSError as failure:
                raise YoError(
                    'cannot drop "%s" from the download: %s'
                    % (ui.shown(path), ui.os_error_reason(failure))
                )
        subdirs[:] = [d for d in subdirs if d not in judged]
    for relative in skipped:
        ui.err('yo: skipped "%s": %s' % (ui.shown(relative), CONTROL_PATH_NOTE))


def build_output_find_argv(sources: Sequence[str]) -> List[str]:
    """`find` printing, NUL-terminated, each directory below `sources` that is
    build output (BUILD_OUTPUT, beside its manifest where it has one), and
    never entering one. A source itself is never judged: the operator named it."""
    tests: List[str] = []
    for name in BUILD_OUTPUT:
        test = ["-name", name]
        manifest = BUILD_OUTPUT_MANIFESTS.get(name)
        if manifest is not None:
            test += ["-exec", "test", "-f", "{}/../%s" % manifest, ";"]
        tests += (["-o"] if tests else []) + test
    return [
        "find",
        *sources,
        "-mindepth",
        "1",
        "-type",
        "d",
        "(",
        *tests,
        ")",
        "-prune",
        "-print0",
    ]


def guest_build_output(sources: Sequence[str]) -> List[str]:
    """The tar member names (source name, then the path below it) of the build
    output under `sources`, found on the box before the download, so its bytes
    never cross."""
    listing = sshx.probe(AGENT, build_output_find_argv(sources), binary=True)
    if listing.returncode != 0:
        raise YoError(
            "cannot read %s in the guest (find exited %d)%s"
            % (
                ", ".join(ui.shown(source) for source in sources),
                listing.returncode,
                proc.what_it_said(listing),
            )
        )
    found = [os.fsdecode(raw) for raw in listing.stdout.split(b"\0") if raw]
    members = []
    for source in sources:
        below = source.rstrip("/") + "/"
        members += [
            posixpath.join(posixpath.basename(source), path[len(below) :])
            for path in found
            if path.startswith(below)
        ]
    return members


def guest_tar_argv(sources: Sequence[str], excluded: Sequence[str]) -> List[str]:
    # Each excluded member is matched whole, from the start of its name, never
    # as a pattern. --add-file takes a name that starts with "-"; the guest's
    # tar is GNU tar.
    argv = ["tar", "--anchored", "--no-wildcards"]
    argv += ["--exclude=" + member for member in excluded] + ["-cf", "-"]
    for source in sources:
        parent, name = posixpath.dirname(source), posixpath.basename(source)
        argv += ["-C", parent, "--add-file=" + name]
    return argv


# The agent writes the stream, so the host's tar lands data, names and mode
# bits only: never an owner, ACL, extended attribute or file flag it carries.
# bsdtar (macOS) restores ACLs, xattrs and flags by default; GNU tar spells the
# refusals differently and has no file flags.
HOST_TAR_UNPACK_FLAGS = {
    "bsdtar": [
        "--no-same-owner",
        "--no-same-permissions",
        "--no-acls",
        "--no-xattrs",
        "--no-fflags",
        "--no-mac-metadata",
    ],
    "GNU tar": [
        "--no-same-owner",
        "--no-same-permissions",
        "--no-acls",
        "--no-xattrs",
        "--no-selinux",
    ],
}


def host_tar_unpack_argv(staging: str) -> List[str]:
    said = proc.run(["tar", "--version"], check=True).stdout
    for flavour, flags in HOST_TAR_UNPACK_FLAGS.items():
        if flavour in said:
            return ["tar", "-x"] + flags + ["-C", staging]
    raise YoError(
        "the host's tar is neither bsdtar nor GNU tar, so yo cannot tell it to "
        "drop owners, ACLs and xattrs from a download; it says: %s"
        % ui.shown(said.strip())
    )


def fetch_guest_sources(sources: Sequence[str], staging: str) -> None:
    # The guest tars the sources into ssh's stdout and the host's tar unpacks
    # that stream into staging, which it never leaves: tar refuses a ".."
    # member and does not write through a symlink. Each stderr goes to a file,
    # because a pipe would need a reader for the process not to block on it.
    excluded = guest_build_output(sources)
    with tempfile.TemporaryFile() as guest_said, tempfile.TemporaryFile() as tar_said:
        unpack = host_tar_unpack_argv(staging)
        ssh = proc.spawn(
            sshx.guest_argv(AGENT, guest_tar_argv(sources, excluded), stdin=False),
            stdout=subprocess.PIPE,
            stderr=guest_said,
        )
        try:
            tar = proc.spawn(unpack, stdin=ssh.stdout, stderr=tar_said)
        except BaseException:
            ssh.kill()
            ssh.wait()
            raise
        finally:
            ssh.stdout.close()  # tar holds the reading end now
        tar.wait()
        ssh.wait()
        guest_said.seek(0)
        tar_said.seek(0)
        said = guest_said.read().decode("utf-8", "replace")
        unpack_said = tar_said.read().decode("utf-8", "replace")
    if ssh.returncode != 0:
        raise YoError(
            "cannot read %s in the guest (tar exited %d)%s"
            % (
                ", ".join(ui.shown(source) for source in sources),
                ssh.returncode,
                proc.said_suffix(ui.quoted_block(said)),
            )
        )
    if tar.returncode != 0:
        raise YoError(
            "cannot unpack the download (tar exited %d)%s"
            % (tar.returncode, proc.said_suffix(ui.shown(unpack_said.strip())))
        )
    for member in excluded:
        ui.err('yo: skipped "%s": %s' % (ui.shown(member), BUILD_OUTPUT_NOTE))


def land_staged(moves: Sequence[Tuple[str, str]]) -> None:
    for landed, (staged, target) in enumerate(moves):
        try:
            os.replace(staged, target)
        except OSError as failure:
            raise YoError(
                "cannot place %s -> %s: %s (%d of %d entries already landed)"
                % (
                    ui.shown(staged),
                    ui.shown(target),
                    ui.os_error_reason(failure),
                    landed,
                    len(moves),
                )
            )


def download_from_guest(sources: Sequence[str], dest: str) -> None:
    # The one place an OSError from the staging and landing (a chmod, utime or
    # listdir on the Mac) becomes a message instead of a traceback. The staging
    # directory is already gone by the time it gets here.
    try:
        stage_and_land(sources, dest)
    except OSError as failure:
        reason = ui.os_error_reason(failure)
        if failure.filename is not None:
            reason = "%s: %s" % (ui.shown(str(failure.filename)), reason)
        raise YoError("the copy failed: %s" % reason)


def stage_and_land(sources: Sequence[str], dest: str) -> None:
    # Unpack into a staging directory beside the destination instead of onto
    # it: landing is then judged entry by entry (plan_merge), so a link the
    # agent committed into a repo (notes -> ../../.zshrc) cannot be written
    # through on the next pull. Same filesystem, so landing each entry is a
    # rename.
    dest_is_dir = os.path.isdir(dest)
    if dest.endswith("/") and not dest_is_dir:
        raise YoError('destination "%s" is not a directory' % ui.shown(dest))
    if not dest_is_dir and len(sources) > 1:
        raise YoError(
            '"%s" is not an existing directory, which %d sources need'
            % (ui.shown(dest), len(sources))
        )
    new_name = dest.rstrip("/") or "/"
    staging_parent = dest if dest_is_dir else os.path.dirname(new_name)
    try:
        staging = tempfile.mkdtemp(prefix=".yo-cp-", dir=staging_parent)
    except OSError as failure:
        raise YoError(
            "cannot stage a copy in %s: %s"
            % (ui.shown(staging_parent), ui.os_error_reason(failure))
        )
    try:
        fetch_guest_sources(sources, staging)
        drop_control_paths(staging)
        moves: List[Tuple[str, str]] = []
        for name in sorted(os.listdir(staging)):
            target = os.path.join(dest, name) if dest_is_dir else new_name
            plan_merge(os.path.join(staging, name), target, moves)
        land_staged(moves)
    except BaseException:
        # The copy's own failure is the one to show; a cleanup failure joins it
        # on stderr instead of replacing it.
        try:
            remove_staging(staging)
        except YoError as cleanup:
            ui.err("yo: %s" % cleanup.message)
        raise
    remove_staging(staging)
