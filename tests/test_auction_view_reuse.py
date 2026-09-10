"""Local Node DOM regressions; no browser, network or production database."""
import json
import re
import shutil
import subprocess
import unittest

from roly.auction_live_panel import JS
from roly.auction_components import JS as COMPONENTS_JS
from tests.test_live_panel_component import DOM_HARNESS


class AuctionViewReuseTests(unittest.TestCase):
    def run_js(self, script):
        harness = DOM_HARNESS.replace("let cleanup=show();", r"""
globalThis.CustomEvent=class {constructor(type,init){this.type=type;this.detail=init.detail;}};
globalThis.dispatchEvent=event=>{for(const fn of [...(listeners.get(event.type)||[])])fn(event);};
let cleanup=show();
""")
        harness += r"""
const api=await import('data:text/javascript;base64,'+Buffer.from(fixture.source).toString('base64'));
const frame=(views,revision,extra={})=>{
  data={...data,...extra,views,transport:{...data.transport,revision,detail_revision:1,request:null,frame_id:revision+1}};
  cleanup=render({parentElement:parent,data,setTriggerValue:(name,value)=>sent.push(value),httpFrame:true,pushFrame:true});
};
const full={event_status:'AUCTION',teams:{1:{name:'Team',remaining:1000}},overview:{kind:'overview',teams:[]},history:{kind:'history',bids:[]},sound:{kind:'sound',event_id:3,bid_id:null}};
"""
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", harness + script],
                                input=json.dumps({"source": JS, "components": COMPONENTS_JS}), text=True, encoding="utf-8",
                                capture_output=True, timeout=20)
        detail = re.sub(r"data:text/javascript;base64,[A-Za-z0-9+/=]+", "[module]", result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, detail[-6000:])

    def test_partial_frame_retains_team_cache_for_late_mount(self):
        self.run_js(r"""
frame(full,10);frame({history:{kind:'history',bids:[{id:1}]}},11);
assert.deepEqual(globalThis._rolyAuctionViews.get('account-event').teams,full.teams);
assert.deepEqual(globalThis._rolyAuctionViews.get('account-event').overview,full.overview);
assert.equal(globalThis._rolyAuctionViews.get('account-event').history.bids[0].id,1);
cleanup();
""")

    def test_partial_event_status_does_not_trigger_framework_rerun(self):
        self.run_js(r"""
frame(full,10);const count=sent.length;
frame({history:{kind:'history',bids:[{id:1}]}},11);
assert.equal(sent.length,count,'ordinary bid has no phase/authority change');
frame({...full,event_status:'BRACKET_SETUP'},12);
assert.equal(sent.length,count+1,'actual workflow transition still reruns');cleanup();
""")

    def test_stage_price_timer_and_profile_nodes_are_reused(self):
        self.run_js(r"""
frame(full,10);const panel=stageRoot.querySelector('.target'),timer=stageRoot.querySelector('.timer');
const price=stageRoot.querySelector('.bid-price'),digits=stageRoot.querySelector('.seconds');
frame({history:{kind:'history',bids:[]}},11,{stage:{...stage,price:'10 P',bidder:'Team 1'},lot:{...data.lot,highest_bid:10}});
assert.ok(stageRoot.querySelector('.target')===panel);
assert.ok(stageRoot.querySelector('.timer')===timer);
assert.ok(stageRoot.querySelector('.bid-price')===price);assert.equal(price.textContent,'10 P');
assert.ok(stageRoot.querySelector('.seconds')===digits);cleanup();
""")

    def test_deletion_and_older_revision_cannot_resurrect_cached_cards(self):
        self.run_js(r"""
frame(full,10);frame({teams:null,overview:null},11);
assert.equal(globalThis._rolyAuctionViews.get('account-event').overview,undefined);
frame(full,9);assert.equal(globalThis._rolyAuctionViews.get('account-event').overview,undefined);
frame(null,12,{available:false});assert.equal(globalThis._rolyAuctionViews.get('account-event'),null);
frame({history:{kind:'history',bids:[]}},13);assert.equal(globalThis._rolyAuctionViews.get('account-event'),null);
frame(full,14,{available:true});assert.deepEqual(globalThis._rolyAuctionViews.get('account-event').overview,full.overview);
cleanup();
""")

    def test_epoch_reset_rejects_old_generation_and_bounds_context_cache(self):
        self.run_js(r"""
const publish=options=>api.publishAuctionViews(globalThis,'context-1',options);
publish({epoch:'old',reset:true});publish({epoch:'old',revision:100,views:full,connection:'live'});
publish({epoch:'new',reset:true});assert.equal(globalThis._rolyAuctionViews.get('context-1'),null);
publish({epoch:'old',revision:101,views:full});assert.equal(globalThis._rolyAuctionViews.get('context-1'),null);
publish({epoch:'new',revision:1,views:full});assert.deepEqual(globalThis._rolyAuctionViews.get('context-1').teams,full.teams);
for(let i=0;i<30;i++)api.publishAuctionViews(globalThis,'context-'+i,{epoch:'generation',revision:1,views:full});
assert.equal(globalThis._rolyAuctionViews.size,8);assert.equal(globalThis._rolyAuctionViewMeta.size,8);cleanup();
""")

    def test_late_overview_uses_merged_cache_and_reports_disconnect_revocation_recovery(self):
        self.run_js(r"""
const component=(await import('data:text/javascript;base64,'+Buffer.from(fixture.components).toString('base64'))).default;
const host=new Element('host'),target=new Element('section');target.className='auction-component';host.append(target);
const card={name:'Current team',remaining:'900 P',slots:[],count:1};
const snapshot={...full,overview:{kind:'overview',teams:[card]}};
frame(snapshot,10);frame({history:{kind:'history',bids:[]}},11);
const showOverview=()=>component({parentElement:host,data:{kind:'overview',teams:[],live_context:'account-event'}});
let closeOverview=showOverview();assert.equal(target.querySelector('.mini-budget').textContent,'900 P');
api.publishAuctionViews(globalThis,'account-event',{connection:'stale'});
assert.equal(target.querySelector('.mini-budget').textContent,'900 P');
assert.match(target.querySelector('.auction-connection').textContent,/마지막으로 확인된/);
api.publishAuctionViews(globalThis,'account-event',{views:null,connection:'stopped'});
assert.equal(target.querySelector('.mini-budget'),null);closeOverview=showOverview();
assert.equal(target.querySelector('.mini-budget'),null);assert.match(target.querySelector('.auction-connection').textContent,/새로고침/);
frame(snapshot,12);assert.equal(target.querySelector('.mini-budget').textContent,'900 P');
assert.equal(target.querySelector('.auction-connection').hidden,true);closeOverview();cleanup();
""")

    def test_retained_profile_image_keeps_error_handler_and_changed_profile_replaces_it(self):
        self.run_js(r"""
const player={...stage.player,profile_icon_url:'https://ddragon.leagueoflegends.com/cdn/16.1.1/img/profileicon/1.png'};
frame(full,10,{stage:{...stage,player}});const portrait=stageRoot.querySelector('.portrait-image');
assert.equal(typeof portrait.onerror,'function');
frame({history:{kind:'history',bids:[]}},11);await Promise.resolve();
assert.ok(stageRoot.querySelector('.portrait-image')===portrait);assert.equal(typeof portrait.onerror,'function');
frame(full,12,{stage:{...stage,player:{...player,nickname:'Corrected'}}});
assert.ok(stageRoot.querySelector('.portrait-image')!==portrait);assert.equal(portrait.onerror,null);cleanup();
""")

    def test_unchanged_tick_does_not_rewrite_button_feedback_or_clock_text(self):
        self.run_js(r"""
frame(full,10);const watched=[controls.querySelector('.bid-feedback'),controls.querySelector('.bid-submit'),stageRoot.querySelector('.seconds')];
let writes=0;for(const node of watched){let value=node.textContent;Object.defineProperty(node,'textContent',{get:()=>value,set:next=>{writes++;value=next;}});}
for(let i=0;i<5;i++)advance(1);
assert.equal(writes,0);add.onclick();assert.ok(writes>0,'actual input must immediately update the feedback');cleanup();
""")

    def test_companion_same_snapshot_remount_keeps_image_handlers_until_final_unmount(self):
        self.run_js(r"""
const component=(await import('data:text/javascript;base64,'+Buffer.from(fixture.components).toString('base64'))).default;
const host=new Element('host'),target=new Element('section');target.className='auction-component';host.append(target);
const item={number:1,label:'대기',player:{name:'Player',nickname:'Player',profile_icon_url:'https://ddragon.leagueoflegends.com/cdn/16.1.1/img/profileicon/1.png'}};
const queue={kind:'queue',items:[item],heading:'대기',caption:'',empty:''};
frame({...full,queue},10);
const showQueue=()=>component({parentElement:host,data:{...queue,live_context:'account-event'}});
let stop=showQueue();const picture=target.querySelector('.portrait-image');assert.equal(typeof picture.onerror,'function');
stop();stop=showQueue();await Promise.resolve();
assert.ok(target.querySelector('.portrait-image')===picture);assert.equal(typeof picture.onerror,'function');
stop();await Promise.resolve();assert.equal(picture.onerror,null);cleanup();
""")


if __name__ == "__main__":
    unittest.main()
