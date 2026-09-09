"""Run the actual HTTP delivery code with controlled network promises."""
import json
import shutil
import subprocess
import unittest

from roly.auction_http_client import HTTP_JS


HARNESS = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=JSON.parse(readFileSync(0,'utf8'));
const {auctionApiUrl,auctionXsrfToken,createHttpDelivery}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const calls=[],frames=[],acks=[],errors=[];
const config={bid_url:'/api/auction/bid',live_url:'/api/auction/live',epoch:'server-one',event_id:3};
const token='test-token-at-least-twenty-characters';
let readToken=()=>token;
const delivery=createHttpDelivery({config,readToken:()=>readToken(),
 fetcher:(url,options)=>new Promise((resolve,reject)=>calls.push({url,options,resolve,reject})),
 onFrame:x=>frames.push(x),onAck:x=>acks.push(x),onError:(message,reload)=>errors.push({message,reload})});
const command={request_id:'00000000-0000-4000-8000-000000000011',lot_id:7,amount:10};
const request=(seq,cmd=null)=>({context:'context-one',epoch:'browser-one',seq,sent_ms:seq*100,command:cmd});
const respond=(call,value,status=200)=>call.resolve({ok:status===200,json:async()=>value});
const payload=extra=>({context:'context-one',epoch:'server-one',...extra});
const reply=(call,extra)=>payload({request:JSON.parse(call.options.body),...extra});
const viewReply=(call,extra)=>payload({panel:{...extra,transport:{context:'context-one',request:JSON.parse(call.options.body)}}});
"""


class AuctionHTTPClientTests(unittest.TestCase):
    def run_js(self, script):
        node = shutil.which("node")
        self.assertIsNotNone(node)
        result = subprocess.run([node, "--input-type=module", "-e", HARNESS + script],
                                input=json.dumps(HTTP_JS), capture_output=True, text=True,
                                encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_api_url_uses_verified_backend_or_cloud_page_prefix(self):
        self.run_js(r"""
const cloud=new URL('https://rolymoly-test.streamlit.app/~/+/auction');
const ordinary=new URL('https://example.test/lounge/auction');
const cases=[
  [{location:cloud,backendBase:undefined},'https://rolymoly-test.streamlit.app/~/+'],
  [{location:cloud,backendBase:'https://rolymoly-test.streamlit.app/~/+/'},'https://rolymoly-test.streamlit.app/~/+'],
  [{location:cloud,backendBase:'https://rolymoly-test.streamlit.app/backend/'},'https://rolymoly-test.streamlit.app/backend'],
  [{location:ordinary,backendBase:'https://example.test/custom/prefix/'},'https://example.test/custom/prefix'],
  [{location:ordinary,backendBase:'/custom/prefix/'},'https://example.test/custom/prefix'],
  [{location:ordinary,backendBase:undefined},'https://example.test'],
  [{location:new URL('http://127.0.0.1:8512/auction'),backendBase:undefined},'http://127.0.0.1:8512'],
];
for(const [options,prefix] of cases)for(const action of ['live','bid'])
  assert.equal(auctionApiUrl('/api/auction/'+action,options),prefix+'/api/auction/'+action);
assert.equal(auctionApiUrl('/api/auction/live',{location:undefined,backendBase:undefined}),'/api/auction/live');
assert.equal(calls.length,0);
""")

    def test_api_url_rejects_foreign_or_ambiguous_addresses(self):
        self.run_js(r"""
const location=new URL('https://example.test/auction');
for(const backendBase of [
  'https://foreign.test/base/', '//foreign.test/base/',
  'https://user:password@example.test/base/', 'https://user@example.test/base/',
  'https://example.test/base/?next=/api/auction/live', 'https://example.test/base/#fragment',
  'http://example.test/base/', 'javascript:alert(1)',
])assert.throws(()=>auctionApiUrl('/api/auction/live',{location,backendBase}),backendBase);
assert.throws(()=>auctionApiUrl('/api/auction/live',{
  location:new URL('http://example.test/auction'),backendBase:undefined}));
for(const path of [
  'https://foreign.test/api/auction/bid','//foreign.test/api/auction/bid',
  '/api/auction/live?next=other','/api/auction/bid#fragment',
  '/api/auction/live/','/api/auction/%62id','/~/+/api/auction/live',
  '/api/auction/../bid','/api/auction/live\n','',null,undefined,
])assert.throws(()=>auctionApiUrl(path,{location,backendBase:undefined}),String(path));
assert.equal(calls.length,0);
""")

    def test_untrusted_api_configuration_stops_before_fetch(self):
        self.run_js(r"""
globalThis.location=new URL('https://example.test/auction');
let fetched=0;
const invalid=[
  {base:'https://foreign.test/base/'},
  {base:'https://user:password@example.test/base/'},
  {base:'https://example.test/base/?query=1'},
  {base:'https://example.test/base/#fragment'},
  {path:'https://foreign.test/api/auction/bid'},
  {path:'//foreign.test/api/auction/live'},
  {path:'/api/auction/bid?query=1'},
];
for(const item of invalid)for(const cmd of [null,command]) {
  globalThis.__streamlit={BACKEND_BASE_URL:item.base};
  const denied=[];
  const guarded=createHttpDelivery({
    config:{...config,...(item.path?{live_url:item.path,bid_url:item.path}:{})},
    fetcher:()=>{fetched++;throw new Error('fetch must not run');},readToken:()=>token,
    onFrame:()=>assert.fail('invalid URL rendered a frame'),
    onAck:()=>assert.fail('invalid URL acknowledged a bid'),
    onError:(message,reload)=>denied.push({message,reload}),
  });
  await guarded.send(request(1,cmd));
  await guarded.send(request(2,cmd));
  assert.equal(guarded.stopped(),true);assert.equal(denied.length,1);
  assert.equal(denied[0].reload,true);assert.ok(!denied[0].message.includes(token));
}
assert.equal(fetched,0);assert.equal(calls.length,0);
""")

    def test_xsrf_cookie_preserves_encoding_and_rejects_ambiguous_values(self):
        self.run_js(r"""
const raw='2|abcdef|012345|1788950000';
const encoded='2%7Cabcdef%7C012345%7C1788950000';
for(const [cookie,value] of [
  ['_streamlit_xsrf='+raw,raw],
  ['unrelated=private; _streamlit_xsrf='+encoded+'; another=private',encoded],
  ['_streamlit_xsrf="'+raw+'"',raw],
  ['_streamlit_xsrf="'+encoded+'"',encoded],
  ['_streamlit_xsrf=abcd1234','abcd1234'],
])assert.equal(auctionXsrfToken(cookie),value);
for(const cookie of [
  '',null,42,'unrelated=private','prefix_streamlit_xsrf=value','_streamlit_xsrf_extra=value',
  '_streamlit_xsrf=','_streamlit_xsrf=with space','_streamlit_xsrf=trailing ',
  '_streamlit_xsrf=one\r\nInjected: two','_streamlit_xsrf="unclosed','_streamlit_xsrf=unopened"',
  '_streamlit_xsrf="escaped\\value"','_streamlit_xsrf=nonasciié','_streamlit_xsrf=comma,value',
  '_streamlit_xsrf='+raw+'; _streamlit_xsrf='+raw,
  '_streamlit_xsrf='+raw+'; _streamlit_xsrf=different',
  '_streamlit_xsrf='+'a'.repeat(1025),
])assert.equal(auctionXsrfToken(cookie),null);
assert.equal(calls.length,0);
""")

    def test_live_and_bid_add_only_existing_xsrf_header(self):
        self.run_js(r"""
globalThis.location=new URL('https://rolymoly-test.streamlit.app/~/+/auction');
const cookieToken='2%7Cabcdef%7C012345%7C1788950000';
let seq=0;
for(const cookie of ['other=private; _streamlit_xsrf='+cookieToken,'','_streamlit_xsrf=bad value']) {
  globalThis.document={cookie};
  for(const cmd of [null,command]) {
    const sending=delivery.send(request(++seq,cmd));
    const call=calls.at(-1);
    const expected=cookie.includes(cookieToken)?cookieToken:undefined;
    assert.equal(call.options.headers['X-Xsrftoken'],expected);
    assert.equal(Object.hasOwn(call.options.headers,'X-Xsrftoken'),expected!==undefined);
    assert.equal(call.options.credentials,'same-origin');assert.equal(call.options.redirect,'error');
    assert.equal(call.options.headers['X-Rolymoly-Session'],token);
    assert.equal(call.options.headers.Authorization,undefined);
    assert.equal(call.options.headers['X-Rolymoly-Server-Epoch'],'server-one');
    assert.ok(call.url.startsWith('https://rolymoly-test.streamlit.app/~/+/api/auction/'));
    assert.ok(!call.url.includes(cookieToken));assert.ok(!call.options.body.includes(cookieToken));
    assert.ok(!call.options.body.includes('private'));
    if(cmd)assert.deepEqual(JSON.parse(call.options.body).command,command);
    respond(call,cmd?reply(call,{ack:{...command,status:'accepted'}}):viewReply(call,{snapshot:seq}));
    await sending;
  }
}
globalThis.document={get cookie(){throw new Error('cookie access unavailable');}};
const sending=delivery.send(request(++seq));
const call=calls.at(-1);assert.equal(Object.hasOwn(call.options.headers,'X-Xsrftoken'),false);
respond(call,viewReply(call,{snapshot:seq}));await sending;
assert.equal(errors.length,0);
""")

    def test_invalid_origin_is_rejected_before_cookie_read(self):
        self.run_js(r"""
globalThis.location=new URL('https://example.test/auction');
let cookieReads=0,fetched=0;
globalThis.document={get cookie(){cookieReads++;return '_streamlit_xsrf=private-cookie';}};
for(const foreign of [true,false])for(const cmd of [null,command]) {
  globalThis.__streamlit={BACKEND_BASE_URL:foreign?'https://foreign.test/base/':undefined};
  const rejected=[];
  const guarded=createHttpDelivery({
    config:{...config,...(foreign?{}:{bid_url:'https://foreign.test/bid',live_url:'//foreign.test/live'})},
    fetcher:()=>{fetched++;throw new Error('fetch must not run');},readToken:()=>token,
    onFrame:()=>assert.fail('unexpected frame'),onAck:()=>assert.fail('unexpected ACK'),
    onError:(message,reload)=>rejected.push({message,reload}),
  });
  await guarded.send(request(1,cmd));
  assert.equal(guarded.stopped(),true);assert.equal(rejected.length,1);
}
assert.equal(cookieReads,0);assert.equal(fetched,0);
""")

    def test_bid_ack_does_not_wait_for_blocked_view(self):
        self.run_js(r"""
const reading=delivery.send(request(1));
const bidding=delivery.send(request(2,command));
assert.equal(calls.length,2);
respond(calls[1],payload({request:request(2,command),ack:{...command,status:'accepted'}}));
await bidding;
assert.equal(acks.length,1);assert.equal(frames.length,0);
respond(calls[0],viewReply(calls[0],{snapshot:1}));await reading;
assert.equal(frames.length,1);
""")

    def test_uncertain_write_retries_only_as_confirmation(self):
        self.run_js(r"""
const first=delivery.send(request(1,command));calls[0].reject(new Error('private network detail'));
await first;assert.equal(acks.length,0);assert.equal(errors.length,1);
const retry=delivery.send(request(2,command));
assert.equal(JSON.parse(calls[0].options.body).confirm_only,false);
assert.equal(JSON.parse(calls[1].options.body).confirm_only,true);
assert.deepEqual(JSON.parse(calls[1].options.body).command,command);
respond(calls[1],reply(calls[1],{ack:{...command,status:'accepted'}}));await retry;
assert.equal(acks.length,1);assert.ok(!JSON.stringify(errors).includes('private network detail'));
""")

    def test_restored_pending_is_never_submitted_as_new_write(self):
        self.run_js(r"""
const pending=delivery.send(request(1,command),{restored:true});
assert.equal(JSON.parse(calls[0].options.body).confirm_only,true);
respond(calls[0],reply(calls[0],{ack:{...command,status:'rejected'}}));await pending;
assert.equal(acks[0].ack.status,'rejected');
""")

    def test_superseded_read_cannot_publish_older_snapshot(self):
        self.run_js(r"""
const one=delivery.send(request(1)),two=delivery.send(request(2));
respond(calls[1],viewReply(calls[1],{version:2}));await two;
respond(calls[0],viewReply(calls[0],{version:1}));await one;
assert.deepEqual(frames.map(frame=>frame.version),[2]);
""")

    def test_process_change_stops_without_automatic_fallback(self):
        self.run_js(r"""
const first=delivery.send(request(1,command));
respond(calls[0],{error:{code:'server_epoch_changed',message:'Reload',reload_required:true}},409);await first;
await delivery.send(request(2,command));
assert.equal(delivery.stopped(),true);assert.equal(calls.length,1);assert.equal(acks.length,0);
""")

    def test_foreign_context_reply_does_not_acknowledge(self):
        self.run_js(r"""
const first=delivery.send(request(1,command));
respond(calls[0],{context:'other',epoch:'server-one',ack:{...command,status:'accepted'}});await first;
assert.equal(delivery.stopped(),true);assert.equal(acks.length,0);
""")

    def test_session_is_private_transport_not_url_or_saved_command(self):
        self.run_js(r"""
const original=request(1,command);const first=delivery.send(original);const call=calls[0];
assert.equal(call.url,'/api/auction/bid');assert.ok(!call.url.includes(token));
assert.ok(!JSON.stringify(original).includes(token));assert.ok(!JSON.stringify(command).includes(token));
assert.equal(JSON.parse(call.options.body).session_token,token);
assert.equal(JSON.parse(call.options.body).server_epoch,'server-one');
assert.equal(call.options.headers['X-Rolymoly-Session'],token);
assert.equal(call.options.headers.Authorization,undefined);
assert.equal(call.options.headers['X-Rolymoly-Server-Epoch'],'server-one');
assert.equal(call.options.cache,'no-store');assert.equal(call.options.redirect,'error');
respond(call,reply(call,{ack:{...command,status:'accepted'}}));await first;
""")

    def test_cleared_login_blocks_request(self):
        self.run_js(r"""
readToken=()=>null;await delivery.send(request(1,command));
assert.equal(calls.length,0);assert.equal(delivery.stopped(),true);
""")

    def test_nested_foreign_frame_is_rejected_before_render(self):
        self.run_js(r"""
const first=delivery.send(request(1));
respond(calls[0],payload({panel:{transport:{context:'foreign',request:request(1)}}}));await first;
assert.equal(frames.length,0);assert.equal(delivery.stopped(),true);
""")

    def test_wrong_request_echo_cannot_acknowledge_bid(self):
        self.run_js(r"""
const first=delivery.send(request(1,command));
respond(calls[0],payload({request:request(99,command),ack:{...command,status:'accepted'}}));await first;
assert.equal(acks.length,0);assert.equal(delivery.stopped(),true);
""")


if __name__ == "__main__":
    unittest.main()
