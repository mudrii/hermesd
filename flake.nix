{
  description = "hermesd — TUI monitoring dashboard for Hermes AI agent";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/c043004d1c6985732bcc1cbc5a9c9aecbbb4e0f0";
    nixpkgsIntelDarwin.url = "github:NixOS/nixpkgs/8029b6c369415ee1ef02a86f352806348f104db9";
  };

  outputs = { self, nixpkgs, nixpkgsIntelDarwin }:
    let
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-darwin"
        "x86_64-linux"
      ];
      pkgsFor = system:
        if system == "x86_64-darwin"
        then import nixpkgsIntelDarwin {
          inherit system;
          # The supported 26.05 Darwin package set still marks Intel deprecated,
          # so opt in explicitly for its remaining support window.
          config.allowDeprecatedx86_64Darwin = true;
        }
        else nixpkgs.legacyPackages.${system};
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f (pkgsFor system));

      # Single source of truth for the package version: the flake always reads
      # it from pyproject.toml, so the two cannot drift apart.
      hermesdVersion = pkgs: (pkgs.lib.importTOML ./pyproject.toml).project.version;

      # The pinned nixpkgs revision provides hatchling 1.31, while the package
      # requires hatchling>=1.32 for current core metadata. Supply that build
      # dependency from its hash-verified PyPI wheel.
      mkHatchling = pkgs:
        pkgs.python312.pkgs.buildPythonPackage rec {
          pname = "hatchling";
          version = "1.32.0";
          format = "wheel";
          src = pkgs.fetchurl {
            url = "https://files.pythonhosted.org/packages/a9/84/1798b6d85ecde0e31546004efd25c5de1b1f49250644a60cce460e12593a/hatchling-1.32.0-py3-none-any.whl";
            hash = "sha256-DhfJw7mqfGJazI0PW2IvEH1QSa+ez1raTeGq2lvnzbw=";
          };
          dependencies = with pkgs.python312.pkgs; [
            packaging
            pathspec
            pluggy
            tomlkit
            trove-classifiers
          ];
          doCheck = false;
        };

      mkHermesd = pkgs:
        let
          python = pkgs.python312;
        in
        python.pkgs.buildPythonApplication {
          pname = "hermesd";
          version = hermesdVersion pkgs;
          pyproject = true;

          src = ./.;

          build-system = [ (mkHatchling pkgs) ];

          # PyPI installs use the exact runtime pins in pyproject.toml. Nix
          # supplies its own package-set versions and validates that set with
          # the build-time test suite and installed CLI smoke below.
          dontCheckRuntimeDeps = true;

          dependencies = with python.pkgs; [
            rich
            pyyaml
            pydantic
          ];

          # The package build itself executes the pytest suite (checkPhase),
          # so `nix build .#hermesd` and `nix flake check` both prove that the
          # packaged code passes its tests — not merely that it evaluates.
          nativeCheckInputs = [
            python.pkgs.pytestCheckHook
            python.pkgs.packaging
            pkgs.git
          ];
          pytestFlags = [ "tests" ];

          # Checkpoint collection invokes git after installation, so keep it
          # on PATH for consumers of the Nix application as well as tests.
          makeWrapperArgs = [ "--prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.git ]}" ];

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

          # Set up the fixture with an absolute git path, then rely on the
          # installed hermesd wrapper to provide git to checkpoint collection.
          hermesd-checkpoint-smoke = pkgs.runCommand "hermesd-checkpoint-smoke"
            {
              nativeBuildInputs = [ hermesd ];
              meta.timeout = 300;
            }
            ''
              home="$TMPDIR/.hermes"
              workdir="$TMPDIR/workspaces/project-alpha"
              repo="$home/checkpoints/smoke000000000000"
              mkdir -p "$home"/{logs,sessions,skills,memories,cron} "$workdir" "$repo"
              echo "$workdir" > "$repo/HERMES_WORKDIR"

              git_bin=${pkgs.git}/bin/git
              "$git_bin" init --quiet --bare "$repo"
              "$git_bin" --git-dir "$repo" --work-tree "$workdir" config user.email smoke@example.invalid
              "$git_bin" --git-dir "$repo" --work-tree "$workdir" config user.name "Smoke Test"
              echo "checkpoint smoke" > "$workdir/notes.txt"
              "$git_bin" --git-dir "$repo" --work-tree "$workdir" add -A
              "$git_bin" --git-dir "$repo" --work-tree "$workdir" commit --quiet -m "checkpoint 0"
              echo "checkpoint smoke v1" > "$workdir/notes.txt"
              "$git_bin" --git-dir "$repo" --work-tree "$workdir" add -A
              "$git_bin" --git-dir "$repo" --work-tree "$workdir" commit --quiet -m "checkpoint 1"

              hermesd --hermes-home "$home" --snapshot-panel 4 --no-color > snapshot.txt
              grep -F "Checkpoints (1)" snapshot.txt > /dev/null
              grep -F "checkpoint 1" snapshot.txt > /dev/null
              touch "$out"
            '';
        });
    };
}
