"""A real process exits after accepting mail; its successor reuses the child and its history."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_COMMON = r'''
import json, os, sqlite3
from pathlib import Path
from openai.types.chat import ChatCompletion
from run_agent import AIAgent
from hermes_state import SessionDB
from tools import delegate_projects
from tools.delegate_mail import mail_action
from tools.delegate_mail_store import MailStore
home = Path(os.environ['HERMES_HOME'])
db = SessionDB(db_path=home/'state.db')
if not db.get_session('main'):
    db.create_session('main', source='cli')
parent = AIAgent(model='gpt-4.1', provider='custom', base_url='http://127.0.0.1:9/v1',
    api_key='test-only', api_mode='chat_completions', session_id='main', session_db=db,
    enabled_toolsets=['delegation'], skip_context_files=True, load_soul_identity=False,
    skip_memory=True, quiet_mode=True)
delegate_projects._origin = lambda: {'wake': False}
seen = []
def model(self, payload, *args, **kwargs):
    seen.append((self.session_id,payload['messages']))
    return ChatCompletion(id='restart',object='chat.completion',created=1,model='gpt-4.1',
        choices=[{'index':0,'message':{'role':'assistant','content':'SAVED_FINDING'},'finish_reason':'stop'}])
AIAgent._interruptible_api_call = model
AIAgent._interruptible_streaming_api_call = model
'''


def test_pending_mail_survives_process_exit_and_is_admitted_once(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("compression:\n  enabled: false\ndelegation:\n  worktree_isolation: false\nauxiliary:\n  title_generation:\n    enabled: false\n")
    repo = str(Path(__file__).resolve().parents[2])
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": repo}
    producer = _COMMON + r'''
entry=json.loads(parent._dispatch_delegate_task({'tasks':[{'project':'order-42','goal':'Analyse the supplier terms'}]}))['projects'][0]
mail=mail_action(parent,'message',entry['subagent_id'],'MONDAY_MAIL','mail-42')
# Assignment before native admission is also recoverable; no in-process registry survives os._exit.
from types import SimpleNamespace
with MailStore(SimpleNamespace(session_id=entry['session_id'],_session_db=db)) as store:
    store.delivery(set())
with sqlite3.connect(home/'state.db') as conn:
    before=conn.execute('SELECT id,session_id,role,content,tool_calls,platform_message_id FROM messages ORDER BY id').fetchall()
(home/'proof.json').write_text(json.dumps({'entry':entry,'mail':mail,'before':before}))
os._exit(0)
'''
    first = subprocess.run([sys.executable, "-c", producer], env=env, cwd=repo, capture_output=True, text=True, timeout=30, check=False)
    assert first.returncode == 0, first.stdout + first.stderr
    consumer = _COMMON + r'''
proof=json.loads((home/'proof.json').read_text())
delegate_projects.recover_projects(parent)
delegate_projects.recover_projects(parent)
assert len(seen)==1,seen
assert seen[0][0]==proof['entry']['session_id']
assert all(s in json.dumps(seen[0][1]) for s in ('SAVED_FINDING','MONDAY_MAIL'))
with sqlite3.connect(home/'state.db') as conn:
    old=[list(conn.execute('SELECT id,session_id,role,content,tool_calls,platform_message_id FROM messages WHERE id=?',(r[0],)).fetchone()) for r in proof['before']]
    assert old==proof['before']
    assert conn.execute('SELECT COUNT(*) FROM delegated_projects').fetchone()[0]==1
    assert conn.execute('SELECT delivered_at IS NOT NULL FROM delegation_mail WHERE message_id=?',(proof['mail']['message_id'],)).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE role='user' AND content LIKE '%MONDAY_MAIL%'").fetchone()[0]==1
parent.close(); db.close()
print(json.dumps({'ok':True,'session_id':proof['entry']['session_id']}))
'''
    second = subprocess.run([sys.executable, "-c", consumer], env=env, cwd=repo, capture_output=True, text=True, timeout=30, check=False)
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(second.stdout.strip().splitlines()[-1])["ok"] is True


@pytest.mark.parametrize("worktree", [False, True])
def test_inflight_task_and_admitted_reply_survive_a_process_death(tmp_path, worktree):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("compression:\n  enabled: false\ndelegation:\n  worktree_isolation: false\nauxiliary:\n  title_generation:\n    enabled: false\n")
    repo = str(Path(__file__).resolve().parents[2])
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": repo}
    if worktree:
        workspace = home / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "--allow-empty", "-qm", "Initial"], check=True)
        (home / "config.yaml").write_text(f"compression:\n  enabled: false\ndelegation:\n  worktree_isolation: true\nterminal:\n  cwd: {workspace}\nauxiliary:\n  title_generation:\n    enabled: false\n")
        env.update(TERMINAL_CWD=str(workspace), MAIL_WORKTREE_PROOF="1")
    producer = _COMMON + r'''
from tests.tools.test_delegate_mail_live import _response
import threading
rounds={}
def interrupted_model(self,payload,*a,**k):
    rounds[self.session_id]=rounds.get(self.session_id,0)+1
    if rounds[self.session_id]==1:
        if os.environ.get('MAIL_WORKTREE_PROOF'):
            from tools.terminal_tool import get_session_cwd
            (Path(get_session_cwd(self._current_task_id))/'evidence.txt').write_text('original worktree evidence')
        return _response(args={'action':'message','subagent_id':'parent','message':'QUESTION_BEFORE_CRASH'})
    if 'REPLY_BEFORE_CRASH' in json.dumps(payload['messages']):
        with sqlite3.connect(home/'state.db') as conn:
            before=conn.execute('SELECT id,session_id,role,content,tool_calls,platform_message_id FROM messages ORDER BY id').fetchall()
        (home/'before.json').write_text(json.dumps(before))
        os._exit(0)
    return _response(args={'action':'wait','timeout_seconds':1})
AIAgent._interruptible_api_call=interrupted_model
AIAgent._interruptible_streaming_api_call=interrupted_model
entry=json.loads(parent._dispatch_delegate_task({'tasks':[{'project':'order-42','goal':'Investigate the original delivery request'}]}))['projects'][0]
(home/'entry.json').write_text(json.dumps(entry))
mail_action(parent,'message',entry['subagent_id'],'REPLY_BEFORE_CRASH','parent-reply')
assert threading.Event().wait(15), 'child never reached the crash window'
'''
    first = subprocess.run([sys.executable, "-c", producer], env=env, cwd=repo, capture_output=True, text=True, timeout=30, check=False)
    assert first.returncode == 0, first.stdout + first.stderr
    consumer = _COMMON + r'''
from tests.waiting import wait_until
entry=json.loads((home/'entry.json').read_text())
def continued(self,payload,*a,**k):
    seen.append((self.session_id,payload['messages']))
    assert 'REPLY_BEFORE_CRASH' in json.dumps(payload['messages'])
    if os.environ.get('MAIL_WORKTREE_PROOF'):
        from tools.terminal_tool import get_session_cwd
        assert (Path(get_session_cwd(self._current_task_id))/'evidence.txt').read_text()=='original worktree evidence'
    return ChatCompletion(id='continued',object='chat.completion',created=1,model='gpt-4.1',choices=[{'index':0,'message':{'role':'assistant','content':'CONTINUED_SAME_TASK'},'finish_reason':'stop'}])
AIAgent._interruptible_api_call=continued
AIAgent._interruptible_streaming_api_call=continued
delegate_projects.recover_projects(parent)
def finished():
    with sqlite3.connect(home/'state.db') as conn:
        return conn.execute('SELECT state FROM delegated_project_messages WHERE message_id=?',(entry['message_id'],)).fetchone()[0]=='completed'
wait_until(finished,timeout=15,what='original task resumed after process loss')
assert len(seen)==1 and seen[0][0]==entry['session_id'],seen
with sqlite3.connect(home/'state.db') as conn:
    before=json.loads((home/'before.json').read_text())
    old=[list(conn.execute('SELECT id,session_id,role,content,tool_calls,platform_message_id FROM messages WHERE id=?',(r[0],)).fetchone()) for r in before]
    assert old==before
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE role='user' AND platform_message_id=?",(entry['message_id'],)).fetchone()[0]==1
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE role='user' AND content LIKE '%REPLY_BEFORE_CRASH%'").fetchone()[0]==1
parent.close();db.close()
print(json.dumps({'ok':True}))
'''
    second = subprocess.run([sys.executable, "-c", consumer], env=env, cwd=repo, capture_output=True, text=True, timeout=30, check=False)
    assert second.returncode == 0, second.stdout + second.stderr
