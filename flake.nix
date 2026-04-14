{
  description = "rippled-workload development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [ "aarch64-darwin" "x86_64-linux" "aarch64-linux" ];
    in
    {
      devShells = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.python313
              pkgs.uv
              pkgs.ruff
              pkgs.mypy
            ];

            shellHook = ''
              cd workload 2>/dev/null || true
              echo "Syncing dependencies..."
              uv sync --quiet 2>/dev/null || uv sync
              cd - >/dev/null 2>/dev/null || true
              echo "Ready. Run 'check-imports' or 'check-endpoints' to verify."
            '';
          };
        }
      );
    };
}
