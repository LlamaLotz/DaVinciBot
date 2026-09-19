import argparse
import json

from davincibot.app import run
from davincibot.diagnostics import system_diagnostics
from davincibot.resolve.install import install_launcher


def main() -> None:
    parser = argparse.ArgumentParser(prog="davincibot")
    parser.add_argument("--diagnose", action="store_true", help="print local dependency status")
    parser.add_argument(
        "--install-resolve-launcher",
        action="store_true",
        help="install the Resolve Free Workspace Scripts launcher",
    )
    arguments = parser.parse_args()
    if arguments.diagnose:
        print(json.dumps(system_diagnostics(), indent=2))
        return
    if arguments.install_resolve_launcher:
        print(install_launcher())
        return
    raise SystemExit(run())


if __name__ == "__main__":
    main()
