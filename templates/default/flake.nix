{
  description = "yolobox project dev shell (override `inputs.yolobox.url` to pin a different yolobox checkout)";

  inputs = {
    yolobox.url = "git+file:///home/xiii.guest/wrk/yolobox";
  };

  outputs = { self, yolobox }:
    {
      devShells.aarch64-linux.default = yolobox.lib.shell [ "rust" "postgres" ];
    };
}
