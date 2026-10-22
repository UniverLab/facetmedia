"""CLI entry point for facetmedia."""

import argparse
import sys

from facetmedia.core import hello


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="facetmedia",
        description="",
    )
    parser.add_argument("--name", default="world", help="Name to greet")
    args = parser.parse_args(argv)

    print(hello(args.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
