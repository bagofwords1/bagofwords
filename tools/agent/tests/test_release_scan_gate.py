"""Credential-free release workflow regression checks (requires PyYAML)."""
import pathlib
import unittest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]


def workflow(name):
    return yaml.safe_load((ROOT / '.github/workflows' / name).read_text())


class ReleaseGateTests(unittest.TestCase):
    def test_each_architecture_is_gated(self):
        jobs = workflow('docker-image.yml')['jobs']
        self.assertEqual(set(jobs['scan']['strategy']['matrix']['platform_pair']),
                         {'linux-amd64', 'linux-arm64'})
        gates = [s for s in jobs['scan']['steps'] if s.get('id') == 'gate']
        self.assertEqual(len(gates), 1)
        self.assertFalse(gates[0].get('continue-on-error', False))
        self.assertEqual(str(gates[0]['with']['exit-code']), '1')
        self.assertFalse(gates[0]['with'].get('ignore-unfixed', False))

    def test_release_publication_requires_success(self):
        jobs = workflow('docker-image.yml')['jobs']
        self.assertIn('scan', jobs['merge']['needs'])
        self.assertNotIn('if', jobs['merge'])  # GitHub's implicit success() gate
        text = (ROOT / '.github/workflows/docker-image.yml').read_text()
        self.assertNotIn('force_latest', text)
        self.assertIn('merge', jobs['airgap']['needs'])

    def test_manual_airgap_scans_bow_image_before_upload(self):
        job = workflow('airgap-bundle.yml')['jobs']['package']
        steps = job['steps']
        upload_index = next(i for i, s in enumerate(steps) if s['name'] == 'Upload release to S3')
        gates = [(i, s) for i, s in enumerate(steps) if s.get('id', '').startswith('gate_')]
        # Only the image we build is gated; upstream postgres/caddy ship as-is.
        self.assertEqual({s['with']['input'] for _, s in gates}, {'scan-images/bow.tar'})
        for i, gate in gates:
            self.assertLess(i, upload_index)
            self.assertFalse(gate.get('continue-on-error', False))
            self.assertEqual(str(gate['with']['exit-code']), '1')
            self.assertFalse(gate['with'].get('ignore-unfixed', False))
        self.assertNotIn('if', steps[upload_index])

    def test_github_release_cannot_bypass_scan(self):
        release = workflow('release.yml')
        triggers = release.get('on', release.get(True))
        self.assertEqual(set(triggers), {'workflow_call'})
        self.assertEqual(workflow('docker-image.yml')['jobs']['release']['needs'], 'merge')

    def test_failure_matrix_with_stubbed_publication(self):
        # Exercise dependency semantics, with publish replaced by an in-memory sink.
        jobs = workflow('docker-image.yml')['jobs']
        self.assertNotIn('if', jobs['merge'])
        for amd64 in ('success', 'failure', 'cancelled', 'skipped'):
            for arm64 in ('success', 'failure', 'cancelled', 'skipped'):
                results = {'prepare': 'success', 'build': 'success',
                           'scan': 'success' if amd64 == arm64 == 'success' else 'failure'}
                published = []
                if all(results[n] == 'success' for n in jobs['merge']['needs']):
                    published.extend(['version', 'dated-version', 'latest', 'airgap'])
                self.assertEqual(bool(published), amd64 == arm64 == 'success')


if __name__ == '__main__':
    unittest.main()
