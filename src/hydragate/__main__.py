"""python -m hydragate {sim|bridge|chaos} [options]"""
import sys

from .cli import bridge_main, chaos_main, sim_main

DISPATCH = {"sim": sim_main, "bridge": bridge_main, "chaos": chaos_main}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in DISPATCH:
        print("usage: python -m hydragate {sim|bridge|chaos} [options]", file=sys.stderr)
        raise SystemExit(2)
    DISPATCH[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
