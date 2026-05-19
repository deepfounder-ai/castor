#!/usr/bin/env python3
"""Install the castor-worker launchd unit (macOS) or systemd unit (Linux).

Renders the template at scripts/com.castor.worker.plist.template (mac) or
scripts/castor-worker.service.template (linux — coming in Phase 1.b), filling
in absolute paths for the current Python interpreter, repo, and home dir,
then drops the result in the standard user-level unit dir.

Usage::

    python scripts/install_worker.py                 # render + install
    python scripts/install_worker.py --print          # print rendered unit, don't write
    python scripts/install_worker.py --uninstall      # remove the unit

After installing on macOS, load with::

    launchctl load ~/Library/LaunchAgents/com.castor.worker.plist
"""
from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()


def _render_macos() -> tuple[Path, str]:
    template = REPO_ROOT / "scripts" / "com.castor.worker.plist.template"
    if not template.exists():
        sys.exit(f"missing template: {template}")
    body = template.read_text()
    body = (
        body.replace("__PYTHON__", sys.executable)
            .replace("__REPO__", str(REPO_ROOT))
            .replace("__HOME__", str(HOME))
    )
    dest = HOME / "Library" / "LaunchAgents" / "com.castor.worker.plist"
    return dest, body


def _install_macos(print_only: bool) -> int:
    dest, body = _render_macos()
    if print_only:
        sys.stdout.write(body)
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body)
    print(f"wrote {dest}")
    print("Load with:")
    print(f"  launchctl load {dest}")
    print("Then tail logs at ~/.castor/logs/worker.{out,err}.log")
    return 0


def _uninstall_macos() -> int:
    dest = HOME / "Library" / "LaunchAgents" / "com.castor.worker.plist"
    if not dest.exists():
        print("no unit installed")
        return 0
    # Best-effort: stop the running unit first so launchd doesn't immediately
    # respawn it after we delete the file.
    subprocess.run(["launchctl", "unload", str(dest)], check=False)
    dest.unlink()
    print(f"removed {dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print", action="store_true",
                        help="print the rendered unit but don't install")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove an installed unit")
    args = parser.parse_args(argv)

    system = platform.system()
    if system != "Darwin":
        print(f"sorry, only macOS is wired up for now (got {system})")
        print("On Linux you can run the worker as: nohup python -m worker &")
        return 1

    if args.uninstall:
        return _uninstall_macos()
    return _install_macos(print_only=args.print)


if __name__ == "__main__":
    sys.exit(main())
