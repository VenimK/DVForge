import subprocess
import unittest
from unittest.mock import patch

from builder.environment import run_native_installer


class NativeApprovalTests(unittest.TestCase):
    def test_terminal_helper_records_real_installer_exit(self):
        import json
        import tempfile
        from pathlib import Path
        from builder.terminal_approval import run
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            with patch("builder.terminal_approval.subprocess.run", return_value=subprocess.CompletedProcess([], 100)) as installer, patch("builtins.print"):
                run(status, ["/usr/bin/apt-get", "install", "-y", "libgtk-3-dev"])
            self.assertEqual(json.loads(status.read_text())["returncode"], 100)
            self.assertEqual(installer.call_args.args[0], ["sudo", "--", "/usr/bin/apt-get", "install", "-y", "libgtk-3-dev"])

    def test_missing_agent_uses_terminal_without_browser_password(self):
        denied = subprocess.CompletedProcess([], 1, "", "sudo: a password is required")
        unavailable = subprocess.CompletedProcess([], 127, "", "No authentication agent found.")
        success = subprocess.CompletedProcess([], 0, "", "")
        with patch("builder.environment.subprocess.run", side_effect=[denied, unavailable]), patch("builder.environment.os.path.isfile", return_value=True), patch("builder.environment.shutil.which", return_value="/usr/bin/pkexec"), patch("builder.terminal_approval.install", return_value=success) as terminal:
            result = run_native_installer(["sudo", "-n", "apt-get", "install", "-y", "libgtk-3-dev"], lambda _: None)
        self.assertIs(result, success)
        self.assertEqual(terminal.call_args.args[0], ["/usr/bin/apt-get", "install", "-y", "libgtk-3-dev"])

    def test_ssh_without_display_has_actionable_error(self):
        from builder.terminal_approval import install
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "not via SSH"):
                install(["/usr/bin/apt-get", "install", "-y", "libgtk-3-dev"], lambda _: None)

    def test_success_does_not_prompt(self):
        success = subprocess.CompletedProcess([], 0, "installed", "")
        with patch("builder.environment.subprocess.run", return_value=success) as run:
            self.assertIs(run_native_installer(["sudo", "-n", "apt-get", "install", "-y", "libgtk-3-dev"], lambda _: None), success)
        self.assertEqual(run.call_count, 1)

    def test_password_opens_polkit_for_fixed_command(self):
        denied = subprocess.CompletedProcess([], 1, "", "sudo: a password is required")
        success = subprocess.CompletedProcess([], 0, "installed", "")
        with patch("builder.environment.subprocess.run", side_effect=[denied, success]) as run, patch("builder.environment.os.path.isfile", return_value=True), patch("builder.environment.shutil.which", return_value="/usr/bin/pkexec"):
            result = run_native_installer(["sudo", "-n", "apt-get", "install", "-y", "libgtk-3-dev"], lambda _: None)
        self.assertIs(result, success)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/pkexec", "--disable-internal-agent", "/usr/bin/apt-get", "install", "-y", "libgtk-3-dev"])

    def test_package_error_is_not_retried_privileged(self):
        failure = subprocess.CompletedProcess([], 100, "", "Package unavailable")
        with patch("builder.environment.subprocess.run", return_value=failure) as run:
            self.assertIs(run_native_installer(["sudo", "-n", "apt-get", "install", "-y", "missing"], lambda _: None), failure)
        self.assertEqual(run.call_count, 1)

    def test_cancel_is_actionable(self):
        denied = subprocess.CompletedProcess([], 1, "", "sudo: a password is required")
        cancelled = subprocess.CompletedProcess([], 126, "", "Dismissed")
        with patch("builder.environment.subprocess.run", side_effect=[denied, cancelled]), patch("builder.environment.os.path.isfile", return_value=True), patch("builder.environment.shutil.which", return_value="/usr/bin/pkexec"):
            with self.assertRaisesRegex(RuntimeError, "cancelled or unavailable"):
                run_native_installer(["sudo", "-n", "apt-get", "install", "-y", "libgtk-3-dev"], lambda _: None)
