{ lib, buildNpmPackage, fetchurl }:

buildNpmPackage rec {
  pname = "pi-lsp";
  version = "0.1.7";

  # No public source repo exists for this package (the npm maintainer's
  # GitHub has none), so the npm registry tarball is the only upstream.
  src = fetchurl {
    url = "https://registry.npmjs.org/pi-lsp/-/pi-lsp-${version}.tgz";
    hash = "sha256-75WAW5kBWlXhmaHIRhfY/xWtgXPWyFASIG634J8CGoc=";
  };

  # The tarball ships TypeScript source with no package-lock.json (pi loads
  # the .ts extension directly at runtime). buildNpmPackage needs a lockfile,
  # so vendor one generated from the tarball's package.json with
  # `npm install --package-lock-only --ignore-scripts --legacy-peer-deps`.
  # Peer deps (@earendil-works/pi-*) are deliberately absent from it —
  # pi itself provides them at runtime — hence --legacy-peer-deps below too.
  postPatch = ''
    cp ${./pi-lsp-package-lock.json} package-lock.json
  '';

  npmFlags = [ "--legacy-peer-deps" ];

  # "prepack" runs typecheck + vitest, which need devDependencies and the
  # pi peer deps; skip it — the tarball contents are already what pi loads.
  npmPackFlags = [ "--ignore-scripts" ];

  npmDepsFetcherVersion = 2;
  npmDepsHash = "sha256-EVC4nBkPppOe/CEpJG8YH37KmSoHMCl9ZbifwmviRkI=";

  # No "build" script in package.json — the package exports its .ts sources
  # directly (pi's loader resolves extensions/pi-lsp/index.ts as-is).
  dontNpmBuild = true;

  meta = {
    description = "Declarative pi extension for LSP diagnostics and language-server navigation tools";
    homepage = "https://www.npmjs.com/package/pi-lsp";
    license = lib.licenses.mit;
  };
}
