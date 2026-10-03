"""Scope Codex's persistent sibling-directory denials to one native process tree."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4


# Only these two native read-deny ACE shapes may be removed, and only when
# absent from the snapshot. Existing rules and all non-experiment paths stay intact.
ACL_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$inputData = [Console]::In.ReadToEnd() | ConvertFrom-Json
$sid = ([System.Security.Principal.NTAccount]::new('CodexSandboxUsers')).Translate([System.Security.Principal.SecurityIdentifier]).Value
$result = @()
foreach ($entry in $inputData.entries) {
    $acl = Get-Acl -LiteralPath $entry.path
    $allows = @($acl.Access | Where-Object {
        $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq $sid -and
        $_.AccessControlType -eq 'Allow' -and -not $_.IsInherited
    } | ForEach-Object { @{rights=[int]$_.FileSystemRights; inheritance=[int]$_.InheritanceFlags; propagation=[int]$_.PropagationFlags} })
    $matches = @($acl.Access | Where-Object {
        $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq $sid -and
        $_.AccessControlType -eq 'Deny' -and -not $_.IsInherited -and
        (([int]$_.FileSystemRights -eq 1179785 -and [int]$_.InheritanceFlags -eq 0 -and [int]$_.PropagationFlags -eq 0) -or
         ([int]$_.FileSystemRights -eq -2146303863 -and [int]$_.InheritanceFlags -eq 3 -and [int]$_.PropagationFlags -eq 2))
    })
    $keys = @($matches | ForEach-Object { '{0}:{1}:{2}' -f [int]$_.FileSystemRights,[int]$_.InheritanceFlags,[int]$_.PropagationFlags })
    $removed = @()
    $restored = @()
    if ($inputData.mode -eq 'restore') {
        if ($acl.Owner -ne $entry.owner) { throw 'Experiment directory owner changed during execution' }
        foreach ($rule in $matches) {
            $key = '{0}:{1}:{2}' -f [int]$rule.FileSystemRights,[int]$rule.InheritanceFlags,[int]$rule.PropagationFlags
            if ($key -notin $entry.keys) {
                $acl.RemoveAccessRuleSpecific($rule)
                $removed += $key
            }
        }
        # Native deny setup can remove an existing explicit grant for this same
        # group. Restore only grants captured before this process was started.
        foreach ($grant in $entry.allows) {
            $present = @($allows | Where-Object { $_.rights -eq $grant.rights -and $_.inheritance -eq $grant.inheritance -and $_.propagation -eq $grant.propagation })
            if (-not $present.Count) {
                $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
                    [System.Security.Principal.SecurityIdentifier]::new($sid),
                    [System.Security.AccessControl.FileSystemRights]$grant.rights,
                    [System.Security.AccessControl.InheritanceFlags]$grant.inheritance,
                    [System.Security.AccessControl.PropagationFlags]$grant.propagation,
                    [System.Security.AccessControl.AccessControlType]::Allow)
                $acl.AddAccessRule($rule)
                $restored += $grant
            }
        }
        if ($removed.Count -or $restored.Count) {
            $expected = $acl.Sddl
            Set-Acl -LiteralPath $entry.path -AclObject $acl
            if ((Get-Acl -LiteralPath $entry.path).Sddl -ne $expected) { throw 'Unexpected ACL change after scoped cleanup' }
        }
    }
    $result += @{path=$entry.path; owner=$acl.Owner; sandbox_sid=$sid; keys=$keys; allows=$allows; removed=$removed; restored=$restored; sddl=$acl.Sddl}
}
ConvertTo-Json -InputObject $result -Depth 6 -Compress
'''


def acl_snapshot(mode, entries):
    result = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
                             '-Command', ACL_SCRIPT], input=json.dumps({'mode': mode, 'entries': entries}),
                            text=True, encoding='utf-8', errors='replace', capture_output=True, timeout=60,
                            env={key: value for key, value in os.environ.items() if key.upper() != 'PSMODULEPATH'},
                            creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError('Native experiment ACL operation failed: ' + result.stderr[-2000:])
    return json.loads(result.stdout)


@contextmanager
def scoped_denials(root, paths, *, timeout, cancelled):
    """Serialize product sandbox calls; clean only newly added sibling denials."""
    import msvcrt
    root = Path(root).resolve()
    directory = root / 'data/native-acl'
    directory.mkdir(parents=True, exist_ok=True)
    # A file lock also covers distinct Workbench processes on the same machine.
    deadline = time.monotonic() + timeout
    with (directory / 'execution.lock').open('a+b') as lock:
        lock.seek(0)
        while True:
            if cancelled() or time.monotonic() >= deadline:
                raise RuntimeError('Native sandbox cancelled or timed out while awaiting isolation lock')
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(.05)
        journal = None
        try:
            for previous in directory.glob('*.json'):
                if json.loads(previous.read_text(encoding='utf-8')).get('status') != 'restored':
                    raise RuntimeError('Unfinished native ACL cleanup requires review: ' + str(previous))
            identities = {}
            for path in dict.fromkeys(map(Path, paths)):
                if not path.is_relative_to(root / 'experiments') or path == root / 'experiments':
                    raise ValueError('Transient ACL path must be inside experiments')
                for parent in (path, *path.parents):
                    if parent == root:
                        break
                    if parent.lstat().st_file_attributes & 0x400:
                        raise ValueError('Transient ACL path must not traverse a reparse point')
                info = path.stat()
                identities[str(path)] = (info.st_dev, info.st_ino)
            before = acl_snapshot('snapshot', [{'path': p} for p in identities]) if identities else []
            if before:
                journal = directory / (uuid4().hex + '.json')
                journal.write_text(json.dumps({'status': 'running', 'before': before}, indent=2), encoding='utf-8')
            try:
                yield max(0, deadline - time.monotonic())
            finally:
                for path, identity in identities.items():
                    info = Path(path).stat()
                    if (info.st_dev, info.st_ino) != identity:
                        raise RuntimeError('Experiment directory replaced; refusing ACL cleanup')
                after = acl_snapshot('restore', before) if before else []
                if journal:
                    journal.write_text(json.dumps({'status': 'restored', 'before': before, 'after': after},
                                                  indent=2), encoding='utf-8')
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
