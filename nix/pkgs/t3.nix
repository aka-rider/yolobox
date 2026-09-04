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
  # sed rather than substituteInPlace for the dist edit: bin.mjs carries four
  # NUL bytes, and substituteInPlace's replacement helper rejects those with
  # "consumeEntire(): ERROR: Input null bytes, won't process". grep gates the
  # sed so that under stdenv's set -e a vanished pattern fails the build.
  #
  # The second edit is unrelated to npm. t3's Claude capability probe used to
  # hardcode strictMcpConfig, and Claude Code refuses --strict-mcp-config
  # whenever an enterprise MCP config is present — a deleted nix/mcp.nix used
  # to render one at /etc/claude-code/managed-mcp.json. The probe then exited
  # 1, t3 swallowed the error, and its Claude provider silently reported no
  # auth and no slash commands. Nothing renders an enterprise config any more,
  # so the guard this sed works around no longer fires either way
  # — but the flip is left in place rather than reverted, because reverting it
  # buys nothing functionally and would rebuild t3 for no gain. The grep still
  # earns its keep as a canary: it turns a t3 bump that renames this field
  # into a build error instead of a silent return of the defect.
  postPatch = ''
    ${lib.getExe jq} 'del(.overrides)' package.json > package.json.stripped
    mv package.json.stripped package.json
    cp ${./t3-package-lock.json} package-lock.json
    grep -q 'strictMcpConfig: true,' dist/bin.mjs
    sed -i 's/strictMcpConfig: true,/strictMcpConfig: false,/' dist/bin.mjs
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
