"""yo cp: scp in both directions with its refusals, staging and merge."""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

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


SCP_GLOB_RE = re.compile(r"([\[\]*?\\])")


def scp_glob_escape(path: str) -> str:
    # scp expands wildcards in a remote *source* itself, so a guest file named
    # "a[1].txt" would otherwise match nothing, or several other files.
    return SCP_GLOB_RE.sub(r"\\\1", path)


def scp_guest_source(guest_path: str) -> str:
    return "%s:%s" % (state.box.vm_host, scp_glob_escape(guest_path))


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
    # scp -p keeps a guest directory's mode, and sftp-mode scp adds S_IRWXU to
    # it. This adds u+rwx anyway, top down so each directory is readable before
    # it is entered, so the tree can be walked and removed whatever mode scp
    # left (a read-only Go module cache refuses to lose its children). Group
    # and other bits stay as scp made them, so a landed directory keeps the
    # mode scp -p gave it. Symlinks are never followed: chmod would reach the
    # Mac file they point at. A stack, not recursion: a pulled tree can be
    # deeper than the interpreter's frame limit.
    pending = [path]
    while pending:
        directory = pending.pop()
        mode = os.lstat(directory).st_mode
        if stat.S_ISLNK(mode):
            continue
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
            ui.os_error_reason(failure) if isinstance(failure, OSError) else str(failure)
        )
        raise YoError(
            'cannot remove the staging directory "%s": %s; delete it by hand'
            % (ui.shown(path), reason)
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

MANIFEST_NAMES = frozenset(m for m in BUILD_OUTPUT.values() if m)

CONTROL_PATH_NOTE = (
    "version-control and editor/direnv control paths are never copied to the "
    "host, because they run code as you"
)
BUILD_OUTPUT_NOTE = (
    "build output is never copied to the host; the project's tooling recreates it"
)


def drop_control_paths(staging: str) -> None:
    # The one place a staged tree is judged for control paths. It prunes the
    # staging tree, not the move list, because a directory with no counterpart
    # on the Mac moves as one rename and never reaches plan_merge's per-entry
    # walk. Every entry is judged once, and a dropped directory is never
    # entered, so a control path inside another is not reported on its own.
    # The guest cannot do this judging: APFS case folding is the Mac's.
    skipped: List[str] = []

    unlock_tree(staging)
    for directory, subdirs, files in os.walk(staging, onerror=walk_failure):
        judged = sorted(
            name for name in subdirs + files if name.casefold() in LANDING_CONTROL_NAMES
        )
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


@dataclass(frozen=True)
class GuestEntry:
    kind: str  # find's %y: "d" directory, "f" file, "l" link, ...
    mode: int
    mtime: float
    path: str


# One record per entry, NUL-terminated because a path may hold any byte but NUL.
GUEST_RECORD = "%y\t%m\t%T@\t%p\\0"

# What a record may say, as narrowly as find prints it: one type letter, an
# octal mode of at most four digits (%m prints the permission bits, 7777 at
# most), a decimal time, then the path, which may hold tabs and newlines.
# Narrow, because int() and float() also take "0o7", "1_0", "nan" and "inf".
GUEST_RECORD_RE = re.compile(
    r"([a-zA-Z])\t([0-7]{1,4})\t([0-9]{1,10}(?:\.[0-9]+)?)\t(.+)", re.DOTALL
)

# The largest mtime a filesystem keeps: APFS and ext4 store signed 64-bit
# nanoseconds, and os.utime past that is an OverflowError, not an OSError.
MAX_MTIME = 2**63 / 10**9


def parse_guest_record(
    record: bytes, starts: Sequence[str], lists_starts: bool
) -> GuestEntry:
    # The listing came out of the agent's login shell, which the agent owns, so
    # it is untrusted input (yo.md, "A guest listing is untrusted"). A path
    # that is not clean (a "..", a "." or an empty component, a trailing
    # slash) or not under a start would steer scp out of the staging
    # directory, past the control-path and merge checks.
    text = os.fsdecode(record)
    fields = GUEST_RECORD_RE.fullmatch(text)
    if fields is None or float(fields.group(3)) >= MAX_MTIME:
        raise YoError("the guest's listing has a malformed record: %r" % text)
    kind, mode, mtime, path = fields.groups()
    clean = posixpath.normpath(path) == path
    under = any(path.startswith(start + "/") for start in starts) or (
        lists_starts and path in starts
    )
    if not (clean and under):
        raise YoError(
            "the guest's listing has a path that is not a clean path under %s: %r"
            % (", ".join(starts), text)
        )
    return GuestEntry(kind, int(mode, 8), float(mtime), path)


def list_guest(
    starts: Sequence[str], tests: Sequence[str], lists_starts: bool
) -> Dict[str, GuestEntry]:
    """Every record find prints for `starts` and `tests`, checked. `lists_starts`
    says whether find also prints the starts themselves."""
    # find -H follows a symlink named on the command line, as scp does for a
    # source. The starts are absolute, so none can be read as an option, and
    # ssh_run quotes them, so no wildcard in them is expanded.
    done = sshx.ssh_run(AGENT, ["find", "-H"] + list(starts) + list(tests), binary=True)
    if done.returncode != 0:
        # find's stderr names what it could not read (an unreadable directory
        # under /tmp); it is guest text, so it is shown as one escaped line.
        # Its stdout is the listing, never the reason.
        raise YoError(
            "cannot list %s in the guest (find exited %d): %s"
            % (
                ", ".join(starts),
                done.returncode,
                ui.shown(done.stderr.strip()) or "it said nothing",
            )
        )
    entries: Dict[str, GuestEntry] = {}
    for record in done.stdout.split(b"\0"):
        if not record:
            continue
        entry = parse_guest_record(record, starts, lists_starts)
        # Overlapping sources list a path twice, alike. Two accounts of one path
        # cannot both be true.
        if entries.setdefault(entry.path, entry) != entry:
            raise YoError(
                "the guest's listing describes %r twice, differently" % entry.path
            )
    return entries


def any_name(names: Sequence[str]) -> List[str]:
    tests = ["("]
    for name in names:
        tests += ["-name", name, "-o"]
    return tests[:-1] + [")"]


def find_build_output(sources: Sequence[str]) -> List[Tuple[str, str]]:
    """The (source, path) of each outermost build-output directory inside a
    guest source. A source itself is never judged: the operator named it."""
    # A name that needs no manifest is pruned, so find never walks a
    # node_modules. The others are walked, because a target that proves
    # nothing may hold a node_modules of its own. The manifests are listed
    # beside them, and the verdict is made here.
    plain = [name for name, manifest in BUILD_OUTPUT.items() if manifest is None]
    needy = [name for name, manifest in BUILD_OUTPUT.items() if manifest]
    tests = ["-mindepth", "1"]
    for kind, names, action in (
        ("d", plain, ["-prune", "-printf", GUEST_RECORD]),
        ("d", needy, ["-printf", GUEST_RECORD]),
        ("f", sorted(MANIFEST_NAMES), ["-printf", GUEST_RECORD]),
    ):
        tests += ["(", "-type", kind] + any_name(names) + action + [")", "-o"]
    found = list_guest(sources, tests[:-1], lists_starts=False)
    asked = {"d": frozenset(BUILD_OUTPUT), "f": MANIFEST_NAMES}
    for entry in found.values():
        # find was asked for these names and types only.
        if posixpath.basename(entry.path) not in asked.get(entry.kind, ()):
            raise YoError(
                "the guest's listing has a path find was not asked for: %r" % entry.path
            )
    manifests_present = {path for path, entry in found.items() if entry.kind == "f"}
    confirmed = sorted(
        path
        for path, entry in found.items()
        if entry.kind == "d" and has_manifest(path, manifests_present)
    )
    outermost: List[str] = []
    for path in confirmed:
        if not any(path.startswith(kept + "/") for kept in outermost):
            outermost.append(path)
    # A path under two sources (":~/proj/sub :~/proj") is reached from both,
    # so both are walked; attributed to one, the other would be fetched whole.
    return [
        (source, path)
        for path in outermost
        for source in sources
        if path.startswith(source + "/")
    ]


def has_manifest(directory: str, manifests_present: Set[str]) -> bool:
    manifest = BUILD_OUTPUT[posixpath.basename(directory)]
    return manifest is None or (
        posixpath.join(posixpath.dirname(directory), manifest) in manifests_present
    )


def directories_down_to(source: str, path: str) -> Iterator[str]:
    """The directories from `source` to the parent of `path`, which lies under it."""
    directory = posixpath.dirname(path)
    while directory != source:
        yield directory
        directory = posixpath.dirname(directory)
    yield source


# The bytes of source operands one scp call is given. A directory with thousands
# of entries would overflow the argv limit (macOS: ARG_MAX of 1 MiB, shared with
# the environment and scp's own options); a sixteenth of it leaves that room and
# still moves a whole directory in a few calls.
SCP_ARGV_BUDGET = 64 * 1024


def in_batches(operands: Sequence[str]) -> Iterator[List[str]]:
    """`operands` (as scp receives them: host, escapes and all) in runs that fit
    SCP_ARGV_BUDGET. An operand over the budget by itself gets a run of its own."""
    batch: List[str] = []
    size = 0
    for operand in operands:
        cost = len(os.fsencode(operand)) + 1  # the NUL that ends it in argv
        if batch and size + cost > SCP_ARGV_BUDGET:
            yield batch
            batch, size = [], 0
        batch.append(operand)
        size += cost
    if batch:
        yield batch


# find's %y letters scp -r copies: directory, regular file, symlink.
FETCHABLE_KINDS = frozenset({"d", "f", "l"})


@dataclass(frozen=True)
class GuestWalk:
    # What is left of the directories that held build output: every entry of
    # `walked`, and for each its children, found on the guest. A directory that
    # `skipped` names is left out whole.
    entries: Dict[str, GuestEntry]
    children: Dict[str, List[str]]
    walked: Set[str]
    skipped: Set[str]


def as_pulled(source: str, path: str) -> str:
    """`path`, inside the guest `source`, spelled as it lands: from the source's
    own name down, which is how every skip is reported. Only for messages, so it
    is already safe to print."""
    return ui.shown(posixpath.relpath(path, posixpath.dirname(source)))


def fetch_walked(walk: GuestWalk, source: str, directory: str, local: str) -> None:
    # scp -r takes a whole directory or none of it. So a directory that holds
    # build output is made here with the mode and time scp -p would give it
    # (sftp-mode scp makes a directory with its mode plus S_IRWXU), and its
    # children are fetched one by one, each a whole tree that holds none.
    entry = walk.entries.get(directory)
    children = walk.children.get(directory)
    # The first listing found build output inside every walked directory, so a
    # second that shows none, or no directory, disagrees with it: the tree
    # changed between the two (npm removing node_modules) or a listing lies.
    if entry is None or entry.kind != "d" or children is None:
        raise YoError(
            '"%s" changed in the guest while it was being listed; copy it again'
            % as_pulled(source, directory)
        )
    os.makedirs(local, mode=(entry.mode & 0o1777) | stat.S_IRWXU, exist_ok=True)
    plain: List[str] = []
    for child in children:
        if child in walk.walked:
            fetch_walked(
                walk, source, child, os.path.join(local, posixpath.basename(child))
            )
        elif child in walk.skipped:
            continue
        elif walk.entries[child].kind in FETCHABLE_KINDS:
            plain.append(child)
        else:
            # scp -r passes over a fifo or a socket inside a directory with a
            # warning, but fails when one is named on its own.
            ui.err('yo: skipped "%s": not a regular file' % as_pulled(source, child))
    for batch in in_batches([scp_guest_source(path) for path in plain]):
        scp_transfer(batch, local)
    os.utime(local, (entry.mtime, entry.mtime))


def fetch_guest_sources(sources: Sequence[str], staging: str) -> None:
    # scp has no exclude option and a directory it fetches is fetched whole, so
    # build output is found on the guest first (the bytes of a node_modules
    # never cross) and scp is asked for only what is left.
    build_output = find_build_output(sources)
    walked = {
        directory
        for source, path in build_output
        for directory in directories_down_to(source, path)
    }
    whole = [source for source in sources if source not in walked]
    if whole:
        scp_transfer([scp_guest_source(source) for source in whole], staging)
    if walked:
        entries = list_guest(
            sorted(walked),
            ["-maxdepth", "1", "-printf", GUEST_RECORD],
            lists_starts=True,
        )
        children: Dict[str, List[str]] = {}
        for path in sorted(entries):
            children.setdefault(posixpath.dirname(path), []).append(path)
        walk = GuestWalk(entries, children, walked, {path for _, path in build_output})
        for source in sources:
            if source in walked:
                fetch_walked(
                    walk,
                    source,
                    source,
                    os.path.join(staging, posixpath.basename(source)),
                )
    for source, path in build_output:
        ui.err('yo: skipped "%s": %s' % (as_pulled(source, path), BUILD_OUTPUT_NOTE))


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
    # sources are guest paths, not yet escaped for scp.
    # scp into a staging directory beside the destination instead of onto it:
    # scp writes through any symlink it meets there, so a link the agent
    # committed into a repo (notes -> ../../.zshrc) would overwrite a Mac file
    # on the next pull. Same filesystem, so landing each entry is a rename.
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
