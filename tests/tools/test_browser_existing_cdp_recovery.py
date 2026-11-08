"""A verified existing Chrome may republish its own missing discovery file, never relaunch."""
import contextlib
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.bot_desktop import browser_instance as instance
from tools.bot_desktop import browser_launch as launch

pytestmark = pytest.mark.platforms('posix')


@pytest.fixture
def existing(monkeypatch,tmp_path):
    profile=tmp_path/'profile';profile.mkdir()
    (profile/'saved.synthetic').write_text('preserve')
    owner={'pid':111,'start':'synthetic','boot':'synthetic','uid':os.getuid(),'cgroup':None,
           'exe':'/opt/chrome','argv':['/opt/chrome','--user-data-dir='+str(profile),'--remote-debugging-port=41111']}
    lock={'pid':111,'identity':(1,2,3)}
    monkeypatch.setattr(instance,'_lock',lambda _:dict(lock))
    monkeypatch.setattr(instance,'_process',lambda _:dict(owner))
    listener=Mock(return_value=True);monkeypatch.setattr(instance,'_owns_listener',listener)
    real_endpoint=launch._endpoint
    endpoint=Mock(return_value='/devtools/browser/synthetic');monkeypatch.setattr(launch,'_endpoint',endpoint)
    monkeypatch.setattr(instance.socket,'create_connection',Mock(return_value=contextlib.nullcontext()))
    process=Mock(side_effect=AssertionError('existing Chrome must not be relaunched'))
    monkeypatch.setattr(launch.subprocess,'Popen',process)
    monkeypatch.setattr(launch.subprocess,'run',Mock(side_effect=AssertionError('no command forwarding during admission')))
    monkeypatch.setattr(launch,'_positive_port',Mock(side_effect=AssertionError('no arbitrary port allocation')))
    return SimpleNamespace(profile=profile,owner=owner,lock=lock,listener=listener,endpoint=endpoint,
                           real_endpoint=real_endpoint,process=process)


@pytest.mark.parametrize('port_args',[['--remote-debugging-port=41111'],['--remote-debugging-port','41111']])
def test_existing_fixed_owned_listener_recovers_missing_discovery_without_relaunch(existing,port_args):
    item=existing;item.owner['argv']=item.owner['argv'][:2]+port_args
    assert instance.inspect_profile(item.profile)=={'state':'blocked'}
    assert launch.ensure(['/own/guarded-chrome','--user-data-dir='+str(item.profile)])=={'state':'attachable','port':41111}
    target=item.profile/'DevToolsActivePort'
    assert target.read_text()=='41111\n/devtools/browser/synthetic\n'
    assert target.stat().st_mode&0o777==0o600
    assert (item.profile/'saved.synthetic').read_text()=='preserve'
    assert item.lock=={'pid':111,'identity':(1,2,3)}
    assert any(call.kwargs=={'address':'127.0.0.1'} for call in item.listener.call_args_list)
    item.endpoint.assert_called_once_with(41111);item.process.assert_not_called()


@pytest.mark.parametrize('port',[None,'0','-1','65536','abc','41111/path','1e3'])
def test_missing_dynamic_or_invalid_owner_port_never_scans_or_relaunches(existing,port):
    item=existing;item.owner['argv']=item.owner['argv'][:2]
    if port is not None:item.owner['argv'].append('--remote-debugging-port='+port)
    with pytest.raises(instance.ProfileUnavailable):launch.ensure(item.owner['argv'])
    assert not (item.profile/'DevToolsActivePort').exists()
    item.endpoint.assert_not_called();item.process.assert_not_called()


@pytest.mark.parametrize('failure',[False,PermissionError('synthetic unavailable')])
def test_unproved_foreign_or_unreadable_listener_never_publishes(existing,failure):
    item=existing
    if failure is False:item.listener.return_value=False
    else:item.listener.side_effect=failure
    with pytest.raises(instance.ProfileUnavailable):launch.ensure(item.owner['argv'])
    assert not (item.profile/'DevToolsActivePort').exists()
    item.endpoint.assert_not_called();item.process.assert_not_called()


@pytest.mark.parametrize('endpoint',[
    'ws://foreign.invalid:41111/devtools/browser/synthetic',
    'ws://127.0.0.1:41112/devtools/browser/synthetic',
    'ws://127.0.0.1:41111/devtools/page/synthetic',
    'ws://127.0.0.1:41111/devtools/browser/synthetic?unexpected=yes',
])
def test_foreign_or_wrong_endpoint_preserves_original_refusal(monkeypatch,existing,endpoint):
    item=existing
    reply=SimpleNamespace(status=200,read=lambda limit:json.dumps({'webSocketDebuggerUrl':endpoint}).encode())
    connection=SimpleNamespace(request=Mock(),getresponse=lambda:reply,close=Mock())
    monkeypatch.setattr(launch.http.client,'HTTPConnection',lambda *args,**kwargs:connection)
    monkeypatch.setattr(launch,'_endpoint',item.real_endpoint)
    with pytest.raises(instance.ProfileUnavailable):launch.ensure(item.owner['argv'])
    assert not (item.profile/'DevToolsActivePort').exists()
    item.process.assert_not_called()


@pytest.mark.parametrize('field,value',[('uid',-1),('exe','/opt/not-a-browser'),('argv',['/opt/chrome','--user-data-dir=/foreign/profile','--remote-debugging-port=41111'])])
def test_unverified_owner_identity_is_never_recovered(existing,field,value):
    item=existing;item.owner[field]=value
    with pytest.raises(instance.ProfileUnavailable):
        launch.ensure(['/own/guarded-chrome','--user-data-dir='+str(item.profile)])
    item.endpoint.assert_not_called();item.process.assert_not_called()
    assert not (item.profile/'DevToolsActivePort').exists()


@pytest.mark.parametrize('change',['generation','lock'])
def test_argv_owner_snapshot_is_bound_before_publication(monkeypatch,existing,change):
    item=existing;publish=launch._publish
    def changed(profile,pid,port,**kwargs):
        if change=='generation':item.owner['start']='recycled'
        else:item.lock['identity']=(1,2,4)
        return publish(profile,pid,port,**kwargs)
    monkeypatch.setattr(launch,'_publish',changed)
    with pytest.raises(instance.ProfileUnavailable):launch.ensure(item.owner['argv'])
    assert not (item.profile/'DevToolsActivePort').exists()
    item.endpoint.assert_not_called();item.process.assert_not_called()


def test_generation_change_during_endpoint_proof_cannot_publish(existing):
    item=existing
    def changed(port):
        item.owner['start']='recycled'
        return '/devtools/browser/synthetic'
    item.endpoint.side_effect=changed
    with pytest.raises(instance.ProfileUnavailable):launch.ensure(item.owner['argv'])
    assert not (item.profile/'DevToolsActivePort').exists()
    item.process.assert_not_called()


@pytest.mark.parametrize('kind',['file','symlink'])
def test_recovery_never_replaces_existing_unknown_discovery(existing,kind):
    item=existing;target=item.profile/'DevToolsActivePort'
    if kind=='file':target.write_text('unverified discovery')
    else:target.symlink_to(item.profile/'missing.synthetic')
    with pytest.raises(instance.ProfileUnavailable):launch.ensure(item.owner['argv'])
    if kind=='file':assert target.read_text()=='unverified discovery'
    else:assert target.is_symlink()
    item.endpoint.assert_not_called();item.process.assert_not_called()


@pytest.mark.parametrize('kind',['file','symlink'])
def test_concurrent_discovery_creation_is_never_overwritten(existing,kind):
    item=existing;target=item.profile/'DevToolsActivePort'
    def appeared(port):
        if kind=='file':target.write_text('concurrent unverified discovery')
        else:target.symlink_to(item.profile/'missing.synthetic')
        return '/devtools/browser/synthetic'
    item.endpoint.side_effect=appeared
    with pytest.raises(instance.ProfileUnavailable):launch.ensure(item.owner['argv'])
    if kind=='file':assert target.read_text()=='concurrent unverified discovery'
    else:assert target.is_symlink()
    assert not list(item.profile.glob('.DevToolsActivePort-*'))
    item.process.assert_not_called()


def test_human_fixed_forward_still_precedes_agent_recovery(monkeypatch,existing):
    item=existing;forward=Mock(return_value={'opened':True});recover=Mock(side_effect=AssertionError('human flow unchanged'))
    monkeypatch.setattr(launch,'_forward_google',forward)
    monkeypatch.setattr(launch,'_recover_existing_cdp',recover,raising=False)
    assert launch.ensure(item.owner['argv'],open_google=True)=={'opened':True}
    forward.assert_called_once_with(item.owner['argv'],item.profile)
    recover.assert_not_called();item.process.assert_not_called()
