"""Exercise repeated CCv2 snapshots and audio lifecycle without browser claims."""
import json
import shutil
import subprocess
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from roly.auction_components import JS, MASTERY_LIMIT, player_data, render_stage, render_sound, stage_data, team_data, queue_data, remaining_data, render_queue, render_remaining, render_overview


HARNESS = r"""
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const fixture=JSON.parse(readFileSync(0,'utf8'));
const source=fixture.source;
const render=(await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'))).default;
const intervals=new Map();let timerId=0;
let clockMs=0;
Object.defineProperty(globalThis,'performance',{configurable:true,value:{now:()=>clockMs}});
globalThis.setInterval=(fn)=>{intervals.set(++timerId,fn);return timerId;};
globalThis.clearInterval=(id)=>intervals.delete(id);
class Element {
  constructor(tag){this.tagName=tag;this.className='';this.children=[];this.dataset={};this.style={setProperty(k,v){this[k]=v;}};this.attributes={};this.ownerDocument=doc;this.isConnected=true;}
  append(...nodes){for(const n of nodes){n.parent=this;this.children.push(n);}}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  replaceWith(node){const i=this.parent.children.indexOf(this);this.parent.children[i]=node;node.parent=this.parent;}
  remove(){if(this.parent){const i=this.parent.children.indexOf(this);if(i>=0)this.parent.children.splice(i,1);this.parent=null;}this.isConnected=false;}
  setAttribute(k,v){this.attributes[k]=v;}
  querySelector(q){const found=this.children.find(n=>n.className.split(' ').includes(q.slice(1)));if(found)return found;for(const n of this.children){const value=n.querySelector(q);if(value)return value;}return null;}
  set innerHTML(_){throw new Error('Do not render member HTML');}
}
const doc={createElement:tag=>new Element(tag),createElementNS:(_ns,tag)=>new Element(tag)};
const root=new Element('section');root.className='auction-component';
const parent=new Element('host');parent.append(root);
let oscillators=0,closed=0;const peaks=[];
class AudioContext {
  constructor(){this.state='suspended';this.currentTime=0;this.destination={};}
  async resume(){this.state='running';}
  async close(){this.state='closed';closed++;}
  createOscillator(){oscillators++;return {frequency:{},connect(){},disconnect(){},start(){},stop(){}};}
  createGain(){return {gain:{setValueAtTime(){},exponentialRampToValueAtTime(v){peaks.push(v);}},connect(){},disconnect(){}};}
}
globalThis.AudioContext=AudioContext;
const soundRoot=new Element('section');soundRoot.className='auction-component';
const soundParent=new Element('host');soundParent.append(soundRoot);
const soundSnapshot={kind:'sound',event_id:7,bid_id:1};
const soundProps={parentElement:soundParent,data:soundSnapshot};
let soundCleanup=render(soundProps);
const updateBid=id=>{soundCleanup=render({...soundProps,data:{...soundSnapshot,bid_id:id}});};
const snapshot={kind:'stage',player:{name:'<img src=x onerror=bad()>',meta:'탑 · 전력 100 P'},target_label:'1번째 선수',bidder:'1팀 · 팀장',price:'최고 입찰 10 P',clock_label:'남은 시간',seconds:7,scale:10,show_clock:true,ticking:true,bid_id:1,rule:'입찰 +5초'};
const props={parentElement:parent,data:snapshot};
let cleanup=render(props);
assert.equal(intervals.size,1);assert.equal(oscillators,0);
assert.equal(root.querySelector('.sound-button'),null,'Stage has no sound controls');
const button=soundRoot.querySelector('.sound-button');const slider=soundRoot.querySelector('.volume');
assert.equal(slider.value,'80');
assert.equal(button.attributes['aria-label'],'입찰 소리 켜기');
assert.equal(button.children[0].tagName,'svg','The compact sound control uses an accessible speaker icon');
await button.onclick();assert.equal(oscillators,2);assert.equal(soundRoot.dataset.soundCount,'1');
assert.equal(button.attributes['aria-label'],'입찰 소리 끄기');
for(let i=0;i<100;i++){cleanup=render(props);updateBid(1);}
assert.equal(intervals.size,1,'Only one visual clock survives repeated data updates');
assert.equal(soundRoot.querySelector('.sound-button'),button,'Do not replace a focused sound control');
assert.equal(soundRoot.querySelector('.volume'),slider,'Keep the slider mounted during a drag');
assert.equal(oscillators,2,'Existing bid must not replay');
cleanup=render({...props,data:{...snapshot,bid_id:2}});
updateBid(2);assert.equal(oscillators,4);assert.equal(soundRoot.dataset.soundCount,'2');
cleanup=render({...props,data:{...snapshot,bid_id:2}});
updateBid(2);
assert.equal(oscillators,4,'Duplicate or rejected bid snapshot is silent');
slider.value='0';slider.oninput();
cleanup=render({...props,data:{...snapshot,bid_id:3}});
updateBid(3);assert.equal(oscillators,4,'Zero volume is silent');assert.equal(soundRoot.dataset.soundVolume,'0');
slider.value='20';slider.oninput();
cleanup=render({...props,data:{...snapshot,bid_id:4}});
updateBid(4);assert.equal(oscillators,6);assert.ok(peaks.includes(.03),'Volume changes actual audio gain');
updateBid(2);assert.equal(oscillators,6,'An older snapshot never replays a historical bid');
updateBid(4);assert.equal(oscillators,6,'Preserved bid history after reset is silent');
updateBid(5);assert.equal(oscillators,8,'The next new bid still rings');
soundCleanup=render({parentElement:soundParent,data:fixture.reset_sound});
assert.equal(oscillators,8,'An unseen archived bid is silent even when READY was missed');
updateBid(7);assert.equal(oscillators,10,'The first actual bid after reset still rings');
updateBid(7);assert.equal(oscillators,10,'The post-reset receipt does not replay');
assert.equal(root.querySelector('.name').textContent,snapshot.player.name);
const running={...snapshot,bid_id:4,event_id:7,lot_id:11,deadline:1010,phase:'RUNNING::OPEN',seconds:10};
const show=data=>{cleanup=render({...props,data});};
const advance=ms=>{clockMs+=ms;for(const draw of intervals.values())draw();};
const digits=()=>root.querySelector('.seconds').textContent;
show(running);assert.equal(digits(),'10초');
advance(1300);assert.equal(digits(),'9초');
show(running);assert.equal(digits(),'9초','A late unchanged deadline must not add display time');
advance(700);assert.equal(digits(),'8초');
show({...running,seconds:9});assert.equal(digits(),'8초','Another late response cannot move the anchor');
show({...running,seconds:6.2});assert.equal(digits(),'7초','A fresher lower estimate can correct the clock');
advance(300);assert.equal(digits(),'6초');
const extended={...running,deadline:1015,seconds:10};
show(extended);assert.equal(digits(),'10초','A real server deadline extension gets a new anchor');
advance(1000);assert.equal(digits(),'9초');
show(extended);assert.equal(digits(),'9초','A capped bid with unchanged deadline adds no time');
const paused={...extended,phase:'PAUSED:RUNNING:OPEN',seconds:8,ticking:false};
show(paused);advance(30000);assert.equal(digits(),'8초','Pause freezes the remaining time');
show(paused);assert.equal(digits(),'8초');
const resumed={...extended,deadline:1050,seconds:8};
show(resumed);advance(1000);assert.equal(digits(),'7초','Resume starts from the saved remainder');
show({...resumed,lot_id:12,seconds:10});assert.equal(digits(),'10초','The next player gets an independent clock');
advance(1000);
show({...resumed,event_id:8,lot_id:12,seconds:10});assert.equal(digits(),'10초','Another event cannot inherit an old anchor');
const waiting={...running,phase:'WAITING::SOLD',deadline:1100,seconds:3,scale:3};
show(waiting);advance(1000);assert.equal(digits(),'2초');
show({...waiting,phase:'PAUSED:WAITING:SOLD',seconds:2,ticking:false});
advance(30000);assert.equal(digits(),'2초','The between-player pause stays frozen');
const next={...waiting,deadline:1130,seconds:2};
show(next);advance(1200);assert.equal(digits(),'1초');
show(next);assert.equal(digits(),'1초','Late transition snapshots cannot lengthen the three-second wait');
advance(1000);assert.equal(digits(),'0초');
show(next);assert.equal(digits(),'0초','A stale snapshot cannot revive an expired display');
assert.equal(soundRoot.querySelector('.sound-button'),button);assert.equal(soundRoot.querySelector('.volume'),slider);
assert.equal(soundRoot.dataset.soundEnabled,'true');assert.equal(soundRoot.dataset.soundVolume,'20');
parent.isConnected=false;cleanup();assert.equal(closed,0,'Leaving the stage does not close the top sound control');
soundParent.isConnected=false;soundCleanup();await Promise.resolve();
assert.equal(intervals.size,0);assert.equal(closed,1,'Release AudioContext on leaving the auction');
assert.equal(button.onclick,null);assert.equal(slider.oninput,null);
const freshRoot=new Element('section');freshRoot.className='auction-component';
const freshParent=new Element('host');freshParent.append(freshRoot);
const soundCountBeforeReconnect=oscillators;
const freshCleanup=render({parentElement:freshParent,data:{...running,seconds:4}});
const freshSoundRoot=new Element('section');freshSoundRoot.className='auction-component';
const freshSoundParent=new Element('host');freshSoundParent.append(freshSoundRoot);
const freshSoundCleanup=render({parentElement:freshSoundParent,data:{kind:'sound',event_id:7,bid_id:5}});
assert.equal(freshRoot.querySelector('.seconds').textContent,'4초','Reconnect uses its fresh server snapshot');
assert.equal(oscillators,soundCountBeforeReconnect,'Reconnect never replays old bid audio');
freshParent.isConnected=false;freshCleanup();freshSoundParent.isConnected=false;freshSoundCleanup();assert.equal(intervals.size,0);
process.stdout.write('delayed clocks, extensions, phase changes, reconnect, sound and cleanup passed');
"""


PROFILE_HARNESS = HARNESS.split("let oscillators=0")[0] + r"""
const snapshot={kind:'stage',player:fixture.player,target_label:'1번째 선수',bidder:'입찰 대기',price:'입찰 없음',clock_label:'남은 시간',seconds:10,scale:10,show_clock:true,ticking:true,bid_id:null,rule:'입찰 +5초'};
const props={parentElement:parent,data:snapshot};
const all=node=>[node,...node.children.flatMap(all)];
let cleanup=render(props);
const imageNodes=all(root).filter(node=>node.tagName==='img');
assert.equal(imageNodes.length,6,'A real profile image and five real champion icons');
assert.equal(root.querySelector('.name').textContent,fixture.player.nickname);
assert.equal(root.querySelector('.riot-id').textContent,fixture.player.riot_id);
assert.equal(root.querySelector('.target-label'),null,'The queue owns the sequence label');
assert.equal(root.querySelector('.profile-source').title,fixture.player.profile_source);
assert.equal(root.querySelector('.profile-source').attributes['aria-label'],fixture.player.profile_source);
assert.equal(root.querySelector('.profile-source').textContent,'ⓘ','Long source timestamps stay in the help tooltip');
assert.equal(root.querySelector('.mastery').parent,root.querySelector('.profile-stats'),'Mastery belongs inside the compact right stats column');
assert.equal(root.querySelector('.champion-name'),null,'Champion names stay in accessible tooltips');
assert.ok(all(root).some(node=>node.className==='stat-value' && node.textContent===fixture.player.clan_tier));
assert.ok(all(root).some(node=>node.className==='stat-value' && node.textContent===fixture.player.current_tier));
const firstChampion=root.querySelector('.champion');
assert.ok(firstChampion.title.includes('123,456'));
assert.equal(firstChampion.attributes['aria-label'],firstChampion.title);
assert.equal(firstChampion.tabIndex,0);
assert.ok(!firstChampion.title.includes('%'),'Mastery is never presented as an invented win rate');
for(const img of imageNodes){assert.ok(img.src.startsWith('https://ddragon.leagueoflegends.com/'));assert.equal(img.referrerPolicy,'no-referrer');}
cleanup=render(props);
assert.equal(root.querySelector('.portrait-image'),imageNodes[0],'Repeating the same player keeps image nodes mounted');
assert.equal(intervals.size,1);
const avatar=root.querySelector('.avatar');
imageNodes[0].onerror();
assert.equal(avatar.children.length,1,'Failed images leave only the initials fallback');
assert.equal(avatar.children[0].textContent,fixture.player.nickname.slice(0,1));
const poisoned={...fixture.player,profile_icon_url:'https://ddragon.leagueoflegends.com.evil.invalid/icon.png',champions:[{name:'<svg onload=bad()>',icon_url:'javascript:bad()',tooltip:'Literal <script> only'}]};
cleanup=render({...props,data:{...snapshot,player:poisoned}});
assert.equal(all(root).filter(node=>node.tagName==='img').length,0,'Frontend independently rejects unsafe URLs, even bypassing Python');
assert.equal(root.querySelector('.champion').title,poisoned.champions[0].tooltip);
assert.equal(root.querySelector('.champion-icon').children[0].textContent,'<','Unsafe names remain literal initials');
for(const img of imageNodes)assert.equal(img.onerror,null,'Removed portraits release image error handlers');
cleanup=render({...props,data:{...snapshot,player:fixture.empty_player}});
assert.equal(all(root).filter(node=>node.tagName==='img').length,0);
assert.equal(root.querySelector('.profile-source').title,'명단 확정 당시 · Riot API 미조회');
assert.equal(root.querySelector('.mastery-empty').textContent,'정보 없음');
assert.equal(root.querySelector('.mastery-empty').title,'Riot API 조회 후 숙련도 정보를 표시합니다.');
parent.isConnected=false;cleanup();await Promise.resolve();
assert.equal(intervals.size,0);
process.stdout.write('profiles, mastery, escaping, image fallback and unchanged image identity passed');
"""


STATIC_HARNESS = HARNESS.split("let oscillators=0")[0] + r"""
const all=node=>[node,...node.children.flatMap(all)];
const listeners=new Map();
globalThis.addEventListener=(kind,fn)=>listeners.set(kind,fn);
globalThis.removeEventListener=(kind,fn)=>{if(listeners.get(kind)===fn)listeners.delete(kind);};
root.clientWidth=1100;
let cleanup;
for(const overview of fixture.overviews){
  cleanup=render({parentElement:parent,data:overview});
  assert.equal(root.querySelector('.overview-grid').style['--cols'],Math.max(2,overview.teams.length/2));
  assert.equal(all(root).filter(n=>n.className==='mini-position').length,overview.teams.length*5);
  const people=all(root).filter(n=>n.className.split(' ').includes('mini-member'));
  assert.equal(people.length,overview.teams.reduce((count,team)=>count+team.count,0),'Duplicate positions must not hide members');
  assert.equal(all(root).filter(n=>n.className.split(' ').includes('captain-row')).length,overview.teams.length);
  assert.ok(all(root).some(n=>n.textContent==='내 팀'));
  const avatar=root.querySelector('.portrait-image');
  for(let i=0;i<50;i++)cleanup=render({parentElement:parent,data:overview});
  assert.equal(root.querySelector('.portrait-image'),avatar,'Unchanged team snapshots keep image DOM mounted');
  assert.equal(listeners.size,1);
}
root.clientWidth=500;listeners.get('resize')();
assert.equal(root.querySelector('.overview-grid').style['--cols'],2);
cleanup=render({parentElement:parent,data:fixture.queue});
assert.equal(listeners.size,0,'Changing view releases overview resize listener');
const items=all(root).filter(n=>n.className.split(' ').includes('queue-item'));
assert.equal(items.length,fixture.queue.items.length);
assert.equal(all(root).filter(n=>n.className==='queue-arrow').length,items.length-1);
assert.equal(items.filter(n=>n.attributes['aria-current']==='step').length,1);
assert.ok(all(root).some(n=>n.textContent?.includes('현재')));
const queueRoot=root.children[0];
cleanup=render({parentElement:parent,data:fixture.queue});assert.equal(root.children[0],queueRoot);
cleanup=render({parentElement:parent,data:fixture.remaining});
const cards=all(root).filter(n=>n.className.split(' ').includes('player'));
assert.equal(cards.length,fixture.remaining.groups.reduce((count,group)=>count+group.players.length,0));
assert.ok(all(root).some(n=>n.textContent==='유찰 · 재경매 대기 · 1명'));
assert.equal(all(root).filter(n=>n.className==='role-count').length,5);
assert.equal(all(root).filter(n=>n.className==='champion').length,cards.length*5);
assert.equal(intervals.size,0,'Read-only queues and teams create no clock timers');
cleanup();assert.equal(listeners.size,0);
for(const img of all(root).filter(n=>n.tagName==='img'))assert.equal(img.onerror,null);
process.stdout.write('compact roles, duplicate roles, responsive columns, queue state, mastery5 and cleanup passed');
"""


class AuctionComponentTests(unittest.TestCase):
    @staticmethod
    def profile_player():
        return {"riot_id": "<img onerror=bad()>#QA", "role": "MID", "score": 123,
            "clan_tier_snapshot": "클랜 A", "clan_tier": "나중에 바뀐 등급",
            "current_tier_snapshot": "실버 2", "current_tier_lp_snapshot": 30,
            "riot_profile": {"current_tier": "다이아몬드 2", "lp": 0,
                "profile_icon_url": "https://ddragon.leagueoflegends.com/cdn/16.18.1/img/profileicon/29.png",
                "updated_at": "2026-09-08T11:00:00+00:00", "puuid": "private-identity-not-forwarded",
                "champions": [{"id": index + 1, "name": name,
                    "icon_url": f"https://ddragon.leagueoflegends.com/cdn/16.18.1/img/champion/{asset}.png",
                    "points": 123456 - index * 1000, "level": 12 - index}
                    for index, (name, asset) in enumerate([("아리", "Ahri"), ("리 신", "LeeSin"), ("오공", "MonkeyKing"), ("진", "Jhin"), ("애쉬", "Ashe"), ("럭스", "Lux"), ("가렌", "Garen")])]}}

    def test_profile_card_uses_frozen_clan_current_api_and_actual_mastery(self):
        player = self.profile_player()
        display = player_data(player)
        self.assertEqual(display["clan_tier"], "클랜 A")
        self.assertEqual(display["current_tier"], "다이아몬드 2 · 0 LP")
        self.assertEqual(display["profile_source"], "Riot API · 갱신 2026.09.08 20:00 (한국 시간)")
        self.assertEqual((display["score"], display["role"]), (123, "미드"))
        self.assertEqual(len(display["champions"]), MASTERY_LIMIT)
        self.assertEqual(display["champions"][0]["tooltip"], "아리 · 숙련도 123,456점 · 레벨 12")
        self.assertNotIn("private-identity", json.dumps(display))
        self.assertTrue(all("%" not in champion["tooltip"] for champion in display["champions"]))
        self.assertEqual(player["current_tier_snapshot"], "실버 2")
        self.assertEqual(len(player["riot_profile"]["champions"]), 7)

    def test_missing_api_preserves_snapshot_unknown_and_empty_values(self):
        player = {**self.profile_player(), "riot_profile": None, "price": 50}
        display = player_data(player, price=True)
        self.assertEqual(display["current_tier"], "실버 2 · 30 LP")
        self.assertEqual(display["profile_source"], "명단 확정 당시 · Riot API 미조회")
        self.assertEqual(display["champions"], [])
        self.assertIsNone(display["profile_icon_url"])
        self.assertIn("낙찰 50 P", display["meta"])
        self.assertEqual(player_data({**player, "clan_tier_snapshot": None})["clan_tier"], "기록 없음")
        self.assertEqual(player_data({**player, "clan_tier_snapshot": ""})["clan_tier"], "미등록")
        del player["clan_tier_snapshot"]
        self.assertEqual(player_data(player)["clan_tier"], "나중에 바뀐 등급")
        self.assertEqual(player_data({**player, "current_tier_snapshot": None, "current_tier": "마스터"})["current_tier"], "기록 없음")
        profile = {"current_tier": "언랭크", "lp": None, "updated_at": "invalid", "champions": []}
        display = player_data({**player, "riot_profile": profile})
        self.assertEqual(display["current_tier"], "언랭크")
        self.assertEqual(display["profile_source"], "Riot API · 갱신 시각 미확인")
        self.assertEqual(display["mastery_empty"], "저장된 챔피언 숙련도 정보가 없습니다.")

    def test_portrait_urls_only_allow_known_https_data_dragon_images(self):
        player = self.profile_player()
        for url in ("javascript:alert(1)", "data:image/svg+xml,<svg/>", "http://ddragon.leagueoflegends.com/cdn/16.18.1/img/profileicon/29.png",
                    "https://ddragon.leagueoflegends.com.evil.invalid/a.png", "https://ddragon.leagueoflegends.com@evil.invalid/a.png",
                    "https://ddragon.leagueoflegends.com:444/cdn/16.18.1/img/profileicon/29.png",
                    "https://ddragon.leagueoflegends.com/cdn/16.18.1/img/profileicon/29.svg",
                    "https://ddragon.leagueoflegends.com/cdn/16.18.1/img/profileicon/29.png?redirect=evil", None):
            with self.subTest(url=url):
                profile = {**player["riot_profile"], "profile_icon_url": url,
                    "champions": [{"name": "실제 챔피언", "icon_url": url, "points": -3, "level": True}]}
                display = player_data({**player, "riot_profile": profile})
                self.assertIsNone(display["profile_icon_url"])
                self.assertIsNone(display["champions"][0]["icon_url"])
                self.assertEqual(display["champions"][0]["tooltip"], "실제 챔피언 · 숙련도 점수 미확인 · 레벨 미확인")

    def test_profile_images_mastery_text_and_failed_images_in_isolated_dom(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for the component regression check")
        player = self.profile_player()
        result = subprocess.run([node, "--input-type=module", "-e", PROFILE_HARNESS],
            input=json.dumps({"source": JS, "player": player_data(player),
                "empty_player": player_data({**player, "riot_profile": None})}),
            text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_render_elapsed_is_subtracted_only_from_ticking_server_clock(self):
        lot = {'id': 11, 'status': 'OPEN', 'highest_team_id': None,
               'highest_bid': None, 'remaining_seconds': 7, 'closes_at': 107,
               'sequence': 0, 'riot_id': 'Clock#QA', 'role': 'TOP', 'score': 100}
        state = {'event_id': 7, 'current_lot': lot, 'teams': [], 'bids': [],
                 'bid_seconds': 10, 'status': 'RUNNING', 'paused_phase': None}
        display = stage_data(state, elapsed_seconds=1.25)
        self.assertEqual((display['seconds'], display['event_id'], display['lot_id'], display['deadline'], display['phase']),
                         (5.75, 7, 11, 107, 'RUNNING::OPEN'))
        self.assertEqual(stage_data(state, elapsed_seconds=20)['seconds'], 0)
        self.assertEqual(stage_data(state, elapsed_seconds=-2)['seconds'], 7)
        paused = {**state, 'status': 'PAUSED', 'paused_phase': 'RUNNING'}
        self.assertEqual(stage_data(paused, elapsed_seconds=100)['seconds'], 7)
        waiting = {**state, 'status': 'WAITING', 'current_lot': {**lot, 'status': 'UNSOLD'}, 'next_at': 110, 'next_in_seconds': 3}
        self.assertEqual(stage_data(waiting, elapsed_seconds=.75)['seconds'], 2.25)
        self.assertEqual(stage_data(waiting)['deadline'], 110)
        paused_waiting = {**waiting, 'status': 'PAUSED', 'paused_phase': 'WAITING', 'next_in_seconds': 2}
        self.assertEqual(stage_data(paused_waiting, elapsed_seconds=100)['seconds'], 2)
        renderer = Mock()
        with patch('roly.auction_components._renderer', return_value=renderer):
            render_stage(state, 'clock-test', elapsed_seconds=1.25)
        self.assertEqual(renderer.call_args.kwargs['data']['seconds'], 5.75)

    def test_cancelled_sale_uses_paused_or_running_three_second_transition(self):
        state = {"current_lot": {"status": "CANCELLED", "highest_team_id": None,
                 "highest_bid": None, "remaining_seconds": 0, "sequence": 4,
                 "riot_id": "정정 선수#QA", "role": "TOP", "score": 100},
                 "teams": [], "bids": [], "bid_seconds": 10}
        for status, deadline, seconds, label, ticking in (
            ("PAUSED", None, 3, "일시정지", False),
            ("WAITING", 103, 2, "다음 선수 준비", True),
        ):
            with self.subTest(status=status):
                display = stage_data({**state, "status": status, "next_at": deadline, "next_in_seconds": seconds})
                self.assertEqual((display["bidder"], display["price"]), ("낙찰 취소", "재경매 대기"))
                self.assertEqual((display["seconds"], display["scale"]), (seconds, 3))
                self.assertEqual(display["clock_label"], label)
                self.assertTrue(display["show_clock"])
                self.assertEqual(display["ticking"], ticking)

    def test_ready_preview_shows_selected_setting_without_an_active_countdown(self):
        lot = self.lot(1, 0, "QUEUED")
        state = {"event_id": 7, "status": "READY", "current_lot": lot, "teams": [], "bids": [], "bid_seconds": 25}
        display = stage_data(state, elapsed_seconds=100)
        self.assertFalse(display["show_clock"])
        self.assertFalse(display["ticking"])
        self.assertEqual((display["clock_label"], display["clock_text"]), ("시작 대기", "설정 25초"))
        self.assertEqual(display["bidder"], "경매 시작을 기다립니다")

    def test_sound_renderer_only_receives_latest_bid_and_event_identity(self):
        renderer = Mock()
        with patch("roly.auction_components._renderer", return_value=renderer):
            render_sound({"event": {"id": 7}, "bids": [{"id": 50, "private": "never-forward"}, {"id": 49}]}, "sound")
        self.assertEqual(renderer.call_args.kwargs["data"], {"kind": "sound", "event_id": 7, "bid_id": 50})
        self.assertEqual(renderer.call_args.kwargs["width"], 200)

    @staticmethod
    def reset_sound_data():
        renderer = Mock()
        with patch("roly.auction_components._renderer", return_value=renderer):
            render_sound({"event_id": 7, "status": "RUNNING",
                "lots": [{"id": 100, "status": "CANCELLED"}, {"id": 101, "status": "OPEN"}],
                "bids": [{"id": 6, "lot_id": 100}]}, "sound")
        return renderer.call_args.kwargs["data"]

    def test_reset_archived_bids_do_not_become_sound_signals(self):
        self.assertEqual(self.reset_sound_data(), {"kind": "sound", "event_id": 7, "bid_id": None})
        renderer = Mock()
        with patch("roly.auction_components._renderer", return_value=renderer):
            render_sound({"event_id": 7, "lots": [{"id": 100, "status": "CANCELLED"}, {"id": 101, "status": "OPEN"}],
                "bids": [{"id": 7, "lot_id": 101}, {"id": 6, "lot_id": 100}]}, "sound")
        self.assertEqual(renderer.call_args.kwargs["data"]["bid_id"], 7)

    def test_repeated_snapshots_sound_volume_and_unmount_cleanup(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for the component regression check")
        result = subprocess.run([node, "--input-type=module", "-e", HARNESS],
            input=json.dumps({"source": JS, "reset_sound": self.reset_sound_data()}), text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    @classmethod
    def lot(cls, member_id, sequence, status, attempt=1):
        return {**cls.profile_player(), "riot_id": f"회원{member_id}#QA", "member_id": member_id,
                "id": sequence + 1, "sequence": sequence, "attempt": attempt, "status": status,
                "role": ("TOP", "JG", "MID", "AD", "SUP")[member_id % 5]}

    @classmethod
    def teams(cls, count):
        teams = []
        for index in range(count):
            players = [{**cls.profile_player(), "member_id": index * 10 + position + 1,
                        "riot_id": f"팀{index + 1}선수{position}#QA", "price": position * 10,
                        "role": ("TOP", "JG", "MID", "AD", "SUP")[position]}
                       for position in range(5)]
            if index == 0:
                players[1]["role"] = "TOP"  # A bid does not assign a different position.
            teams.append({"id": index + 1, "name": f"{index + 1}팀", "remaining": 800,
                          "captain_id": players[0]["member_id"], "players": players})
        return teams

    def test_latest_attempt_queue_keeps_pending_original_round_and_correction_order(self):
        lots = [self.lot(11, 0, "UNSOLD"), self.lot(12, 1, "OPEN"),
                self.lot(13, 2, "QUEUED"), self.lot(11, 3, "QUEUED", 2)]
        state = {"status": "RUNNING", "current_lot": lots[1], "lots": lots}
        before = deepcopy(state)
        queue = queue_data(state)
        self.assertEqual([item["member_id"] for item in queue["items"]], [12, 13, 11])
        self.assertEqual([item["label"] for item in queue["items"]], ["현재", "다음", "대기"])
        self.assertEqual([item["lot_id"] for item in queue["items"] if item["active"]], [2])
        self.assertEqual(state, before)
        remaining = remaining_data(state)
        self.assertEqual([p["riot_id"] for p in remaining["groups"][0]["players"]], ["회원13#QA", "회원11#QA"])
        self.assertFalse(remaining["groups"][1]["players"])
        self.assertEqual(sum(row["count"] for row in remaining["counts"]), 2)

    def test_retry_transition_marks_next_without_repeating_previous_lot(self):
        lots = [self.lot(1, 0, "UNSOLD"), self.lot(2, 1, "SOLD"), self.lot(1, 2, "QUEUED", 2)]
        for status, phase in (("WAITING", None), ("PAUSED", "WAITING")):
            with self.subTest(status=status):
                queue = queue_data({"status": status, "paused_phase": phase, "current_lot": lots[0], "lots": lots})
                self.assertEqual([(item["member_id"], item["label"], item["active"]) for item in queue["items"]], [(1, "다음 준비", True)])
                self.assertTrue(queue["label"].startswith("재경매"))
        ready = queue_data({"status": "READY", "current_lot": None, "lots": [self.lot(1, 0, "QUEUED")]})
        self.assertEqual(ready["items"][0]["label"], "첫 선수")
        self.assertFalse(queue_data({"status": "CANCELLED", "lots": [self.lot(1, 0, "CANCELLED")]})["items"])
        finished = queue_data({"status": "WAITING", "lots": [self.lot(1, 0, "UNSOLD")]})
        self.assertEqual(finished["label"], "이번 순서 0명 · 유찰 1명")

    def test_live_participant_assignment_filters_obsolete_unassigned_lots(self):
        lots = [self.lot(1, 0, "QUEUED"), self.lot(2, 1, "UNSOLD"), self.lot(3, 2, "QUEUED"), self.lot(4, 3, "QUEUED")]
        state = {"status": "WAITING", "lots": lots, "event": {"players": [
            {"member_id": 1, "team_id": 9}, {"member_id": 2, "team_id": None}, {"member_id": 3, "team_id": None}]}}
        remaining = remaining_data(state)
        self.assertEqual([p["riot_id"] for p in remaining["groups"][0]["players"]], ["회원3#QA"])
        self.assertEqual([p["riot_id"] for p in remaining["groups"][1]["players"]], ["회원2#QA"])
        self.assertEqual(sum(row["count"] for row in remaining["counts"]), 2)

    def test_role_overview_preserves_duplicate_positions_and_my_team(self):
        team = self.teams(4)[0]
        card = team_data(team, member_id=team["players"][1]["member_id"])
        self.assertTrue(card["mine"])
        self.assertEqual([slot["role_code"] for slot in card["slots"]], ["TOP", "JG", "MID", "AD", "SUP"])
        self.assertEqual([len(slot["players"]) for slot in card["slots"]], [2, 0, 1, 1, 1])
        self.assertEqual(sum(len(slot["players"]) for slot in card["slots"]), 5)
        self.assertTrue(card["captain"]["captain"])
        self.assertEqual(len(card["players"]), 4)
        self.assertTrue(all(len(person["champions"]) == 5 for slot in card["slots"] for person in slot["players"]))
        self.assertNotIn("private-identity", json.dumps(card))

    def test_target_distinguishes_solo_flex_and_unknown_flex_without_changing_snapshot(self):
        player = self.profile_player()
        player["riot_profile"].update(flex_current_tier="마스터", flex_lp=321)
        before = deepcopy(player)
        self.assertEqual(player_data(player)["flex_current_tier"], "마스터 · 321 LP")
        self.assertEqual(player_data(self.profile_player())["flex_current_tier"], "미입력")
        self.assertEqual(player, before)

    def test_queue_remaining_and_overview_use_readonly_renderer_contract(self):
        state = {"status": "READY", "lots": [self.lot(1, 0, "QUEUED")], "teams": self.teams(4)}
        renderer = Mock()
        with patch("roly.auction_components._renderer", return_value=renderer):
            render_queue(state, "queue")
            render_remaining(state, "remaining")
            render_overview(state, "overview", member_id=1)
        self.assertEqual([call.kwargs["data"]["kind"] for call in renderer.call_args_list], ["queue", "remaining", "overview"])
        self.assertTrue(renderer.call_args.kwargs["data"]["teams"][0]["mine"])

    def test_compact_team_queue_and_remaining_dom_preserves_images_and_releases_handlers(self):
        node = shutil.which("node")
        self.assertIsNotNone(node)
        lots = [self.lot(1, 0, "OPEN"), self.lot(2, 1, "QUEUED"), self.lot(3, 2, "UNSOLD")]
        state = {"status": "RUNNING", "lots": lots, "current_lot": lots[0]}
        overviews = [{"kind": "overview", "teams": [team_data(team, member_id=1, index=index)
            for index, team in enumerate(self.teams(count))]} for count in (4, 6, 8)]
        result = subprocess.run([node, "--input-type=module", "-e", STATIC_HARNESS],
            input=json.dumps({"source": JS, "overviews": overviews, "queue": queue_data(state), "remaining": remaining_data(state)}),
            text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
