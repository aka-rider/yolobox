# nix/lsp.nix: the stories behind its settings

`nix/lsp.nix` carries the short reason beside each setting. This file keeps the incident and the ways to recognise a failure. It covers why the box has no direnv, what `lspWrapper` does, and why claude-code and pi are outside it.

## No direnv

An `.envrc` is code, and the agent writes it. The operator pulls the repo to the host, and the first `cd` into it would run that file with the operator's `sudo` available.

A direnv whitelist for one account does not help when the file travels by git to another account's machine, so the rule is to have no loader, not to fence it. There is no direnv anywhere: not in the box, not in `templates/`, not in the agent's environment.

A project's environment is entered explicitly, with `devbox shell` or `devbox run`. `yo cp` also refuses to land an `.envrc` on the host (see [yo.md](../yo.md#downloads-never-land-control-paths)), so a download cannot plant one either.

Zed has its own direnv loader. Its `load_direnv` setting defaults to `"direct"`, so deleting the key would not turn it off. It would only be off while no `direnv` binary is on PATH. `zed-settings.json` sets `load_direnv = "disabled"`, which makes the rule true whatever is on PATH.

## lspWrapper

Each project's devbox provides its language servers. They are nixpkgs builds, because the npm download that Zed does by itself is glibc-linked and does not run on NixOS. With no direnv, nothing puts the devbox on PATH by itself, so every consumer launches its server through `lspWrapper <server> <args...>`.

The wrapper walks up from `$PWD` to `/` looking for a `devbox.json`. Looking only in `$PWD` missed a server started from a subdirectory of a project. That server took the bare-binary branch and failed, because basedpyright is not on the box-wide PATH. If a `devbox.json` is found, the wrapper runs `devbox install -c <dir>` and then `exec devbox run -c <dir> -q -- <server>`. The `-c` flag points devbox at the project while the server keeps the cwd it was started in, which matters because the editor expects paths relative to it. If none is found, the wrapper execs the server by name.

stdout is the server's protocol stream. A cold devbox prints installation progress, so `devbox install` sends its output to stderr. If the install fails, the error shows there and `devbox run` fails visibly right after it.

Zed is the only consumer the box wires. It reads `/etc/yolobox/lsp/zed-settings.json`, linked to `~/.config/zed/settings.json`, and that file names the wrapper as the `basedpyright` binary. Zed on the host is never consulted for remote projects, so this is a server-side setting.

claude-code's and pi's language servers are the user's dotfiles concern, not the box's.

## pi

pi is not wired through the wrapper. Its language servers are the user's dotfiles concern, like claude's. Whatever extension the user installs finds basedpyright on PATH only, so a pi started outside `devbox shell` has no Python diagnostics; `devbox run pi` or a launch command in the extension's own config fixes that on the user's side. The flake ships no pi LSP extension of its own, because a second one would register another `lsp_diagnostics` tool, and pi refuses to start on a tool-name conflict.

## How to recognise the failure

- A language server fails to start in a subdirectory of a devbox project, with "command not found" for the server name. The walk-up is broken, or the project has no `devbox.json` above that directory.
- A `devbox install` error in the stderr of the editor's language-server log. The server never starts and `devbox run` fails right after it. Run `devbox install` in the project to see the same message.
- Zed running `direnv export json` in its log means `load_direnv` is not `"disabled"`.
