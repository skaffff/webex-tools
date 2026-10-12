import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from space_copy import SpaceCopy, WriteRejected, WriteUncertain, post_webex


class ImmediateThread:
    def __init__(self, target, args, **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class SpaceCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.post = Mock(return_value={'id': 'new-space'})
        def fetch(url, token):
            data = {'id': 'me', 'emails': ['me@example.com']} if url.endswith('people/me') else {'id': 'source', 'title': 'Internal'}
            return io.BytesIO(json.dumps(data).encode())
        self.service = SpaceCopy(Path(self.temp.name), lambda: 'fake-token', fetch,
                                 lambda *args: iter([[{'personId': 'me', 'personEmail': 'me@example.com'}],
                                                    [{'personId': 'colleague', 'personEmail': 'coworker@example.com'}]]), self.post)
        self.body = {'roomId': 'source', 'title': 'Client space', 'personIds': ['me', 'colleague'], 'clientEmail': 'client@example.org'}

    def start(self, operation):
        with patch('space_copy.threading.Thread', ImmediateThread):
            self.service.start(operation['id'])
        return self.service.status(operation['id'])

    def test_preview_is_read_only_and_reads_all_members(self):
        operation = self.service.preview(self.body)
        self.assertEqual(len(operation['recipients']), 2)
        self.post.assert_not_called()
        self.assertNotIn('fake-token', next(self.service.root.glob('*.json')).read_text())

    def test_only_new_space_is_written_and_creator_is_not_readded(self):
        operation = self.service.preview(self.body)
        result = self.start(operation)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.post.call_args_list[0].args[0:2], ('rooms', {'title': 'Client space'}))
        memberships = [c.args[1] for c in self.post.call_args_list[1:]]
        self.assertEqual(memberships, [{'roomId': 'new-space', 'personId': 'colleague'},
                                      {'roomId': 'new-space', 'personEmail': 'client@example.org'}])
        self.start(operation)
        self.assertEqual(self.post.call_count, 3)

    def test_duplicate_member_and_client_are_deduplicated(self):
        self.body.update(personIds=['me', 'colleague', 'colleague'], clientEmail='COWORKER@example.com')
        self.assertEqual(len(self.service.preview(self.body)['recipients']), 1)

    def test_multiple_clients_accept_mixed_separators_and_deduplicate(self):
        self.body['clientEmails'] = ' FIRST@example.org;second@example.org,\nfirst@example.org\r\nCOWORKER@example.com;me@example.com; '
        operation = self.service.preview(self.body)
        self.assertEqual([p['email'] for p in operation['recipients']],
                         ['coworker@example.com', 'first@example.org', 'second@example.org'])
        result = self.start(operation)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['results']), 3)

    def test_invalid_address_in_list_rejects_entire_preview(self):
        self.body['clientEmails'] = 'valid@example.org;broken;another@example.org'
        with self.assertRaisesRegex(ValueError, 'Client email 2 is invalid'):
            self.service.preview(self.body)
        self.post.assert_not_called()
        self.assertFalse(self.service.root.exists())

    def test_empty_client_list_keeps_selected_members(self):
        self.body['clientEmails'] = ' ;,\n '
        operation = self.service.preview(self.body)
        self.assertEqual([p['personId'] for p in operation['recipients']], ['colleague'])

    def test_invalid_selection_and_email_rejected_without_writes(self):
        for changes in ({'personIds': ['outsider']}, {'clientEmail': 'not-an-email'}, {'title': ' '}):
            with self.assertRaises(ValueError):
                self.service.preview(dict(self.body, **changes))
        self.post.assert_not_called()

    def test_partial_failure_keeps_new_room_and_records_each_person(self):
        self.post.side_effect = [{'id': 'new-space'}, WriteRejected('Forbidden'), {'id': 'member'}]
        result = self.start(self.service.preview(self.body))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['roomId'], 'new-space')
        self.assertEqual([r['status'] for r in result['results']], ['failed', 'added'])

    def test_unknown_room_creation_is_not_retried(self):
        self.post.side_effect = WriteUncertain('Unknown result')
        operation = self.service.preview(self.body)
        self.assertEqual(self.start(operation)['status'], 'uncertain')
        self.start(operation)
        self.post.assert_called_once()

    def test_restart_detects_interruption_and_does_not_retry(self):
        operation = self.service.preview(self.body)
        operation['status'] = 'running'
        operation['roomId'] = 'new-space'
        self.service.save(operation)
        self.assertEqual(self.service.start(operation['id'])['status'], 'interrupted')
        self.post.assert_not_called()

    def test_preview_expiry_and_account_change_block_creation(self):
        operation = self.service.preview(self.body)
        operation['createdAt'] = 0
        self.service.save(operation)
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.service.start(operation['id'])
        operation = self.service.preview(self.body)
        with patch.object(self.service, 'read_api', return_value={'id': 'different-user'}):
            with self.assertRaisesRegex(ValueError, 'another account'):
                self.service.start(operation['id'])
        self.post.assert_not_called()

    def test_http_writes_never_retry_after_timeout_or_server_error(self):
        for failure in (URLError('timeout'), HTTPError('url', 503, 'Unavailable', {}, None)):
            with patch('space_copy.build_opener') as factory:
                factory.return_value.open.side_effect = failure
                with self.assertRaises(WriteUncertain):
                    post_webex('rooms', {'title': 'Test'}, 'fake')
                factory.return_value.open.assert_called_once()


if __name__ == '__main__':
    unittest.main()
