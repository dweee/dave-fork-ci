{
  description = "Regression checks for the guarded public fork CI controller";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/f4a03c008967841f2a0b3cb7c436fc6fc31671f6";
  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
    in
    {
      checks = nixpkgs.lib.genAttrs systems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (p: [ p.pyyaml ]);
        in
        {
          controller =
            pkgs.runCommand "fork-controller-regression-tests"
              {
                nativeBuildInputs = [
                  python
                  pkgs.git
                  pkgs.bash
                ];
                src = nixpkgs.lib.cleanSource self;
              }
              ''
                cp -R "$src" source
                chmod -R u+w source
                cd source
                export HOME="$TMPDIR/home"
                mkdir -p "$HOME"
                export PYTHONDONTWRITEBYTECODE=1
                python3 -m unittest discover -s tests -v
                touch "$out"
              '';
        }
      );
    };
}
