{
  description = "hermesd — TUI monitoring dashboard for Hermes AI agent";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/c043004d1c6985732bcc1cbc5a9c9aecbbb4e0f0";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-darwin"
        "x86_64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # Single source of truth for the package version: the flake always reads
      # it from pyproject.toml, so the two cannot drift apart.
      hermesdVersion = pkgs: (pkgs.lib.importTOML ./pyproject.toml).project.version;

      mkHermesd = pkgs:
        let
          python = pkgs.python312;
        in
        python.pkgs.buildPythonApplication {
          pname = "hermesd";
          version = hermesdVersion pkgs;
          pyproject = true;

          src = ./.;

          build-system = [ python.pkgs.hatchling ];

          dependencies = with python.pkgs; [
            rich
            pyyaml
            pydantic
          ];

          # The package build itself executes the pytest suite (checkPhase),
          # so `nix build .#hermesd` and `nix flake check` both prove that the
          # packaged code passes its tests — not merely that it evaluates.
          nativeCheckInputs = [ python.pkgs.pytestCheckHook ];

          meta = with pkgs.lib; {
            description = "TUI monitoring dashboard for Hermes AI agent";
            homepage = "https://github.com/mudrii/hermesd";
            license = licenses.mit;
            mainProgram = "hermesd";
          };
        };
    in
    {
      packages = forAllSystems (pkgs:
        let hermesd = mkHermesd pkgs;
        in {
          default = hermesd;
          inherit hermesd;
        });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${mkHermesd pkgs}/bin/hermesd";
        };
      });

      devShells = forAllSystems (pkgs:
        let python = pkgs.python312;
        in {
          default = pkgs.mkShell {
            packages = [
              python
              python.pkgs.rich
              python.pkgs.pyyaml
              python.pkgs.pydantic
              python.pkgs.pytest
              pkgs.uv
            ];
          };
        });

      checks = forAllSystems (pkgs:
        let hermesd = mkHermesd pkgs;
        in {
          # `nix flake check` builds every derivation under checks, so listing
          # the package here forces its realization (and its pytest suite)
          # even for callers that only run the flake-level check.
          inherit hermesd;

          # Realize the package and exercise the installed executable — the
          # same smoke every published artifact must pass.
          hermesd-cli-smoke = pkgs.runCommand "hermesd-cli-smoke"
            {
              nativeBuildInputs = [ hermesd ];
              expectedVersion = hermesdVersion pkgs;
              meta.timeout = 300;
            }
            ''
              output="$(hermesd --version)"
              echo "installed executable: $output"
              echo "$output" | grep -F "hermesd $expectedVersion" > /dev/null || {
                echo "installed hermesd does not report version $expectedVersion" >&2
                exit 1
              }
              touch "$out"
            '';
        });
    };
}
