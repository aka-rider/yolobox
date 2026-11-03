#!/usr/bin/env bash
# Prints the store path of the egress rule file of a box with
# yolobox.network.tunnelInterfaces = [ "wg0" ], the render behind
# tests/fixtures/egress-tunnel.nft. Run from the repo root with
# YOLOBOX_USERNAME, YOLOBOX_HOST_OS and YOLOBOX_FLAKE_REF set, in CI or in the guest
# (nix/egress.md). Arguments go to nix eval, e.g. --system aarch64-linux.
set -euo pipefail
nix eval --impure --raw "$@" --expr '
  ((builtins.getFlake (toString ./.)).nixosConfigurations.yolobox.extendModules {
    modules = [ { yolobox.network.tunnelInterfaces = [ "wg0" ]; } ];
  }).config.system.build.egressRules'
