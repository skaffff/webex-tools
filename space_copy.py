"""Preview and create separate Webex spaces, with durable one-use operation IDs."""
import json
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class WriteRejected(ValueError):
    pass


class WriteUncertain(ValueError):
    pass


def post_webex(path, payload, token):
    """Never retry a write: a lost response may still have created the resource."""
    if path not in ('rooms', 'memberships'):
        raise ValueError('Unsupported Webex operation.')
    request = Request('https://webexapis.com/v1/' + path,
                      data=json.dumps(payload).encode(), method='POST',
                      headers={'Authorization': 'Bearer ' + token,
                               'Content-Type': 'application/json'})
    try:
        with build_opener(NoRedirect()).open(request, timeout=45) as response:
            return json.load(response)
    except HTTPError as exc:
        if 400 <= exc.code < 500 and exc.code != 408:
            raise WriteRejected(f'Webex rejected this request (HTTP {exc.code}). Check token validity, membership permissions, and external communication policy.') from None
        raise WriteUncertain('Webex returned an uncertain result. Check the new space in Webex before taking further action.') from None
    except Exception:
        raise WriteUncertain('The response was lost or unreadable. The action may have succeeded; check Webex before taking further action.') from None


class SpaceCopy:
    def __init__(self, root, get_token, fetch, pages, post=post_webex):
        self.root = Path(root) / '.space-copy'
        self.get_token, self.fetch, self.pages, self.post = get_token, fetch, pages, post
        self.lock = threading.RLock()
        self.active = set()

    def read_api(self, path, token):
        with self.fetch('https://webexapis.com/v1/' + path, token) as response:
            return json.load(response)

    def members(self, room_id):
        if not isinstance(room_id, str) or not room_id or len(room_id) > 2000:
            raise ValueError('Choose a source space first.')
        token = self.get_token()
        room = self.read_api('rooms/' + quote(room_id, safe=''), token)
        owner = self.read_api('people/me', token)
        unique = {}
        for batch in self.pages('memberships?' + urlencode({'roomId': room_id, 'max': 100}), token):
            for member in batch:
                person_id = member.get('personId')
                if person_id:
                    unique[person_id] = {'personId': person_id,
                                         'name': member.get('personDisplayName') or member.get('personEmail') or person_id,
                                         'email': member.get('personEmail', ''),
                                         'isSelf': person_id == owner['id']}
        return {'source': {'id': room['id'], 'title': room.get('title', 'Untitled')},
                'owner': {'personId': owner['id'], 'name': owner.get('displayName', 'You'),
                          'email': (owner.get('emails') or [''])[0]},
                'members': list(unique.values())}

    def save(self, operation):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / (operation['id'] + '.json')
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(operation, indent=2))
        temp.chmod(0o600)
        temp.replace(path)

    def load(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r'[a-f0-9]{32}', operation_id):
            raise ValueError('Invalid operation ID.')
        try:
            return json.loads((self.root / (operation_id + '.json')).read_text())
        except FileNotFoundError:
            raise ValueError('Preview not found. Prepare a new preview.') from None

    def preview(self, body):
        title = body.get('title', '')
        email_input = body.get('clientEmails', body.get('clientEmail', ''))
        selected = body.get('personIds', [])
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 128:
            raise ValueError('Enter a new space name between 1 and 128 characters.')
        if not isinstance(email_input, str):
            raise ValueError('Enter client email addresses as text.')
        emails = list(dict.fromkeys(part.strip().lower() for part in
                                    re.split(r'[,;\r\n]+', email_input) if part.strip()))
        for index, email in enumerate(emails, 1):
            if len(email) > 254 or not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', email):
                raise ValueError(f'Client email {index} is invalid. Separate addresses with commas, semicolons, or new lines.')
        if not isinstance(selected, list) or not all(isinstance(v, str) for v in selected):
            raise ValueError('Invalid member selection.')
        source = self.members(body.get('roomId'))
        available = {m['personId']: m for m in source['members']}
        if not set(selected).issubset(available):
            raise ValueError('The source membership changed. Reload the members and review your selection.')
        recipients = [dict(available[pid]) for pid in dict.fromkeys(selected)
                      if pid != source['owner']['personId']]
        existing_emails = {m.get('email', '').lower() for m in recipients + [source['owner']]}
        for email in emails:
            if email not in existing_emails:
                recipients.append({'email': email, 'name': email, 'client': True})
                existing_emails.add(email)
        if not recipients:
            raise ValueError('Select at least one other member or enter a client email.')
        operation = {'id': secrets.token_hex(16), 'status': 'preview', 'createdAt': time.time(),
                     'title': title.strip(), 'source': source['source'], 'owner': source['owner'],
                     'recipients': recipients, 'results': [], 'roomId': None,
                     'phase': 'Ready to create'}
        with self.lock:
            self.save(operation)
        return operation

    def status(self, operation_id):
        with self.lock:
            operation = self.load(operation_id)
            if operation['status'] == 'running' and operation_id not in self.active:
                operation.update(status='interrupted', phase='The server stopped during creation. Check Webex before creating another space.')
                self.save(operation)
            return operation

    def start(self, operation_id):
        with self.lock:
            operation = self.status(operation_id)
            # Repeated clicks and retries always return the original operation.
            if operation['status'] != 'preview':
                return operation
            if time.time() - operation['createdAt'] > 1800:
                raise ValueError('This preview expired. Prepare a new preview before creating the space.')
            token = self.get_token()
            owner = self.read_api('people/me', token)
            if owner['id'] != operation['owner']['personId']:
                raise ValueError('The token now belongs to another account. Prepare a new preview.')
            operation.update(status='running', phase='Creating the new space')
            self.save(operation)  # Persist before any Webex write.
            self.active.add(operation_id)
            threading.Thread(target=self.run, args=(operation, token), daemon=True).start()
            return operation

    def run(self, operation, token):
        try:
            room = self.post('rooms', {'title': operation['title']}, token)
            if not room.get('id') or room['id'] == operation['source']['id']:
                raise WriteUncertain('Webex did not return a valid new space ID. Check Webex before trying again.')
            operation['roomId'] = room['id']
            operation['phase'] = 'Adding members to the new space'
            with self.lock:
                self.save(operation)
            for person in operation['recipients']:
                payload = {'roomId': operation['roomId']}
                if person.get('personId'):
                    payload['personId'] = person['personId']
                else:
                    payload['personEmail'] = person['email']
                result = {'name': person['name'], 'email': person.get('email', '')}
                try:
                    self.post('memberships', payload, token)
                    result['status'] = 'added'
                except WriteRejected as exc:
                    result.update(status='failed', error=str(exc))
                except WriteUncertain as exc:
                    result.update(status='unknown', error=str(exc))
                operation['results'].append(result)
                with self.lock:
                    self.save(operation)
            failures = any(r['status'] != 'added' for r in operation['results'])
            operation.update(status='partial' if failures else 'completed',
                             phase='Space created; review member results.' if failures else 'Space created and all selected members added.')
        except WriteRejected as exc:
            operation.update(status='failed', phase=str(exc))
        except WriteUncertain as exc:
            operation.update(status='uncertain', phase=str(exc))
        except Exception:
            operation.update(status='interrupted', phase='Creation stopped unexpectedly. Check Webex and the member results before trying again.')
        finally:
            with self.lock:
                try:
                    self.save(operation)
                finally:
                    self.active.discard(operation['id'])
