"""The harness the bash-with-fakes test modules share.

A script under test runs the way nix's writeShellApplication runs it, with
`set -o errexit -o nounset -o pipefail`, and finds a scratch `bin/` of fakes
first on PATH, ahead of the real tools it also needs. A fake is a small shell
script a test writes with `fake()`; it answers from FAKE_* variables in the
environment and logs what it was asked, so a test reads what the script did,
not how.

`facts_sh()` renders facts.json the way nix renders it into the box's
/etc/yolobox/facts.sh, for the scripts that source that file.
"""

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FACTS = json.loads((REPO / "facts.json").read_text())
BASH = ["bash", "-o", "errexit", "-o", "nounset", "-o", "pipefail"]


def render_fact(key, value):
    """One fact as the words of its line, as nix/lib/args.nix renders it: a
    string or an integer as itself, a list of strings as its words, a map of
    strings as key:value words in key order. Any other shape is refused."""
    if isinstance(value, bool):
        pass
    elif isinstance(value, (str, int)):
        return str(value)
    elif isinstance(value, list) and all(isinstance(w, str) for w in value):
        return " ".join(value)
    elif isinstance(value, dict) and all(isinstance(v, str) for v in value.values()):
        return " ".join("%s:%s" % (k, value[k]) for k in sorted(value))
    raise TypeError(
        "facts.json: %s is %s, which has no YOLOBOX_%s rendering"
        % (key, type(value).__name__, key.upper())
    )


def facts_sh(**overrides):
    """/etc/yolobox/facts.sh: YOLOBOX_<KEY>=value per fact, rendered by
    render_fact and shell-quoted. `overrides` replace or add facts."""
    facts = dict(FACTS, **overrides)
    return "".join(
        "YOLOBOX_%s=%s\n" % (key.upper(), shlex.quote(render_fact(key, value)))
        for key, value in facts.items()
    )


def is_gnu(tool: str) -> bool:
    """Whether `tool` on PATH is GNU's: `tool --version` says GNU. A missing
    tool, or one that rejects --version (BSD), is not."""
    try:
        probe = subprocess.run(
            [tool, "--version"], capture_output=True, text=True, timeout=20
        )
    except OSError:
        return False
    return probe.returncode == 0 and "GNU" in probe.stdout


class ShellCase(unittest.TestCase):
    """A scratch directory `tmp` with `bin_dir` of fakes first on `env["PATH"]`."""

    timeout = 20

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.env = {
            # The ambient PATH stays after the fakes: the scripts need mktemp,
            # find and the rest, and on NixOS they are not in /usr/bin.
            "PATH": os.pathsep.join(
                [str(self.bin_dir), os.environ.get("PATH", "/usr/bin:/bin")]
            )
        }

    def fake(self, name, text):
        """An executable `name` in bin_dir with `text`, which starts with its
        own shebang line."""
        path = self.bin_dir / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def run_bash(self, script, *args, env=None, **kwargs):
        """Run `script` under bash with the three options, `env` over self.env."""
        return subprocess.run(
            [*BASH, str(script), *args],
            capture_output=True,
            text=True,
            env=dict(self.env, **(env or {})),
            timeout=self.timeout,
            **kwargs,
        )
