import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import vote_cat as app


class VotingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        identity_patch = patch.object(app, 'IDENTITY_FILE', Path(self.temp.name) / 'identity.json')
        identity_patch.start()
        self.addCleanup(identity_patch.stop)
        self.args = SimpleNamespace(hearts=25, batch_size=12, batch_pause=300,
                                    delay=0, device="existing-device", state=Path(self.temp.name) / "state.json")
        self.now = 1000.0
        self.accepted = 0
        self.left = {}

    def sleep(self, seconds):
        self.now += seconds

    def api(self, path, body=None):
        if path.startswith('/dogs?'):
            return [{'name': app.CAT_NAME}]
        device = body['p_device']
        if path.endswith('heart_status'):
            self.left.setdefault(device, self.args.hearts)
            return {'open': True, 'left': self.left[device]}
        self.left[device] -= 1
        self.assertGreaterEqual(self.left[device], 0)
        self.accepted += 1
        return {'ok': True, 'left': self.left[device]}

    def execute(self, api, state=None):
        if state is None:
            state = app.load(self.args.state, self.args.hearts)
        with patch.object(app, 'request', side_effect=api), \
                patch.object(app.time, 'time', side_effect=lambda: self.now), \
                patch.object(app.time, 'sleep', side_effect=self.sleep), \
                contextlib.redirect_stdout(io.StringIO()):
            app.run(self.args, state)
        return state

    def test_rate_retries_batches_and_exact_total(self):
        rejected_at = []
        def api(path, body=None):
            if path.endswith('give_heart') and len(rejected_at) < 2:
                rejected_at.append(self.now)
                raise app.RateLimited()
            return self.api(path, body)
        state = self.execute(api)
        self.assertEqual(rejected_at, [1000, 1300])
        self.assertEqual(self.now, 2200)  # Two retries and two batch pauses.
        self.assertEqual(self.accepted, 25)
        self.assertEqual(state['confirmed'], 25)
        self.assertFalse(state['pending'])
        self.assertLess(self.args.state.stat().st_size, 1024)

    def test_resume_keeps_count_and_cooldown(self):
        state = app.load(self.args.state, 25)
        state.update(confirmed=7, batch_done=7, wait_until=1300)
        app.save(self.args.state, state)
        state = self.execute(self.api, app.load(self.args.state, 25))
        self.assertEqual(self.accepted, 18)
        self.assertEqual(state['confirmed'], 25)
        self.assertGreaterEqual(self.now, 1300)

    def test_full_1200_batch_and_final_partial_batch(self):
        self.args.hearts = 1201
        self.args.batch_size = 1200
        # Keep the test fast; snapshot contents and replacement are still tested.
        with patch.object(app.os, 'fsync'):
            state = self.execute(self.api)
        self.assertEqual(self.accepted, 1201)
        self.assertEqual(self.now, 1300)
        self.assertEqual(state['batch_done'], 1)
        self.assertLess(self.args.state.stat().st_size, 1024)

    def test_unknown_vote_outcome_blocks_automatic_resume(self):
        def api(path, body=None):
            if path.endswith('give_heart'):
                raise RuntimeError('timeout')
            return self.api(path, body)
        with self.assertRaisesRegex(RuntimeError, 'timeout'):
            self.execute(api)
        with self.assertRaisesRegex(RuntimeError, 'may have counted'):
            app.load(self.args.state, 25)

    def test_exhausted_allowance_waits_without_rotating_device(self):
        self.args.hearts = 2
        self.args.device = "existing-device"
        checks = []

        def api(path, body=None):
            if path.endswith('heart_status'):
                checks.append((self.now, body['p_device']))
                self.left[body['p_device']] = 1 if self.now != 1000 or not self.accepted else 0
                return {'open': True, 'left': self.left[body['p_device']]}
            return self.api(path, body)

        state = self.execute(api)
        self.assertEqual(checks, [(1000, 'existing-device'), (1000, 'existing-device'),
                                  (1300, 'existing-device')])
        self.assertEqual(state['confirmed'], 2)
        self.assertEqual(state['device'], 'existing-device')

    def test_changed_identity_stops_before_requests(self):
        for saved, supplied in [('original', 'replacement')]:
            state = app.load(self.args.state, self.args.hearts)
            state['device'] = saved
            self.args.device = supplied
            with patch.object(app, 'request') as request:
                with self.assertRaises(RuntimeError):
                    app.run(self.args, state)
                request.assert_not_called()

    def test_automatic_identity_persists_across_progress_files(self):
        self.args.device = None
        state = app.load(self.args.state, self.args.hearts)
        first = app.stable_device(self.args, state)
        self.assertTrue(first)
        fresh = app.load(Path(self.temp.name) / 'other.json', self.args.hearts)
        self.assertEqual(app.stable_device(self.args, fresh), first)
        state = self.execute(self.api, state)
        self.assertEqual(state['device'], first)
        self.assertEqual(set(self.left), {first})

    def test_rate_limit_http_error_classification(self):
        for code, body, expected in [
            (400, {'hint': 'rate_limited'}, app.RateLimited),
            (429, {}, app.RateLimited),
            (400, {'hint': 'closed'}, app.Rejected),
            (500, {}, RuntimeError),
        ]:
            error = HTTPError('https://example.invalid', code, 'error', {},
                              io.BytesIO(json.dumps(body).encode()))
            with self.subTest(code=code, body=body), \
                    patch.object(app, 'urlopen', side_effect=error), \
                    self.assertRaises(expected):
                app.request('/rpc/give_heart', {})


if __name__ == '__main__':
    unittest.main()
