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
const {createHttpDelivery}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
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

    def test_bearer_is_confined_to_header(self):
        self.run_js(r"""
const first=delivery.send(request(1,command));const call=calls[0];
assert.equal(call.url,'/api/auction/bid');assert.ok(!call.url.includes(token));assert.ok(!call.options.body.includes(token));
assert.equal(call.options.headers.Authorization,'Bearer '+token);
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
