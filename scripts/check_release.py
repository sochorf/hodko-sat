"""Offline inventory/privacy check. Publication remains a separate explicit step."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def require(condition, message):
    if not condition:
        raise ValueError(message)


def inventory(root):
    result = {}
    for p in root.rglob('*'):
        require(not p.is_symlink(), 'Symlinks are not allowed')
        if p.is_file() and '__pycache__' not in p.parts and not (root.name == 'tests' and p.relative_to(root).parts[0] == 'testing_config'):
            result[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def check(root, *, publication=False, tag=None):
    policy = json.loads((root / 'release-policy.json').read_text())
    component = root / 'custom_components/sat'
    manifest = json.loads((component / 'manifest.json').read_text())
    require(manifest['domain'] == 'sat', 'Domain changed')
    require(manifest['version'] == policy['version'], 'Version differs from policy')
    require(re.fullmatch(r'\d+\.\d+\.\d+\.\d+', manifest['version']), 'Expected numeric fork revision')
    if tag is not None:
        require(tag == manifest['version'], 'Tag must match manifest')
    metadata = {'version', 'documentation', 'issue_tracker', 'codeowners'}
    require({k: v for k, v in manifest.items() if k not in metadata} == {
        k: v for k, v in policy['original_manifest'].items() if k not in metadata
    }, 'Unreviewed manifest behavior/dependency change')
    actual = inventory(component)
    require(actual == policy['component_hashes'], 'Changed/missing/extra reviewed component bytes')
    before = policy['baseline_component_hashes']
    require(set(before) - set(actual) == set(policy['removed_local_files']), 'Removal inventory changed')
    require(sorted(k for k in actual if actual[k] != before.get(k)) == policy['changed_component_files'], 'Unexpected .51 delta')
    require(policy['upstream_commit'] == 'a23f3d6a0150986d769ebd5b484d5c2209aae30d', 'Unreviewed upstream base')
    for name, digest in policy['preserved_ally_core'].items():
        require(actual.get(name) == digest, 'Changed preserved PI/HR core: ' + name)
    require(inventory(root / 'tests') == policy['regression_hashes'], 'Changed/missing/extra regression files')
    require(json.loads((root / 'hacs.json').read_text())['homeassistant'] == '2026.11.2', 'Minimum HA changed')
    require((root / 'LICENSE').is_file() and (component / 'LICENSE').is_file(), 'License missing')
    # Inspect only publishable paths. Never traverse .git objects or a local venv.
    names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z',
                                     '--cached', '--others', '--exclude-standard']).decode().split('\0')
    for name in set(filter(None, names)):
        path = root / name
        if not path.exists():  # a reviewed deletion from upstream
            continue
        require(not path.is_symlink(), 'Publishable symlink')
        require(not any(s in Path(name).parts for s in ('.storage', '.env', '.sat-lab')), 'Private path')
        text = path.read_bytes()
        require(not re.search(rb'/Users/[A-Za-z0-9_-]+/|192\.168\.\d+\.\d+|(?i:bearer)\s+eyJ|(?i:password)\s*[:=]\s*["\'][^"\']{8}', text), 'Private host/user/credential pattern: ' + name)
        # The fork must never initialize the original project's Sentry client.
        if name.startswith('custom_components/'):
            require(b'ingest.de.sentry.io' not in text, 'Upstream Sentry endpoint')
    if publication:
        require(policy.get('publication_ready') is True, 'Publication is not approved for this offline candidate')
        repo = policy['published_repository']
        require(isinstance(repo, str) and re.fullmatch(r'[A-Za-z0-9-]+/hodko-sat', repo), 'Public owner/repository not approved')
        require(manifest['documentation'] == 'https://github.com/' + repo, 'Wrong documentation URL')
        require(manifest['issue_tracker'] == 'https://github.com/' + repo + '/issues', 'Wrong issue URL')
    return policy


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--for-publication', action='store_true')
    parser.add_argument('--tag')
    args = parser.parse_args()
    p = check(Path(__file__).resolve().parents[1], publication=args.for_publication, tag=args.tag)
    print(f"Verified {p['version']}: {len(p['component_hashes'])} reviewed files; publication={args.for_publication}")
