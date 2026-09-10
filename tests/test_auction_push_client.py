"""Execute the real socket/HTTP client against deterministic browser peers."""
import json
import shutil
import subprocess
import unittest

from roly.auction_http_client import HTTP_JS, PUSH_JS
from roly.auction_live_panel import JS
from tests.test_live_panel_component import DOM_HARNESS


HARNESS = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=JSON.parse(readFileSync(0,'utf8'));
const {auctionSocketUrl,createPushDelivery}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
globalThis.location=new URL('https://example.test/~/+/auction');
let clock=0,seq=0;const peers=[],calls=[],frames=[],acks=[],errors=[],clocks=[],modes=[];
const config={epoch:'server-one',event_id:3,ws_url:'/api/auction/ws',bid_url:'/api/auction/bid',live_url:'/api/auction/live'};
const token='test-token-at-least-twenty-characters';
const request=(command=null)=>({context:'context-one',epoch:'browser-one',seq:++seq,sent_ms:clock,command});
class Peer {
 constructor(url){this.url=url;this.sent=[];this.closed=false;peers.push(this);}
 send(value){this.sent.push(JSON.parse(value));}
 open(){this.onopen();}
 receive(value){this.onmessage?.({data:JSON.stringify(value)});}
 close(){this.closed=true;}
 disconnect(){this.onclose?.();}
}
const delivery=createPushDelivery({config,context:'context-one',now:()=>clock,
 socketFactory:url=>new Peer(url),readToken:()=>token,makeRequest:()=>request(),
 fetcher:(url,options)=>new Promise((resolve,reject)=>calls.push({url,options,resolve,reject})),
 onFrame:(panel,source)=>frames.push({panel,source}),onAck:ack=>acks.push(ack),
 onError:(message,reload)=>errors.push({message,reload}),onClock:frame=>clocks.push(frame),onMode:mode=>modes.push(mode)});
const panel=(extra={})=>({available:true,status:'RUNNING',server_now:1000,
 stage:{event_id:3},lot:{id:11,status:'OPEN',highest_bid:10},control:null,
 transport:{context:'context-one',request:null},...extra});
const frame=(revision,type='state',extra={})=>({type,revision,detail_revision:revision,epoch:'server-one',context:'context-one',panel:panel(extra)});
const subscribe=()=>{delivery.start();const peer=peers.at(-1);peer.open();return peer;};
const snapshot=(peer,revision=1,extra={})=>peer.receive(frame(revision,'snapshot',{
 ...extra,transport:{context:'context-one',request:peer.sent[0]}}));
const respond=(call,payload)=>call.resolve({ok:true,json:async()=>({epoch:'server-one',context:'context-one',...payload})});
"""


class AuctionPushClientTests(unittest.TestCase):
    def run_js(self, script, *, dom=False):
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", (DOM_HARNESS if dom else HARNESS) + script],
            input=json.dumps({"source": JS}) if dom else json.dumps(HTTP_JS + PUSH_JS),
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cloud_path_and_first_message_auth_keep_secrets_out_of_url(self):
        self.run_js(r"""
const peer=subscribe();
assert.equal(peer.url,'wss://example.test/~/+/api/auction/ws');
assert.equal(peer.sent[0].type,'subscribe');assert.equal(peer.sent[0].session_token,token);
assert.equal(peer.sent[0].server_epoch,'server-one');assert.equal(peer.sent[0].command,null);
assert.ok(!peer.url.includes(token));assert.equal(peer.sent[0].event_id,3);
assert.equal(auctionSocketUrl('/api/auction/ws',{location:new URL('http://127.0.0.1:8501/auction')}),'ws://127.0.0.1:8501/api/auction/ws');
assert.throws(()=>auctionSocketUrl('/api/auction/ws?session='+token));
assert.throws(()=>auctionSocketUrl('/api/auction/ws',{location:globalThis.location,backendBase:'https://foreign.test'}));
""")

    def test_snapshot_stops_reads_without_blocking_bid_or_confirmations(self):
        self.run_js(r"""
const reading=delivery.send(request());const peer=subscribe();snapshot(peer);
await delivery.send(request());assert.equal(calls.length,1);assert.equal(delivery.streaming(),true);
const command={request_id:'00000000-0000-4000-8000-000000000011',lot_id:11,amount:20};
const bid=request(command),writing=delivery.send(bid);
assert.equal(calls.length,2);assert.equal(JSON.parse(calls[1].options.body).confirm_only,false);
respond(calls[1],{request:bid,ack:{...command,status:'accepted'}});await writing;
assert.equal(acks.length,1);assert.equal(frames.length,1);
respond(calls[0],{panel:panel({lot:{id:9},transport:{context:'context-one',request:JSON.parse(calls[0].options.body)}})});await reading;
assert.equal(frames.length,1,'old HTTP response cannot replace the socket snapshot');
const retry=delivery.send(request(command));assert.equal(JSON.parse(calls[2].options.body).confirm_only,true);
respond(calls[2],{request:JSON.parse(calls[2].options.body),ack:{...command,status:'accepted'}});await retry;
assert.equal(peer.sent.length,1,'socket must never carry a bid');
""")

    def test_commit_order_and_cold_sections_survive_small_hot_updates(self):
        self.run_js(r"""
const peer=subscribe();snapshot(peer,4,{views:{teams:{id:'roster'},queue:{id:'queue'},history:{id:'old'}}});
peer.receive(frame(6,'state',{views:{history:{id:'new'}},lot:{id:12}}));
peer.receive(frame(5,'state',{lot:{id:1}}));peer.receive(frame(6,'state',{lot:{id:2}}));
assert.equal(frames.length,2);assert.equal(frames[1].panel.lot.id,12);
assert.deepEqual(frames[1].panel.views,{teams:{id:'roster'},queue:{id:'queue'},history:{id:'new'}});
peer.receive(frame(8,'state',{lot:{id:13}}));assert.equal(frames.at(-1).panel.views.queue.id,'queue');
""")

    def test_disconnect_backoff_full_reconnect_and_old_socket_are_bounded(self):
        self.run_js(r"""
const peer=subscribe();snapshot(peer,4);const oldMessage=peer.onmessage;
peer.disconnect();assert.equal(delivery.streaming(),false);assert.equal(peer.closed,true);
const fallback=delivery.send(request());assert.equal(calls.length,1);
respond(calls[0],{panel:panel({transport:{context:'context-one',request:JSON.parse(calls[0].options.body),revision:5,detail_revision:5}})});await fallback;
clock=999;delivery.tick();assert.equal(peers.length,1);
clock=1000;delivery.tick();const next=peers[1];next.open();snapshot(next,5);
assert.equal(delivery.streaming(),true);assert.equal(frames.length,3,'equal revision reconnect snapshot restores full state');
oldMessage({data:JSON.stringify(frame(999))});assert.equal(frames.length,3);
next.disconnect();clock=2000;delivery.tick();peers[2].disconnect();
clock=3999;delivery.tick();assert.equal(peers.length,3);
clock=4000;delivery.tick();assert.equal(peers.length,4,'consecutive connection failures double the retry delay');
""")

    def test_heartbeat_requires_exact_echo_and_stalled_peer_falls_back(self):
        self.run_js(r"""
const peer=subscribe();snapshot(peer);clock=2000;delivery.tick();
assert.deepEqual(peer.sent.at(-1),{type:'ping',sent_ms:2000});
peer.receive({type:'pong',epoch:config.epoch,context:'context-one',sent_ms:999,server_now:1002});assert.equal(clocks.length,0);
clock=2200;peer.receive({type:'pong',epoch:config.epoch,context:'context-one',sent_ms:2000,server_now:1002.1});assert.equal(clocks.length,1);
clock=7201;delivery.tick();assert.equal(delivery.streaming(),false);assert.equal(peer.closed,true);
""")

    def test_terminal_auth_error_stops_reconnect_and_dispose_ignores_late_frames(self):
        self.run_js(r"""
const peer=subscribe();snapshot(peer);const oldMessage=peer.onmessage;
peer.receive({type:'error',error:{message:'Reload',reload_required:true}});
assert.equal(delivery.stopped(),true);clock=60000;delivery.tick();assert.equal(peers.length,1);
await delivery.send(request());assert.equal(calls.length,0);
delivery.dispose();oldMessage({data:JSON.stringify(frame(999))});assert.equal(frames.length,1);
assert.equal(peer.onmessage,null);
""")

    def test_mismatched_snapshot_echo_and_partial_hot_panel_are_rejected(self):
        self.run_js(r"""
const peer=subscribe();
peer.receive(frame(1,'snapshot',{transport:{context:'context-one',request:{...peer.sent[0],seq:999}}}));
assert.equal(frames.length,0);assert.equal(delivery.stopped(),true);
""")
        self.run_js(r"""
const peer=subscribe();snapshot(peer);
const partial=frame(2);delete partial.panel.lot;peer.receive(partial);
assert.equal(frames.length,1);assert.equal(delivery.stopped(),true);
""")

    def test_component_keeps_socket_on_repaint_and_cleans_final_unmount(self):
        self.run_js(r"""
cleanup();delete parent._rolyLivePanel;sent.length=0;
globalThis.location=new URL('https://example.test/~/+/auction');
const network=[],peers=[];
globalThis.CustomEvent=class {constructor(type,init){this.type=type;this.detail=init.detail;}};
globalThis.dispatchEvent=event=>{for(const fn of listeners.get(event.type) || [])fn(event);};
globalThis.fetch=(url,options)=>new Promise(resolve=>network.push({url,options,resolve}));
globalThis.WebSocket=class {
 constructor(url){this.url=url;this.sent=[];this.closed=false;peers.push(this);}
 send(value){this.sent.push(JSON.parse(value));}close(){this.closed=true;}
};
storage.set('login-key','test-token-with-at-least-twenty-characters');
data={...data,direct:{live_url:'/api/auction/live',bid_url:'/api/auction/bid',ws_url:'/api/auction/ws',epoch:'server-one',event_id:3,storage_key:'login-key'},
 transport:{context:'account-event',frame_id:0,request:null}};
const dispose=show();const peer=peers[0];peer.onopen();const subscription=peer.sent[0];
clock+=100;peer.onmessage({data:JSON.stringify({type:'snapshot',epoch:'server-one',context:'account-event',revision:1,detail_revision:1,
 panel:{...data,views:{history:{kind:'history',bids:[]}},server_now:1000.2,transport:{context:'account-event',request:subscription,server_elapsed_ms:20}}})});
assert.equal(root.dataset.deliveryMode,'websocket');assert.equal(root.dataset.clockCalibrated,'true');
const before=network.length;advance(1000);assert.equal(network.length,before);
const amount=controls.querySelector('.bid-amount');amount.value='10';amount.oninput();controls.querySelector('.bid-submit').onclick();
assert.equal(network.length,before+1);const bidding=JSON.parse(network.at(-1).options.body);
clock+=100;network.at(-1).resolve({ok:true,json:async()=>({context:'account-event',epoch:'server-one',request:bidding,
 ack:{...bidding.command,status:'accepted',message:'Accepted'}})});await new Promise(setImmediate);
assert.equal(root.dataset.lastBidStatus,'accepted');assert.equal(peer.closed,false);
assert.ok(globalThis._rolyAuctionViews.get('account-event').history);
peer.onmessage({data:JSON.stringify({type:'state',epoch:'server-one',context:'account-event',revision:2,detail_revision:2,
 panel:{...data,available:false,status:null,stage:null,lot:null,control:null,views:null,
 transport:{context:'account-event',request:null}}})});
assert.equal(globalThis._rolyAuctionViews.has('account-event'),true);
assert.equal(globalThis._rolyAuctionViews.get('account-event'),null,'deleted event clears companion cache and keeps a tombstone');
data={...data,lot:{id:999},stage:{...stage,lot_id:999}};const lastCleanup=show();
assert.equal(parent._rolyLivePanel.lot,null,'slow framework props cannot restore a deleted event');
assert.equal(peers.length,1);dispose();assert.equal(peer.closed,false,'old framework cleanup must not close a new mount');
lastCleanup();await Promise.resolve();assert.equal(peer.closed,true);assert.equal(peer.onmessage,null);assert.equal(timers.size,0);
""", dom=True)

    def test_companion_tombstone_clears_cards_and_survives_framework_repaint(self):
        self.run_js(r"""
const companion=new Element('host'),target=new Element('section');target.className='auction-component';companion.append(target);
globalThis._rolyAuctionViews=new Map();
const source=fixture.source.slice(0,fixture.source.indexOf('const finite =')).replace('function renderAuctionStage(', 'export default function(');
const renderView=(await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'))).default;
const history={kind:'history',bids:[],live_context:'account-event'};
let dispose=renderView({parentElement:companion,data:history});assert.ok(target.children.length);
globalThis._rolyAuctionViews.set('account-event',null);
for(const listener of listeners.get('roly-auction-frame') || [])listener({detail:{context:'account-event',views:null}});
assert.equal(target.children.length,0);
dispose=renderView({parentElement:companion,data:history});assert.equal(target.children.length,0,'old native props must not resurrect cleared history');
globalThis._rolyAuctionViews.set('account-event',{history});
for(const listener of listeners.get('roly-auction-frame') || [])listener({detail:{context:'account-event',views:{history}}});
assert.ok(target.children.length,'new complete companion section can render after deletion');dispose();
""", dom=True)


if __name__ == "__main__":
    unittest.main()
