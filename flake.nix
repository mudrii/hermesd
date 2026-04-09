{
  description = "hermesd — TUI monitoring dashboard for Hermes AI agent";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python311;
        hermesd = python.pkgs.buildPythonApplication {
          pname = "hermesd";
          version = "0.1.0";
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
            maintainers = [ ];
            mainProgram = "hermesd";
          };
        };
      in
      {
        packages.default = hermesd;
        packages.hermesd = hermesd;

        apps.default = flake-utils.lib.mkApp {
          drv = hermesd;
        };

        devShells.default = pkgs.mkShell {
          packages = [
            python
            python.pkgs.rich
            python.pkgs.pyyaml
            python.pkgs.pydantic
            python.pkgs.pytest
            pkgs.uv
          ];
        };
      }
    );
}
