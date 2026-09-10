"""Run diagnostic recorder and production renderer in Node; no browser or DB."""
import json
import shutil
import subprocess
import unittest

from roly.auction_live_panel import JS
from tests.test_live_panel_component import DOM_HARNESS


class AuctionDiagnosticsTests(unittest.TestCase):
    def run_js(self, script):
        node = shutil.which("node")
        self.assertIsNotNone(node)
        harness = DOM_HARNESS + r"""
const api=await import('data:text/javascript;base64,'+Buffer.from(fixture.source).toString('base64'));
const events=()=>root.querySelector('.auction-diagnostics')?.children.map(n=>JSON.parse(n.textContent)) || [];
await Promise.resolve();
"""
        result = subprocess.run([node, "--input-type=module", "-e", harness + script],
                                input=json.dumps({"source": JS}), text=True, encoding="utf-8",
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_input_pending_ack_and_confirmation_dom_keep_exact_uuid(self):
        self.run_js(r"""
add.onclick();clock=250;submit.onclick();const bid=sent.at(-1);
assert.equal(events().filter(e=>e.type==='input').length,0,'serialization is deferred beyond the click handler');
await Promise.resolve();
const inputEvent=events().find(e=>e.type==='input'),pendingEvent=events().find(e=>e.type==='pending_dom');
assert.equal(inputEvent.request_id,bid.command.request_id);assert.equal(inputEvent.at_ms,250);
assert.equal(pendingEvent.request_id,inputEvent.request_id);assert.equal(pendingEvent.matches,true);
assert.equal(pendingEvent.click_elapsed_ms,0);assert.equal(pendingEvent.display_lot_id,11);
clock=450;data={...data,transport:{...data.transport,frame_id:2,request:bid,
 ack:{...bid.command,status:'accepted',message:'Accepted'}}};cleanup=show();await Promise.resolve();
const ack=events().find(e=>e.type==='ack'),dom=events().find(e=>e.type==='ack_dom');
assert.equal(ack.request_id,inputEvent.request_id);assert.equal(ack.at_ms,450);
assert.equal(ack.click_elapsed_ms,200);assert.equal(dom.request_id,inputEvent.request_id);
assert.equal(dom.at_ms,450);assert.equal(dom.click_elapsed_ms,200);assert.equal(dom.matches,true);
assert.equal(root.querySelector('.auction-diagnostics').dataset.observation,'dom-not-paint');
cleanup();
""")

    def test_http_ack_retains_only_allowlisted_numeric_stage_timings(self):
        self.run_js(r"""
cleanup();delete parent._rolyLivePanel;sent.length=0;const network=[];
globalThis.fetch=(url,options)=>new Promise(resolve=>network.push({url,options,resolve}));
storage.set('login-key','test-token-with-at-least-twenty-characters');
data={...data,direct:{live_url:'/api/auction/live',bid_url:'/api/auction/bid',epoch:'server-one',event_id:3,storage_key:'login-key'},
 transport:{context:'account-event',frame_id:0,request:null}};
cleanup=show();const polling=JSON.parse(network[0].options.body);clock+=100;
network[0].resolve({ok:true,json:async()=>({context:'account-event',epoch:'server-one',panel:{...data,
 server_now:1000.2,transport:{context:'account-event',request:polling,server_elapsed_ms:30}}})});
await new Promise(setImmediate);
const amount=controls.querySelector('.bid-amount');amount.value='10';amount.oninput();controls.querySelector('.bid-submit').onclick();
const bidding=JSON.parse(network[1].options.body);clock+=120;
network[1].resolve({ok:true,json:async()=>({context:'account-event',epoch:'server-one',request:bidding,server_elapsed_ms:70,callback_elapsed_ms:60,
 timings:{pool_checkout:{count:1,elapsed_ms:5,errors:0,token:'private-extra'},writer_begin:{count:1,elapsed_ms:20,errors:0},
 bid_commit_pipeline:{count:1,elapsed_ms:30,errors:0},password:{count:1,elapsed_ms:99,errors:0},
 statement:{count:1,elapsed_ms:'secret-as-string',errors:0}},
 ack:{...bidding.command,status:'accepted',message:'Accepted'}})});
await new Promise(setImmediate);
const ack=events().find(e=>e.type==='ack');
assert.deepEqual(ack.timings,{pool_checkout:{count:1,elapsed_ms:5,errors:0},writer_begin:{count:1,elapsed_ms:20,errors:0},bid_commit_pipeline:{count:1,elapsed_ms:30,errors:0}});
assert.equal(ack.server_elapsed_ms,70);assert.equal(ack.callback_elapsed_ms,60);
assert.equal(ack.request_id,bidding.command.request_id);
const raw=JSON.stringify(events());
for(const secret of ['test-token-with-at-least-twenty-characters','account-event','private-extra','secret-as-string','password','login-key','server-one'])assert.ok(!raw.includes(secret),secret);
const metadata=root.querySelector('.auction-diagnostics').dataset;
assert.equal(metadata.serverEpoch,'server-one');assert.equal(metadata.eventId,'3');
const metadataRaw=JSON.stringify(metadata);
for(const secret of ['test-token-with-at-least-twenty-characters','account-event','login-key','storage_key','context','token'])assert.ok(!metadataRaw.includes(secret),secret);
cleanup();
""")

    def test_late_old_lot_ack_is_retained_without_claiming_confirmation_on_new_lot(self):
        self.run_js(r"""
add.onclick();submit.onclick();const bid=sent.at(-1);clock+=100;
data={...data,lot:{id:12,status:'OPEN',highest_bid:null},stage:{...stage,lot_id:12,deadline:1031},
 transport:{...data.transport,frame_id:2,request:bid}};cleanup=show();
clock+=100;data={...data,transport:{...data.transport,frame_id:3,ack:{...bid.command,status:'accepted',message:'Previous accepted'}}};
cleanup=show();await Promise.resolve();
assert.equal(events().filter(e=>e.type==='ack' && e.request_id===bid.command.request_id).length,1);
assert.equal(events().filter(e=>e.type==='ack_dom' && e.request_id===bid.command.request_id).length,0);
assert.equal(events().find(e=>e.type==='ack').lot_id,11);
assert.equal(events().filter(e=>e.type==='state_dom').at(-1).lot_id,12);
assert.doesNotMatch(controls.querySelector('.bid-feedback').textContent,/Previous accepted/);
cleanup();
""")

    def test_stale_ack_never_overwrites_new_command_metrics_or_adds_false_receipt(self):
        self.run_js(r"""
const observed=[],channel=api.createLiveChannel({context:'private-context',now:()=>clock,uuid:()=>crypto.randomUUID(),emit:()=>{},readPending:()=>null,writePending:()=>{},observe:(type,value)=>observed.push({type,...value})});
const first=channel.submit(11,10,clock);clock+=100;
channel.receiveAck({context:'private-context',request:first,ack:{...first.command,status:'accepted'},server_elapsed_ms:80});
const second=channel.submit(11,20,clock);clock+=90;
channel.receiveAck({context:'private-context',request:second,ack:{...second.command,status:'accepted'},server_elapsed_ms:60});
clock+=20;channel.receiveAck({context:'private-context',request:first,ack:{...first.command,status:'accepted'},server_elapsed_ms:9999});
assert.equal(channel.commandMetrics.request_id,second.command.request_id);assert.equal(channel.commandMetrics.server_elapsed_ms,60);
assert.equal(observed.filter(e=>e.type==='ack').length,2);assert.equal(channel.lastAcknowledgement.request_id,second.command.request_id);
cleanup();
""")

    def test_state_rows_survive_remount_and_reject_stale_frames(self):
        self.run_js(r"""
clock=300;data={...data,lot:{...data.lot,highest_bid:10},stage:{...stage,price:'10 P',bid_id:32},
 transport:{...data.transport,frame_id:3,revision:120}};cleanup=show();await Promise.resolve();
let rows=events().filter(e=>e.type==='state_dom');const event=rows.at(-1);
assert.equal(event.bid_id,32);assert.equal(event.lot_id,11);assert.equal(event.amount,10);
assert.equal(event.deadline,1030);assert.equal(event.revision,120);assert.equal(event.price_matches,true);
const count=rows.length;clock+=100;cleanup=show();await Promise.resolve();assert.equal(events().filter(e=>e.type==='state_dom').length,count);
data={...data,lot:{...data.lot,highest_bid:5},stage:{...stage,price:'5 P',bid_id:31},transport:{...data.transport,frame_id:2,revision:119}};
cleanup=show();await Promise.resolve();rows=events().filter(e=>e.type==='state_dom');
assert.equal(rows.length,count);assert.equal(rows.at(-1).revision,120);assert.equal(rows.at(-1).bid_id,32);
cleanup();
""")

    def test_late_confirmation_for_same_uuid_cannot_replace_original_bid_timing(self):
        self.run_js(r"""
const observed=[],channel=api.createLiveChannel({context:'same-uuid',now:()=>clock,uuid:()=>crypto.randomUUID(),emit:()=>{},
 readPending:()=>null,writePending:()=>{},observe:(type,value)=>observed.push({type,...value})});
const first=channel.submit(11,10,clock);clock+=3500;const confirmation=channel.pump();
assert.equal(confirmation.command.request_id,first.command.request_id);
clock+=100;channel.receiveAck({context:'same-uuid',request:first,ack:{...first.command,status:'accepted'},server_elapsed_ms:3000});
clock+=20;channel.receiveAck({context:'same-uuid',request:confirmation,ack:{...confirmation.command,status:'accepted'},server_elapsed_ms:5});
assert.equal(channel.commandMetrics.server_elapsed_ms,3000);assert.equal(observed.filter(e=>e.type==='ack').length,1);
assert.equal(channel.sent.has(confirmation.seq),false);cleanup();
""")

    def test_restored_pending_has_no_invented_click_or_elapsed_evidence(self):
        self.run_js(r"""
const target=new Element('target'),recorder=api.createAuctionDiagnostics({now:()=>clock});recorder.mount(target);
const command={request_id:crypto.randomUUID(),lot_id:11,amount:10};
const channel=api.createLiveChannel({context:'restored',now:()=>clock,uuid:()=>crypto.randomUUID(),emit:()=>{},
 readPending:()=>command,writePending:()=>{},observe:(type,value,at)=>recorder.record(type,value,at)});
const confirmation=channel.pump();clock+=50;
channel.receiveAck({context:'restored',request:confirmation,ack:{...command,status:'accepted'}});await Promise.resolve();
const rows=target.querySelector('.auction-diagnostics').children.map(n=>JSON.parse(n.textContent));
assert.equal(rows.filter(e=>e.type==='input').length,0);assert.equal(rows.find(e=>e.type==='attempt').restored,true);
const ack=rows.find(e=>e.type==='ack');assert.equal(ack.request_id,command.request_id);
assert.equal(ack.command_elapsed_ms,undefined);assert.equal(ack.click_elapsed_ms,undefined);
recorder.dispose();cleanup();
""")

    def test_idle_ticks_do_not_reserialize_or_repeat_the_buffer(self):
        self.run_js(r"""
const baseline=events().length,original=JSON.stringify;let serializedEvents=0;
JSON.stringify=(value,...rest)=>{if(value?.type && value?.sequence)serializedEvents++;return original(value,...rest);};
for(let i=0;i<100;i++)advance(100);
await Promise.resolve();JSON.stringify=original;
assert.equal(serializedEvents,0);assert.equal(events().length,baseline);cleanup();
""")

    def test_recorder_is_bounded_expires_and_does_not_copy_arbitrary_fields(self):
        self.run_js(r"""
const target=new Element('target'),scheduled=[];
const recorder=api.createAuctionDiagnostics({now:()=>clock,timeOrigin:()=>123456,visible:()=>false,schedule:fn=>scheduled.push(fn),limit:3,ttlMs:1000});
recorder.mount(target);
for(let i=0;i<700;i++)recorder.record('ack',{request_id:'bad-secret',lot_id:11,amount:i,context:'secret-context',token:'secret-token',
 message:'private-error',password:'private-password',timings:{read_pipeline:{count:1,elapsed_ms:3,errors:0,sql:'SELECT private'},private:{count:1,elapsed_ms:1,errors:0}}});
assert.equal(scheduled.length,1);scheduled.shift()();
const container=target.querySelector('.auction-diagnostics');assert.equal(container.children.length,3);
assert.equal(container.dataset.dropped,'697');assert.equal(container.dataset.timeOriginMs,'123456');
assert.deepEqual(container.children.map(n=>JSON.parse(n.textContent).amount),[697,698,699]);
const raw=container.children.map(n=>n.textContent).join('');
for(const secret of ['bad-secret','secret-context','secret-token','private','SELECT'])assert.ok(!raw.includes(secret),secret);
clock+=1001;recorder.expire();assert.equal(container.children.length,0);assert.equal(container.dataset.expired,'3');
recorder.dispose();cleanup();
""")

    def test_diagnostics_can_be_disabled_and_unmount_discards_pending_flush(self):
        self.run_js(r"""
data={...data,diagnostics:false};cleanup=show();await Promise.resolve();
assert.equal(root.querySelector('.auction-diagnostics'),null);add.onclick();submit.onclick();await Promise.resolve();
assert.equal(root.querySelector('.auction-diagnostics'),null);
data={...data,diagnostics:true};cleanup=show();assert.ok(root.querySelector('.auction-diagnostics'));
cleanup();await Promise.resolve();await Promise.resolve();assert.equal(root.querySelector('.auction-diagnostics'),null);
""")

    def test_diagnostic_failure_does_not_prevent_bid_or_ack(self):
        self.run_js(r"""
const channel=api.createLiveChannel({context:'safe-context',now:()=>clock,uuid:()=>crypto.randomUUID(),emit:()=>{},
 readPending:()=>null,writePending:()=>{},observe:()=>{throw Error('diagnostic unavailable');}});
const bid=channel.submit(11,10,clock);assert.ok(bid.command);
clock+=100;channel.receiveAck({context:'safe-context',request:bid,ack:{...bid.command,status:'accepted'}});
assert.equal(channel.pending,null);assert.equal(channel.lastAcknowledgement.status,'accepted');cleanup();
""")


if __name__ == "__main__":
    unittest.main()
