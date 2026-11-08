"""Interactive commands: ssh, enter, code, zed, port, aws-check and project picking."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import urllib.parse
from typing import Optional

from . import common, forwards, guest, paths, proc, sshconfig, sshx, state, ui
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


def on_terminal() -> bool:
    """Whether both ends are terminals: ssh -t on redirected output turns every newline into CRLF."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def cmd_ssh(args: argparse.Namespace) -> None:
    proc.exec_process(
        sshx.ssh_argv(
            OPERATOR,
            *sshx.forward_agent_opts(),
            *(["-t"] if on_terminal() else []),
            state.box.vm_host,
            *args.ssh_cmd,
        )
    )


def cmd_enter(args: argparse.Namespace) -> None:
    # enter exists to give an interactive shell; without a terminal ssh would
    # print "Pseudo-terminal will not be allocated" and run it without a pty.
    if not on_terminal():
        raise YoError(
            "%s opens an interactive shell, and stdin or stdout is not a terminal"
            % state.yo_cmd("enter")
        )
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


def cmd_aws_check(args: argparse.Namespace) -> None:
    sshx.require_agent_account()
    if not forwards.vm_services():
        raise YoError(
            "AWS is disabled on this host (no 'aws' CLI, or no SSO profiles in ~/.aws/config) — nothing to check"
        )
    completed = guest.guest_helper(AGENT, "aws-check", stream=True)
    if completed.returncode != 0:
        raise YoError(None, code=completed.returncode)


def cmd_port(args: argparse.Namespace) -> None:
    ui.out("%s:%d" % (LOOPBACK, state.driver().forward_port(args.guest_port)))
