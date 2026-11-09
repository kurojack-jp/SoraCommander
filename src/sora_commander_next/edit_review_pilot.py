"""Opt-in local Owner file-edit trial, restricted to disposable test files.

Not a protected security boundary. Never enable this broker for production files.
"""
from pathlib import Path
from hashlib import sha256
from threading import Lock, Thread
from uuid import uuid4
from .file_edits import (preview_text_edit, review_and_replace_local,
                         preview_text_create, review_and_create_local,
                         _verify_created_text, CreateVerificationError,
                         WriteNotStartedError)


class EditReviewPilot:
    def __init__(self, root, *, enable_create=False):
        if type(enable_create) is not bool:
            raise ValueError('explicit boolean create opt-in required')
        # Keep the literal root too: resolving it must not hide a junction.
        self._create_root = str(Path(root).absolute())
        self.enable_create = enable_create
        self.root = Path(root).resolve(strict=True)
        self._lock = Lock()
        self._results = {}
        self._active = None

    def _target(self, path):
        if type(path) is not str:
            raise ValueError('invalid path')
        p = Path(path)
        root_txt = (p.parent == self.root
                    and p.name.startswith('owner-edit-pilot-') and p.suffix == '.txt')
        # One existing disposable Python fixture only. Never generalize to repo *.py.
        fixture_py = (p.parent == self.root / 'codex-e2e'
                      and p.name == 'counter.py')
        if (not p.is_absolute() or not (root_txt or fixture_py)
                or p.is_symlink() or p.is_junction() or not p.is_file()
                or p.resolve(strict=True).parent != p.parent):
            raise PermissionError('disposable pilot target required')
        return p

    def request(self, path, content, expected_sha256):
        target = self._target(path)
        info = preview_text_edit(str(target), (str(self.root),), content, expected_sha256)
        if info['diff_truncated']:
            raise ValueError('incomplete diff')
        if not info['changed']:
            return {'status': 'UNCHANGED', 'old_sha256': expected_sha256}
        expected_new = info['new_sha256']
        with self._lock:
            if self._active is not None:
                return {'status': 'BUSY'}
            pending_id = uuid4().hex
            self._active = pending_id
            self._results[pending_id] = {'status': 'PENDING', 'target': str(target),
                                         'old_sha256': expected_sha256, 'new_sha256': expected_new}
            try:
                Thread(target=self._execute, args=(pending_id, str(target), content, expected_sha256, expected_new),
                       daemon=True, name='SoraEditPilotReview').start()
            except Exception:
                self._results[pending_id] = {'status': 'ERROR', 'error': 'ReviewStartFailed'}
                self._active = None
                while len(self._results) > 64:
                    self._results.pop(next(iter(self._results)))
                return {'status': 'ERROR', 'error': 'ReviewStartFailed'}
        return {'status': 'REVIEW', 'pending_id': pending_id,
                'old_sha256': expected_sha256, 'new_sha256': expected_new}

    def request_create(self, path, content):
        if not self.enable_create:
            raise PermissionError('create pilot not enabled')
        info = preview_text_create(path, (self._create_root,), content)
        if info['review_truncated']:
            raise ValueError('incomplete create review')
        expected_new = info['new_sha256']
        with self._lock:
            if self._active is not None:
                return {'status': 'BUSY'}
            pending_id = uuid4().hex
            self._active = pending_id
            self._results[pending_id] = {'status': 'PENDING', 'operation': 'create',
                'target': info['path'], 'old_sha256': None, 'new_sha256': expected_new}
            try:
                Thread(target=self._execute,
                       args=(pending_id, info['path'], content, None, expected_new, True),
                       daemon=True, name='SoraCreatePilotReview').start()
            except Exception:
                self._results[pending_id] = {'status': 'ERROR', 'error': 'ReviewStartFailed'}
                self._active = None
                while len(self._results) > 64:
                    self._results.pop(next(iter(self._results)))
                return {'status': 'ERROR', 'error': 'ReviewStartFailed'}
        return {'status': 'REVIEW', 'pending_id': pending_id, 'new_sha256': expected_new}

    def _execute(self, pending_id, path, content, digest, expected_new=None, create=False):
        expected_new = expected_new or sha256(content.encode('utf-8')).hexdigest()
        if sha256(content.encode('utf-8')).hexdigest() != expected_new:
            with self._lock:
                self._results[pending_id] = {'status': 'ERROR', 'error': 'ProposalDigestMismatch'}
                if self._active == pending_id:
                    self._active = None
            return
        try:
            # A queued request must still match its immutable preview record.
            with self._lock:
                bound = self._results.get(pending_id)
                if bound is None or (self._active != pending_id or
                        bound.get('target') != path or
                        bound.get('old_sha256') != digest or
                        bound.get('new_sha256') != expected_new or
                        bound.get('operation', 'edit') != ('create' if create else 'edit') or
                        bound.get('status') != 'PENDING'):
                    raise ValueError('ReviewBindingMismatch')
            if create:
                if not self.enable_create or digest is not None:
                    raise PermissionError('create pilot not enabled')
                preview = preview_text_create(path, (self._create_root,), content)
                if preview['review_truncated'] or preview['new_sha256'] != expected_new:
                    raise ValueError('CreateProposalMismatch')
                try:
                    result = review_and_create_local(path, (self._create_root,), content, expected_new)
                except Exception as exc:
                    # The filesystem operation may have succeeded before the
                    # exception. Never infer a clean rollback or retry.
                    raise CreateVerificationError('create outcome unknown') from exc
                if result['status'] == 'CREATED_LOCAL_DEV':
                    try:
                        actual = _verify_created_text(path, (self._create_root,), expected_new)
                        if result.get('sha256') != expected_new or actual != expected_new:
                            raise CreateVerificationError('create digest mismatch')
                    except Exception as exc:
                        raise CreateVerificationError('create post-verification failed') from exc
                    outcome = {'status': 'CREATED_LOCAL_DEV', 'sha256': actual}
                elif result['status'] == 'DENIED':
                    outcome = {'status': 'DENIED'}
                else:
                    raise ValueError('UnexpectedCreateResult')
            else:
                outcome = self._replace(path, content, digest, expected_new)
        except Exception as exc:
            outcome = {'status': 'ERROR', 'error': type(exc).__name__}
        # Verification failures never trigger deletion or restoration.
        if outcome.get('error') in {'PostWriteVerificationFailed', 'CreateVerificationError',
                                    'WriteOutcomeUnknown'}:
            outcome['recovery'] = 'MANUAL_REVIEW_REQUIRED'
            outcome['auto_rollback'] = False
            outcome['auto_retry'] = False
        with self._lock:
            self._results[pending_id] = outcome
            if (self._active == pending_id
                    and outcome.get('recovery') != 'MANUAL_REVIEW_REQUIRED'):
                self._active = None
            # Ambiguous write outcome pins admission closed until a fresh
            # explicit Owner inspection. This DEV pilot provides NO automatic
            # resolution or restart-safe authorization recovery.
            while len(self._results) > 64:
                self._results.pop(next(iter(self._results)))

    def _replace(self, path, content, digest, expected_new):
            # Re-check the same disposable target and old digest before prompting.
            target = self._target(path)
            current = sha256(target.read_bytes()).hexdigest()
            if current != digest:
                raise ValueError('StaleEditRequest')
            try:
                result = review_and_replace_local(path, (str(self.root),), content, digest)
            except WriteNotStartedError:
                # Only a trusted pre-replace boundary may issue this exception.
                # No write was attempted; release this pending review normally.
                return {'status': 'ERROR', 'error': 'WriteNotStarted'}
            except Exception:
                # The write may have completed even when the call raised.
                return {'status': 'ERROR', 'error': 'WriteOutcomeUnknown'}
            outcome = {'status': result['status']}
            if result['status'] == 'REPLACED_LOCAL_DEV':
                try:
                    actual_new = sha256(self._target(path).read_bytes()).hexdigest()
                    if result.get('sha256') != expected_new or actual_new != expected_new:
                        raise ValueError('post-write hash mismatch')
                except Exception:
                    outcome = {'status': 'ERROR', 'error': 'PostWriteVerificationFailed'}
                else:
                    outcome['sha256'] = actual_new
            elif 'sha256' in result:
                outcome['sha256'] = result['sha256']
            return outcome

    def status(self, pending_id):
        if type(pending_id) is not str or len(pending_id) != 32:
            return {'status': 'ERROR', 'error': 'InvalidPendingId'}
        with self._lock:
            return dict(self._results.get(pending_id, {'status': 'UNKNOWN'}))
