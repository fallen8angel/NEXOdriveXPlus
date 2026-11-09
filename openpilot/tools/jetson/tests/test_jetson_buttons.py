"""Run the shipped management page handlers with a simulated DOM and HTTP."""
from pathlib import Path
import runpy
import shutil
import subprocess

import pytest


def test_inline_checks_pending_duplicate_empty_http_error_and_download():
  node = shutil.which('node')
  if not node:
    pytest.skip('Node.js required for JavaScript page test')
  page = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'selfdrive/carrot/server/features/jetson_pages.py'))['PAGE']
  script = page.split('<script>', 1)[1].split('</script>', 1)[0]
  harness = """
const assert = require('node:assert/strict');
const elements = new Map();
const buttons = ['model', 'video_test'].map(action=>({dataset:{action}}));
let finish, calls=0, download=null;
global.document = {
  getElementById(id){if(!elements.has(id))elements.set(id,{});return elements.get(id);},
  querySelectorAll(s){return s==='button[data-action]'?buttons:[];},
  createElement(){return {click(){download=this.download;}};}
};
global.setInterval=()=>{};
global.fetch=async url=>{
  if(url==='/api/jetson/action'){calls++;return await new Promise(resolve=>finish=resolve);}
  return {ok:true,json:async()=>({})};
};
"""
  checks = """
(async()=>{
  const running = buttons[1].onclick();
  assert.match($('checkResult').textContent,/검사 중/);
  assert.equal(buttons.every(b=>b.disabled),true);
  await buttons[0].onclick();
  assert.equal(calls,1);
  finish({ok:true,status:200,text:async()=>JSON.stringify({ok:true,note:'수신 영상 확인 불가'})});
  await running;
  assert.match($('checkResult').textContent,/수신 영상 확인 불가/);
  assert.equal($('checkDownloads').hidden,false);
  $('checkJson').onclick();
  assert.equal(download,'jetson-video_test.json');
  const error = buttons[0].onclick();
  finish({ok:false,status:502,text:async()=>'SSH host key failed'});
  await error;
  assert.match($('checkResult').textContent,/HTTP 502.*SSH host key failed/);
  $('checkTxt').onclick();
  assert.equal(download,'jetson-model.txt');
  const empty = buttons[1].onclick();
  finish({ok:true,status:200,text:async()=>''});
  await empty;
  assert.match($('checkResult').textContent,/응답 내용 없음/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
  subprocess.run([node, '-'], input=harness + script + checks, encoding='utf-8', check=True, timeout=15)
