"""Real daemon/model policy functions with hardware and OS primitives doubled."""
import ast
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from openpilot.selfdrive.modeld.jetlink.compat import ProtocolChoice, Reconnect
from openpilot.selfdrive.modeld.jetlink.spec import ModelSpec
from openpilot.selfdrive.modeld.jetlink.transport import UnsafeCleanupError


def functions(filename, names=None):
  source=Path(__file__).resolve().parents[1]/'jetlink'/filename
  tree=ast.parse(source.read_text(encoding='utf8'))
  tree.body=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and (names is None or n.name in names)]
  return compile(tree,str(source),'exec')


@pytest.mark.parametrize('failure',['late_boot','enumeration','NCM','server_late','reboot','power_cycle','cable'])
@pytest.mark.parametrize('tcp',[False,True])
@pytest.mark.parametrize('failure_attempts', [1, 2])
def test_daemon_recovers_with_backoff_without_losing_native_contract(monkeypatch,tmp_path,failure,tcp,failure_attempts):
  flag=[True]
  clock=[0.]
  setups=[]
  versions=[]
  attempts=[0]
  active_transports=[]
  contract=ModelSpec('a'*64,123,4,{'img':(1,12,2,2),'features_buffer':(1,2,4),
                     'desire_pulse':(1,2,3),'traffic_convention':(1,2),'action_t':(1,1)},
                     {'output':(1,7)},{'hidden_state':slice(3,7),'plan':slice(0,3)})
  listener=SimpleNamespace(bind=lambda *a:None,listen=lambda *a:None,settimeout=lambda *a:None,close=lambda:None)
  params=SimpleNamespace(get_bool=lambda key:False)
  fcntl=ModuleType('fcntl')
  fcntl.LOCK_EX,fcntl.LOCK_NB,fcntl.flock=1,2,lambda *a:None
  monkeypatch.setitem(sys.modules,'fcntl',fcntl)
  monkeypatch.delenv('NEXO_JETLINK_TCP_HOST',raising=False)
  def setup(**kwargs):
    setups.append(kwargs)
    attempts[0]+=1
    assert attempts[0] <= failure_attempts + 1  # Regression cannot loop indefinitely.
    assert not active_transports  # Previous session closed before any rebind.
    if attempts[0]<=failure_attempts and failure not in ('server_late','late_boot'):
      raise OSError(failure)
  def transport(*args,**kwargs):
    versions.append(kwargs['version'])
    active_transports.append(kwargs['version'])
    return SimpleNamespace(close=lambda:active_transports.pop())
  def tcp_connect(host,version):
    assert host=='192.0.2.1'
    setup(takeover_display=False)
    return transport(version=version)
  class Client:
    def __init__(self,t):
      self.t=t
    def hello(self,**kwargs):
      if attempts[0]<=failure_attempts:
        raise TimeoutError(failure)
      return {'protocol':versions[-1]}
    def ensure_engine(self,spec):
      assert spec is contract
      self.spec=spec
    def infer(self,*args,**kwargs):
      return np.zeros(7,np.float32)
    def state(self,**kwargs):
      return {}
    def close(self):
      self.t.close()
  class FakePath:
    def __init__(self,path):
      self.path=path
    def unlink(self,**kwargs):
      pass
    def iterdir(self):
      return iter([SimpleNamespace(name='test-udc')])
  def serve(*args):
    assert args[2] is contract
    flag[0]=False
  records=[]
  scope={'Path':FakePath,'SOCKET':'mock','load_spec':lambda:contract,'ModelSpec':ModelSpec,
         'socket':SimpleNamespace(socket=lambda *a:listener,AF_UNIX=1,SOCK_STREAM=1),
         'os':SimpleNamespace(chmod=lambda *a:None,environ={'NEXO_JETLINK_TCP_HOST':'192.0.2.1'} if tcp else {}),
         'time':SimpleNamespace(monotonic=lambda:clock[0],sleep=lambda delay:clock.__setitem__(0,clock[0]+delay)),
         'np':np,'Params':lambda:params,'ProtocolChoice':ProtocolChoice,'Reconnect':Reconnect,
         'RECOVERY':Reconnect(),'SESSION':{},'publish':lambda *a,**k:records.append((a,k)),
         'enabled':lambda:flag[0],'setup_gadget':setup,'JetlinkFfsTransport':transport,
         'JetlinkClient':Client,'serve_modeld':serve,'FFS_MOUNT':'mock','GADGET':'mock',
         'JetlinkTcpTransport':SimpleNamespace(connect=tcp_connect),
         'UnsafeCleanupError':UnsafeCleanupError,
         'cloudlog':SimpleNamespace(warning=lambda *a:None,exception=lambda *a:None),
         'open':lambda *a:SimpleNamespace(close=lambda:None)}
  exec(functions('daemon.py',{'main'}),scope)
  scope['main']()
  assert attempts[0]==failure_attempts+1 and clock[0]==(2. if failure_attempts==1 else 6.)
  assert not active_transports
  assert all(v['takeover_display'] is not tcp for v in setups)
  assert scope['SESSION']['verified_sha256']==contract.sha256
  assert versions==([2,3,2][:failure_attempts+1] if failure in ('late_boot','server_late') else [2])
  assert scope['RECOVERY'].status()['reconnect_count']==failure_attempts


@pytest.mark.parametrize('active',[False,True])
def test_existing_local_fallback_keeps_active_failure_restart_policy(tmp_path,active):
  scope={'np':np,'time':SimpleNamespace(monotonic=lambda:10.),'FAULT':tmp_path/'fault',
         'cloudlog':SimpleNamespace(warning=lambda *a:None,exception=lambda *a:None),
         'os':SimpleNamespace(getenv=lambda *a:False),'json':json}
  exec(functions('model.py',{'JoiningModel'}),scope)
  model=scope['JoiningModel'].__new__(scope['JoiningModel'])
  model.sm=SimpleNamespace(update=lambda *a:None)
  model._write_status=model._try_join=lambda:None
  model.spec=SimpleNamespace(output_slices={'hidden_state':slice(0,4)})
  model.prev_desire=np.zeros(3,np.float32)
  model.views={'desire':np.zeros(3,np.float32),'traffic_convention':np.zeros(2,np.float32),
               'action_t':np.zeros(1,np.float32),'prev_feat':np.zeros(4,np.float32)}
  model.warp=lambda *a:None
  model.frame=0
  model.reset=True
  model.packed=np.zeros(10,np.float32)
  def fail(*a):
    raise OSError('cable disconnected')
  model.client=SimpleNamespace(infer=fail,close=lambda:None)
  model.active=active
  reset=[]
  model._reset_small=lambda:reset.append(True)
  model.small_runs=0
  model.small=SimpleNamespace(run=lambda *a:'LOCAL')
  inputs={'desire_pulse':np.zeros(3,np.float32),'traffic_convention':np.zeros(2,np.float32),
          'action_t':np.zeros(1,np.float32)}
  if active:
    with pytest.raises(RuntimeError,match='safe modeld restart'):
      model.run(None,None,inputs,False)
    assert reset and (tmp_path/'fault').exists()
  else:
    assert model.run(None,None,inputs,False)=='LOCAL'
    assert not reset and not (tmp_path/'fault').exists()
  assert not model.active and model.client is None
