"""AGE-6 close proof uses a retained admission in the owning native ledger."""
import json

from tests.tools.test_delegate_external_wait import _args, _call

pytest_plugins = ['tests.tools.test_delegate_external_wait']


def test_close_requires_new_retained_native_message_admission(owner):
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input, settle_session_input
    from tools.delegate_project_store import ProjectStore

    db = owner._session_db
    epoch = begin_runtime_epoch(db, instance_id='offline')
    source = {'platform': 'telegram', 'chat_id': 'fixture-chat', 'user_id': 'fixture-user', 'is_bot': False}
    principal = 'messaging:' + json.dumps([None, 'telegram', 'fixture-chat', None, 'fixture-user'], separators=(',', ':'))

    def admit(request, *, native=True):
        payload = {'text': 'Supplier confirms receipt'}
        if native:
            payload['native_text_v1'] = {'source': source, 'event': {'message_id': request}, 'provenance': {
                'runtime_home': str(db.db_path.parent), 'platform': 'telegram', 'connector': 'fixture'},
            }
        row = admit_session_input(db, epoch=epoch, principal_id=principal, session_id=owner.session_id,
                                  request_id=request, payload=payload)
        claimed = claim_session_input(db, epoch=epoch, session_id=owner.session_id)
        assert claimed['admission_id'] == row['admission_id']
        settle_session_input(db, epoch=epoch, admission_id=row['admission_id'], generation=claimed['generation'],
                             outcome='completed')
        return row['admission_id']

    old = admit('earlier-message')
    with ProjectStore(owner) as store:
        project = store.create('order', 'Check reply', '')['subagent_id']
    opened = _call(owner, **_args(project))
    close = {'op': 'close', 'project_id': project, 'wait_id': opened['wait_id'], 'reason': 'reply'}
    assert _call(owner, **close, reply_ref={'kind': 'session_admission', 'admission_id': old}).get('error')
    unrelated = admit('ordinary-queue-input', native=False)
    new = admit('later-message')
    valid_ref = {'kind': 'session_admission', 'admission_id': new}
    bad_refs = [
        new, {'kind': 'session_admission'}, {**valid_ref, 'kind': 'home_inbox'},
        {**valid_ref, 'profile_id': 'fixture'}, {**valid_ref, 'admission_id': ''},
        {**valid_ref, 'admission_id': 42}, {**valid_ref, 'admission_id': f' {new}'},
        {**valid_ref, 'admission_id': unrelated},
    ]
    for ref in bad_refs:
        assert _call(owner, **close, reply_ref=ref).get('error'), ref
    assert _call(owner, op='select', project_id=project, wait_id=opened['wait_id']) == opened
    assert _call(owner, **close, reply_ref=valid_ref)['state'] == 'CLOSED'
