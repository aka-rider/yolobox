{ lib, buildNpmPackage, fetchurl, python3, jq }:

buildNpmPackage rec {
  pname = "t3";
  version = "0.0.33";

  src = fetchurl {
    url = "https://registry.npmjs.org/t3/-/t3-${version}.tgz";
    hash = "sha256-pgvwhuSi3oG32WrSXW0R/zcBxVXOR9AkbQ7271eMg2c=";
  };

  # node-pty 1.1.0 publishes prebuilds for darwin and win32 only, so its
  # install script falls through to node-gyp on aarch64-linux and needs a
  # python3 here — which is exactly why t3 is built by nix: the box's runtime
  # stays free of a C and Python toolchain.
  nativeBuildInputs = [ python3 ];

  # The published package.json keeps the t3code monorepo's pnpm-style
  # "overrides", whose keys use the "parent>child" form ("@clerk/clerk-js>@base-org/account").
  # npm's arborist validates those keys as package names once this package is
  # the install root and dies with EINVALIDPACKAGENAME, so the field must go
  # before any npm command runs. The vendored lockfile is generated from the
  # same stripped package.json; regenerate both together on a version bump.
  # jq by store path, not via nativeBuildInputs: this postPatch also runs in
  # the npm-deps fixed-output derivation, which does not inherit them.
  #
  # t3's Claude capability probe hardcodes strictMcpConfig: true
  # (pingdotgg/t3code#5392), which only makes Claude Code refuse the probe
  # when an enterprise MCP config exists. Nothing in this box renders one, so
  # the probe is left unpatched.
  postPatch = ''
    ${lib.getExe jq} 'del(.overrides)' package.json > package.json.stripped
    mv package.json.stripped package.json
    cp ${./t3-package-lock.json} package-lock.json
  '';

  npmDepsFetcherVersion = 2;
  npmDepsHash = "sha256-7AlDllcn0LQs9+FaJWQ3PTlfLTlVuAYPqExCyyi9l3U=";

  # No "scripts" field at all — the tarball ships a prebuilt dist/.
  dontNpmBuild = true;

  meta = {
    description = "t3code — a coding-agent server and web UI driving claude and opencode";
    homepage = "https://github.com/pingdotgg/t3code";
    license = lib.licenses.mit;
    mainProgram = "t3";
  };
}
