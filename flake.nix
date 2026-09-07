{
  description = "hermesd — TUI monitoring dashboard for Hermes AI agent";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/c043004d1c6985732bcc1cbc5a9c9aecbbb4e0f0";
  };

  outputs = { self, nixpkgs }:
    nixpkgs.lib.genAttrs [
      "aarch64-darwin"
      "aarch64-linux"
      "x86_64-darwin"
      "x86_64-linux"
    ] (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python311;
        hermesd = python.pkgs.buildPythonApplication {
          pname = "hermesd";
          version = "2026.7.11";
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
