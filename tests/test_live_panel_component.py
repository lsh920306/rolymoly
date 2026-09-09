"""Execute the real browser channel/draft code without network or shared DB."""
import json
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

from roly.auction_live_panel import CHANNEL_JS, JS, COMPONENT_REVISION, live_panel_data, render_live_panel


CHANNEL_HARNESS = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const fixture=JSON.parse(readFileSync(0,'utf8'));
const {createLiveChannel}=await import('data:text/javascript;base64,'+Buffer.from(fixture.source).toString('base64'));
let clock=0,uuidIndex=0,stored=null;const sent=[];
const uuid=()=>`00000000-0000-4000-8000-${String(++uuidIndex).padStart(12,'0')}`;
const make=(context='account-event',load=()=>stored)=>createLiveChannel({context,now:()=>clock,uuid,emit:v=>sent.push(v),readPending:load,writePending:v=>{stored=v?{...v}:null;}});
const frame=(request,ack=null,extra={})=>({context:'account-event',frame_id:request.seq,request,server_elapsed_ms:100,snapshot_elapsed_ms:20,ack,...extra});
const channel=make();
"""

DOM_HARNESS = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const fixture=JSON.parse(readFileSync(0,'utf8'));
const render=(await import('data:text/javascript;base64,'+Buffer.from(fixture.source).toString('base64'))).default;
let clock=0,index=0,timerId=0;const timers=new Map(),listeners=new Map(),storage=new Map(),sent=[];
Object.defineProperty(globalThis,'performance',{configurable:true,value:{now:()=>clock}});
Object.defineProperty(globalThis,'crypto',{configurable:true,value:{randomUUID:()=>`00000000-0000-4000-8000-${String(++index).padStart(12,'0')}`}});
globalThis.setInterval=fn=>{timers.set(++timerId,fn);return timerId;};globalThis.clearInterval=id=>timers.delete(id);
globalThis.addEventListener=(name,fn)=>{if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(fn);};
globalThis.removeEventListener=(name,fn)=>listeners.get(name)?.delete(fn);
globalThis.sessionStorage={getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
class Element {
 constructor(tag){this.tagName=tag;this.className='';this.children=[];this.dataset={};this.attributes={};this.style={setProperty(k,v){this[k]=v;}};this.ownerDocument=doc;this.isConnected=true;this.value='';}
 append(...nodes){for(const n of nodes){n.parent=this;this.children.push(n);}}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}
 replaceWith(node){const i=this.parent.children.indexOf(this);this.parent.children[i]=node;node.parent=this.parent;}
 remove(){if(this.parent){const i=this.parent.children.indexOf(this);if(i>=0)this.parent.children.splice(i,1);}this.isConnected=false;}
 setAttribute(k,v){this.attributes[k]=v;}
 querySelector(q){const found=this.children.find(n=>n.className.split(' ').includes(q.slice(1)));if(found)return found;for(const n of this.children){const v=n.querySelector(q);if(v)return v;}return null;}
}
const doc={defaultView:globalThis,visibilityState:'visible',createElement:tag=>new Element(tag),createElementNS:(_n,tag)=>new Element(tag),addEventListener:globalThis.addEventListener,removeEventListener:globalThis.removeEventListener};
const parent=new Element('host'),root=new Element('section'),stageMount=new Element('div'),stageRoot=new Element('section'),controls=new Element('div');
root.className='live-panel';stageMount.className='live-stage';stageRoot.className='auction-component';controls.className='live-bid-controls';parent.append(root);root.append(stageMount,controls);stageMount.append(stageRoot);
const stage={kind:'stage',player:{name:'Example#TEST',nickname:'Example',champions:[]},bidder:'First bid',price:'No bid',clock_label:'Remaining',clock_text:'',seconds:30,scale:30,event_id:3,lot_id:11,deadline:1030,phase:'RUNNING::OPEN',show_clock:true,ticking:true};
let data={available:true,status:'RUNNING',server_now:1000,stage,lot:{id:11,status:'OPEN',highest_bid:null},control:{team_name:'Team 1',remaining:1000,can_bid:true,context:'account-event'},transport:{context:'account-event',frame_id:0,request:null,server_elapsed_ms:0,snapshot_elapsed_ms:0,ack:null}};
const show=()=>render({parentElement:parent,data,setTriggerValue:(event,value)=>{assert.equal(event,'event');sent.push(value);}});
const advance=ms=>{clock+=ms;for(const fn of [...timers.values()])fn();};
let cleanup=show();
const request=sent[0];assert.ok(request);assert.equal(root.dataset.clockCalibrated,'false');
advance(200);
data={...data,server_now:1000.1,transport:{...data.transport,frame_id:1,request,server_elapsed_ms:100}};
cleanup=show();
assert.equal(root.dataset.clockCalibrated,'true');
const input=controls.querySelector('.bid-amount'),submit=controls.querySelector('.bid-submit');
const add=controls.querySelector('.bid-increments').children.find(b=>b.dataset.increment==='10');
"""


class LivePanelComponentTests(unittest.TestCase):
    def run_js(self, script, *, dom=False):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required to verify the live browser component")
        result = subprocess.run(
            [node, "--input-type=module", "-e", (DOM_HARNESS if dom else CHANNEL_HARNESS) + script],
            input=json.dumps({"source": JS if dom else CHANNEL_JS}), text=True,
            encoding="utf-8", capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ack_driven_poll_has_one_outstanding_request_and_recovers_timeout(self):
        self.run_js(r"""
const first=channel.pump();assert.equal(sent.length,1);
for(let i=0;i<10;i++){clock+=100;channel.pump();}assert.equal(sent.length,1);
clock=2000;const retry=channel.pump();assert.equal(sent.length,2);assert.equal(retry.command,null);
clock=2200;channel.receive(frame(first),1001);assert.equal(channel.outstanding.seq,retry.seq,'old reply cannot acknowledge newer poll');
clock=2300;channel.receive(frame(retry),1002);assert.equal(channel.outstanding,null);
clock=2499;channel.pump();assert.equal(sent.length,2);
clock=2500;channel.pump();assert.equal(sent.length,3);
""")

    def test_lost_command_stays_in_following_envelopes_and_only_exact_receipt_clears(self):
        self.run_js(r"""
const poll=channel.pump();clock=10;const bid=channel.submit(11,0);const command=bid.command;
assert.equal(command.amount,0);assert.equal(sent.length,2);
clock=2010;assert.equal(channel.pump(),null,'normal command latency must not trigger a two-second retry');
clock=3010;const retry=channel.pump();assert.deepEqual(retry.command,command);
channel.receive(frame(poll),1000);assert.deepEqual(channel.pending,command);
channel.receive(frame(retry,{...command,status:'accepted',amount:1}),1002);assert.deepEqual(channel.pending,command);
channel.receive(frame(retry,{...command,status:'pending'}),1002);assert.deepEqual(channel.pending,command);
channel.receive(frame(retry,{...command,status:'accepted'}),1002);assert.equal(channel.pending,null);assert.equal(stored,null);
channel.receive(frame(retry,{...command,status:'pending'}),1002);assert.equal(channel.pending,null,'late unknown reply must not revive an acknowledged bid');
""")

    def test_poll_cadence_includes_round_trip_and_never_overlaps_reads(self):
        self.run_js(r"""
const first=channel.pump();clock=900;assert.equal(channel.pump(),null);
channel.receive(frame(first),1000);assert.equal(channel.nextAt,900);
const next=channel.pump();assert.ok(next,'slow read must not add another 350ms');
clock=1000;channel.receive(frame(next),1000.1);
clock=1399;assert.equal(channel.pump(),null,'fast reads remain rate limited');
clock=1400;assert.ok(channel.pump());assert.equal(channel.pump(),null);
""")

    def test_command_timing_survives_retries_and_does_not_learn_retry_rtt(self):
        self.run_js(r"""
const bid=channel.submit(11,10);clock=2200;assert.equal(channel.pump(),null);
clock=2400;channel.receive(frame(bid,{...bid.command,status:'accepted'}),1002);
assert.equal(channel.lastCommandTiming.elapsed_ms,2400);
assert.equal(channel.lastCommandTiming.attempts,1);
assert.equal(channel.commandRoundTripMs,2400);
clock=2500;const second=channel.submit(11,20);
clock=6500;assert.equal(channel.pump(),null,'learned command budget exceeds 3s');
clock=6600;const retry=channel.pump();assert.deepEqual(retry.command,second.command);
clock=6900;channel.receive(frame(retry,{...second.command,status:'rejected'}),1006);
assert.equal(channel.lastCommandTiming.elapsed_ms,4400,'measure from original submit');
assert.equal(channel.lastCommandTiming.attempts,2);
assert.equal(channel.commandRoundTripMs,2400,'short retry must not replace original RTT');
clock=7000;channel.receive(frame(retry,{...second.command,status:'rejected'}),1006);
assert.equal(channel.lastCommandTiming.elapsed_ms,4400,'duplicate receipt must not move first confirmation');
""")

    def test_unknown_command_confirmation_retains_backoff_and_remount_has_no_fake_timing(self):
        self.run_js(r"""
const bid=channel.submit(11,10);clock=800;
channel.receive(frame(bid,{...bid.command,status:'pending'}),1000);
clock=1999;assert.equal(channel.pump(),null);
clock=2000;const check=channel.pump();assert.deepEqual(check.command,bid.command);
const remount=make();const replay=remount.pump();clock=2100;
remount.receive(frame(replay,{...replay.command,status:'accepted'}),1001);
assert.equal(remount.lastCommandTiming,null,'unknown original click time must remain unavailable');
""")

    def test_unknown_receipt_survives_remount_and_other_context_never_inherits_it(self):
        self.run_js(r"""
channel.submit(11,10);const command={...channel.pending};
const remount=make();assert.deepEqual(remount.pending,command);assert.deepEqual(remount.pump().command,command);
const other=make('other-account',()=>null);assert.equal(other.pending,null);
other.receive({context:'account-event',ack:{...command,status:'pending'}},1000);assert.equal(other.pending,null);
const empty=make('account-event',()=>null);
empty.receive({context:'account-event',frame_id:20,request:null,ack:{...command,status:'pending'}},1000);
assert.deepEqual(empty.pending,command,'server pending state repairs unavailable browser storage');
""")

    def test_clock_uses_monotonic_round_trip_and_server_processing_without_wall_clock(self):
        self.run_js(r"""
const first=channel.pump();clock=300;
channel.receive(frame(first,null,{server_elapsed_ms:160,snapshot_elapsed_ms:40}),1000);
assert.equal(channel.metrics.residual_ms,100);
assert.ok(Math.abs(channel.estimatedServerNow()-1000.09)<1e-9);
clock=1300;assert.ok(Math.abs(channel.estimatedServerNow()-1001.09)<1e-9);
const second=channel.send();clock=1500;
channel.receive(frame(second,null,{server_elapsed_ms:150,snapshot_elapsed_ms:30}),1001.47);
assert.equal(channel.bestClock.residual,20);
assert.ok(Math.abs(channel.estimatedServerNow()-1001.51)<1e-9);
clock=6600;assert.equal(channel.stale(),true);
""")

    def test_local_increments_and_typing_do_not_send_and_snapshot_keeps_focused_node(self):
        self.run_js(r"""
const before=sent.length;
add.onclick();add.onclick();assert.equal(input.value,'20');assert.equal(sent.length,before);
input.value='35';input.oninput();assert.equal(sent.length,before);assert.match(submit.textContent,/35 P/);
data={...data,server_now:1000.2,transport:{...data.transport,frame_id:2}};cleanup=show();
assert.equal(controls.querySelector('.bid-amount'),input);assert.equal(input.value,'35');
submit.onclick();assert.equal(sent.length,before+1);assert.equal(sent.at(-1).command.amount,35);
assert.equal(submit.disabled,true);submit.onclick();assert.equal(sent.length,before+1);
assert.equal(root.dataset.pendingRequest,sent.at(-1).command.request_id);
""", dom=True)

    def test_snapshot_phase_clock_and_old_receipt_cannot_change_next_lot_draft(self):
        self.run_js(r"""
add.onclick();submit.onclick();const bid=sent.at(-1),command=bid.command;
data={...data,server_now:1001,lot:{id:12,status:'OPEN',highest_bid:null},stage:{...stage,lot_id:12,deadline:1031},transport:{...data.transport,frame_id:2,request:bid}};
advance(200);cleanup=show();assert.equal(input.value,'0');assert.equal(submit.disabled,true);
data={...data,transport:{...data.transport,frame_id:3,ack:{...command,status:'accepted'}}};cleanup=show();
assert.equal(input.value,'0');assert.equal(root.dataset.pendingRequest,'');
data={...data,status:'PAUSED',stage:{...data.stage,phase:'PAUSED:RUNNING:OPEN',ticking:false,seconds:12},control:{...data.control,can_bid:false},transport:{...data.transport,frame_id:4}};
cleanup=show();const before=stageRoot.querySelector('.seconds').textContent;advance(1000);
assert.equal(stageRoot.querySelector('.seconds').textContent,before);assert.equal(submit.disabled,true);
""", dom=True)

    def test_missing_snapshot_keeps_polling_and_dispose_cleans_timers_and_listeners(self):
        self.run_js(r"""
data={...data,available:false,status:null,stage:null,lot:null,control:null,server_now:null,transport:{...data.transport,frame_id:2}};
cleanup=show();advance(350);assert.ok(sent.length>=2);assert.equal(submit.disabled,true);
parent.isConnected=false;cleanup();assert.equal(timers.size,0);
for(const values of listeners.values())assert.equal(values.size,0);
""", dom=True)

    def test_definitive_rejection_survives_deadline_and_new_highest_price(self):
        self.run_js(r"""
add.onclick();submit.onclick();const bid=sent.at(-1),command=bid.command;
advance(200);
data={...data,server_now:1031,lot:{...data.lot,highest_bid:20},
 stage:{...stage,seconds:0},transport:{...data.transport,frame_id:2,request:bid,
 ack:{...command,status:'rejected',message:'The bid deadline has passed.'}}};
cleanup=show();
assert.equal(submit.disabled,true);
assert.equal(controls.querySelector('.bid-feedback').textContent,'The bid deadline has passed.');
assert.match(controls.querySelector('.bid-feedback').className,/error/);
data={...data,server_now:1031.1,lot:{id:12,status:'OPEN',highest_bid:null},
 stage:{...stage,lot_id:12,deadline:1061},transport:{...data.transport,frame_id:3}};
cleanup=show();
assert.doesNotMatch(controls.querySelector('.bid-feedback').textContent,/deadline has passed/);
""", dom=True)

    def test_late_receipt_for_previous_lot_cannot_describe_new_lot(self):
        self.run_js(r"""
add.onclick();submit.onclick();const bid=sent.at(-1),command=bid.command;
advance(200);
data={...data,server_now:1000.3,lot:{id:12,status:'OPEN',highest_bid:null},
 stage:{...stage,lot_id:12,deadline:1031},transport:{...data.transport,frame_id:2,request:bid}};
cleanup=show();assert.equal(submit.disabled,true);
data={...data,transport:{...data.transport,frame_id:3,
 ack:{...command,status:'accepted',message:'Previous lot accepted.'}}};
cleanup=show();
assert.equal(root.dataset.pendingRequest,'');
assert.doesNotMatch(controls.querySelector('.bid-feedback').textContent,/Previous lot accepted/);
""", dom=True)

    def test_expired_visible_clock_cannot_be_reopened_by_delayed_same_deadline(self):
        self.run_js(r"""
advance(30000);assert.equal(stageRoot.querySelector('.seconds').textContent,'0초');
const late=sent.at(-1);advance(100);
data={...data,server_now:1000.2,transport:{...data.transport,frame_id:20,request:late,server_elapsed_ms:100}};
cleanup=show();assert.equal(root.dataset.clockStale,'false');
assert.equal(stageRoot.querySelector('.seconds').textContent,'0초');assert.equal(submit.disabled,true);
const before=sent.length;submit.onclick();assert.equal(sent.length,before);
""", dom=True)

    def test_duplicate_old_cleanup_cannot_remove_current_clock_or_handlers(self):
        self.run_js(r"""
const oldCleanup=cleanup;
data={...data,transport:{...data.transport,frame_id:2}};cleanup=show();
const timerCount=timers.size;oldCleanup();oldCleanup();
assert.equal(timers.size,timerCount);assert.equal(typeof add.onclick,'function');
add.onclick();assert.equal(input.value,'10');
cleanup();cleanup();assert.equal(timers.size,0);
for(const values of listeners.values())assert.equal(values.size,0);
""", dom=True)

    def test_dom_delivery_metrics_preserve_first_seen_frame_and_exact_ack(self):
        self.run_js(r"""
add.onclick();submit.onclick();const bid=sent.at(-1);
advance(1800);
data={...data,lot:{...data.lot,highest_bid:10},stage:{...stage,bid_id:5},
 transport:{...data.transport,frame_id:10,request:bid,ack:{...bid.command,status:'accepted'}}};
cleanup=show();
assert.equal(root.dataset.lastBidElapsedMs,'1800');
assert.equal(root.dataset.lastBidAttempts,'1');
assert.equal(root.dataset.lastBidStatus,'accepted');
assert.equal(root.dataset.displayBidId,'5');
assert.equal(root.dataset.displayBidSeenAtMs,'2000');
advance(200);cleanup=show();
assert.equal(root.dataset.displayBidSeenAtMs,'2000');
data={...data,stage:{...stage,bid_id:4},transport:{...data.transport,frame_id:9}};
cleanup=show();assert.equal(root.dataset.displayBidId,'5','older frames must not move display timing');
assert.equal(root.dataset.lastBidElapsedMs,'1800');
""", dom=True)

    def test_transport_projection_excludes_unrelated_secrets_and_renders_without_state(self):
        transport={"context":"opaque","frame_id":1,"request":None,"server_elapsed_ms":20,
                   "snapshot_elapsed_ms":5,"ack":None,"token":"must-not-appear"}
        data=live_panel_data(None,control={"team_name":"one","remaining":1000,"can_bid":False,
                                         "context":"opaque","password":"must-not-appear"},transport=transport)
        self.assertEqual(data["kind"],"stage")
        self.assertFalse(data["available"])
        self.assertNotIn("must-not-appear",json.dumps(data))
        renderer=Mock(return_value="result")
        with patch("roly.auction_live_panel._register",return_value=renderer) as register, \
                patch("roly.auction_live_panel.st.session_state",{}):
            callback=Mock()
            self.assertEqual(render_live_panel(None,key="live_stage_3",transport=transport,on_event_change=callback),"result")
        self.assertIs(renderer.call_args.kwargs["on_event_change"],callback)
        self.assertEqual(renderer.call_args.kwargs["key"],"live_stage_3")
        self.assertEqual(register.call_args.args[1], COMPONENT_REVISION)


if __name__ == "__main__":
    unittest.main()
