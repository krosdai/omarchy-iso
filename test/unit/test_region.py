"""Region bootstrap contracts without Docker, pacman, or a running guest."""

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "configs/airootfs/usr/share/omarchy-iso"))
sys.modules.setdefault("orchestrator.archinstall_adapter", types.ModuleType("orchestrator.archinstall_adapter"))
from orchestrator import phases_impl


class RegionTargetTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.target = self.root / "target"
        self.marker = self.root / "omarchy_region"
        self.payload = self.root / "payload"
        for name, value in (("ISO_REGION_FILE", self.marker), ("ISO_REGION_PAYLOAD", self.payload)):
            patch = mock.patch.object(phases_impl, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_global_does_not_need_a_profile_or_change_user_defaults(self):
        phases_impl._stage_region_defaults(self.target)
        self.assertEqual((self.target / "etc/omarchy/region").read_text(), "global\n")
        self.assertFalse((self.target / "etc/skel").exists())

    def test_cn_seeds_skel_without_touching_existing_users_or_locale(self):
        self.marker.write_text("cn\n")
        profile = self.payload / "skel/.config/fcitx5/profile"
        profile.parent.mkdir(parents=True)
        profile.write_text("regional input defaults\n")
        existing = self.target / "home/alice/.config/fcitx5/profile"
        existing.parent.mkdir(parents=True)
        existing.write_text("personal settings\n")
        skel = self.target / "etc/skel"
        skel.mkdir(parents=True)
        (skel / ".bashrc").write_text("original\n")
        (self.target / "etc/locale.conf").write_text("LANG=en_US.UTF-8\n")

        phases_impl._stage_region_defaults(self.target)
        self.assertEqual((skel / ".config/fcitx5/profile").read_text(), "regional input defaults\n")
        self.assertEqual((skel / ".bashrc").read_text(), "original\n")
        self.assertEqual(existing.read_text(), "personal settings\n")
        self.assertEqual((self.target / "etc/locale.conf").read_text(), "LANG=en_US.UTF-8\n")
        self.assertEqual((self.target / "etc/omarchy/region").read_text(), "cn\n")

    def test_bad_region_and_missing_payload_fail_before_target_changes(self):
        for region in ("CN", "chn", "zz", "../cn", "", "cn"):
            with self.subTest(region=region):
                self.marker.write_text(region)
                with self.assertRaises(RuntimeError):
                    phases_impl._stage_region_defaults(self.target)
                self.assertFalse(self.target.exists())

    def test_cn_keyring_is_initialized_before_finalization_for_both_install_modes(self):
        self.marker.write_text("cn\n")
        for deferred in (False, True):
            ctx = types.SimpleNamespace(target=self.target, username="alice", defer_provisioning=deferred)
            with mock.patch.object(phases_impl, "_run_target_setup_command") as run, \
                 mock.patch.object(phases_impl, "_mask_mkinitcpio_pacman_hooks"), \
                 mock.patch.object(phases_impl, "_unmask_mkinitcpio_pacman_hooks"):
                phases_impl.run_system_finalizer(ctx)
                commands = [call.args[1] for call in run.call_args_list]
                self.assertEqual(commands[:2], [["pacman-key", "--init"], ["pacman-key", "--populate", "archlinux", "archlinuxcn"]])
                expected = ["--defer-provisioning"] if deferred else ["--install-user", "alice"]
                self.assertEqual(commands[2], ["/usr/bin/omarchy-apply-system", *expected, "--first-install"])

    def test_keyring_failure_prevents_enabling_online_repositories(self):
        self.marker.write_text("cn\n")
        ctx = types.SimpleNamespace(target=self.target, username="alice", defer_provisioning=False)
        with mock.patch.object(phases_impl, "_run_target_setup_command", side_effect=RuntimeError("keyring failed")) as run:
            with self.assertRaisesRegex(RuntimeError, "keyring failed"):
                phases_impl.run_system_finalizer(ctx)
            self.assertEqual(run.call_count, 1)


class RegionBuildTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / "runtime"
        self.profile = self.runtime / "default/regions/cn"
        (self.profile / "pacman").mkdir(parents=True)
        (self.runtime / "bin").mkdir()
        (self.profile / "skel").mkdir()
        (self.runtime / "bin/omarchy-apply-pacman").touch()
        (self.profile / "packages").write_text("archlinuxcn-keyring\nfcitx5-rime\n")
        (self.profile / "pacman/pacman.conf.append").write_text("[archlinuxcn]\nServer = https://mirrors.ustc.edu.cn/archlinuxcn/$arch\n")
        (self.profile / "pacman/mirrorlist.append").write_text("Server = https://mirrors.ustc.edu.cn/archlinux/$repo/os/$arch\n")
        self.iso = self.root / "iso"
        self.payload = self.iso / "usr/share/omarchy-iso"
        self.payload.mkdir(parents=True)
        self.packages = self.payload / "omarchy-base.packages"
        self.packages.write_text("base\n")
        self.config = self.root / "pacman.conf"
        shutil.copyfile(ROOT / "configs/pacman-online-stable.conf", self.config)
        self.original_config = self.config.read_bytes()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands"
        for command in ("pacman", "pacman-key"):
            stub = self.bin / command
            stub.write_text('#!/bin/bash\nprintf "%s %s\\n" "${0##*/}" "$*" >> "$TEST_LOG"\nif [[ ${0##*/} == pacman ]]; then exit "${TEST_PACMAN_STATUS:-0}"; fi\n')
            stub.chmod(0o755)

    def prepare(self, region, **env):
        return subprocess.run(
            ["bash", str(ROOT / "builder/prepare-region.sh"), region, str(self.runtime), str(self.iso), str(self.config)],
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "TEST_LOG": str(self.log), **env},
            capture_output=True, text=True,
        )

    def test_cn_adds_real_install_targets_and_verifies_keyring_first(self):
        result = self.prepare("cn")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.packages.read_text(), "base\n\narchlinuxcn-keyring\nfcitx5-rime\n")
        self.assertEqual(self.config.read_bytes(), self.original_config + b"\n[archlinuxcn]\nServer = https://mirrors.ustc.edu.cn/archlinuxcn/$arch\n")
        self.assertEqual((self.payload / "region/packages").read_text(), "archlinuxcn-keyring\nfcitx5-rime\n")
        self.assertEqual(self.log.read_text().splitlines(), [f"pacman --config {self.config} --noconfirm -Sy --needed archlinuxcn-keyring", "pacman-key --populate archlinuxcn"])
        with mock.patch.object(phases_impl, "Path", return_value=self.packages), \
             mock.patch.object(phases_impl, "_package_targets", return_value={"runtime": "omarchy-dev", "settings": "omarchy-settings-dev", "nvim": "omarchy-nvim"}):
            self.assertEqual(phases_impl._runtime_package_list(None), ["omarchy-dev", "base", "archlinuxcn-keyring", "fcitx5-rime"])

    def test_global_is_unchanged_without_region_support_in_runtime(self):
        shutil.rmtree(self.runtime)
        result = self.prepare("global")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.packages.read_text(), "base\n")
        self.assertEqual(self.config.read_bytes(), self.original_config)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.payload / "region").exists())

    def test_missing_runtime_support_fails_before_package_operations(self):
        (self.runtime / "bin/omarchy-apply-pacman").unlink()
        result = self.prepare("cn")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--local-source", result.stderr)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.config.read_bytes(), self.original_config)

    def test_profile_without_user_defaults_fails_before_package_operations(self):
        (self.profile / "skel").rmdir()
        result = self.prepare("cn")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.packages.read_text(), "base\n")

    def test_untrusted_keyring_aborts_without_importing_keys(self):
        result = self.prepare("cn", TEST_PACMAN_STATUS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("pacman-key", self.log.read_text())


class RegionMakeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "bin").mkdir()
        shutil.copyfile(ROOT / "bin/omarchy-iso-make", self.root / "bin/omarchy-iso-make")
        for directory in ("configs/airootfs/root", "configs/airootfs/usr/local/bin", "stubs"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "configs/profiledef.sh").touch()
        self.log = self.root / "docker-args"
        stubs = {
            "git": '#!/bin/bash\nexit 0\n',
            "sudo": '#!/bin/bash\necho "unexpected sudo" >&2\nexit 99\n',
            "docker": '''#!/bin/bash
if [[ $1 == version ]]; then exit 0; fi
printf '%s\\n' "$@" > "$TEST_LOG"
touch "$TEST_ROOT/release/omarchy-test-x86_64.iso"
''',
        }
        for name, content in stubs.items():
            stub = self.root / "stubs" / name
            stub.write_text(content)
            stub.chmod(0o755)

    def make(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.startswith("OMARCHY_")}
        return subprocess.run(
            ["bash", "bin/omarchy-iso-make", "--keep-pkg-cache", "--no-boot-offer", *args],
            cwd=self.root, env={**env, "HOME": str(self.root), "PATH": f"{self.root / 'stubs'}:{env['PATH']}", "TEST_LOG": str(self.log), "TEST_ROOT": str(self.root)},
            capture_output=True, text=True,
        )

    def test_region_is_independent_of_channel_and_names_cache_and_artifact(self):
        for flags, channel, ref in (([], "stable", "quattro"), (["--rc"], "rc", "rc"), (["--edge"], "edge", "edge"), (["--dev"], "edge", "dev")):
            with self.subTest(channel=ref):
                release = self.root / "release"
                if release.exists():
                    shutil.rmtree(release)
                result = self.make(*flags, "--region", "cn")
                self.assertEqual(result.returncode, 0, result.stderr)
                args = self.log.read_text().splitlines()
                self.assertIn("OMARCHY_REGION=cn", args)
                self.assertIn(f"OMARCHY_MIRROR={channel}", args)
                self.assertIn(f"OMARCHY_ISO_REF={ref}", args)
                self.assertIn(f"{self.root}/.cache/omarchy/iso_{channel}-cn/airootfs/var/cache/omarchy:/var/cache/airootfs/var/cache/omarchy", args)
                self.assertTrue((release / f"omarchy-test-x86_64-{ref}-cn.iso").exists())

    def test_default_preserves_existing_global_cache_and_name(self):
        result = self.make()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OMARCHY_REGION=global", self.log.read_text().splitlines())
        self.assertIn("/iso_stable/", self.log.read_text())
        self.assertTrue((self.root / "release/omarchy-test-x86_64-quattro.iso").exists())

    def test_local_source_keeps_region_and_mounts_both_checkouts(self):
        for name in ("runtime", "packages"):
            (self.root / name).mkdir()
        result = self.make("--region", "cn", "--local-source", str(self.root / "runtime"), str(self.root / "packages"))
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.log.read_text().splitlines()
        self.assertIn("OMARCHY_REGION=cn", args)
        self.assertIn("OMARCHY_ISO_REF=local", args)
        self.assertIn(f"{self.root}/runtime:/omarchy-source:ro", args)
        self.assertIn(f"{self.root}/packages:/omarchy-pkgs:ro", args)
        self.assertTrue((self.root / "release/omarchy-test-x86_64-local-cn.iso").exists())

    def test_invalid_regions_fail_without_starting_build(self):
        for flags in (("--region",), ("--region", "chn"), ("--region", "CN"), ("--region", "zz"), ("--region", "../cn")):
            with self.subTest(flags=flags):
                result = self.make(*flags)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log.exists())
                self.assertFalse((self.root / "release").exists())


if __name__ == "__main__":
    unittest.main()
