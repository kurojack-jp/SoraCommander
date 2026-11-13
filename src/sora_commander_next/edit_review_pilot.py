"""Opt-in local Owner file-edit trial, restricted to disposable test files.

Not a protected security boundary. Never enable this broker for production files.
"""
from pathlib import Path
from hashlib import sha256
from threading import Lock, Thread
from uuid import uuid4
from math import isfinite
from time import monotonic
from .file_edits import (preview_text_edit, review_and_replace_local,
                         preview_text_create, review_and_create_local,
                         _verify_created_text, CreateVerificationError,
                         WriteNotStartedError)
from .file_edits import (review_and_create_timeout_dev, review_and_replace_timeout_dev,
                         _VerifiedFileReviewExpiry)


class EditReviewPilot:
    def __init__(self, root, *, enable_create=False, enable_timeout_dev=False, locale="en"):
        if type(enable_create) is not bool:
            raise ValueError('explicit boolean create opt-in required')
        if type(enable_timeout_dev) is not bool:
            raise ValueError('explicit boolean timeout DEV opt-in required')
        if locale not in ("en", "ja"):
            raise ValueError("unsupported Owner window language")
        self._locale = locale
        self._enable_timeout_dev = enable_timeout_dev
        self._dev_bindings = {}
        self._dev_running = set()
        self._dev_admission_hold = False
        # Keep the literal root too: resolving it must not hide a junction.
        self._create_root = str(Path(root).absolute())
        self.enable_create = enable_create
        self.root = Path(root).resolve(strict=True)
        self._lock = Lock()
        self._results = {}
        self._active = None
        self._activity_types = {}

    def _publish_activity(self, pending_id, state, operation_type=None):
        # Called under the pilot lock; only fixed scalar metadata leaves it.
        try:
            from .activity import publish
            if operation_type is not None:
                self._activity_types[pending_id] = operation_type
            operation_type = self._activity_types[pending_id]
            publish(getattr(self, '_activity_store', None), pending_id=pending_id,
                    operation_type=operation_type, state=state, source='edit')
            self._activity_types = {pid: kind for pid, kind in self._activity_types.items()
                                    if pid in self._results}
        except Exception:
            try:
                self._activity_store.invalidate()
            except Exception:
                pass

    def activity_snapshot(self, identities):
        from .activity import Record
        with self._lock:
            return [Record(operation_type=self._activity_types[pid], pending_id=pid,
                           state=value['status'], state_source='edit').data()
                    for pid, value in self._results.items() if pid in identities]

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
            if self._active is not None or self._dev_admission_hold:
                return {'status': 'BUSY'}
            pending_id = uuid4().hex
            self._active = pending_id
            self._results[pending_id] = {'status': 'PENDING', 'target': str(target),
                                         'old_sha256': expected_sha256, 'new_sha256': expected_new}
            self._publish_activity(pending_id, 'PENDING', 'text_edit')
            if self._enable_timeout_dev:
                self._dev_bindings[pending_id] = (str(target), expected_sha256,
                                                 expected_new, 'edit', object())
            try:
                Thread(target=self._execute, args=(pending_id, str(target), content, expected_sha256, expected_new),
                       daemon=True, name='SoraEditPilotReview').start()
            except Exception:
                self._results[pending_id] = {'status': 'ERROR', 'error': 'ReviewStartFailed'}
                self._publish_activity(pending_id, 'ERROR')
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
            if self._active is not None or self._dev_admission_hold:
                return {'status': 'BUSY'}
            pending_id = uuid4().hex
            self._active = pending_id
            self._results[pending_id] = {'status': 'PENDING', 'operation': 'create',
                'target': info['path'], 'old_sha256': None, 'new_sha256': expected_new}
            self._publish_activity(pending_id, 'PENDING', 'text_create')
            if self._enable_timeout_dev:
                self._dev_bindings[pending_id] = (info['path'], None,
                                                 expected_new, 'create', object())
            try:
                Thread(target=self._execute,
                       args=(pending_id, info['path'], content, None, expected_new, True),
                       daemon=True, name='SoraCreatePilotReview').start()
            except Exception:
                self._results[pending_id] = {'status': 'ERROR', 'error': 'ReviewStartFailed'}
                self._publish_activity(pending_id, 'ERROR')
                self._active = None
                while len(self._results) > 64:
                    self._results.pop(next(iter(self._results)))

                return {'status': 'ERROR', 'error': 'ReviewStartFailed'}
        return {'status': 'REVIEW', 'pending_id': pending_id, 'new_sha256': expected_new}

    def _execute(self, pending_id, path, content, digest, expected_new=None, create=False):
        if self._enable_timeout_dev:
            return self._execute_timeout_dev(pending_id, path, content, digest,
                                             expected_new, create)
        expected_new = expected_new or sha256(content.encode('utf-8')).hexdigest()
        if sha256(content.encode('utf-8')).hexdigest() != expected_new:
            with self._lock:
                self._results[pending_id] = {'status': 'ERROR', 'error': 'ProposalDigestMismatch'}
                self._publish_activity(pending_id, 'ERROR')
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
            self._publish_activity(pending_id, outcome['status'])
            if (self._active == pending_id
                    and outcome.get('recovery') != 'MANUAL_REVIEW_REQUIRED'):
                self._active = None
            # Ambiguous write outcome pins admission closed until a fresh
            # explicit Owner inspection. This DEV pilot provides NO automatic
            # resolution or restart-safe authorization recovery.
            while len(self._results) > 64:
                self._results.pop(next(iter(self._results)))


    def _dev_matches(self, pending_id, binding):
        record = self._results.get(pending_id)
        return (not self._dev_admission_hold
                and binding is not None and self._dev_bindings.get(pending_id) is binding
                and self._active == pending_id and type(record) is dict
                and record.get('status') == 'PENDING'
                and (record.get('target'), record.get('old_sha256'),
                     record.get('new_sha256'), record.get('operation', 'edit')) == binding[:4])

    def _execute_timeout_dev(self, pending_id, path, content, digest, expected_new, create):
        # This branch is intentionally absent from stdio_main/Host construction.
        with self._lock:
            binding = self._dev_bindings.get(pending_id)
            if (not self._dev_matches(pending_id, binding)
                    or pending_id in self._dev_running
                    or binding[:4] != (path, digest, expected_new, 'create' if create else 'edit')
                    or type(content) is not str
                    or sha256(content.encode('utf-8')).hexdigest() != expected_new):
                self._dev_admission_hold = True
                return  # Never overwrite another/later result, even on corrupt state.
            self._dev_running.add(pending_id)
        token = binding[4]
        def verify_binding():
            with self._lock:
                if not self._dev_matches(pending_id, binding):
                    self._dev_admission_hold = True
                    raise WriteNotStartedError('ReviewBindingMismatch')
        try:
            if create:
                if not self.enable_create or digest is not None:
                    raise ValueError('CreateBindingMismatch')
                preview = preview_text_create(path, (self._create_root,), content)
                if preview['review_truncated'] or preview['new_sha256'] != expected_new:
                    raise ValueError('CreateProposalMismatch')
                result = review_and_create_timeout_dev(path, (self._create_root,),
                    content, expected_new, review_token=token, verify_binding=verify_binding, locale=self._locale)
                if type(result) is _VerifiedFileReviewExpiry:
                    outcome = result
                elif result.get('status') == 'CREATED_LOCAL_DEV':
                    actual = _verify_created_text(path, (self._create_root,), expected_new)
                    if result.get('sha256') != expected_new or actual != expected_new:
                        raise CreateVerificationError('create digest mismatch')
                    outcome = {'status': 'CREATED_LOCAL_DEV', 'sha256': actual}
                elif result.get('status') in {'DENIED', 'ERROR'}:
                    outcome = result
                else:
                    raise CreateVerificationError('unexpected create result')
            else:
                outcome = self._replace(path, content, digest, expected_new,
                    review_token=token, verify_binding=verify_binding)
        except Exception:
            outcome = {'status': 'ERROR', 'error': 'WriteOutcomeUnknown'}
        if type(outcome) is dict and outcome.get('status') == 'TIMED_OUT':
            outcome = {'status': 'ERROR', 'error': 'UntypedExpiryResult'}
        if type(outcome) is _VerifiedFileReviewExpiry:
            if (outcome.review_token is token and outcome.write_outcome == 'NOT_STARTED'
                    and type(outcome.reason) is str and outcome.reason in {'EXPIRED', 'CLOSED'}
                    and type(outcome.deadline) in (int, float) and isfinite(outcome.deadline)
                    and type(outcome.timer_at) in (int, float) and isfinite(outcome.timer_at)
                    and outcome.timer_at <= monotonic()
                    and (outcome.reason == 'CLOSED' or outcome.timer_at >= outcome.deadline)
                    and (outcome.path, outcome.old_sha256, outcome.new_sha256,
                         outcome.operation) == binding[:4]):
                outcome = {'status': 'TIMED_OUT', 'stage': 'FILE_GUI', 'reason': outcome.reason,
                           'approval_state': 'EXPIRED', 'write_outcome': 'NOT_STARTED',
                           'auto_retry': False, 'required_action': 'REPORT_AND_WAIT'}
            else:
                outcome = {'status': 'ERROR', 'error': 'ExpiryBindingMismatch'}
        # Unknown/malformed responses cannot release admission in this DEV path.
        if type(outcome) is not dict or type(outcome.get('status')) is not str:
            outcome = {'status': 'ERROR', 'error': 'MalformedReviewResult'}
        not_started = outcome.get('write_outcome') == 'NOT_STARTED'
        if outcome.get('status') not in {'TIMED_OUT', 'DENIED', 'CREATED_LOCAL_DEV',
                                         'REPLACED_LOCAL_DEV', 'UNCHANGED_LOCAL_DEV'}:
            outcome = {'status': 'ERROR', 'error': outcome.get('error', 'UnexpectedReviewResult'),
                       'recovery': 'MANUAL_REVIEW_REQUIRED',
                       'auto_retry': False, 'auto_rollback': False}
            if not_started:
                outcome['write_outcome'] = 'NOT_STARTED'
        if outcome.get('status') == 'TIMED_OUT' and (
                set(outcome) != {'status', 'stage', 'reason', 'approval_state',
                                 'write_outcome', 'auto_retry', 'required_action'}
                or outcome['stage'] != 'FILE_GUI'
                or outcome['reason'] not in {'EXPIRED', 'CLOSED'}
                or outcome['approval_state'] != 'EXPIRED'
                or outcome['write_outcome'] != 'NOT_STARTED'
                or outcome['auto_retry'] is not False
                or outcome['required_action'] != 'REPORT_AND_WAIT'):
            outcome = {'status': 'ERROR', 'error': 'MalformedExpiryTuple',
                       'recovery': 'MANUAL_REVIEW_REQUIRED', 'auto_retry': False,
                       'auto_rollback': False}
        with self._lock:
            if not self._dev_matches(pending_id, binding):
                self._dev_admission_hold = True
                return
            self._results[pending_id] = dict(outcome, pending_id=pending_id)
            self._publish_activity(pending_id, outcome['status'])
            self._dev_bindings.pop(pending_id)
            self._dev_running.discard(pending_id)
            if outcome.get('recovery') != 'MANUAL_REVIEW_REQUIRED':
                self._active = None
            while len(self._results) > 64:
                self._results.pop(next(iter(self._results)))


    def _replace(self, path, content, digest, expected_new, *, review_token=None,
                 verify_binding=None):
            # Re-check the same disposable target and old digest before prompting.
            target = self._target(path)
            current = sha256(target.read_bytes()).hexdigest()
            if current != digest:
                raise ValueError('StaleEditRequest')
            try:
                if review_token is None:
                    result = review_and_replace_local(path, (str(self.root),), content, digest)
                else:
                    result = review_and_replace_timeout_dev(path, (str(self.root),),
                        content, digest, review_token=review_token, verify_binding=verify_binding, locale=self._locale)
            except WriteNotStartedError:
                # Only a trusted pre-replace boundary may issue this exception.
                # No write was attempted; release this pending review normally.
                return {'status': 'ERROR', 'error': 'WriteNotStarted'}
            except Exception:
                # The write may have completed even when the call raised.
                return {'status': 'ERROR', 'error': 'WriteOutcomeUnknown'}
            if review_token is not None:
                if type(result) is _VerifiedFileReviewExpiry:
                    return result
                if result.get('status') == 'ERROR':
                    return result
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
            result = dict(self._results.get(pending_id, {'status': 'UNKNOWN'}))
            if (self._enable_timeout_dev and self._dev_admission_hold
                    and result.get('status') == 'PENDING'):
                return {'status': 'ERROR', 'pending_id': pending_id,
                        'error': 'ReviewBindingMismatch', 'recovery': 'MANUAL_REVIEW_REQUIRED',
                        'auto_retry': False, 'auto_rollback': False}
            return result
