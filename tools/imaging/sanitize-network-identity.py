#!/usr/bin/env python3
"""Golden-only Ethernet identity sanitation; never executes image code."""
import argparse
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile

PHYSICAL = {'eth0', 'eth1', 'end0', 'end1'}
HOOK = re.compile(r'(?:up|post-up)\s+/home/klogic/adjust-(eth[01]|end[01])(?:\s+\|\|\s+(?:/bin/)?true)?')
MAC = re.compile(r'(?:ether\s+)?(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}')
WRITER = re.compile(r'\bhwaddress\b|\bmacchanger\b|\bip\s+link\s+[^\n;]*\baddress\b|'
                    r'\bifconfig\s+[^\n;]*\bhw\s+ether\b|\bMACAddress\s*=|'
                    r'\b(?:cloned-mac-address|assigned-mac-address)\s*=', re.I)
VENDOR = re.compile(r'/home/klogic(?:/|\b)|\bklogic(?:[-.\s]|$)|\badjust-(?:eth|end)[01]\b|\bset-ip[01]\b|\bset-route\b', re.I)


class Refused(Exception):
    pass


def refuse(path, reason):
    # Never include file contents: vendor configuration can contain secrets.
    raise Refused(f'{path}: {reason}')


def active_lines(text):
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith(('#', ';'))]


class Image:
    def __init__(self, root):
        self.root = Path(os.path.abspath(root))
        if self.root.is_symlink() or not self.root.is_dir():
            refuse('--root', 'expected a real directory')
        if self.root.resolve() != self.root:
            refuse('--root', 'symlink component in root')
        self.count = 0
        self.total = 0

    def path(self, rel, links=False):
        """Resolve symlinks with image-root semantics, never host semantics."""
        pending = list(PurePosixPath('/' + str(rel).lstrip('/')).parts[1:])
        parts = []
        followed = 0
        while pending:
            name = pending.pop(0)
            if name == '..':
                if not parts: refuse(rel, 'path escapes image root')
                parts.pop(); continue
            if name in ('', '.'): continue
            p = self.root.joinpath(*parts, name)
            if p.is_symlink():
                if not links: refuse(rel, 'symlink input')
                followed += 1
                if followed > 20: refuse(rel, 'symlink loop')
                target = os.readlink(p)
                if target == '/dev/null' and not pending:
                    return None  # A systemd mask is data, not a host /dev/null read.
                if target.startswith('/'): parts = []
                pending = list(PurePosixPath(target).parts) + pending
                pending = [x for x in pending if x != '/']
            else:
                parts.append(name)
        return self.root.joinpath(*parts)

    def read(self, rel, *, network=False, links=False):
        p = self.path(rel, links)
        if p is None or not p.exists(): return None
        info = p.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            refuse(rel, 'expected an unshared regular file')
        limit = 32768 if network else 262144
        if info.st_size > limit: refuse(rel, 'file exceeds review limit')
        self.count += 1; self.total += info.st_size
        if self.count > 4096 or self.total > 16 * 1024 * 1024:
            refuse(rel, 'inspection exceeds bounded tree limit')
        try:
            text = p.read_bytes().decode('utf-8')
        except (UnicodeError, OSError):
            refuse(rel, 'cannot read text configuration')
        if '\0' in text or (network and len(text.splitlines()) > 200):
            refuse(rel, 'unsupported network file shape')
        return text

    def entries(self, rel, *, links=False):
        p = self.path(rel, links)
        if p is None or not p.exists(): return []
        if not p.is_dir(): refuse(rel, 'expected directory')
        entries = sorted(p.iterdir(), key=lambda x: x.name)
        if len(entries) > 1024: refuse(rel, 'directory exceeds review limit')
        return [str(PurePosixPath(rel) / e.name) for e in entries]


def network_content(rel, text, apply):
    stanza = None
    seen = set()
    output = []
    mirror = re.fullmatch(r'home/klogic/(eth[01])\.conf', rel)
    for line in text.splitlines(keepends=True):
        value = line.strip()
        if not value or value.startswith('#'):
            output.append(line); continue
        if '\\' in value or '\x00' in value:
            refuse(rel, 'unsupported continuation or escape')
        fields = value.split()
        key = fields[0]
        if key == 'iface':
            if len(fields) != 4 or fields[1] not in PHYSICAL | {'lo'} \
                    or fields[2] not in ('inet', 'inet6') or fields[3] not in ('static', 'dhcp', 'manual', 'loopback'):
                refuse(rel, 'unsupported interface stanza')
            stanza = fields[1]
            signature = tuple(fields[1:3])
            if signature in seen: refuse(rel, 'duplicate interface stanza')
            seen.add(signature)
            if mirror and stanza not in (mirror[1], 'end' + mirror[1][-1]):
                refuse(rel, 'unexpected KLogic mirror stanza')
        elif key in ('source', 'source-directory', 'mapping'):
            if rel != 'etc/network/interfaces' or value != 'source /etc/network/interfaces.d/*.conf':
                refuse(rel, 'unsupported include or mapping')
            stanza = None
        elif key in ('auto', 'allow-auto', 'allow-hotplug', 'no-auto-down'):
            if len(fields) < 2 or not set(fields[1:]) <= PHYSICAL | {'lo'}:
                refuse(rel, 'unsupported interface selection')
            stanza = None
        elif key == 'hwaddress':
            if stanza not in PHYSICAL or not MAC.fullmatch(value[len(key):].strip()):
                refuse(rel, 'ambiguous MAC override')
            if not apply: refuse(rel, 'active donor MAC override')
            continue
        elif HOOK.fullmatch(value):
            if stanza not in PHYSICAL: refuse(rel, 'KLogic hook outside physical stanza')
            if not apply: refuse(rel, 'active KLogic ifup hook')
            continue
        elif WRITER.search(value) or VENDOR.search(value):
            refuse(rel, 'unrecognized network identity writer')
        elif stanza is None:
            refuse(rel, 'option outside supported stanza')
        output.append(line)
    if mirror and not seen: refuse(rel, 'unrecognized KLogic mirror format')
    return ''.join(output)


def unit_fields(text, rel):
    """Read systemd logical lines before interpreting assignment values.

    systemd.syntax(5): odd trailing backslashes join physical lines with a
    space; intervening comment lines are ignored. No shell code is executed.
    """
    values = {}
    pending = ''
    section = None
    for raw in text.removeprefix('\ufeff').splitlines():
        if raw.lstrip().startswith(('#', ';')): continue
        line = pending + raw
        backslashes = len(line) - len(line.rstrip('\\'))
        if backslashes % 2:
            pending = line[:-1] + ' '
            continue
        pending = ''
        line = line.strip()
        if not line: continue
        if re.fullmatch(r'\[[A-Za-z][A-Za-z0-9]*\]', line):
            section = line[1:-1]
        elif section is not None and re.match(r'^[A-Za-z][A-Za-z0-9]*\s*=', line):
            key, value = line.split('=', 1)
            values.setdefault((section, key.strip()), []).append(value.strip())
        else:
            refuse(rel, 'unsupported systemd logical line')
    if pending: refuse(rel, 'unfinished systemd continuation')
    return values


def check_boot_entries(image):
    # Load unit definitions plus drop-ins in increasing precedence. Disabled
    # vendor software is retained, but every enabled dependency edge is checked.
    definitions = {}; edges = {}; seeds = {'default.target'}; masked = set()
    for base in ('usr/lib/systemd/system', 'lib/systemd/system', 'etc/systemd/system'):
        for rel in image.entries(base, links=True):
            name = PurePosixPath(rel).name
            if name.endswith(('.wants', '.requires', '.upholds')):
                for child in image.entries(rel, links=True):
                    target = PurePosixPath(child).name
                    if image.path(child, links=True) is not None:
                        seeds.add(target)
                        edges.setdefault(name.rsplit('.', 1)[0], set()).add(target)
            elif name.endswith(('.service', '.path', '.timer', '.socket', '.target')):
                text = image.read(rel, links=True)
                if image.path(rel, links=True) is None:
                    masked.add(name); definitions.pop(name, None)
                elif text is not None:
                    masked.discard(name); definitions[name] = (rel, [(rel, text)])
    dropins = {}
    for base in ('usr/lib/systemd/system', 'lib/systemd/system', 'etc/systemd/system'):
        for rel in image.entries(base, links=True):
            if not rel.endswith('.d'): continue
            name = PurePosixPath(rel).name[:-2]
            for child in image.entries(rel, links=True):
                if child.endswith('.conf'):
                    text = image.read(child, links=True)
                    if text is not None:
                        dropins.setdefault(name, []).append((child, text))
    # An enabled instance may exist only as a .wants link to its template.
    # Preserve its own name while inspecting instance-specific drop-ins.
    for name in dropins:
        if name in definitions or name in masked or '@' not in name or '@.' in name or '.' not in name:
            continue
        template = name.split('@', 1)[0] + '@.' + name.rsplit('.', 1)[1]
        if template in definitions:
            rel, fragments = definitions[template]
            definitions[name] = (rel, list(fragments))
    def scopes(name):
        stem, kind = name.rsplit('.', 1)
        result = {name, kind}
        if '@' in stem: result.add(stem.split('@', 1)[0] + '@.' + kind)
        while '-' in stem:
            stem = stem.rsplit('-', 1)[0]
            result.add(stem + '-.' + kind)
        return result
    # systemd.unit(5): aliases share drop-ins; type, template and dash-prefix
    # scopes are inherited. Conservatively retain all assignments: an override
    # cannot silently hide a suspicious writer from this preparation guard.
    def canonical_name(name, rel):
        canonical = image.path(rel, links=True).name
        if '@' in name and '@.' not in name and '@.' in canonical:
            instance = name.split('@', 1)[1].rsplit('.', 1)[0]
            canonical = canonical.replace('@.', '@' + instance + '.', 1)
        return canonical
    aliases = {}
    for name, (rel, _) in definitions.items():
        canonical = canonical_name(name, rel)
        aliases.setdefault(canonical, set()).update((name, canonical))
        # Alias symlink targets identify a unit name, not a permission to
        # bypass its higher-priority /etc definition with the /usr/lib file.
        if canonical != name: edges.setdefault(name, set()).add(canonical)
    for name, (rel, fragments) in definitions.items():
        inherited = set()
        for alias in aliases[canonical_name(name, rel)]: inherited.update(scopes(alias))
        for scope in sorted(inherited): fragments.extend(dropins.get(scope, ()))
    dangerous = {}; ambiguous = {}
    for name, (rel, fragments) in definitions.items():
        fields = {}
        for path, text in fragments:
            # A continuation never crosses a physical file boundary.
            for key, values in unit_fields(text, path).items():
                fields.setdefault(key, []).extend(values)
        commands = '\n'.join(v for (_, k), vals in fields.items()
                             if k.startswith('Exec') or k in ('WorkingDirectory', 'PathChanged', 'PathModified') for v in vals)
        if '\\' in commands:
            ambiguous[name] = rel
        if VENDOR.search(name) or VENDOR.search(commands) or WRITER.search(commands):
            dangerous[name] = rel
        def add_edges(section, key):
            for value in fields.get((section, key), []):
                # C escapes/quoted unit names need full systemd expansion;
                # refuse a reachable ambiguous edge instead of losing it.
                if any(c in value for c in '\\"\''):
                    ambiguous[name] = rel
                else:
                    edges.setdefault(name, set()).update(value.split())
        for key in ('Wants', 'Requires', 'BindsTo', 'Upholds', 'OnFailure', 'OnSuccess'):
            add_edges('Unit', key)
        if name.endswith('.service'):
            add_edges('Service', 'Sockets')
        if name.endswith(('.path', '.timer')):
            section = 'Path' if name.endswith('.path') else 'Timer'
            add_edges(section, 'Unit')
            if not any(fields.get((section, 'Unit'), [])):
                edges.setdefault(name, set()).add(name.rsplit('.', 1)[0] + '.service')
        if name.endswith('.socket'):
            accept = fields.get(('Socket', 'Accept'), ['no'])[-1].lower()
            if accept not in ('yes', 'true', 'on', '1', 'no', 'false', 'off', '0'):
                ambiguous[name] = rel
            elif accept in ('yes', 'true', 'on', '1'):
                if any(fields.get(('Socket', 'Service'), [])): ambiguous[name] = rel
                edges.setdefault(name, set()).add(name[:-7] + '@.service')
            else:
                add_edges('Socket', 'Service')
                if not any(fields.get(('Socket', 'Service'), [])):
                    edges.setdefault(name, set()).add(name[:-7] + '.service')
    visited = set()
    while seeds:
        name = seeds.pop()
        if name in visited or name in masked: continue
        visited.add(name)
        # Dependency names can select an instance without a top-level alias.
        if name not in definitions and '@' in name and '.' in name:
            template = name.split('@', 1)[0] + '@.' + name.rsplit('.', 1)[1]
            if template in definitions or template in masked:
                seeds.add(template)
                continue
        if name in ambiguous:
            refuse(ambiguous[name], 'unsupported systemd activation value')
        if '%' in name:
            refuse(name, 'unresolved systemd activation specifier')
        if name in dangerous or VENDOR.search(name):
            refuse(dangerous.get(name, name), 'enabled network identity regeneration entry')
        seeds.update(edges.get(name, ()))

    for base in ('etc/udev/rules.d', 'usr/lib/udev/rules.d', 'lib/udev/rules.d',
                 'etc/cron.d', 'etc/cron.hourly', 'etc/cron.daily', 'etc/cron.weekly',
                 'etc/cron.monthly', 'var/spool/cron/crontabs',
                 'etc/network/if-pre-up.d', 'etc/network/if-up.d',
                 'etc/network/if-down.d', 'etc/network/if-post-down.d'):
        for rel in image.entries(base, links=True):
            text = image.read(rel, links=True)
            if text is None: continue
            active = '\n'.join(active_lines(text))
            if VENDOR.search(active) or WRITER.search(active) or re.search(r'ATTR\{address\}\s*==', active):
                refuse(rel, 'unresolved network identity trigger')
    for rel in ('etc/crontab', 'etc/rc.local'):
        text = image.read(rel)
        if text and (VENDOR.search('\n'.join(active_lines(text))) or WRITER.search('\n'.join(active_lines(text)))):
            refuse(rel, 'active legacy network identity trigger')
    for level in ('S', '0', '1', '2', '3', '4', '5', '6'):
        for rel in image.entries('etc/rc' + level + '.d'):
            if not PurePosixPath(rel).name.startswith('S'): continue
            text = image.read(rel, links=True)
            if text and (VENDOR.search(rel) or VENDOR.search('\n'.join(active_lines(text))) or WRITER.search(text)):
                refuse(rel, 'enabled init network identity trigger')

    for base in ('etc/systemd/network', 'usr/lib/systemd/network', 'lib/systemd/network'):
        for rel in image.entries(base, links=True):
            if re.fullmatch(r'etc/systemd/network/1[01]-(?:eth|end)[01]\.link', rel):
                refuse(rel, 'retired interface naming file must be absent')
            text = image.read(rel, links=True)
            if text is None: continue
            active = '\n'.join(active_lines(text))
            if re.search(r'\b(?:PermanentMACAddress|MACAddress)\s*=', active):
                refuse(rel, 'residual MAC-matched or fixed-MAC network policy')
            if re.search(r'\bMACAddressPolicy\s*=', active):
                # The distro default does not contain a donor value. Custom
                # policy belongs to explicit review, never automatic deletion.
                if not (rel in ('usr/lib/systemd/network/99-default.link', 'lib/systemd/network/99-default.link')
                        and re.findall(r'^MACAddressPolicy=(.*)$', active, re.M) == ['persistent']):
                    refuse(rel, 'custom MAC generation policy')


def plan(image, apply):
    rels = ['etc/network/interfaces']
    rels += [p for p in image.entries('etc/network/interfaces.d') if p.endswith('.conf')]
    if len(rels) > 33: refuse('etc/network/interfaces.d', 'too many network configurations')
    rels += ['home/klogic/eth0.conf', 'home/klogic/eth1.conf']
    edits = []
    for rel in rels:
        text = image.read(rel, network=True)
        if text is None:
            if rel == 'etc/network/interfaces': refuse(rel, 'required network root is absent')
            continue
        updated = network_content(rel, text, apply)
        network_content(rel, updated, False)
        if updated != text: edits.append((rel, text, updated))
    check_boot_entries(image)
    return edits


def replace_all(image, edits):
    staged = []
    try:
        for rel, before, after in edits:
            p = image.path(rel); info = p.stat()
            fd, temporary = tempfile.mkstemp(prefix='.' + p.name + '.identity-', dir=p.parent)
            staged.append((p, temporary, info, before))
            with os.fdopen(fd, 'wb') as stream:
                if (os.geteuid(), os.getegid()) != (info.st_uid, info.st_gid):
                    os.fchown(stream.fileno(), info.st_uid, info.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
                stream.write(after.encode('utf-8')); stream.flush(); os.fsync(stream.fileno())
        for p, temporary, info, before in staged:
            current = p.lstat()
            if (current.st_ino, current.st_dev, current.st_mtime_ns, current.st_size, current.st_nlink) != \
                    (info.st_ino, info.st_dev, info.st_mtime_ns, info.st_size, 1) or p.read_bytes() != before.encode('utf-8'):
                refuse(p.relative_to(image.root), 'configuration changed during sanitation')
            os.replace(temporary, p)
            fd = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
    finally:
        for _, temporary, _, _ in staged:
            try: os.unlink(temporary)
            except FileNotFoundError: pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        image = Image(args.root)
        edits = plan(image, args.apply)
        if args.apply:
            replace_all(image, edits)
            plan(Image(args.root), False)
        print(f'network identity guard: clean; changed_files={len(edits)}; vendor provenance requires preparation evidence')
        return 0
    except (Refused, OSError) as error:
        reason = str(error) if isinstance(error, Refused) else \
            f'{error.filename or args.root}: filesystem operation failed (errno {error.errno})'
        print('network identity guard: REFUSED: ' + reason, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
