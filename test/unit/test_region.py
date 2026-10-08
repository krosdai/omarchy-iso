"""The target's region comes from its timezone, using the runtime's profiles."""

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "configs/airootfs/usr/share/omarchy-iso"))
sys.modules.setdefault(
    "orchestrator.archinstall_adapter",
    types.ModuleType("orchestrator.archinstall_adapter"),
)
from orchestrator import context, phases_impl, region  # noqa: E402


def write_profiles(root: Path) -> Path:
    regions = root / "regions"
    cn = regions / "cn"
    (cn / "pacman").mkdir(parents=True)
    (cn / "timezones").write_text("# China\nAsia/Shanghai\n\n  Asia/Urumqi  \n")
    (cn / "packages").write_text("# keyring first\narchlinuxcn-keyring\nextra-tool\n")
    (cn / "pacman/pacman.conf.append").write_text("[archlinuxcn]\nServer = https://example.invalid/$arch\n")
    (cn / "pacman/mirrorlist.append").write_text("Server = https://example.invalid/$repo/os/$arch\n")
    return regions


class RegionSelectionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.regions = write_profiles(self.root)
        patcher = mock.patch.object(region, "REGIONS_DIR", self.regions)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_timezone_selects_region(self):
        for timezone, expected in [
            ("Asia/Shanghai", "cn"),
            ("Asia/Urumqi", "cn"),
            ("Asia/Hong_Kong", "global"),
            ("America/New_York", "global"),
            ("UTC", "global"),
            ("# China", "global"),
            ("", "global"),
            (None, "global"),
        ]:
            with self.subTest(timezone=timezone):
                self.assertEqual(region.region_for_timezone(timezone), expected)

    def test_iso_without_profiles_is_global(self):
        self.assertEqual(region.region_for_timezone("Asia/Shanghai", self.root / "missing"), "global")
        self.assertEqual(region.resolve_region({}, "Asia/Shanghai", self.root / "missing"), "global")

    def test_autoinstall_region_overrides_timezone(self):
        self.assertEqual(region.resolve_region({"region": "cn"}, "UTC"), "cn")
        self.assertEqual(region.resolve_region({"region": "global"}, "Asia/Shanghai"), "global")
        self.assertEqual(region.resolve_region({}, "Asia/Shanghai"), "cn")
        for bad in ("us", "CN", "..", "cn/../cn"):
            with self.subTest(region=bad), self.assertRaises(RuntimeError):
                region.resolve_region({"region": bad}, "UTC")

    def test_profile_packages_and_keyrings(self):
        self.assertEqual(region.region_packages("cn"), ["archlinuxcn-keyring", "extra-tool"])
        self.assertEqual(region.region_keyrings("cn"), ["archlinuxcn"])
        self.assertEqual(region.region_packages("global"), [])
        self.assertEqual(region.region_keyrings("global"), [])

    def from_env(self, config: dict) -> context.InstallContext:
        config_path = self.root / "config.json"
        creds_path = self.root / "creds.json"
        config_path.write_text(json.dumps(config))
        creds_path.write_text(json.dumps({"users": [{"username": "test"}]}))
        with mock.patch.dict(os.environ, {
            "OMARCHY_INSTALL_CONFIG": str(config_path),
            "OMARCHY_INSTALL_CREDS": str(creds_path),
            "OMARCHY_INSTALL_STATE_DIR": str(self.root / "state"),
        }, clear=True), mock.patch.object(context, "_default_kernel", return_value="linux-omarchy"):
            return context.InstallContext.from_env()

    def test_context_resolves_region_before_install(self):
        self.assertEqual(self.from_env({"timezone": "Asia/Shanghai"}).region, "cn")
        self.assertEqual(self.from_env({"timezone": "Europe/Berlin"}).region, "global")
        # Deferred installs carry UTC until first boot, so they are global
        # unless the autoinstall config names a region.
        self.assertEqual(self.from_env({"timezone": "UTC"}).region, "global")
        self.assertEqual(self.from_env({"timezone": "UTC", "omarchy_install": {"region": "cn"}}).region, "cn")
        with self.assertRaises(RuntimeError):
            self.from_env({"omarchy_install": {"region": "zz"}})

    def test_marker_is_written_only_for_regional_targets(self):
        target = self.root / "target"
        phases_impl._write_region_marker(types.SimpleNamespace(target=target, region="global"))
        self.assertFalse((target / "etc/omarchy/region").exists())

        phases_impl._write_region_marker(types.SimpleNamespace(target=target, region="cn"))
        marker = target / "etc/omarchy/region"
        self.assertEqual(marker.read_text(), "cn\n")
        self.assertEqual(marker.stat().st_mode & 0o777, 0o644)

    def test_finalizer_trusts_only_the_selected_region(self):
        for selected, expected in [
            ("global", []),
            ("cn", [["pacman-key", "--init"], ["pacman-key", "--populate", "archlinux", "archlinuxcn"]]),
        ]:
            with self.subTest(region=selected), \
                    mock.patch.object(phases_impl, "_run_target_setup_command") as run, \
                    mock.patch.object(phases_impl, "_mask_mkinitcpio_pacman_hooks"), \
                    mock.patch.object(phases_impl, "_unmask_mkinitcpio_pacman_hooks"):
                ctx = types.SimpleNamespace(region=selected, defer_provisioning=True, target=Path("/unused"))
                phases_impl.run_system_finalizer(ctx)
                commands = [call.args[1] for call in run.call_args_list]
                self.assertEqual(commands[:-1], expected)
                self.assertEqual(commands[-1][0], "/usr/bin/omarchy-apply-system")


class PrepareRegionsTest(unittest.TestCase):
    def run_prepare(self, regions: Path, root: Path) -> tuple[Path, Path, str]:
        payload = root / "payload"
        payload.mkdir()
        online = root / "online.conf"
        online.write_text("[core]\nInclude = /etc/pacman.d/mirrorlist\n")
        stubs = root / "bin"
        stubs.mkdir()
        log = root / "calls.log"
        for name in ("pacman", "pacman-key"):
            stub = stubs / name
            stub.write_text(f'#!/bin/bash\necho "{name} $*" >> "{log}"\n')
            stub.chmod(0o755)
        subprocess.run(
            ["bash", str(ROOT / "builder/prepare-regions.sh"), str(regions), str(payload), str(online)],
            env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"},
            check=True, capture_output=True, text=True,
        )
        return payload, online, log.read_text() if log.exists() else ""

    def test_ships_every_profile_and_bootstraps_its_keyring(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            regions = write_profiles(root)
            payload, online, calls = self.run_prepare(regions, root)
            self.assertEqual((payload / "regions/cn/timezones").read_text(), (regions / "cn/timezones").read_text())
            self.assertIn("[archlinuxcn]", online.read_text())
            self.assertTrue(online.read_text().startswith("[core]\n"))
            self.assertEqual(calls.splitlines(), [
                f"pacman --config {online} --noconfirm -Sy --needed archlinuxcn-keyring",
                "pacman-key --populate archlinuxcn",
            ])

    def test_runtime_without_profiles_builds_a_global_iso(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload, online, calls = self.run_prepare(root / "missing", root)
            self.assertFalse((payload / "regions").exists())
            self.assertNotIn("archlinuxcn", online.read_text())
            self.assertEqual(calls, "")


if __name__ == "__main__":
    unittest.main()
