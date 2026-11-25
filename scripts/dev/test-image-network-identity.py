#!/usr/bin/env python3
# comment-mutation-proof-exempt: runs the shipped sanitizer and offline guard on real temporary trees; binding assertions include omission/fatality mutations within this suite.
"""Golden network guard: real filesystem behavior and mandatory offline call."""
import importlib.util
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT/'tools/imaging/sanitize-network-identity.py'
PATCHER = ROOT/'tools/imaging/patch-firstboot-image.sh'
BASE = 'auto lo\niface lo inet loopback\nsource /etc/network/interfaces.d/*.conf\n'
CONF = ('# hwaddress is only a comment\nauto eth0\niface eth0 inet static\n'
        '    address 192.168.1.136\n    netmask 255.255.255.0\n    gateway 192.168.1.5\n'
        '    dns-nameservers 1.1.1.1 8.8.8.8\n    metric 100\n    mtu 1500\n')
DIRTY = CONF + '    hwaddress ether 02:53:f9:42:bc:1f\n    post-up /home/klogic/adjust-eth0 || /bin/true\n'


def guard_body(source):
    start = source.index('sanitize_network_identity() {')
    end = source.index('\n}\n', start) + 3
    body = source[start:end]
    outside = source[:start] + source[end:]
    active = '\n'.join(line for line in outside.splitlines() if not line.lstrip().startswith('#'))
    if len(re.findall(r'^sanitize_network_identity "\$mnt"$', active, re.M)) != 1:
        raise AssertionError('offline guard must be invoked unconditionally exactly once')
    if '|| true' in body: raise AssertionError('guard must fail fatally')
    for flag in ('--apply', '--check'):
        commands = '\n'.join(line for line in body.splitlines() if not line.lstrip().startswith('#'))
        if len(re.findall(r'python3 "\$SCRIPT_DIR/sanitize-network-identity.py" --root "\$1" ' + flag, commands)) != 1:
            raise AssertionError('both real sanitizer modes are mandatory')
    return body


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'image'; self.root.mkdir()
        self.write('etc/network/interfaces', BASE)
        self.write('etc/network/interfaces.d/eth0.conf', DIRTY)

    def write(self, rel, text):
        p = self.root/rel; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text); return p

    def run_helper(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(HELPER), '--root', str(self.root), *args],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        if expected: self.assertNotIn('guard: clean', result.stdout)
        return result

    def clean(self):
        self.write('etc/network/interfaces.d/eth0.conf', CONF)

    def test_default_check_is_read_only_then_apply_preserves_fields_metadata_and_is_idempotent(self):
        p = self.root/'etc/network/interfaces.d/eth0.conf'; p.chmod(0o640)
        mirror = self.write('home/klogic/eth0.conf', DIRTY)
        original = p.stat()
        self.run_helper(expected=1)
        self.assertEqual(p.read_text(), DIRTY); self.assertEqual(mirror.read_text(), DIRTY)
        self.run_helper('--apply'); self.run_helper('--check')
        self.assertEqual(p.read_text(), CONF); self.assertEqual(mirror.read_text(), CONF)
        final = p.stat()
        self.assertEqual((stat.S_IMODE(final.st_mode), final.st_uid, final.st_gid),
                         (0o640, original.st_uid, original.st_gid))
        self.run_helper('--apply')
        self.assertEqual(p.stat().st_ino, final.st_ino, 'clean apply should not replace files')
        self.assertEqual(list(p.parent.glob('.*.identity-*')), [])

    def test_bare_and_ether_mac_and_known_hook_variants_only(self):
        for iface in ('eth0', 'eth1', 'end0', 'end1'):
            for prefix in ('', 'ether '):
                for hook in ('up', 'post-up'):
                    with self.subTest(iface=iface, prefix=prefix, hook=hook):
                        content = CONF.replace('eth0', iface)
                        self.write('etc/network/interfaces.d/eth0.conf', content +
                                   f'    hwaddress {prefix}02:53:8b:00:d4:30\n    {hook} /home/klogic/adjust-{iface} || true\n')
                        self.run_helper('--apply')
                        self.assertEqual((self.root/'etc/network/interfaces.d/eth0.conf').read_text(), content)

    def test_unrelated_route_hook_and_inert_backup_are_preserved(self):
        route = '    post-up ip route add 10.2.0.0/16 via 192.168.1.5\n'
        self.write('etc/network/interfaces.d/eth0.conf', DIRTY + route)
        backup = self.write('etc/network/interfaces.d/eth0.conf.bak', DIRTY)
        self.run_helper('--apply')
        self.assertEqual((self.root/'etc/network/interfaces.d/eth0.conf').read_text(), CONF + route)
        self.assertEqual(backup.read_text(), DIRTY)

    def test_unsafe_configuration_is_refused_before_any_edit(self):
        cases = ('source /etc/network/interfaces.d/*\n', 'source /outside/*.conf\n',
                 'source-directory /etc/network/interfaces.d\n', 'mapping eth0\n',
                 'iface custom0 inet static\n', '    mtu 1500 \\\n',
                 '    pre-up ip link set dev eth0 address 02:53:8b:00:d4:30\n',
                 '    post-up /home/klogic/adjust-eth0 --klogic\n')
        for suffix in cases:
            with self.subTest(suffix=suffix.split()[0]):
                self.write('etc/network/interfaces.d/eth1.conf', CONF.replace('eth0', 'eth1') + suffix)
                self.run_helper('--apply', expected=1)
                self.assertEqual((self.root/'etc/network/interfaces.d/eth0.conf').read_text(), DIRTY)

    def test_symlink_hardlink_fifo_and_limits_are_refused(self):
        p = self.root/'etc/network/interfaces.d/eth0.conf'
        outside = Path(self.tmp.name)/'host-conf'; outside.write_text(DIRTY)
        p.unlink(); p.symlink_to(outside)
        self.run_helper('--apply', expected=1); self.assertEqual(outside.read_text(), DIRTY)
        p.unlink(); os.link(outside, p)
        self.run_helper('--apply', expected=1); self.assertEqual(outside.read_text(), DIRTY)
        p.unlink(); os.mkfifo(p)
        self.run_helper('--apply', expected=1)
        p.unlink(); p.write_text(CONF + '# comment\n' * 201)
        self.run_helper('--apply', expected=1)
        p.write_text(CONF + '#' + 'x' * 32768)
        self.run_helper('--apply', expected=1)

    def test_parent_symlink_and_in_image_absolute_symlink_do_not_touch_host(self):
        self.clean()
        (self.root/'home').symlink_to('/tmp')
        self.run_helper('--apply', expected=1)
        (self.root/'home').unlink()
        self.write('etc/systemd/system/alias.service', '[Service]\nExecStart=/home/klogic/adjust-eth0 --klogic\n')
        enabled = self.root/'etc/systemd/system/multi-user.target.wants/alias.service'
        enabled.parent.mkdir(); enabled.symlink_to('/etc/systemd/system/alias.service')
        self.run_helper('--apply', expected=1)

    def vendor(self):
        # Inert provenance fixture mirrors the observed fixed-MAC regenerator.
        self.write('home/klogic/adjust-eth0', '#!/bin/sh\ncat <<EOF\nhwaddress ether 02:53:8B:00:D4:30\npost-up /home/klogic/adjust-eth0\nEOF\n')
        self.write('etc/systemd/system/klogic.service', '[Service]\nExecStartPre=/home/klogic/pre-start\nExecStart=/home/klogic/klogic-sa02\n')
        self.write('etc/systemd/system/sa02m-klogic-net-sync.service', '[Service]\nExecStart=/home/klogic/adjust-eth0 --klogic\n')
        self.write('etc/systemd/system/sa02m-klogic-net-sync.path', '[Path]\nPathChanged=/home/klogic/set-ip0\nPathChanged=/home/klogic/set-route\nUnit=sa02m-klogic-net-sync.service\n')

    def test_disabled_daemon_cannot_conceal_enabled_path_watcher(self):
        self.vendor()
        link = self.root/'etc/systemd/system/multi-user.target.wants/sa02m-klogic-net-sync.path'
        link.parent.mkdir(); link.symlink_to('../sa02m-klogic-net-sync.path')
        self.run_helper('--apply', expected=1)
        self.assertEqual((self.root/'etc/network/interfaces.d/eth0.conf').read_text(), DIRTY)
        link.unlink(); before = (self.root/'home/klogic/adjust-eth0').read_bytes()
        self.run_helper('--apply')
        self.assertEqual((self.root/'home/klogic/adjust-eth0').read_bytes(), before)

    def test_timer_dependency_and_mask_are_resolved_in_image(self):
        self.vendor()
        self.write('etc/systemd/system/custom.timer', '[Timer]\nUnit=sa02m-klogic-net-sync.service\n')
        link = self.root/'etc/systemd/system/timers.target.wants/custom.timer'
        link.parent.mkdir(); link.symlink_to('../custom.timer')
        self.run_helper('--apply', expected=1)
        service = self.root/'etc/systemd/system/sa02m-klogic-net-sync.service'
        service.unlink(); service.symlink_to('/dev/null')
        self.run_helper('--apply')

    def test_other_boot_writers_and_custom_link_policy_are_refused(self):
        for rel, text in (
            ('etc/cron.d/vendor', '* * * * * root /home/klogic/adjust-eth0\n'),
            ('etc/udev/rules.d/vendor.rules', 'ACTION=="add", RUN+="/home/klogic/adjust-eth0"\n'),
            ('etc/rc.local', '#!/bin/sh\n/home/klogic/adjust-eth0\n'),
            ('etc/network/if-up.d/custom', '#!/bin/sh\nip link set dev eth0 address 02:53:8B:00:D4:30\n'),
            ('etc/systemd/network/10-eth0.link', '[Match]\nMACAddress=02:53:8B:00:D4:30\n[Link]\nName=eth0\n'),
            ('etc/systemd/network/custom.link', '[Link]\nMACAddressPolicy=random\n')):
            with self.subTest(path=rel):
                p = self.write(rel, text); self.run_helper('--apply', expected=1); p.unlink()
        init = self.write('etc/init.d/klogic', '#!/bin/sh\n/home/klogic/pre-start\n')
        link = self.root/'etc/rc2.d/S20klogic'; link.parent.mkdir(); link.symlink_to('../init.d/klogic')
        self.run_helper('--apply', expected=1)

    def test_unit_drop_in_dependency_and_unknown_mac_service(self):
        self.vendor()
        self.write('etc/systemd/system/ordinary.service', '[Service]\nExecStart=/bin/true\n')
        self.write('etc/systemd/system/ordinary.service.d/vendor.conf', '[Unit]\nWants=sa02m-klogic-net-sync.service\n')
        link = self.root/'etc/systemd/system/multi-user.target.wants/ordinary.service'
        link.parent.mkdir(); link.symlink_to('../ordinary.service')
        self.run_helper('--apply', expected=1)
        (self.root/'etc/systemd/system/ordinary.service.d/vendor.conf').unlink()
        self.write('etc/systemd/system/ordinary.service', '[Service]\nExecStart=/sbin/ip link set dev eth0 address 02:53:8B:00:D4:30\n')
        self.run_helper('--apply', expected=1)

    def test_usrmerge_and_distro_default_do_not_become_host_reads(self):
        self.clean()
        self.write('usr/lib/systemd/network/99-default.link', '[Link]\nMACAddressPolicy=persistent\nNamePolicy=keep kernel database onboard slot path\n')
        (self.root/'lib').symlink_to('usr/lib')
        self.run_helper('--check')
        self.write('usr/lib/systemd/network/10-fixed.link', '[Link]\nMACAddress=02:53:8B:00:D4:30\n')
        self.run_helper('--apply', expected=1)

    def test_retired_link_file_is_rejected_even_when_masked(self):
        self.clean()
        p = self.root/'etc/systemd/network/10-end0.link'
        p.parent.mkdir(parents=True); p.symlink_to('/dev/null')
        self.run_helper('--apply', expected=1)

    def enable(self, name):
        p = self.root/'etc/systemd/system/multi-user.target.wants'/name
        p.parent.mkdir(parents=True, exist_ok=True); p.symlink_to('../' + name)

    def test_enabled_multiline_exec_cannot_hide_mac_writer(self):
        self.clean()
        self.write('etc/systemd/system/clone-network.service',
                   '[Service]\nExecStart=/bin/sh -c "\\\n'
                   ' # skipped comment\n ; skipped comment\n'
                   ' ip link set dev eth0 address 02:11:22:33:44:55"\n')
        self.enable('clone-network.service')
        self.run_helper('--check', expected=1)

    def test_continued_wants_cannot_hide_vendor_dependency(self):
        self.clean(); self.vendor()
        self.write('etc/systemd/system/boot-setup.service',
                   '[Unit]\nWants=benign.service \\\n ; skipped comment\n klogic.service\n'
                   '[Service]\nExecStart=/bin/true\n')
        self.write('etc/systemd/system/benign.service', '[Service]\nExecStart=/bin/true\n')
        self.enable('boot-setup.service')
        self.run_helper('--check', expected=1)

    def test_default_target_requires_is_an_active_boot_root(self):
        self.clean()
        self.write('etc/systemd/system/default.target', '[Unit]\nRequires=clone-network.service\n')
        self.write('etc/systemd/system/clone-network.service',
                   '[Service]\nExecStart=/usr/sbin/ip link set dev eth0 address 02:11:22:33:44:55\n')
        self.run_helper('--check', expected=1)

    def test_socket_service_override_reaches_its_actual_writer(self):
        self.clean()
        self.write('etc/systemd/system/maintenance.socket',
                   '[Socket]\nListenStream=12345\nService=clone-network.service\n')
        self.write('etc/systemd/system/clone-network.service',
                   '[Service]\nExecStart=/usr/sbin/ip link set dev eth0 address 02:11:22:33:44:55\n')
        self.enable('maintenance.socket')
        self.run_helper('--check', expected=1)

    def test_accept_socket_activates_service_template(self):
        self.clean()
        self.write('etc/systemd/system/maintenance.socket', '[Socket]\nListenStream=12345\nAccept=yes\n')
        self.write('etc/systemd/system/maintenance@.service',
                   '[Service]\nExecStart=/usr/sbin/ip link set dev eth0 address 02:11:22:33:44:55\n')
        self.enable('maintenance.socket'); self.run_helper('--check', expected=1)

    def test_enabled_instance_resolves_template_without_top_level_alias(self):
        self.clean()
        self.write('etc/systemd/system/maintenance@.service',
                   '[Service]\nExecStart=/usr/sbin/ip link set dev eth0 address 02:11:22:33:44:55\n')
        p = self.root/'etc/systemd/system/multi-user.target.wants/maintenance@port.service'
        p.parent.mkdir(); p.symlink_to('../maintenance@.service')
        self.run_helper('--check', expected=1)

    def test_alias_cannot_hide_higher_priority_canonical_definition(self):
        self.clean()
        self.write('usr/lib/systemd/system/maintenance.service', '[Service]\nExecStart=/bin/true\n')
        self.write('etc/systemd/system/maintenance.service',
                   '[Service]\nExecStart=/usr/sbin/ip link set dev eth0 address 02:11:22:33:44:55\n')
        (self.root/'etc/systemd/system/alias.service').symlink_to('/usr/lib/systemd/system/maintenance.service')
        self.enable('alias.service')
        self.run_helper('--check', expected=1)

    def test_instance_only_dropin_survives_template_fallback(self):
        self.clean()
        self.write('etc/systemd/system/maintenance@.service', '[Service]\nExecStart=/bin/true\n')
        self.write('etc/systemd/system/maintenance@port.service.d/override.conf',
                   '[Service]\nExecStartPost=/usr/sbin/ip link set dev eth0 address 02:11:22:33:44:55\n')
        p = self.root/'etc/systemd/system/multi-user.target.wants/maintenance@port.service'
        p.parent.mkdir(); p.symlink_to('../maintenance@.service')
        self.run_helper('--check', expected=1)

    def test_inactive_instance_dropin_does_not_taint_other_instance(self):
        self.clean()
        self.write('etc/systemd/system/maintenance@.service', '[Service]\nExecStart=/bin/true\n')
        self.write('etc/systemd/system/maintenance@inactive.service.d/override.conf',
                   '[Service]\nExecStartPost=/usr/sbin/ip link set dev eth0 address 02:11:22:33:44:55\n')
        p = self.root/'etc/systemd/system/multi-user.target.wants/maintenance@clean.service'
        p.parent.mkdir(); p.symlink_to('../maintenance@.service')
        self.run_helper('--check')

    def test_clean_alias_keeps_canonical_dropin_identity(self):
        self.clean()
        self.write('usr/lib/systemd/system/maintenance.service', '[Service]\nExecStart=/bin/true\n')
        self.write('etc/systemd/system/maintenance.service', '[Service]\nExecStart=/bin/echo clean\n')
        self.write('etc/systemd/system/alias.service.d/note.conf', '[Unit]\nDescription=Clean alias\n')
        (self.root/'etc/systemd/system/alias.service').symlink_to('/usr/lib/systemd/system/maintenance.service')
        self.enable('maintenance.service'); self.run_helper('--check')
        self.write('etc/systemd/system/alias.service.d/note.conf',
                   '[Service]\nExecStartPost=/usr/sbin/ip link set dev eth0 address 02:11:22:33:44:55\n')
        self.run_helper('--check', expected=1)

    def test_default_target_alias_and_continued_dropin(self):
        self.clean()
        self.write('etc/systemd/system/multi-user.target', '[Unit]\nRequires=maintenance.service\n')
        (self.root/'etc/systemd/system/default.target').symlink_to('multi-user.target')
        self.write('etc/systemd/system/maintenance.service', '[Service]\nExecStart=/bin/true\n')
        self.write('etc/systemd/system/maintenance.service.d/override.conf',
                   '[Service]\nExecStart=\nExecStart=/usr/sbin/ip link set \\\n# comment\n dev eth0 address 02:11:22:33:44:55\n')
        self.run_helper('--check', expected=1)

    def test_alias_type_and_prefix_dropins_cannot_hide_writers(self):
        self.clean()
        self.write('etc/systemd/system/maintenance-one.service', '[Service]\nExecStart=/bin/true\n')
        (self.root/'etc/systemd/system/alias.service').symlink_to('maintenance-one.service')
        self.enable('alias.service')
        for scope in ('maintenance-one.service', 'service', 'maintenance-.service'):
            with self.subTest(scope=scope):
                dropin = self.write(f'etc/systemd/system/{scope}.d/override.conf',
                                    '[Service]\nExecStart=\nExecStart=/usr/sbin/ip link set dev eth0 address 02:11:22:33:44:55\n')
                self.run_helper('--check', expected=1); dropin.unlink()

    def test_logical_line_parser_preserves_clean_continuations_and_comments(self):
        self.clean()
        self.write('etc/systemd/system/default.target', '[Unit]\nWants=benign.service \\\n# klogic.service is only a comment\n ; comment too\n other.service\n')
        self.write('etc/systemd/system/benign.service', '[Service]\nExecStart=/bin/echo "a \\\n# ip link set dev eth0 address 02:11:22:33:44:55\n b"\n')
        self.write('etc/systemd/system/other.service', '[Service]\nExecStart=/bin/true\n')
        self.run_helper('--check')

    def test_unresolved_active_dependency_and_unfinished_continuation_fail_closed(self):
        self.clean()
        self.write('etc/systemd/system/default.target', '[Unit]\nWants="custom.service"\n')
        self.run_helper('--check', expected=1)
        self.write('etc/systemd/system/default.target', '[Unit]\nWants=custom.service \\\n')
        self.write('etc/systemd/system/default.target.d/override.conf', '[Unit]\nDescription=Cannot complete another file\n')
        self.run_helper('--check', expected=1)

    def test_upholds_and_service_socket_edges_are_not_ordering_only(self):
        self.clean()
        self.write('etc/systemd/system/ordinary.service', '[Service]\nExecStart=/bin/true\nSockets=maintenance.socket\n')
        self.write('etc/systemd/system/maintenance.socket', '[Socket]\nListenStream=12345\nService=writer.service\n')
        self.write('etc/systemd/system/writer.service', '[Service]\nExecStart=/sbin/ip link set dev eth0 address 02:11:22:33:44:55\n')
        link = self.root/'etc/systemd/system/multi-user.target.upholds/ordinary.service'
        link.parent.mkdir(); link.symlink_to('../ordinary.service')
        self.run_helper('--check', expected=1)

    def test_escaped_active_command_and_ambiguous_accept_are_refused(self):
        self.clean()
        self.write('etc/systemd/system/ordinary.service', '[Service]\nExecStart=/usr/sbin/\\x69p link set dev eth0 address 02:11:22:33:44:55\n')
        self.enable('ordinary.service'); self.run_helper('--check', expected=1)
        self.write('etc/systemd/system/ordinary.service', '[Service]\nExecStart=/bin/true\n')
        self.write('etc/systemd/system/ordinary.socket', '[Socket]\nListenStream=12345\nAccept=unknown\n')
        self.enable('ordinary.socket'); self.run_helper('--check', expected=1)

    def test_unknown_mirror_and_excess_file_count_are_refused(self):
        mirror = self.write('home/klogic/eth0.conf', 'hwaddress ether 02:53:8B:00:D4:30\n')
        self.run_helper('--apply', expected=1); mirror.unlink()
        for number in range(33): self.write(f'etc/network/interfaces.d/extra-{number}.conf', '# inert\n')
        self.run_helper('--apply', expected=1)

    def test_staging_failure_does_not_edit_any_source(self):
        spec = importlib.util.spec_from_file_location('network_identity', HELPER)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        self.write('home/klogic/eth0.conf', DIRTY)
        image = mod.Image(self.root); edits = mod.plan(image, True)
        original = mod.tempfile.mkstemp
        calls = []
        def failure(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2: raise OSError('simulated full filesystem')
            return original(*args, **kwargs)
        with patch.object(mod.tempfile, 'mkstemp', side_effect=failure):
            with self.assertRaises(OSError): mod.replace_all(image, edits)
        for rel, before, _ in edits:
            p = self.root/rel; self.assertEqual(p.read_text(), before)
            self.assertEqual(list(p.parent.glob('.*.identity-*')), [])

    def test_offline_guard_is_mandatory_real_and_fatal(self):
        source = PATCHER.read_text()
        body = guard_body(source)
        for changed in (source.replace('sanitize_network_identity "$mnt"', '# omitted'),
                        source.replace('sanitize_network_identity "$mnt"', 'true # omitted'),
                        source.replace('    python3 "$SCRIPT_DIR/sanitize-network-identity.py" --root "$1" --check',
                                       '    # omitted check'),
                        source.replace('    python3 "$SCRIPT_DIR/sanitize-network-identity.py" --root "$1" --apply',
                                       '    # omitted apply')):
            with self.assertRaises(AssertionError): guard_body(changed)
        script = 'set -eu\ndie() { echo FATAL >&2; exit 41; }\n' + body + '\nsanitize_network_identity "$1"\necho FINISHED\n'
        env = dict(os.environ, SCRIPT_DIR=str(HELPER.parent))
        result = subprocess.run(['bash', '-c', script, 'guard', str(self.root)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('FINISHED', result.stdout); self.assertEqual((self.root/'etc/network/interfaces.d/eth0.conf').read_text(), CONF)
        self.write('etc/cron.d/vendor', '* * * * * root /home/klogic/adjust-eth0\n')
        result = subprocess.run(['bash', '-c', script, 'guard', str(self.root)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 41); self.assertNotIn('FINISHED', result.stdout)


if __name__ == '__main__':
    unittest.main()
