# Exercise an actual native compilation, rather than only substituting a package.
let
  controller = builtins.getFlake (toString ../.);
  pkgs = import controller.inputs.nixpkgs { system = builtins.currentSystem; };
in
pkgs.runCommandCC "hosted-nix-cleanup-trial" { } ''
  cat > hello.c <<'EOF'
  #include <stdio.h>
  int main(void) { puts("native compilation passed"); return 0; }
  EOF
  $CC hello.c -o hello
  ./hello
  mkdir -p "$out/bin"
  cp hello "$out/bin/hosted-nix-trial"
''
