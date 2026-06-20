{ pkgs }: {
  deps = [
    pkgs.python311
    pkgs.python311Packages.pip
    # libpq for psycopg; gcc for any binary builds.
    pkgs.postgresql
    pkgs.gcc
  ];
}
