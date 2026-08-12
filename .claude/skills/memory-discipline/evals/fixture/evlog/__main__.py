import json
import sys

from .replay import rebuild


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["rebuild"]:
        print(json.dumps(rebuild(), sort_keys=True))
        return 0
    print("usage: python -m evlog rebuild", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
