"""yo cp: scp in both directions with its refusals, staging and merge."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import paths, proc, sshx, state, ui
from .config import AGENT_HOME
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
        guest_paths = [resolve_guest_operand(source, cwd_mirror) for source in sources]
        return CpPlan(
            direction="from_guest", local_paths=[dest], guest_paths=guest_paths
        )
    guest_paths = [resolve_guest_operand(dest, cwd_mirror)]
    return CpPlan(
        direction="to_guest", local_paths=list(sources), guest_paths=guest_paths
    )


def cp_mkdir_target(dest_operand: str, resolved_dest: str, source_count: int) -> str:
    if dest_operand == ":" or dest_operand.endswith("/") or source_count > 1:
        return resolved_dest
    return os.path.dirname(resolved_dest)


SCP_GLOB_RE = re.compile(r"([\[\]*?\\])")


def scp_glob_escape(path: str) -> str:
    # scp expands wildcards in a remote *source* itself, so a guest file named
    # "a[1].txt" would otherwise match nothing, or several other files.
    return SCP_GLOB_RE.sub(r"\\\1", path)


def local_cp_operand(operand: str) -> str:
    # Absolute, so a Mac file "a:b.png" is not read as host "a" and a name
    # starting with "-" is not read as an option. Joined, not normalised: a
    # lexical ".." collapse would change which file a symlinked path means.
    return os.path.join(os.getcwd(), operand)


def walk_failure(failure: OSError) -> None:
    raise YoError("cannot read %s: %s" % (failure.filename, failure.strerror))


def refuse_symlinks_in(source: str) -> None:
    # scp -r follows symlinks and uploads what they point at, so a link the
    # agent pushed into a repo (leak -> ~/.ssh/id_ed25519) would copy a file
    # the user never named into the guest.
    def refuse(link: str) -> None:
        raise YoError(
            'refusing to upload symlink "%s": scp would copy its target "%s", not the link'
            % (link, os.readlink(link))
        )

    # lstat of "link/" follows the link, so a trailing slash would hide it.
    source = source.rstrip("/") or "/"
    try:
        mode = os.lstat(source).st_mode
    except OSError as failure:
        raise YoError("cannot read %s: %s" % (source, failure.strerror))
    if stat.S_ISLNK(mode):
        refuse(source)
    if not stat.S_ISDIR(mode):
        return
    walk = os.walk(source, onerror=walk_failure, followlinks=False)
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
        for source in local_paths:
            refuse_symlinks_in(source)
        target = cp_mkdir_target(
            args.paths[-1], plan.guest_paths[0], len(plan.local_paths)
        )
        sshx.ssh_run(AGENT, ["mkdir", "-p", target], check=True)
        scp_transfer(local_paths, "%s:%s" % (state.box.vm_host, plan.guest_paths[0]))
    else:
        sources = [
            "%s:%s" % (state.box.vm_host, scp_glob_escape(guest))
            for guest in plan.guest_paths
        ]
        download_from_guest(sources, local_paths[0])


def scp_transfer(sources: Sequence[str], dest: str) -> None:
    # -s: sftp, which sshd serves without a login shell (CONSTITUTION: never
    # disable allowSFTP). Older scp defaulted to the legacy protocol, which
    # runs `scp` in the agent's shell.
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
        raise YoError("cannot read %s: %s" % (target, failure.strerror))
    if stat.S_ISLNK(existing):
        raise YoError(
            'refusing to copy over symlink "%s": the copy would overwrite its target "%s", not the link'
            % (target, os.readlink(target))
        )
    staged_is_dir = stat.S_ISDIR(os.lstat(staged).st_mode)
    if staged_is_dir and stat.S_ISDIR(existing):
        for name in sorted(os.listdir(staged)):
            plan_merge(os.path.join(staged, name), os.path.join(target, name), moves)
    elif staged_is_dir or stat.S_ISDIR(existing):
        raise YoError(
            'cannot copy over "%s": it is a directory on one side and not on the other'
            % target
        )
    else:
        moves.append((staged, target))


def unlock_tree(path: str) -> None:
    # scp -p keeps a guest directory's mode, and sftp-mode scp adds only u+wx
    # to it: a read-only directory (a Go module cache) refuses to lose its
    # children, and an agent-side chmod 300 leaves one nobody can list. Top
    # down, so each directory is readable before it is entered. It adds u+rwx
    # so the tree can be walked and removed; group and other bits stay as scp
    # made them, so a landed directory keeps the mode scp -p gave it. Symlinks
    # are never followed: chmod would reach the Mac file they point at.
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        return
    os.chmod(path, stat.S_IMODE(mode) | 0o700)
    with os.scandir(path) as entries:
        subdirs = [e.path for e in entries if e.is_dir(follow_symlinks=False)]
    for subdir in subdirs:
        unlock_tree(subdir)


def remove_tree(path: str) -> None:
    unlock_tree(path)
    shutil.rmtree(path)


def remove_staging(path: str) -> None:
    try:
        remove_tree(path)
    except OSError as failure:
        raise YoError(
            'cannot remove the staging directory "%s": %s; delete it by hand'
            % (path, failure.strerror or failure)
        )


# Names that make git, an editor or direnv execute what a tree says the next
# time the operator touches it. A pulled guest tree never lands these: the
# agent writes them (yo.md, "Downloads never land control paths").
# Compared with casefold(), because APFS folds names with Unicode case folding
# (.GIT and .vſcode are .git and .vscode).
LANDING_CONTROL_NAMES = frozenset({".git", ".hg", ".svn", ".envrc", ".vscode", ".idea"})


# Build output and dependency installs a pulled tree never lands: the project's
# own tooling recreates them, and they dwarf the source. The same names the
# guest's gc knows (BUILD_DIRS, REMOVED_OUTRIGHT and NEEDS_MANIFEST in
# nix/guest/yolobox-guest.sh); tests/test_consistency.py keeps them equal. A
# value names the manifest that must sit beside the directory, because the
# name alone proves nothing: a `target` with no Cargo.toml may hold data.
BUILD_OUTPUT: Dict[str, Optional[str]] = {
    "node_modules": None,
    ".venv": None,
    "target": "Cargo.toml",
    ".next": "package.json",
}

CONTROL_PATH_NOTE = (
    "version-control and editor/direnv control paths are never copied to the "
    "host, because they run code as you"
)
BUILD_OUTPUT_NOTE = (
    "build output is never copied to the host; the project's tooling recreates it"
)


def is_build_output(directory: str, name: str) -> bool:
    # Only a real directory counts: a file or a link called node_modules is not
    # build output. The manifest must be a regular file beside it, so a
    # manifest that is itself a directory proves nothing either.
    path = os.path.join(directory, name)
    if name not in BUILD_OUTPUT or os.path.islink(path) or not os.path.isdir(path):
        return False
    manifest = BUILD_OUTPUT[name]
    if manifest is None:
        return True
    try:
        mode = os.lstat(os.path.join(directory, manifest)).st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode)


def drop_from_staging(staging: str) -> None:
    # The one place a staged tree is judged. It prunes the staging tree, not
    # the move list, because a directory with no counterpart on the Mac moves
    # as one rename and never reaches plan_merge's per-entry walk.
    # Every entry is judged once, and a dropped directory is never entered, so
    # a control path inside build output is not reported on its own.
    # The entries of the staging directory itself are the sources the operator
    # named, so they are never judged as build output; control names are.
    skipped: List[Tuple[str, str]] = []

    unlock_tree(staging)
    for directory, subdirs, files in os.walk(staging, onerror=walk_failure):
        at_top = directory == staging
        judged = {
            name: CONTROL_PATH_NOTE
            for name in subdirs + files
            if name.casefold() in LANDING_CONTROL_NAMES
        }
        if not at_top:
            judged.update(
                {
                    name: BUILD_OUTPUT_NOTE
                    for name in subdirs
                    if is_build_output(directory, name)
                }
            )
        for name in sorted(judged):
            path = os.path.join(directory, name)
            skipped.append((os.path.relpath(path, staging), judged[name]))
            try:
                if os.path.islink(path) or not os.path.isdir(path):
                    os.unlink(path)
                else:
                    remove_tree(path)
            except OSError as failure:
                raise YoError(
                    'cannot drop "%s" from the download: %s'
                    % (path, failure.strerror or failure)
                )
        subdirs[:] = [d for d in subdirs if d not in judged]
    for relative, note in skipped:
        ui.err('yo: skipped "%s": %s' % (relative, note))


def land_staged(moves: Sequence[Tuple[str, str]]) -> None:
    for landed, (staged, target) in enumerate(moves):
        try:
            os.replace(staged, target)
        except OSError as failure:
            raise YoError(
                "cannot place %s -> %s: %s (%d of %d entries already landed)"
                % (staged, target, failure.strerror or failure, landed, len(moves))
            )


def download_from_guest(sources: Sequence[str], dest: str) -> None:
    # scp into a staging directory beside the destination instead of onto it:
    # scp writes through any symlink it meets there, so a link the agent
    # committed into a repo (notes -> ../../.zshrc) would overwrite a Mac file
    # on the next pull. Same filesystem, so landing each entry is a rename.
    dest_is_dir = os.path.isdir(dest)
    if dest.endswith("/") and not dest_is_dir:
        raise YoError('destination "%s" is not a directory' % dest)
    if not dest_is_dir and len(sources) > 1:
        raise YoError(
            '"%s" is not an existing directory, which %d sources need'
            % (dest, len(sources))
        )
    new_name = dest.rstrip("/") or "/"
    staging_parent = dest if dest_is_dir else os.path.dirname(new_name)
    try:
        staging = tempfile.mkdtemp(prefix=".yo-cp-", dir=staging_parent)
    except OSError as failure:
        raise YoError(
            "cannot stage a copy in %s: %s" % (staging_parent, failure.strerror)
        )
    try:
        scp_transfer(sources, staging)
        drop_from_staging(staging)
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
