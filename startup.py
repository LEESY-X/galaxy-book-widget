"""Opt-in current-user login startup; changes only this application's Run entry."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
ENTRY_NAME = "GalaxyBook3ProWidget"


def startup_command():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    script = Path(__file__).resolve().with_name("widget.py")
    if not pythonw.is_file() or not script.is_file():
        raise FileNotFoundError("pythonw.exe and widget.py must be available")
    return subprocess.list2cmdline([str(pythonw), str(script)])


def install():
    command = startup_command()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, ENTRY_NAME, 0, winreg.REG_SZ, command)


def remove():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, ENTRY_NAME)
    except FileNotFoundError:
        pass


def enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            command, _ = winreg.QueryValueEx(key, ENTRY_NAME)
        return bool(command)
    except FileNotFoundError:
        return False


def main():
    parser = argparse.ArgumentParser(description="Configure login startup for the current Windows user")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true")
    action.add_argument("--remove", action="store_true")
    action.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.install:
        install()
        print("Login startup enabled.")
    elif args.remove:
        remove()
        print("Login startup disabled.")
    else:
        print("Enabled" if enabled() else "Disabled")


if __name__ == "__main__":
    main()
