class Yolobox < Formula
  desc "NixOS VM devbox for AI agents, run by Lima on a Mac"
  homepage "https://github.com/aka-rider/yolobox"
  url "@URL@"
  sha256 "@SHA256@"
  license "MIT"

  depends_on :macos
  depends_on "fzf"
  depends_on "lima"

  def install
    libexec.install "yo", "aws-broker", "facts.json"
    # macOS runs the Lima driver; the libvirt driver's modules and the Linux host
    # files stay out.
    (libexec/"yolobox_cli").install Dir["yolobox_cli/*.py"] - ["yolobox_cli/libvirt.py", "yolobox_cli/libvirt_host.py"]
    (libexec/"lima").install "lima/yolobox.yaml"
    inreplace libexec/"yolobox_cli/config.py", 'YO_VERSION = "dev"', "YO_VERSION = \"#{version}\""
    bin.install_symlink libexec/"yo"
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/yo --version")
  end
end
