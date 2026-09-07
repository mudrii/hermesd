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
      mkHermesd = pkgs:
        let
          python = pkgs.python311;
        in
        python.pkgs.buildPythonApplication {
          pname = "hermesd";
          version = "2026.9.8";
          pyproject = true;

          src = ./.;

          build-system = [ python.pkgs.hatchling ];

          dependencies = with python.pkgs; [
            rich
            pyyaml
            pydantic
          ];

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
        let python = pkgs.python311;
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
    };
}
