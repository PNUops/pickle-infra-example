#!/usr/bin/env python3
"""Offline checks for reload convergence and guarded rollback."""

import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[1] / 'apply-candidate-synthetic-login.py'
SPEC = importlib.util.spec_from_file_location('candidate_synthetic_login', SOURCE)
ingress = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ingress)


class ReloadConvergenceTest(unittest.TestCase):
    def test_first_old_worker_response_then_new_worker_response(self):
        with patch.object(ingress, 'matrix', side_effect=[
                ingress.MatrixMismatch('GET /login: expected 200, got 404',
                                       retryable=True), None]) as matrix:
            with patch.object(ingress.time, 'monotonic', side_effect=[0.0, 0.1]):
                with patch.object(ingress.time, 'sleep') as sleep:
                    self.assertEqual(ingress.wait_matrix(True), 2)
        self.assertEqual(matrix.call_count, 2)
        sleep.assert_called_once_with(ingress.MATRIX_RETRY_SECONDS)

    def test_persistent_mismatch_times_out_with_installed_hash(self):
        target = Mock()
        target.read_bytes.return_value = b'candidate bytes'
        with patch.object(ingress, 'TARGET', target), \
                patch.object(ingress, 'matrix', side_effect=ingress.MatrixMismatch(
                    'GET /login: expected 200, got 404', retryable=True)) as matrix, \
                patch.object(ingress.time, 'monotonic', side_effect=[0.0, 0.1, 10.0]), \
                patch.object(ingress.time, 'sleep') as sleep:
            with self.assertRaises(ingress.MatrixTimeout) as caught:
                ingress.wait_matrix(True)
        self.assertEqual(matrix.call_count, 2)
        sleep.assert_called_once()
        self.assertIn('last mismatch: GET /login: expected 200, got 404',
                      str(caught.exception))
        self.assertIn('installed_sha256=' + hashlib.sha256(b'candidate bytes').hexdigest(),
                      str(caught.exception))

    def test_probe_failure_is_reported_without_retry(self):
        with patch.object(ingress, 'matrix', side_effect=OSError('TLS socket closed')) as matrix, \
                patch.object(ingress.time, 'monotonic', return_value=0.0), \
                patch.object(ingress.time, 'sleep') as sleep:
            with self.assertRaisesRegex(OSError, 'TLS socket closed'):
                ingress.wait_matrix(True)
        matrix.assert_called_once_with(True)
        sleep.assert_not_called()

    def test_security_invariant_failure_is_reported_without_retry(self):
        def response(path, source, host='staging.example.com', method='GET'):
            return '200' if path in ('/__ingress_probe', '/') else '404'

        with patch.object(ingress, 'probe', side_effect=response), \
                patch.object(ingress.time, 'monotonic', return_value=0.0), \
                patch.object(ingress.time, 'sleep') as sleep:
            with self.assertRaises(ingress.MatrixMismatch) as caught:
                ingress.wait_matrix(True)
        self.assertIn('GET /', str(caught.exception))
        self.assertIn('expected 404, got 200', str(caught.exception))
        self.assertFalse(caught.exception.retryable)
        sleep.assert_not_called()

    def test_recovery_old_trial_worker_login_is_retryable(self):
        def response(path, source, host='staging.example.com', method='GET'):
            if path == '/__ingress_probe':
                if host != 'staging.example.com':
                    return '421'
                return '200' if source == '203.0.113.14' else '403'
            return '200' if path == '/login' else '404'

        with patch.object(ingress, 'probe', side_effect=response):
            with self.assertRaises(ingress.MatrixMismatch) as caught:
                ingress.matrix(False)
        self.assertTrue(caught.exception.retryable)
        self.assertIn('GET /login', str(caught.exception))

    def test_apply_old_health_worker_login_is_retryable(self):
        def response(path, source, host='staging.example.com', method='GET'):
            if path == '/__ingress_probe':
                if host != 'staging.example.com':
                    return '421'
                return '200' if source == '203.0.113.14' else '403'
            return '404'

        with patch.object(ingress, 'probe', side_effect=response):
            with self.assertRaises(ingress.MatrixMismatch) as caught:
                ingress.matrix(True)
        self.assertTrue(caught.exception.retryable)
        self.assertIn('GET /login', str(caught.exception))

    def test_upstream_error_is_not_treated_as_old_worker(self):
        def response(path, source, host='staging.example.com', method='GET'):
            if path == '/__ingress_probe':
                if host != 'staging.example.com':
                    return '421'
                return '200' if source == '203.0.113.14' else '403'
            return '503' if path == '/login' else '404'

        with patch.object(ingress, 'probe', side_effect=response):
            with self.assertRaises(ingress.MatrixMismatch) as caught:
                ingress.matrix(True)
        self.assertFalse(caught.exception.retryable)
        self.assertIn('expected 200, got 503', str(caught.exception))

    def test_apply_timeout_runs_recovery_and_reports_final_hash(self):
        backup = Mock()
        backup.exists.return_value = True
        backup.is_symlink.return_value = False
        target = Mock()
        target.value = b'baseline bytes'
        target.read_bytes.side_effect = lambda: target.value
        values = {b'trial bytes': ingress.TRIAL_SHA,
                  b'baseline bytes': ingress.BASE_SHA}

        def source_bytes(path, expected, mode):
            return b'trial bytes' if path == ingress.SOURCE else b'baseline bytes'

        def write_bytes(path, data, mode):
            if path is target:
                target.value = data

        output = io.StringIO()
        with patch.object(ingress, 'BACKUP', backup), patch.object(ingress, 'TARGET', target), \
                patch.object(ingress, 'pinned', side_effect=source_bytes), \
                patch.object(ingress, 'digest', side_effect=lambda data: values[data]), \
                patch.object(ingress, 'atomic', side_effect=write_bytes) as atomic, \
                patch.object(ingress, 'run', return_value='active'), \
                patch.object(ingress, 'matrix') as matrix, \
                patch.object(ingress, 'wait_matrix', side_effect=[
                    ingress.MatrixTimeout('new connections did not converge'), 3]) as wait, \
                contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError,
                                        'apply failed \\(MatrixTimeout\\).*health-only restored') as caught:
                ingress.apply()
        matrix.assert_called_once_with(False)
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(atomic.call_count, 2)
        self.assertEqual(target.value, b'baseline bytes')
        self.assertIn('"health_only_new_connections": true', output.getvalue())
        self.assertIn('installed_sha256=' + ingress.BASE_SHA,
                      str(caught.exception))

    def test_recovery_waits_for_new_worker_after_restoring_file(self):
        target = Mock()
        target.read_bytes.return_value = b'trial bytes'
        values = {b'trial bytes': ingress.TRIAL_SHA, b'baseline bytes': ingress.BASE_SHA}
        output = io.StringIO()
        with patch.object(ingress, 'TARGET', target), \
                patch.object(ingress, 'pinned', return_value=b'baseline bytes'), \
                patch.object(ingress, 'digest', side_effect=lambda data: values[data]), \
                patch.object(ingress, 'atomic') as atomic, \
                patch.object(ingress, 'run'), \
                patch.object(ingress, 'matrix', side_effect=[
                    ingress.MatrixMismatch('GET /login: expected 404, got 200',
                                           retryable=True), None]) as matrix, \
                patch.object(ingress.time, 'monotonic', side_effect=[0.0, 0.1]), \
                patch.object(ingress.time, 'sleep') as sleep, \
                contextlib.redirect_stdout(output):
            ingress.recover()
        atomic.assert_called_once_with(target, b'baseline bytes', 0o644)
        self.assertEqual(matrix.call_count, 2)
        sleep.assert_called_once_with(ingress.MATRIX_RETRY_SECONDS)
        self.assertIn('"matrix_attempts": 2', output.getvalue())
        self.assertIn('"existing_sessions_not_revoked": true', output.getvalue())


if __name__ == '__main__':
    unittest.main()
