import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from builder import artifacts, detect, environment, orchestrator, pub_config
from farm import worker, queue as farm_queue

LINUX = {"os": "Linux", "os_raw": "Linux", "arch": "x86_64"}


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dvforge tests ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def pub_file(self):
        path = self.root / ".dart_tool" / "package_config.json"
        path.parent.mkdir(exist_ok=True)
        for name in ("flutter", "ffigen"):
            (self.root / name).mkdir(exist_ok=True)
        path.write_text(json.dumps({"configVersion": 2, "packages": [
            {"name": name, "rootUri": "../" + name} for name in ("flutter", "ffigen")]}))
        return path

    def test_valid_pub_resolution(self):
        pub_config.validate(str(self.pub_file()))

    def test_stale_pub_cannot_pass_zero_exit(self):
        path = self.pub_file()
        with self.assertRaisesRegex(RuntimeError, "Invalid Flutter"):
            pub_config.resolve(str(path), Mock(return_value=0))
        self.assertFalse(path.exists())
        self.assertTrue(Path(str(path) + ".dvforge-previous").exists())

    def test_pub_nonzero_stops_before_validation(self):
        runner = Mock(side_effect=RuntimeError("pub failed: network unreachable"))
        with self.assertRaisesRegex(RuntimeError, "network unreachable"):
            pub_config.resolve(str(self.pub_file()), runner)
        runner.assert_called_once_with(["flutter", "pub", "get"], check=True)

    def test_pub_dry_run_preserves_existing_file(self):
        path = self.pub_file()
        before = path.read_bytes()
        pub_config.resolve(str(path), Mock(), dry_run=True)
        self.assertEqual(before, path.read_bytes())
        self.assertFalse(Path(str(path) + ".dvforge-previous").exists())

    def test_corrupt_or_empty_pub_config_rejected(self):
        path = self.pub_file()
        for value in ("{}", "not json", '{"configVersion":2,"packages":[]}'):
            path.write_text(value)
            with self.assertRaises(RuntimeError):
                pub_config.validate(str(path))

    def test_missing_package_root_rejected(self):
        path = self.pub_file()
        (self.root / "ffigen").rmdir()
        with self.assertRaisesRegex(RuntimeError, "root is missing"):
            pub_config.validate(str(path))

    def test_version_layout_and_traversal(self):
        path = artifacts.output_dir(str(self.root), "linux", "v1.4.9", "aarch64")
        self.assertTrue(path.endswith("output/linux/1.4.9/aarch64"))
        with self.assertRaises(ValueError):
            artifacts.output_dir(str(self.root), "linux", "../../escape", "aarch64")

    def test_wrong_deb_architecture_rejected(self):
        path = self.root / "test.deb"
        path.write_bytes(b"fixture")
        with patch.object(artifacts.subprocess, "check_output", return_value="amd64\n"):
            with self.assertRaisesRegex(RuntimeError, "expected arm64"):
                artifacts.verify_package(str(path), "aarch64")

    def test_elf_checks_real_machine_not_filename(self):
        path = self.root / "arm64-binary"
        path.write_bytes(b"\x7fELF\x02\x01" + bytes(12) + struct.pack("<H", 62))
        self.assertEqual("x86_64", artifacts.elf_arch(path))

    def test_pe_architecture(self):
        path = self.root / "driver.dll"
        path.write_bytes(b"MZ" + bytes(58) + struct.pack("<I", 64) + b"PE\x00\x00" + struct.pack("<H", 0x8664))
        self.assertEqual("x86_64", artifacts.pe_arch(path))

    def test_checksum(self):
        path = self.root / "artifact"
        path.write_bytes(b"abc")
        self.assertEqual(artifacts.metadata(str(path))["sha256"],
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_distro_identification_not_brand_name(self):
        self.assertEqual("apt", environment.package_manager({"ID": "debian", "NAME": "ECZOS"}))
        self.assertEqual("dnf", environment.package_manager({"ID": "custom", "ID_LIKE": "fedora"}))
        self.assertEqual("pacman", environment.package_manager({"ID": "arch"}))
        self.assertIsNone(environment.package_manager({"ID": "unknown"}))

    def test_requirements_are_target_scoped(self):
        required, errors = environment.required(["linux-x86_64-deb"], LINUX)
        self.assertFalse(errors)
        self.assertIn("dpkg_deb", required)
        self.assertIn("llvm", required)
        self.assertNotIn("java", required)
        self.assertNotIn("msbuild", required)
        self.assertNotIn("appimage_builder", required)

    def test_wrong_arch_requires_remote_not_local_dependencies(self):
        needed, errors = environment.required(["linux-aarch64-deb"], LINUX)
        self.assertEqual([], needed)
        self.assertIn("native aarch64", errors[0])
        row = next(t for t in detect.build_matrix(LINUX) if t["id"] == "linux-aarch64-deb")
        self.assertFalse(row["ready"])
        self.assertEqual("remote", row["route"])

    def test_installed_dependencies_are_skipped(self):
        report = {"ok": True, "host": LINUX, "requirements": [], "problems": []}
        with patch.object(environment, "report", return_value=report), patch.object(environment.subprocess, "run") as runner:
            self.assertTrue(environment.ensure([], str(self.root), Mock(), auto_install=True)["ok"])
        runner.assert_not_called()

    def test_report_only_never_installs(self):
        report = {"ok": False, "host": LINUX, "requirements": [], "problems": ["missing"]}
        with patch.object(environment, "report", return_value=report), patch.object(environment.subprocess, "run") as runner:
            self.assertFalse(environment.ensure([], str(self.root), Mock())["ok"])
        runner.assert_not_called()

    def install_fixture(self, runner, check):
        report = {"ok": False, "host": LINUX, "requirements": [{"id": "git", "present": False}], "problems": ["git"]}
        with patch.object(environment, "report", return_value=report), patch.object(environment, "required", return_value=(["git"], [])), patch.object(environment, "native_command", return_value=["apt-get", "install", "-y", "git"]), patch.object(environment, "check", side_effect=check), patch.object(environment.subprocess, "run", runner):
            return environment.ensure(["linux-x86_64-deb"], str(self.root), Mock(), auto_install=True)

    def test_install_failure_is_critical(self):
        result = self.install_fixture(Mock(return_value=Mock(returncode=1, stdout="", stderr="denied")), [{"present": False}])
        self.assertFalse(result["ok"])
        self.assertIn("denied", result["problems"][0])

    def test_install_success_without_valid_tool_is_failure(self):
        result = self.install_fixture(Mock(return_value=Mock(returncode=0, stdout="", stderr="")), [{"present": False}, {"present": False}])
        self.assertIn("verification failed", result["problems"][0])

    def test_successful_install_is_verified_then_idempotent(self):
        before = {"ok": False, "host": LINUX, "requirements": [{"id": "git", "present": False}], "problems": ["git"]}
        after = {"ok": True, "host": LINUX, "requirements": [{"id": "git", "present": True}], "problems": []}
        runner = Mock(return_value=Mock(returncode=0, stdout="installed", stderr=""))
        with patch.object(environment, "report", side_effect=[before, after, after]), patch.object(environment, "required", return_value=(["git"], [])), patch.object(environment, "native_command", return_value=["apt-get", "install", "-y", "git"]), patch.object(environment, "check", side_effect=[{"present": False}, {"present": True}]), patch.object(environment.subprocess, "run", runner):
            self.assertTrue(environment.ensure(["linux-x86_64-deb"], str(self.root), Mock(), auto_install=True)["ok"])
            self.assertTrue(environment.ensure(["linux-x86_64-deb"], str(self.root), Mock(), auto_install=True)["ok"])
        self.assertEqual(1, runner.call_count)

    def test_linux_native_commands_are_strict(self):
        with patch.object(detect, "host_info", return_value=LINUX):
            build = orchestrator.Build("1.4.9", ["linux-x86_64-deb"], {}, str(self.root), log=Mock())
        build.run = Mock(side_effect=RuntimeError("cargo failed"))
        with self.assertRaisesRegex(RuntimeError, "cargo failed"):
            build._build_linux_core()
        self.assertEqual(1, build.run.call_count)
        self.assertTrue(build.run.call_args.kwargs["check"])

    def test_linux_rpm_dry_run_does_not_touch_packaging(self):
        with patch.object(detect, "host_info", return_value=LINUX):
            build = orchestrator.Build("1.4.9", ["linux-x86_64-rpm"], {}, str(self.root), log=Mock(), dry_run=True)
        build._package_linux_rpm()
        self.assertFalse((self.root / "packaging").exists())

    def test_download_blocks_symlink_escape_and_private_config(self):
        output = self.root / "output"
        output.mkdir()
        outside = self.root / "outside.deb"
        outside.write_bytes(b"test")
        (output / "link.deb").symlink_to(outside)
        with self.assertRaises(ValueError):
            artifacts.download_path(str(self.root), str(output / "link.deb"))
        secret = output / "custom_.txt"
        secret.write_bytes(b"private")
        with self.assertRaises(ValueError):
            artifacts.download_path(str(self.root), str(secret))

    def test_remote_worker_requires_https_and_token(self):
        for url, token in (("http://build.example.org", "x" * 32),
                           ("https://build.example.org", ""),
                           ("https://user:secret@build.example.org", "x" * 32),
                           ("https://build.example.org?token=secret", "x" * 32)):
            with self.assertRaises(ValueError):
                worker.validate_queue_url(url, token)
        worker.validate_queue_url("https://build.example.org", "x" * 32)
        worker.validate_queue_url("http://127.0.0.1:8766", "")

    def test_query_token_is_not_authorization(self):
        handler = farm_queue.Handler.__new__(farm_queue.Handler)
        handler.headers = {}
        handler.path = "/job?token=" + "x" * 32
        handler._send = Mock()
        with patch.object(farm_queue, "TOKEN", "x" * 32):
            self.assertFalse(handler._auth())
        handler._send.assert_called_once()

    def test_backend_does_not_build_when_dependencies_fail(self):
        import app
        session = app.BuildSession()
        with patch.object(app.orchestrator, "Build") as build_class, patch.object(app.environment, "ensure", return_value={"ok": False, "problems": ["missing Flutter"]}):
            self.assertTrue(session.start("1.4.9", ["macos-arm64-dmg"], {}, auto_install=False)[0])
            session.thread.join(5)
            self.assertFalse(session.running)
            self.assertFalse(session.result["ok"])
            build_class.return_value.execute.assert_not_called()

    def test_existing_command_stdout_in_failure_result(self):
        with patch.object(detect, "host_info", return_value=LINUX):
            build = orchestrator.Build("1.4.9", [], {}, str(self.root), log=Mock())
        build._effective_path = lambda: os.environ["PATH"]
        with self.assertRaisesRegex(RuntimeError, "pub diagnostic detail"):
            build.run([sys.executable, "-c", "print('pub diagnostic detail'); raise SystemExit(7)"])

    def test_linux_deb_dry_run_does_not_open_or_write_source(self):
        with patch.object(detect, "host_info", return_value=LINUX):
            build = orchestrator.Build("1.4.9", ["linux-x86_64-deb"], {}, str(self.root), log=Mock(), dry_run=True)
        build._package_linux_deb()
        self.assertFalse((self.root / "rustdesk-src").exists())

    def test_platform_output_architecture_mapping(self):
        with patch.object(detect, "host_info", return_value=LINUX):
            build = orchestrator.Build("1.4.9", [], {}, str(self.root), log=Mock())
        for platform_name, filename, expected in (
            ("macos", "Client-1.4.9-aarch64.dmg", "aarch64"),
            ("macos", "UniversalClient-1.4.9-aarch64.dmg", "aarch64"),
            ("macos", "Client-1.4.9-x86_64.dmg", "x86_64"),
            ("macos", "Client-1.4.9-universal.dmg", "universal"),
            ("android", "app-arm64-v8a-release.apk", "aarch64"),
            ("android", "app-armeabi-v7a-release.apk", "armv7"),
            ("android", "app-x86_64-release.apk", "x86_64"),
            ("android", "app-release.apk", "universal")):
            self.assertEqual(expected, Path(build._artifact_dir(platform_name, filename)).name)

    def test_api_guard_rejects_cross_origin_and_nonloopback_host(self):
        import app
        handler = app.Handler.__new__(app.Handler)
        handler.server = Mock(server_address=("127.0.0.1", 8765))
        handler._send_json = Mock()
        for headers in ({"Host": "evil.example:8765"},
                        {"Host": "127.0.0.1:8765", "Origin": "http://evil.example"},
                        {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8766"}):
            handler.headers = headers
            self.assertFalse(handler._trusted_local_request())
        handler.headers = {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"}
        self.assertTrue(handler._trusted_local_request())

    def test_target_validation_rejects_unstructured_payload(self):
        for ids in ([], "linux-x86_64-deb", [{}], ["unknown"]):
            with self.assertRaises(ValueError):
                detect.validate_target_ids(ids)
        self.assertEqual(["linux-x86_64-deb"], detect.validate_target_ids(["linux-x86_64-deb"] * 2))


if __name__ == "__main__":
    unittest.main()
