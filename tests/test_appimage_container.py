import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from builder import appimage_container, toolchains, environment, prereqs


class AppImageContainerTests(unittest.TestCase):
    def test_sidebar_marks_legacy_builder_missing_without_apt_key(self):
        with patch("builder.prereqs._which", side_effect=lambda name: "/usr/bin/appimage-builder" if name == "appimage-builder" else None), patch("builder.appimage_container.is_wrapper", return_value=False):
            result = prereqs.check_appimage_builder()
        self.assertFalse(result["present"])
        self.assertIn("Auto install", result["hint"])

    def test_sidebar_marks_missing_container_installable(self):
        with patch("builder.prereqs._which", return_value="/tools/appimage-builder"), patch("builder.appimage_container.is_wrapper", return_value=True), patch("builder.appimage_container.ready", return_value=False):
            self.assertFalse(prereqs.check_appimage_builder()["present"])

    def test_registry_wires_auto_installer(self):
        self.assertEqual(toolchains.SATISFIES["appimage_builder"], "appimage_builder")
        self.assertTrue(toolchains.installable("Linux", "x86_64")["appimage_builder"]["ok"])
        self.assertFalse(toolchains.installable("macOS", "arm64")["appimage_builder"]["ok"])

    def test_existing_image_creates_only_local_wrapper(self):
        with tempfile.TemporaryDirectory() as directory, patch("builder.appimage_container.shutil.which", return_value="/usr/bin/podman"), patch("builder.appimage_container.os.geteuid", return_value=1000), patch("builder.appimage_container.ready", return_value=True), patch("builder.appimage_container.subprocess.Popen") as process:
            result = appimage_container.install(directory, lambda _: None, lambda: False)
            self.assertTrue(appimage_container.is_wrapper(Path(directory) / "bin/appimage-builder"))
            self.assertEqual(result["env"]["path"], [str(Path(directory) / "bin")])
            process.assert_not_called()

    def test_container_wrapper_satisfies_missing_host_apt_key(self):
        with patch("builder.environment.prereqs.CHECKS", {"appimage_builder": lambda: {"present": True}}), patch("builder.environment.shutil.which", return_value="/tools/appimage-builder"), patch("builder.appimage_container.is_wrapper", return_value=True), patch("builder.appimage_container.ready", return_value=True):
            self.assertTrue(environment.check("appimage_builder")["present"])

    def test_packaging_mount_is_source_only_and_not_privileged(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve() / "source"
            (source / "appimage").mkdir(parents=True)
            with patch("builder.appimage_container.Path.cwd", return_value=source / "appimage"), patch("builder.appimage_container.subprocess.call", return_value=0) as run:
                self.assertEqual(appimage_container.run(["--skip-tests", "--recipe", str(source / "appimage/recipe.yml")]), 0)
            command = run.call_args.args[0]
            self.assertIn(f"{source}:{source}:rw", command)
            self.assertNotIn("--privileged", command)
            self.assertNotIn("sudo", command)
