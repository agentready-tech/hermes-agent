"""Private provenance for already-authorized operator input.

Operator access is profile-local, not account linking or a change of authorship.
It never claims unowned legacy history: that still requires the native migration
path. Only SessionAuthority.submit writes this envelope after authorization;
public submission parameters cannot supply it.
"""
from hermes_state_runtime import RuntimeStoreError


def stamp_operator(authority, actor, ref, *, prior=None):
    source = authority.sessions[ref.session_id].source
    payload = {}
    if source is not None and source.user_id != actor.subject:
        payload['local_operator_v1'] = {
            'profile_id': authority.profile_id, 'session_id': ref.session_id, 'principal_id': actor.subject}
    if source is not None:
        from hermes_runtime.adapters.main_control_origin import capture_origin
        if prior is not None and 'native_origin_v1' in prior['payload']:
            payload['native_origin_v1'] = prior['payload']['native_origin_v1']
        else:
            payload.update(capture_origin(authority, ref, source))
    return payload


def native_origin(payload):
    """The private route proof, without changing a control input into a platform input."""
    if 'native_text_v1' in payload:
        return payload
    if 'native_origin_v1' in payload:
        return {'text': '', 'native_text_v1': payload['native_origin_v1']}
    return None


def check_local_input(authority, ref, row, *, source=None):
    payload = row['payload']
    allowed = {'text', 'attachments_v1', 'finite', 'unattended', 'surface_v1', 'display_v1', 'cli_turn_v1'}
    if 'local_operator_v1' in payload:
        allowed.add('local_operator_v1')
        if payload['local_operator_v1'] != {
                'profile_id': authority.profile_id, 'session_id': ref.session_id,
                'principal_id': row['principal_id']}:
            raise RuntimeStoreError('permission_denied')
    else:
        source = source or authority.sessions[ref.session_id].source
        if row['principal_id'] != source.user_id:
            raise RuntimeStoreError('permission_denied')
    if 'native_origin_v1' in payload:
        from gateway.session_envelope import restore_native
        event = restore_native(native_origin(payload), authority.runner)
        route = authority.runner.session_store._generate_session_key(event.source)
        source = source or authority.sessions[ref.session_id].source
        if route != authority.runner.session_store._generate_session_key(source):
            raise RuntimeStoreError('permission_denied')
        allowed.add('native_origin_v1')
    if not {'text'} <= set(payload) <= allowed:
        raise RuntimeStoreError('permission_denied')
