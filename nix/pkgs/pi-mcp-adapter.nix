{ lib, buildNpmPackage, fetchurl }:

buildNpmPackage rec {
  pname = "pi-mcp-adapter";
  version = "2.20.1";

  # The published npm tarball ships TypeScript source with no
  # package-lock.json (pi resolves index.ts directly via jiti at runtime, so
  # nicobailon/pi-mcp-adapter never needed to publish one). buildNpmPackage
  # needs a lockfile to build a deterministic dependency tree, so this fetches
  # the upstream GitHub source at the matching release tag instead, which
  # does commit one (verified: same version, same package.json, lockfileVersion 3).
  src = fetchurl {
    url = "https://github.com/nicobailon/pi-mcp-adapter/archive/refs/tags/v${version}.tar.gz";
    hash = "sha256-/NOVKfJ1KwDQniBWCCM2vyyAcAIJhvyadJgAK+GXQYs=";
  };

  npmDepsHash = lib.fakeHash;

  # No "build" script in package.json — the package exports its .ts sources
  # directly (pi's loader resolves index.ts before index.js).
  dontNpmBuild = true;

  meta = {
    description = "MCP (Model Context Protocol) adapter extension for the pi coding agent";
    homepage = "https://github.com/nicobailon/pi-mcp-adapter";
    license = lib.licenses.mit;
    mainProgram = "pi-mcp-adapter";
  };
}
