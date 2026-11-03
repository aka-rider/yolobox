"""Interactive commands: ssh, enter, code, zed, link, port, aws-check and project picking."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import urllib.parse
from typing import Optional

from . import aws, common, forwards, guest, paths, proc, sshconfig, sshx, state, ui
from .config import AGENT_HOME, AGENT_USER, LOOPBACK
from .sshx import AGENT, OPERATOR
from .ui import YoError


FZF_NO_MATCH = 1
FZF_CANCELLED = 130


def pick_project(query: str) -> str:
    proc.require_tool("fzf", "brew install fzf")
    listing = guest.agent_paths("projects", check=False)
    prefix = AGENT_HOME + "/"
    projects = sorted(
        entry[len(prefix) :] for entry in listing if entry.startswith(prefix)
    )
    if not projects:
        raise YoError("no projects in the VM")
    # fzf draws these names on the terminal, and a repo directory name is
    # guest-controlled; do not depend on the installed fzf stripping controls.
    projects = [common.sanitize(project) for project in projects]
    picker = subprocess.run(
        ["fzf", "--read0", "--print0", "--select-1", "--exit-0", "--query=" + query],
        input="\0".join(projects).encode(),
        stdout=subprocess.PIPE,
    )
    if picker.returncode == FZF_NO_MATCH:
        ui.err("yo: no project matches '%s'" % query)
        raise YoError(None, code=picker.returncode)
    if picker.returncode == FZF_CANCELLED:
        raise YoError(None, code=picker.returncode)
    if picker.returncode != 0:
        raise YoError("fzf failed (exit %d)" % picker.returncode)
    choice = picker.stdout.decode("utf-8", "replace").rstrip("\0")
    return paths.guest_path(AGENT_HOME + "/" + choice)


def target_dir(project: Optional[str]) -> str:
    if project is not None:
        return pick_project(project)
    return paths.dir_for_cwd()


def cmd_ssh(args: argparse.Namespace) -> None:
    # A pty only when both ends are terminals: ssh -t on redirected output
    # turns every newline into CRLF.
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    proc.exec_process(
        sshx.ssh_argv(
            OPERATOR,
            *sshx.forward_agent_opts(),
            *(["-t"] if interactive else []),
            state.box.vm_host,
            *args.ssh_cmd,
        )
    )


def cmd_enter(args: argparse.Namespace) -> None:
    sshx.require_agent_account()
    if os.environ.get("HERDR_ENV") == "1":
        ui.err(
            "yo: an agent started in this shell is invisible to herdr — "
            "run agents in a workspace on the yolobox herdr machine"
        )
    target = target_dir(args.project)

    proc.exec_process(
        sshx.ssh_argv(
            AGENT,
            "-t",
            state.box.vm_host,
            shlex.join(["cd", target]) + ' && exec "$SHELL" -l',
        )
    )


def editor_uri_path(guest_dir: str) -> str:
    # The path goes into a URI: a space, "#" or "?" in a project name would
    # otherwise end or split it.
    return urllib.parse.quote(guest_dir, safe="/")


def cmd_code(args: argparse.Namespace) -> None:
    code_cli = shutil.which("code")
    if code_cli is None:
        code_cli = (
            "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
        )
    if not os.access(code_cli, os.X_OK):
        raise YoError("VS Code CLI not found")
    sshconfig.ensure_ssh_config_include(refuse=True)
    sshx.require_agent_account()
    target = target_dir(args.project)
    proc.exec_process(
        [
            code_cli,
            "--folder-uri",
            "vscode-remote://ssh-remote+%s@%s%s"
            % (AGENT_USER, state.box.vm_host, editor_uri_path(target)),
        ]
    )


def cmd_zed(args: argparse.Namespace) -> None:
    if shutil.which("zed") is None:
        raise YoError("zed CLI not found")
    sshconfig.ensure_ssh_config_include(refuse=True)
    sshx.require_agent_account()
    target = target_dir(args.project)
    proc.exec_process(
        [
            "zed",
            "ssh://%s@%s%s" % (AGENT_USER, state.box.vm_host, editor_uri_path(target)),
        ]
    )


def link_remote_action(existing_url: Optional[str], wanted_url: str) -> str:
    if not existing_url:
        return "add"
    if existing_url == wanted_url:
        return "keep"
    # urlsplit lowercases the host it reports, and -m names keep their case.
    if urllib.parse.urlsplit(existing_url).hostname == state.box.vm_host.lower():
        return "update"
    raise YoError(
        "remote 'yolobox' points at %s, which is not this VM — "
        'run "git remote remove yolobox" or fix it by hand' % existing_url
    )


def current_branch(toplevel: str) -> str:
    branch = proc.run(["git", "-C", toplevel, "symbolic-ref", "--short", "HEAD"])
    if branch.returncode != 0 or not branch.stdout.strip():
        raise YoError(
            "HEAD is detached in %s — check out the branch to push first, so the "
            "VM's copy starts on it" % toplevel
        )
    return branch.stdout.strip()


def cmd_link(args: argparse.Namespace) -> None:
    sshx.require_agent_account()
    physical_top = proc.run(["git", "rev-parse", "--show-toplevel"])
    if physical_top.returncode != 0 or not physical_top.stdout.strip():
        raise YoError("link must run inside a git repo")
    toplevel = paths.logical_toplevel(physical_top.stdout.strip(), paths.logical_cwd())

    vmdir = paths.mirror(toplevel)
    if vmdir is None:
        raise YoError("repo must be under $HOME (it is at %s)" % toplevel)
    if vmdir == AGENT_HOME:
        raise YoError("refusing to link $HOME itself")
    vmdir = paths.guest_path(vmdir)
    branch = current_branch(toplevel)
    url = "ssh://%s@%s%s" % (AGENT_USER, state.box.vm_host, vmdir)

    probe = proc.run(["git", "-C", toplevel, "remote", "get-url", "yolobox"])
    existing = probe.stdout.strip() if probe.returncode == 0 else None
    action = link_remote_action(existing, url)

    guest.guest_helper_check(
        guest.guest_helper(AGENT, "ensure-repo", [vmdir, branch]), "ensure-repo"
    )

    if action == "add":
        proc.run(["git", "-C", toplevel, "remote", "add", "yolobox", url], check=True)
    elif action == "update":
        proc.run(
            ["git", "-C", toplevel, "remote", "set-url", "yolobox", url], check=True
        )
        ui.err("yo: remote 'yolobox' pointed at %s; updated to %s" % (existing, url))
    else:
        ui.err("yo: remote 'yolobox' already points at %s" % url)
    proc.run(
        [
            "git",
            "-C",
            toplevel,
            "config",
            "remote.yolobox.sshCommand",
            shlex.join(sshx.ssh_argv(OPERATOR)),
        ],
        check=True,
    )


def cmd_aws_check(args: argparse.Namespace) -> None:
    if not guest.vm_units_present():
        raise YoError(forwards.units_missing_note())
    if not aws.ensure_aws_broker():
        raise YoError(
            "AWS is disabled on this host (no 'aws' CLI, or no SSO profiles in ~/.aws/config) — nothing to check"
        )
    if not forwards.heal_reverse_forward(forwards.aws_broker_forward()):
        raise YoError(
            "the aws-broker forward into the VM is still dead after re-issuing it — "
            "recreate the master: %s && %s"
            % (state.driver().stop_command(), state.yo_cmd("up"))
        )
    completed = guest.guest_helper(AGENT, "aws-check", stream=True)
    if completed.returncode != 0:
        raise YoError(None, code=completed.returncode)


def cmd_port(args: argparse.Namespace) -> None:
    ui.out("%s:%d" % (LOOPBACK, state.driver().forward_port(args.guest_port)))
