{ pkgs, lib, ... }:
{
  imports = [
    ./harnesses.nix
    ./mcp.nix
    ./herd-report.nix
  ];

  yolobox.mcp.servers = {
    context7 = {
      command = lib.getExe pkgs.context7-mcp;
    };
    playwright = {
      command = lib.getExe pkgs.playwright-mcp;
      args = [ "--headless" "--isolated" "--no-sandbox" "--browser" "chromium" ];
      env = {
        # Same nixpkgs revision as playwright-mcp itself (Gotcha 9) — a
        # mismatched browser bundle is the classic playwright-mcp trap.
        PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers-chromium}";
        PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
      };
    };
  };
}
