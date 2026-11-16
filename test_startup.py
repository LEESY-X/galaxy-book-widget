"""Startup tests mock the registry; they do not modify login settings."""
import unittest
from unittest.mock import patch

import startup


class StartupTests(unittest.TestCase):
    def test_install_only_writes_the_apps_current_user_entry(self):
        with patch.object(startup,"startup_command",return_value='"pythonw.exe" "widget.py"'), patch.object(startup.winreg,"CreateKeyEx") as create, patch.object(startup.winreg,"SetValueEx") as write:
            startup.install()
            create.assert_called_once_with(startup.winreg.HKEY_CURRENT_USER,startup.RUN_KEY,0,startup.winreg.KEY_SET_VALUE)
            write.assert_called_once_with(create.return_value.__enter__.return_value,startup.ENTRY_NAME,0,startup.winreg.REG_SZ,'"pythonw.exe" "widget.py"')

    def test_removing_a_missing_entry_is_idempotent(self):
        with patch.object(startup.winreg,"OpenKey",side_effect=FileNotFoundError):
            startup.remove()

    def test_absent_entry_is_disabled(self):
        with patch.object(startup.winreg,"OpenKey",side_effect=FileNotFoundError):
            self.assertFalse(startup.enabled())


if __name__ == "__main__":
    unittest.main()
