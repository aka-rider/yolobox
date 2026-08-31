{ pkgs, lib, ... }:
let
  mkPlaywrightMcp = engine: pkgs.writeShellApplication {
    name = "playwright-mcp-${engine}";
    runtimeInputs = [ pkgs.coreutils pkgs.git ];
    text = ''
      [ -S /tmp/.X11-unix/X0 ] || {
        echo "yolobox: display :0 is not up (xvfb.service) — refusing to start silently headless" >&2
        exit 1
      }
      top="$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")"
      project="''${top#"$HOME/"}"
      project="''${project//\//--}"
      export PLAYWRIGHT_MCP_USER_DATA_DIR="$HOME/.local/state/yolobox/browser-profiles/${engine}/$project"
      export PLAYWRIGHT_MCP_OUTPUT_DIR="$HOME/artifacts/$project"
      mkdir -p "$PLAYWRIGHT_MCP_USER_DATA_DIR" "$PLAYWRIGHT_MCP_OUTPUT_DIR"
      exec ${lib.getExe pkgs.playwright-mcp} --browser ${engine} "$@"
    '';
  };

  mkPlaywrightServer = engine: {
    command = lib.getExe (mkPlaywrightMcp engine);
    args = [ "--caps" "vision,pdf,devtools" ];
    env = {
      DISPLAY = ":0";
      PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
    };
  };
in
{
  imports = [
    ./harnesses.nix
    ./mcp.nix
    ./herd-report.nix
    ./display.nix
    ./t3.nix
  ];

  yolobox.mcp.servers = {
    context7 = {
      command = lib.getExe pkgs.context7-mcp;
    };
    playwright-chromium = mkPlaywrightServer "chromium";
    playwright-firefox = mkPlaywrightServer "firefox";
  };
}
