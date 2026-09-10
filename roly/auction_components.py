"""Auction presentation: isolated cards, visual clock and opt-in bid chime.

Only server snapshots determine bids, deadlines and team membership. The small
browser clock interpolates the display; it never closes or awards a lot.
"""
from datetime import datetime
from hashlib import sha256
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st
from roly.riot_api import MASTERY_LIMIT


ROLES = {"TOP": "탑", "JG": "정글", "MID": "미드", "AD": "원딜", "SUP": "서포터"}
TEAM_COLORS = ("#2269c7", "#ce332f", "#238149", "#d27019", "#7753b9", "#21818b", "#ad4d80", "#657336")

HTML = """
<section class="auction-component"></section>
"""
CSS = """
:host { display:block; width:100%; min-width:0; container-type:inline-size; font-family:var(--st-font, "Pretendard", sans-serif); color:#142238; }
* { box-sizing:border-box; }
.auction-component { min-width:0; }
.panel { background:#fff; border:1px solid #dce1e8; border-radius:16px; padding:18px; }
.bid-history { height:520px; overflow:auto; display:flex; flex-direction:column; gap:9px; scrollbar-width:thin; }
.history-card { padding:13px; border:1px solid #dce1e8; background:#fff; border-radius:12px; color:#182c49; }
.history-card header { display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:7px; }
.history-card strong { font-size:16px; }
.history-card time,.history-captain { color:#536176; font-size:12px; }
.history-team { font-size:14px; margin-bottom:5px; }
.row { display:flex; align-items:center; justify-content:space-between; gap:10px; min-width:0; }
.muted { color:#536176; font-size:13px; line-height:1.5; }
.name { font-size:15px; font-weight:700; line-height:1.5; overflow-wrap:anywhere; }
.role { font-size:13px; color:#34465d; }
.avatar { flex:0 0 44px; height:44px; display:grid; place-items:center; overflow:hidden; background:#edf1f5; color:#34465d; border-radius:12px; font-size:21px; font-weight:700; }
.person { display:flex; gap:12px; align-items:center; min-width:0; }
.person-text { min-width:0; flex:1; }
.person-text > * + * { margin-top:3px; }
.team-title { color:var(--team-color,#2269c7); font-size:18px; font-weight:800; }
.team-balance { text-align:right; color:#34465d; font-size:13px; font-weight:700; }
.team-balance strong { display:block; font-size:17px; }
.team-badge { display:inline-block; padding:2px 6px; border-radius:5px; background:#edf3ff; color:#245e9b; font-size:10px; font-weight:750; white-space:nowrap; }
.captain-badge { background:#fff0ed; color:#bb3026; }
.compact-meta { color:#536176; font-size:11px; line-height:1.4; }
.compact-ranks { display:flex; flex-wrap:wrap; gap:3px 9px; font-size:11px; line-height:1.4; color:#34465d; }
.compact-masteries { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); width:100%; max-width:137px; gap:3px; margin-top:6px; }
.compact-masteries .champion-icon { width:100%; max-width:25px; height:auto; aspect-ratio:1; margin:0; font-size:10px; }
.compact-masteries .champion { width:100%; min-width:0; }
.captain { margin:14px 0; padding:13px; background:#f5f6fa; border-radius:12px; border:1px solid #dce1e8; }
.player { margin-top:8px; padding:10px; border:1px solid #dce1e8; border-radius:10px; background:#fff; }
.slot { margin-top:8px; padding:17px 12px; text-align:center; color:#536176; border:1px dashed #dce1e8; border-radius:12px; font-size:12px; }
.money { font-size:20px; font-weight:750; white-space:nowrap; }
.target { padding:14px 16px; }
.target .avatar { flex-basis:58px; height:58px; font-size:27px; }
.target .name { font-size:18px; line-height:1.35; }
.target .muted { font-size:13px; }
.target-header { display:grid; grid-template-columns:minmax(0,1.1fr) minmax(180px,1fr); gap:16px; align-items:center; }
.target .avatar { overflow:hidden; border:1px solid #dce1e8; }
.avatar .image-initial, .avatar img, .champion-icon .image-initial, .champion-icon img { grid-area:1/1; }
.avatar img, .champion-icon img { display:block; width:100%; height:100%; object-fit:cover; }
.riot-id { font-size:12px; color:#536176; line-height:1.5; overflow-wrap:anywhere; }
.profile-stats { min-width:0; display:grid; gap:5px; }
.profile-stat { display:flex; gap:10px; justify-content:space-between; align-items:baseline; }
.stat-label { flex-shrink:0; color:#536176; font-size:12px; }
.stat-value { min-width:0; text-align:right; font-size:13px; font-weight:650; overflow-wrap:anywhere; }
.profile-source { appearance:none; border:0; background:transparent; color:#536176; font-size:12px; padding:0 3px; cursor:help; }
.profile-source:focus-visible { outline:2px solid #245e9b; outline-offset:2px; border-radius:3px; }
.mastery { margin-top:2px; padding-top:7px; border-top:1px solid #e5e9ee; }
.mastery-label { display:flex; justify-content:space-between; align-items:center; font-size:11px; color:#536176; margin-bottom:6px; }
.mastery-empty { font-size:12px; color:#536176; line-height:1.5; }
.champions { display:flex; gap:clamp(5px,2vw,14px); align-items:flex-start; flex-wrap:wrap; }
.target .champions { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:6px; width:100%; max-width:184px; }
.target .champion { width:100%; }
.target .champion-icon { width:100%; max-width:32px; height:auto; aspect-ratio:1; font-size:14px; }
.champion { margin:0; width:48px; min-width:0; text-align:center; }
.champion-icon { display:grid; place-items:center; width:42px; height:42px; margin:auto; overflow:hidden; border-radius:50%; border:1px solid #dce1e8; background:#edf1f5; color:#34465d; font-size:17px; font-weight:700; }
.champion-name { margin-top:5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:11px; color:#34465d; }
.champion:focus-visible { outline:2px solid #245e9b; outline-offset:3px; border-radius:6px; }
.timer { margin-top:12px; border-radius:14px; border:1px solid #dce1e8; overflow:hidden; background:#f5f6fa; }
.timer-content { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:18px; }
.bidder { font-size:16px; font-weight:700; overflow-wrap:anywhere; }
.bid-price { font-size:23px; font-weight:750; margin-top:5px; }
.clock { text-align:right; flex-shrink:0; }
.seconds { font-size:33px; font-weight:800; color:#b42318; line-height:1.2; }
.bar { height:4px; background:#dce1e8; }
.fill { height:100%; background:#b42318; width:0; transition:width .12s linear; }
.sound-row { display:flex; justify-content:flex-end; gap:8px; align-items:center; padding:5px 8px; background:#fff; border:1px solid #dce1e8; border-radius:10px; }
.sound-button { display:grid; place-items:center; flex:0 0 28px; width:28px; height:28px; border:0; border-radius:7px; padding:4px; background:#fff; color:#34465d; cursor:pointer; }
.sound-button:focus-visible { outline:2px solid #245e9b; outline-offset:2px; }
.sound-button[aria-pressed="true"] { color:#164574; background:#eaf2fb; }
.sound-controls { display:flex; align-items:center; gap:8px; min-width:0; }
.volume { width:78px; min-width:35px; accent-color:#245e9b; }
.volume-label { font-size:12px; color:#34465d; min-width:32px; }
.overview-grid { display:grid; gap:10px; grid-template-columns:repeat(var(--cols),minmax(0,1fr)); }
.mini-team { min-width:0; border:1px solid #e0e4eb; border-radius:13px; padding:10px; background:#fff; box-shadow:0 3px 12px rgb(28 37 59 / 3%); }
.mini-heading { display:flex; justify-content:space-between; align-items:center; gap:8px; min-height:44px; padding:0 0 8px 11px; position:relative; }
.mini-heading::before { content:''; position:absolute; left:0; top:1px; bottom:9px; width:5px; border-radius:5px; background:#7852ff; }
.mini-heading-title { min-width:0; display:flex; align-items:center; flex-wrap:wrap; gap:5px; font-size:16px; font-weight:800; }
.mini-captain { margin-top:2px; color:#536176; font-size:12px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.mini-budget { flex-shrink:0; padding:4px 8px; border-radius:20px; background:#eafbf1; color:#087e3a; font-size:14px; font-weight:800; white-space:nowrap; }
.mini-position { display:grid; grid-template-columns:38px minmax(0,1fr); gap:7px; align-items:center; min-height:46px; padding:5px 7px; margin-top:4px; border:1px solid transparent; border-radius:10px; background:#f2f3f5; }
.mini-position.occupied { background:#eff6ff; border-color:#c2d9ff; }
.mini-role { color:#536176; font-size:12px; font-weight:750; text-align:center; }
.mini-people { min-width:0; }
.mini-member { display:flex; gap:7px; align-items:center; min-width:0; min-height:34px; font-size:14px; line-height:1.3; }
.mini-member .avatar { flex-basis:34px; height:34px; border-radius:50%; font-size:15px; overflow:hidden; }
.mini-identity { flex:1; min-width:0; }
.mini-name { font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.mini-tag { font-size:11px; color:#536176; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.mini-price { font-size:11px; color:#536176; white-space:nowrap; }
.mini-member .captain-badge { padding:3px 5px; border-radius:20px; background:#ef3038; color:#fff; font-size:11px; }
.mini-empty { color:#6b7688; font-size:12px; font-weight:600; text-align:center; padding-right:45px; }
@media(max-height:760px) and (min-width:621px) { .mini-position{min-height:40px;padding:3px 5px;} .mini-member{min-height:32px;} .mini-member .avatar{flex-basis:32px;height:32px;} .mini-heading{min-height:40px;} }
.queue-panel { display:flex; gap:14px; align-items:center; padding:12px 14px; background:#fff; border:1px solid #dce1e8; border-radius:12px; }
.queue-heading { flex-shrink:0; font-size:13px; font-weight:750; }
.queue-heading .muted { font-weight:400; font-size:11px; margin-top:4px; }
.queue-strip { display:flex; flex:1; align-items:center; min-width:0; gap:7px; overflow-x:auto; scrollbar-width:thin; padding:2px 0 5px; }
.queue-item { display:flex; gap:7px; align-items:center; flex-shrink:0; padding:7px 9px; border:1px solid #e2e7ee; border-radius:9px; background:#fff; max-width:190px; }
.queue-item.active { background:#edf3ff; border-color:#8bb0ef; }
.queue-item.done { background:#f5f7fa; }
.queue-item .avatar { flex-basis:28px; width:28px; height:28px; font-size:13px; border-radius:50%; overflow:hidden; }
.queue-name { font-size:12px; font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:110px; }
.queue-status { font-size:10px; color:#536176; margin-top:2px; }
.queue-arrow { flex-shrink:0; color:#a3adba; font-size:17px; }
.remaining-panel { background:#fff; border:1px solid #dce1e8; border-radius:14px; padding:14px; }
.remaining-header { display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap; align-items:center; margin-bottom:10px; }
.role-counts { display:flex; gap:5px; flex-wrap:wrap; }
.role-count { background:#eef3fa; color:#345a88; padding:3px 7px; font-size:10px; border-radius:6px; }
.remaining-scroll { max-height:330px; overflow-y:auto; scrollbar-width:thin; padding-right:4px; }
.remaining-group { font-size:12px; color:#536176; font-weight:650; margin:8px 0; }
.remaining-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }
.remaining-grid .player { margin:0; background:#f7f9fc; }
.remaining-grid .avatar { flex-basis:34px; height:34px; font-size:17px; overflow:hidden; }
.remaining-grid .name { font-size:12px; }
@media(max-width:500px) { .remaining-grid{grid-template-columns:minmax(0,1fr);} .queue-panel{gap:9px;padding:10px;} .mini-team{padding:7px;} .mini-heading{font-size:13px;} .mini-member{gap:4px;} .mini-position{grid-template-columns:25px minmax(0,1fr);} }
@media(max-width:420px) { .panel{padding:13px;} .target .name{font-size:17px;} .target-header{grid-template-columns:minmax(0,1fr);gap:14px;} .timer-content{padding:14px;} .bidder{font-size:14px;} .bid-price{font-size:21px;} .seconds{font-size:28px;} }
@container(max-width:500px) { .remaining-grid{grid-template-columns:minmax(0,1fr);} .person{gap:9px;} .captain{padding:10px;} }
@container(max-width:420px) { .panel{padding:13px;} .target-header{grid-template-columns:minmax(0,1fr);gap:14px;} .target .name{font-size:17px;} .timer-content{padding:14px;} .bidder{font-size:14px;} .bid-price{font-size:21px;} .seconds{font-size:28px;} }
"""
JS = r"""
export default function({parentElement, data}) {
  const root = parentElement.querySelector('.auction-component');
  if (!root) return;
  const doc = root.ownerDocument;
  const make = (tag, cls, text) => {
    const node = doc.createElement(tag); node.className = cls;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const imageHandlers=[];
  const imageUrl=value=>typeof value==='string' && /^https:\/\/ddragon\.leagueoflegends\.com\/cdn\/[0-9]+\.[0-9]+\.[0-9]+\/img\/(?:profileicon\/[0-9]+|champion\/[A-Za-z0-9]+)\.png$/.test(value) ? value : null;
  const portrait=(url, name, cls)=>{
    const box=make('div',cls);
    box.append(make('span','image-initial',(name || '?').slice(0,1)));
    const safe=imageUrl(url);
    if(safe){
      const img=make('img','portrait-image');img.alt=String(name || '프로필');
      img.referrerPolicy='no-referrer';img.decoding='async';
      img.onerror=()=>{img.onerror=null;img.remove();};
      imageHandlers.push(img);img.src=safe;box.append(img);
    }
    return box;
  };
  const cleanImages=()=>{for(const img of imageHandlers)img.onerror=null;};
  const staticView=['overview','team','queue','remaining','history'].includes(data?.kind);
  const staticSignature=staticView ? JSON.stringify(data) : null;
  if(staticView && root.dataset.staticSignature===staticSignature)return root._rolyStaticCleanup;
  root._rolyStaticCleanup?.();
  if(!staticView){delete root.dataset.staticSignature;root._rolyStaticCleanup=null;}
  const remember=(node,extra=()=>{})=>{
    root.replaceChildren(node);root.dataset.staticSignature=staticSignature;
    const cleanup=()=>{cleanImages();extra();};root._rolyStaticCleanup=cleanup;return cleanup;
  };
  const masteryIcons=p=>{
    const row=make('div','compact-masteries');
    for(const champion of Array.isArray(p.champions) ? p.champions.slice(0,__MASTERY_LIMIT__) : []){
      const item=make('figure','champion');item.title=String(champion.tooltip || champion.name);item.tabIndex=0;
      item.setAttribute('aria-label',item.title);item.append(portrait(champion.icon_url,champion.name,'champion-icon'));row.append(item);
    }
    return row;
  };
  const person = (p, cls, label) => {
    const node = make('div', 'person '+cls);
    const info = make('div', 'person-text');
    if (label) info.append(make('span', 'team-badge captain-badge', label));
    info.append(make('div', 'name', p.nickname || p.name), make('div', 'riot-id', p.tag || p.riot_id || ''));
    info.append(make('div','compact-meta',p.meta));
    const ranks=make('div','compact-ranks');ranks.append(make('span','','클랜 '+p.clan_tier),make('span','','솔로 '+p.current_tier));
    info.append(ranks,masteryIcons(p));
    node.append(portrait(p.profile_icon_url,p.name,'avatar'), info);
    return node;
  };
  const targetProfile=p=>{
    const header=make('div','target-header');
    const identity=make('div','person');
    const info=make('div','person-text');
    info.append(make('div','name',p.nickname || p.name));
    if(p.riot_id)info.append(make('div','riot-id',p.riot_id));
    info.append(make('div','muted',p.power_label || p.meta));
    identity.append(portrait(p.profile_icon_url,p.nickname || p.name,'avatar'),info);
    const stats=make('div','profile-stats');
    for(const [label,value] of [['클랜 티어',p.clan_tier],['현재 솔로',p.current_tier],['현재 자유',p.flex_current_tier],['포지션',p.role]]){
      const row=make('div','profile-stat');
      row.append(make('span','stat-label',label),make('span','stat-value',value || '기록 없음'));
      stats.append(row);
    }
    const mastery=make('div','mastery');
    const masteryLabel=make('div','mastery-label','숙련도 TOP __MASTERY_LIMIT__');
    const source=make('button','profile-source','ⓘ');source.type='button';
    source.title=p.profile_source || 'Riot API 미조회';source.setAttribute('aria-label',source.title);
    masteryLabel.append(source);mastery.append(masteryLabel);
    const champions=Array.isArray(p.champions) ? p.champions.slice(0,__MASTERY_LIMIT__) : [];
    if(champions.length){
      const items=make('div','champions');
      for(const champion of champions){
        const item=make('figure','champion');
        item.title=String(champion.tooltip || champion.name);item.tabIndex=0;
        item.setAttribute('aria-label',item.title);
        item.append(portrait(champion.icon_url,champion.name,'champion-icon'));
        items.append(item);
      }
      mastery.append(items);
    }else {
      const empty=make('div','mastery-empty','정보 없음');
      empty.title=p.mastery_empty || 'Riot API 조회 후 숙련도 정보를 표시합니다.';mastery.append(empty);
    }
    stats.append(mastery);header.append(identity,stats);
    const content=make('div','target-profile');content.append(header);
    return content;
  };
  if (data?.kind === 'overview') {
    const teams=data.teams;
    const grid=make('div','overview-grid');
    const layout=()=>{
      const columns=(root.clientWidth || globalThis.innerWidth) < 620 ? 2 : (teams.length > 4 ? Math.ceil(teams.length/2) : 2);
      grid.style.setProperty('--cols',columns);
    };
    for(const team of teams){
      const card=make('article','mini-team'+(team.mine?' mine':''));card.style.setProperty('--team-color',team.color);
      const header=make('div','mini-heading');
      const title=make('div','mini-heading-title');title.append(make('strong','',team.name));
      if(team.mine)title.append(make('span','team-badge','내 팀'));
      const teamInfo=make('div','mini-identity');
      const captain=make('div','mini-captain','팀장 '+(team.captain?.nickname || '미정'));
      captain.title=team.captain?.name || '팀장 미정';teamInfo.append(title,captain);
      const budget=make('span','mini-budget',team.remaining);budget.title='포인트 · '+team.count+'/5명';
      header.append(teamInfo,budget);card.append(header);
      for(const slot of team.slots){
        const position=make('div','mini-position'+(slot.players.length?' occupied':''));position.append(make('span','mini-role',slot.role));
        const people=make('div','mini-people');
        for(const p of slot.players){
          const line=make('div','mini-member'+(p.captain?' captain-row':''));
          const identity=make('div','mini-identity');const name=make('div','mini-name',p.nickname);name.title=p.name;
          identity.append(name,make('div','mini-tag',p.tag));
          line.append(portrait(p.profile_icon_url,p.name,'avatar'),identity,
            p.captain ? make('span','team-badge captain-badge','팀장') : make('span','mini-price',p.price));people.append(line);
        }
        if(!slot.players.length)people.append(make('div','mini-empty','미정'));
        position.append(people);card.append(position);
      }
      grid.append(card);
    }
    if(parentElement._rolyOverviewResize)globalThis.removeEventListener('resize',parentElement._rolyOverviewResize);
    parentElement._rolyOverviewResize=layout;layout();globalThis.addEventListener('resize',layout);
    return remember(grid,()=>{globalThis.removeEventListener('resize',layout);});
  }
  if (data?.kind === 'team') {
    const team = data.team;
    const panel = make('div', 'panel');
    const title = make('div', 'row');
    panel.style.setProperty('--team-color',team.color);
    const name=make('div','team-title',team.name);if(team.mine)name.append(make('span','team-badge','내 팀'));
    const balance=make('div','team-balance',team.count+'/5명 · 포인트');balance.append(make('strong','',team.remaining));
    title.append(name,balance);panel.append(title);
    if(team.captain)panel.append(person(team.captain, 'captain', '팀장 · 입찰 담당'));
    else panel.append(make('div','slot','팀장 지정 대기'));
    panel.append(make('div', 'muted', '팀원'));
    for (const p of team.players) panel.append(person(p, 'player'));
    for (let i=team.players.length; i<4; i++) panel.append(make('div', 'slot', '팀원 배정 대기'));
    return remember(panel);
  }
  if(data?.kind==='queue'){
    const panel=make('section','queue-panel');const heading=make('div','queue-heading','경매 대상');
    heading.append(make('div','muted',data.label));const strip=make('div','queue-strip');strip.setAttribute('role','list');strip.tabIndex=0;strip.setAttribute('aria-label','경매 대상 순서');
    let active=null;
    for(const [index,item] of data.items.entries()){
      if(index)strip.append(make('span','queue-arrow','›'));
      const card=make('div','queue-item'+(item.active?' active':'')+(item.done?' done':''));card.setAttribute('role','listitem');
      if(item.active){card.setAttribute('aria-current','step');active=card;}
      const info=make('div','person-text');const name=make('div','queue-name',item.player.nickname);name.title=item.player.riot_id;
      info.append(name,make('div','queue-status','#'+item.number+' · '+item.label));
      card.append(portrait(item.player.profile_icon_url,item.player.name,'avatar'),info);strip.append(card);
    }
    if(!data.items.length)strip.append(make('div','muted',data.empty));panel.append(heading,strip);
    const cleanup=remember(panel);
    if(active && typeof active.offsetLeft==='number')strip.scrollLeft=Math.max(0,active.offsetLeft-strip.offsetLeft-(strip.clientWidth || 0)/3);
    return cleanup;
  }
  if(data?.kind==='remaining'){
    const panel=make('section','remaining-panel'),header=make('div','remaining-header');
    header.append(make('strong','name','남은 경매 대상'));const counts=make('div','role-counts');
    for(const count of data.counts)counts.append(make('span','role-count',count.role+' '+count.count));
    header.append(counts);panel.append(header);const scroll=make('div','remaining-scroll');scroll.tabIndex=0;scroll.setAttribute('aria-label','남은 경매 대상 목록');
    for(const group of data.groups){
      if(!group.players.length)continue;
      scroll.append(make('div','remaining-group',group.label+' · '+group.players.length+'명'));
      const grid=make('div','remaining-grid');for(const player of group.players)grid.append(person(player,'player'));scroll.append(grid);
    }
    if(!data.groups.some(group=>group.players.length))scroll.append(make('div','muted','대기 중인 선수가 없습니다.'));
    panel.append(scroll);return remember(panel);
  }
  if(data?.kind==='history'){
    const list=make('section','bid-history');list.setAttribute('aria-label','입찰 기록 목록');
    if(!data.bids.length)list.append(make('p','muted','접수된 입찰이 없습니다.'));
    for(const bid of data.bids){
      const card=make('article','history-card');card.dataset.bidId=String(bid.id);
      const header=make('header','');header.append(make('strong','',bid.amount),make('time','',bid.time));
      card.append(header,make('div','history-team',bid.team),make('div','history-captain','팀장 '+bid.captain));
      list.append(card);
    }
    return remember(list);
  }
  if(data?.kind==='sound'){
  const memory = parentElement._rolyAuctionAudio ||= { lastBid:null, initialized:false, enabled:false, ctx:null, count:0, volume:.8, eventId:data.event_id };
  if(memory.eventId!==data.event_id){memory.lastBid=null;memory.initialized=false;memory.eventId=data.event_id;}
  const previousSound=root.querySelector('.sound-row');
  const sound = previousSound || make('div', 'sound-row');
  const controls=previousSound?.querySelector('.sound-controls') || make('div','sound-controls');
  const button = previousSound?.querySelector('.sound-button') || make('button', 'sound-button'); button.type='button';
  const slider = previousSound?.querySelector('.volume') || make('input','volume');
  slider.type='range';slider.min='0';slider.max='100';slider.step='5';slider.value=String(Math.round(memory.volume*100));
  slider.setAttribute('aria-label','입찰 효과음 음량');
  const volumeLabel=previousSound?.querySelector('.volume-label') || make('span','volume-label');
  const updateSound = () => {
    const label=memory.enabled ? '입찰 소리 끄기' : '입찰 소리 켜기';
    button.title=label;button.setAttribute('aria-label',label);
    if(button.dataset.enabled!==String(memory.enabled)){
      const icon=doc.createElementNS('http://www.w3.org/2000/svg','svg');
      icon.setAttribute('viewBox','0 0 24 24');icon.setAttribute('width','16');icon.setAttribute('height','16');
      icon.setAttribute('aria-hidden','true');icon.setAttribute('fill','currentColor');
      const path=doc.createElementNS('http://www.w3.org/2000/svg','path');
      path.setAttribute('d',memory.enabled
        ? 'M3 9v6h4l5 4V5L7 9H3zm11-1v8a5 5 0 0 0 0-8zm0-4v2a7 7 0 0 1 0 12v2a9 9 0 0 0 0-16z'
        : 'M3 9v6h4l5 4V5L7 9H3zm12-1-1 1 3 3-3 3 1 1 3-3 3 3 1-1-3-3 3-3-1-1-3 3-3-3z');
      icon.append(path);button.replaceChildren(icon);button.dataset.enabled=String(memory.enabled);
    }
    button.setAttribute('aria-pressed', String(memory.enabled));
    root.dataset.soundCount = String(memory.count);
    root.dataset.soundEnabled = String(memory.enabled);
    root.dataset.soundVolume = String(Math.round(memory.volume*100));
    volumeLabel.textContent=Math.round(memory.volume*100)+'%';
  };
  const ding = () => {
    if (!memory.enabled || !memory.ctx || memory.ctx.state !== 'running' || memory.volume <= 0) return;
    const ctx = memory.ctx;
    for (const [frequency, delay] of [[880,0], [1320,.10]]) {
      const osc = ctx.createOscillator(); const gain = ctx.createGain();
      osc.type='sine'; osc.frequency.value=frequency;
      const start=ctx.currentTime+delay;
      gain.gain.setValueAtTime(0.0001,start);
      gain.gain.exponentialRampToValueAtTime(Math.max(.0001,.15*memory.volume),start+.012);
      gain.gain.exponentialRampToValueAtTime(.0001,start+.32);
      osc.connect(gain); gain.connect(ctx.destination);
      osc.start(start); osc.stop(start+.34);
      osc.onended=()=>{osc.disconnect();gain.disconnect();};
    }
    memory.count += 1; updateSound();
  };
  button.onclick = async () => {
    if (memory.enabled) { memory.enabled=false; updateSound(); return; }
    try {
      const Audio = globalThis.AudioContext || globalThis.webkitAudioContext;
      if (!Audio) throw new Error('Audio unavailable');
      if (!memory.ctx || memory.ctx.state === 'closed') memory.ctx=new Audio();
      await memory.ctx.resume();
      memory.enabled=memory.ctx.state === 'running'; updateSound(); ding();
    } catch (_) { memory.enabled=false; updateSound(); button.title='브라우저에서 소리를 허용해 주세요';button.setAttribute('aria-label',button.title); }
  };
  slider.oninput=()=>{const value=Number(slider.value);memory.volume=Number.isFinite(value)?Math.max(0,Math.min(100,value))/100:0;updateSound();};
  if (!previousSound) {
    controls.append(button,slider,volumeLabel);
    sound.append(controls);root.replaceChildren(sound);
  }
  // A new arrival never replays history. Stale/duplicate IDs stay silent;
  // an empty history after an auction reset establishes a new baseline.
  if (memory.initialized && data.bid_id != null && (memory.lastBid==null || data.bid_id>memory.lastBid)) ding();
  memory.lastBid=data.bid_id==null?null:Math.max(memory.lastBid || 0,data.bid_id);
  memory.initialized=true;updateSound();
  return ()=>{
    button.onclick=null;slider.oninput=null;
    queueMicrotask(()=>{
      if (!parentElement.isConnected && memory.ctx && memory.ctx.state !== 'closed') {
        memory.ctx.close().catch(()=>{});memory.enabled=false;
      }
    });
  };
  }
  const memory = parentElement._rolyAuctionClock ||= {interval:null};
  // Replace only the visual clock; sound has its own mounted component.
  if (memory.interval != null) clearInterval(memory.interval);
  const signature=JSON.stringify([data.target_label,data.player]);
  const previousPanel=root.querySelector('.target');
  let panel=previousPanel;
  if(!panel || panel.dataset.profileSignature!==signature){
    previousPanel?._rolyCleanImages?.();
    panel=make('div','panel target');panel.dataset.profileSignature=signature;
    panel.append(targetProfile(data.player));
    panel._rolyCleanImages=cleanImages;
  }
  let timer=root.querySelector('.timer');
  if(!timer){
    timer=make('div','timer');const content=make('div','timer-content'),current=make('div','person-text');
    current.append(make('div','bidder'),make('div','bid-price'));
    const clock=make('div','clock');clock.append(make('div','muted clock-label'),make('div','seconds'));
    content.append(current,clock);const bar=make('div','bar');bar.append(make('div','fill'));
    timer.append(content,bar);root.replaceChildren(panel,timer);
  }else if(previousPanel!==panel){
    if(previousPanel)previousPanel.replaceWith(panel);else root.replaceChildren(panel,timer);
  }
  const write=(node,value)=>{const text=String(value ?? '');if(node.textContent!==text)node.textContent=text;};
  write(timer.querySelector('.bidder'),data.bidder);write(timer.querySelector('.bid-price'),data.price);
  write(timer.querySelector('.clock-label'),data.clock_label);
  const digits=timer.querySelector('.seconds'),fill=timer.querySelector('.fill');
  const generation=memory.generation=(memory.generation || 0)+1;
  const received=performance.now();
  const clockKey=JSON.stringify([data.event_id,data.lot_id,data.deadline,data.phase]);
  let startingSeconds=Math.max(0,Number(data.seconds || 0));
  if (memory.clock?.key === clockKey && memory.clock.ticking === Boolean(data.ticking)) {
    const elapsed=memory.clock.ticking ? Math.max(0,(received-memory.clock.received)/1000) : 0;
    // An unchanged server deadline cannot move away just because a delayed
    // snapshot arrived. A changed deadline or phase establishes a new anchor.
    startingSeconds=Math.min(startingSeconds,Math.max(0,memory.clock.seconds-elapsed));
  }
  memory.clock={key:clockKey,received,seconds:startingSeconds,ticking:Boolean(data.ticking)};
  const draw=()=>{
    const elapsed=data.ticking ? Math.max(0,(performance.now()-received)/1000) : 0;
    const seconds=Math.max(0,startingSeconds-elapsed);
    write(digits,data.show_clock ? Math.ceil(seconds)+'초' : data.clock_text);
    const width=(Math.min(1,seconds/Math.max(1,data.scale))*100)+'%';
    if(fill.style.width!==width)fill.style.width=width;
  };
  draw(); const interval=data.ticking?setInterval(draw,100):null;memory.interval=interval;
  return ()=>{
    // Cleanup precedes same-node rerenders; keep image error handlers while
    // that profile remains mounted, and release only the last generation.
    queueMicrotask(()=>{if(memory.generation===generation)panel._rolyCleanImages?.();});
    clearInterval(interval);
    if (memory.interval === interval) memory.interval=null;
  };
}
""".replace("__MASTERY_LIMIT__", str(MASTERY_LIMIT))

# Each presentation owns its own DOM. Fresh HTTP snapshots fan out to those
# components in the same login/event context without rerunning Python widgets.
JS = JS.replace("export default function(", "function renderAuctionView(", 1) + r"""
export default function({parentElement,data}) {
  const view=parentElement.ownerDocument.defaultView || globalThis;
  const context=data?.live_context;
  parentElement._rolyLiveViewDispose?.();
  const select=views=>{
    if(data.kind==='team')return views?.teams?.[String(data.live_team)] ? {kind:'team',team:views.teams[String(data.live_team)]} : null;
    return views?.[data.kind] || null;
  };
  const cached=context ? select(view._rolyAuctionViews?.get(context)) : null;
  let cleanup=null;
  const clear=()=>{
    cleanup?.();cleanup=null;
    const root=parentElement.querySelector('.auction-component');
    if(root){root.replaceChildren();delete root.dataset.staticSignature;}
  };
  const connection=()=>{
    if(!context)return;
    const state=view._rolyAuctionViewMeta?.get(context);
    const root=parentElement.querySelector('.auction-component');if(!root)return;
    const mismatch=data.live_epoch && state?.epoch!==data.live_epoch;
    const status=mismatch?'connecting':state?.connection || 'connecting';root.dataset.liveConnection=status;
    if(mismatch || ['stopped','unavailable'].includes(status))clear();
    if(data.kind!=='overview')return;
    let note=root.querySelector('.auction-connection');
    if(!note){note=root.ownerDocument.createElement('p');note.className='muted auction-connection';note.setAttribute('role','status');root.append(note);}
    const message={live:'',connecting:'최신 팀 현황을 확인하고 있습니다.',stale:'연결 확인 중 · 마지막으로 확인된 현황입니다.',
      stopped:'로그인 또는 경매 연결이 변경되었습니다. 화면을 새로고침해 주세요.',unavailable:'경매 정보를 확인할 수 없습니다.'}[status] || '';
    if(note.textContent!==message)note.textContent=message;note.hidden=!message;
  };
  if(context && view._rolyAuctionViews?.has(context) && view._rolyAuctionViews.get(context)===null)clear();
  else cleanup=renderAuctionView({parentElement,data:cached || data});
  connection();
  if(!context)return cleanup;
  let disposed=false;
  const update=event=>{
    if(disposed || !parentElement.isConnected || event.detail?.context!==context)return;
    if(event.detail.views===null){clear();connection();return;}
    const next=select(event.detail.views);
    if(next)cleanup=renderAuctionView({parentElement,data:next});
    else if(event.detail.views && Object.hasOwn(event.detail.views,data.kind) && event.detail.views[data.kind]===null)clear();
    connection();
  };
  view.addEventListener('roly-auction-frame',update);
  const dispose=()=>{
    if(disposed)return;disposed=true;
    view.removeEventListener('roly-auction-frame',update);
    // Streamlit disposes immediately before a synchronous remount. Preserve
    // unchanged images/resize ownership until that replacement takes over.
    queueMicrotask(()=>{if(parentElement._rolyLiveViewDispose===dispose){
      cleanup?.();parentElement._rolyLiveViewDispose=null;
    }});
  };
  parentElement._rolyLiveViewDispose=dispose;
  return dispose;
}
"""

COMPONENT_REVISION = sha256((HTML + "\0" + CSS + "\0" + JS).encode()).hexdigest()[:16]

@st.cache_resource(scope="session", show_spinner=False)
def _register(scope, revision):
    return st.components.v2.component("auction_presentation_" + revision, html=HTML, css=CSS, js=JS, isolate_styles=True)


def _renderer():
    st.session_state.setdefault("_auction_component_scope", uuid4().hex)
    return _register(st.session_state["_auction_component_scope"], COMPONENT_REVISION)


def _image_url(value):
    if not isinstance(value, str):
        return None
    return value if re.fullmatch(
        r"https://ddragon\.leagueoflegends\.com/cdn/[0-9]+\.[0-9]+\.[0-9]+/img/(?:profileicon/[0-9]+|champion/[A-Za-z0-9]+)\.png",
        value) else None


def _tier_label(tier, lp=None):
    if tier is None:
        return "기록 없음"
    tier = str(tier).strip()
    if not tier:
        return "미등록"
    return f"{tier} · {lp:,} LP" if tier != "언랭크" and type(lp) is int and lp >= 0 else tier


def _profile_time(value):
    try:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError()
        return "갱신 " + value.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y.%m.%d %H:%M") + " (한국 시간)"
    except (AttributeError, TypeError, ValueError, OverflowError):
        return "갱신 시각 미확인"


def player_data(player, *, price=False):
    meta = f"{ROLES.get(player['role'], player['role'])} · 전력 {player['score']:g} P"
    if price:
        meta += f" · 낙찰 {int(player.get('price') or 0):,} P"
    profile = player.get("riot_profile")
    profile = profile if isinstance(profile, dict) else None
    clan_tier = player.get("clan_tier_snapshot") if "clan_tier_snapshot" in player else player.get("clan_tier")
    champions = []
    if profile is not None:
        current = _tier_label(profile.get("current_tier"), profile.get("lp"))
        source = "Riot API · " + _profile_time(profile.get("updated_at"))
        source_champions = profile.get("champions")
        for champion in source_champions[:MASTERY_LIMIT] if isinstance(source_champions, list) else []:
            if not isinstance(champion, dict) or not isinstance(champion.get("name"), str) or not champion["name"].strip():
                continue
            name = champion["name"].strip()[:80]
            points, level = champion.get("points"), champion.get("level")
            points = points if type(points) is int and points >= 0 else None
            level = level if type(level) is int and level >= 0 else None
            mastery = f"숙련도 {points:,}점" if points is not None else "숙련도 점수 미확인"
            level_label = f"레벨 {level}" if level is not None else "레벨 미확인"
            champion_id = champion.get("id")
            champions.append({"id": champion_id if type(champion_id) is int and champion_id > 0 else None, "name": name[:80], "icon_url": _image_url(champion.get("icon_url")),
                "points": points, "level": level, "tooltip": f"{name} · {mastery} · {level_label}"})
    else:
        current = _tier_label(player.get("current_tier_snapshot"), player.get("current_tier_lp_snapshot"))
        source = "명단 확정 당시 · Riot API 미조회"
    return {"name": player["riot_id"], "nickname": player["riot_id"].split("#", 1)[0], "tag": "#" + player["riot_id"].split("#", 1)[-1], "riot_id": player["riot_id"],
        "meta": meta, "role": ROLES.get(player["role"], player["role"]), "score": player["score"],
        "power_label": f"전력 {player['score']:g} P", "clan_tier": _tier_label(clan_tier),
        "current_tier": current, "flex_current_tier": _tier_label(profile["flex_current_tier"], profile.get("flex_lp")) if profile and profile.get("flex_current_tier") else "미입력", "profile_source": source,
        "profile_icon_url": _image_url(profile.get("profile_icon_url")) if profile else None,
        "champions": champions, "mastery_empty": "저장된 챔피언 숙련도 정보가 없습니다." if profile is not None else "Riot API 조회 후 숙련도 정보를 표시합니다."}


def team_data(team, *, member_id=None, index=None):
    if index is None:
        number = re.match(r"^(\d+)", team["name"])
        index = int(number.group(1)) - 1 if number else max(0, int(team.get("id", 1)) - 1)
    people = []
    for player in team["players"]:
        person = player_data(player, price=player["member_id"] != team["captain_id"])
        person.update(member_id=player["member_id"], role_code=player["role"], captain=player["member_id"] == team["captain_id"], price=f"{int(player.get('price') or 0):,}P")
        people.append(person)
    return {"name": team["name"], "count": len(people), "remaining": f"{team['remaining']:,} P",
        "color": TEAM_COLORS[index % len(TEAM_COLORS)], "mine": member_id is not None and any(p["member_id"] == member_id for p in people),
        "captain": next((p for p in people if p["captain"]), None), "players": [p for p in people if not p["captain"]],
        "slots": [{"role_code": role, "role": label, "players": [p for p in people if p["role_code"] == role]} for role, label in ROLES.items()]}


def render_team(team, key, *, member_id=None, live_context=None):
    data = {"kind": "team", "team": team_data(team, member_id=member_id)}
    if live_context:
        data.update(live_context=live_context, live_team=team["id"])
    _renderer()(data=data, key=key, height="content", width="stretch")


def stage_data(state, *, elapsed_seconds=0):
    lot = state["current_lot"]
    team = next((t for t in state["teams"] if t["id"] == lot.get("highest_team_id")), None)
    captain = next((p for p in team["players"] if p["member_id"] == team["captain_id"]), None) if team else None
    bidder = f"{team['name']} · 팀장 {captain['riot_id']}" if captain else "첫 입찰을 기다립니다"
    price = f"최고 입찰 {int(lot.get('highest_bid') or 0):,} P" if team else "입찰 없음"
    status = state["status"]
    waiting = lot["status"] in ("SOLD", "UNSOLD", "CANCELLED")
    seconds = state.get("next_in_seconds") if waiting else lot.get("remaining_seconds")
    label = "다음 선수 준비" if waiting else "남은 시간"
    if lot["status"] == "SOLD":
        price = f"{int(lot['highest_bid']):,} P 낙찰"
    elif lot["status"] == "UNSOLD":
        bidder, price = "이번 선수 유찰", "입찰 종료"
    elif lot["status"] == "CANCELLED":
        bidder, price = "낙찰 취소", "재경매 대기"
    if status == "PAUSED":
        label = "일시정지"
    if status == "READY":
        label, bidder, price = "시작 대기", "경매 시작을 기다립니다", "입찰 없음"
    ticking = status in ("RUNNING", "WAITING")
    seconds = max(0, float(seconds or 0) - (max(0, float(elapsed_seconds)) if ticking else 0))
    return {"kind": "stage", "player": player_data(lot), "target_label": f"{lot['sequence'] + 1}번째 선수",
        "bidder": bidder, "price": price, "clock_label": label, "clock_text": f"설정 {state['bid_seconds']}초" if status == "READY" else "완료" if status == "COMPLETED" else "대기",
        "seconds": seconds, "scale": 3 if waiting else state["bid_seconds"],
        "event_id": state.get("event_id"), "lot_id": lot.get("id"),
        "deadline": state.get("next_at") if waiting else lot.get("closes_at"),
        "phase": f"{status}:{state.get('paused_phase') or ''}:{lot['status']}",
        "show_clock": status != "READY" and bool(not waiting or state.get("next_at") is not None or status == "PAUSED"),
        "ticking": ticking, "bid_id": state["bids"][0]["id"] if state.get("bids") else None,
        "rule": f"입찰 +5초 · 최대 {state['bid_seconds']}초 · 다음 선수 준비 3초"}


def render_history(bids, key, *, live_context=None):
    """A stable DOM list instead of dozens of Streamlit blocks per refresh."""
    return _renderer()(data={"kind": "history", "bids": bids[:30], **({"live_context": live_context} if live_context else {})}, key=key,
                       height="content", width="stretch")


def render_stage(state, key, *, elapsed_seconds=0):
    _renderer()(data=stage_data(state, elapsed_seconds=elapsed_seconds), key=key, height="content", width="stretch")


def render_sound(state, key, *, live_context=None):
    event_id = state.get("event_id", state.get("event", {}).get("id"))
    cancelled = {lot["id"] for lot in state.get("lots") or [] if lot["status"] == "CANCELLED"}
    # Reset archives lots while retaining their bids. An unseen old bid must
    # not ring after reset, even if the browser missed the intermediate READY.
    latest = next((bid for bid in state.get("bids") or [] if bid.get("lot_id") not in cancelled), None)
    bid_id = latest.get("id") if latest else None
    _renderer()(data={"kind": "sound", "event_id": event_id, "bid_id": bid_id, **({"live_context": live_context} if live_context else {})}, key=key, height="content", width=200)


def render_overview(teams, key, *, member_id=None, live_context=None, live_epoch=None):
    teams = teams["teams"] if isinstance(teams, dict) else teams
    cards = [team_data(team, member_id=member_id, index=index) for index, team in enumerate(teams)]
    with st.container(key="live_overview_panel" if len(cards) <= 4 else "live_overview_panel_many"):
        st.caption("팀별 포인트와 포지션 배정 현황을 한눈에 확인하세요.")
        _renderer()(data={"kind": "overview", "teams": cards, **({"live_context": live_context} if live_context else {}),
                          **({"live_epoch": live_epoch} if live_epoch else {})}, key=key, height="content", width="stretch")


def _latest_lots(state):
    latest = {}
    players = state.get("event", {}).get("players")
    participants = {p["member_id"]: p for p in players} if players is not None else None
    for lot in sorted(state.get("lots", []), key=lambda row: (row["sequence"], row["id"])):
        member_id = lot["member_id"]
        if participants is not None and member_id not in participants:
            continue
        latest[member_id] = lot
    return [lot for lot in sorted(latest.values(), key=lambda row: row["sequence"])
            if lot["status"] != "CANCELLED" and not (
                participants is not None and lot["status"] in ("OPEN", "QUEUED", "UNSOLD")
                and participants[lot["member_id"]].get("team_id") is not None)]


def queue_data(state):
    lots = _latest_lots(state)
    active = [lot for lot in lots if lot["status"] in ("OPEN", "QUEUED")]
    attempt = min((lot.get("attempt", 1) for lot in active), default=max((lot.get("attempt", 1) for lot in lots), default=1))
    cohort = lots if state.get("status") == "COMPLETED" else [lot for lot in lots if lot in active or lot.get("attempt", 1) == attempt]
    current = state.get("current_lot") or {}
    current_id = current.get("id") if current.get("status") == "OPEN" else None
    next_lot = next((lot for lot in active if lot["status"] == "QUEUED"), None)
    focus = current_id or (next_lot["id"] if next_lot else None)
    labels = {"QUEUED": "대기", "OPEN": "현재", "SOLD": "낙찰", "UNSOLD": "유찰"}
    items = []
    for index, lot in enumerate(cohort, 1):
        label = labels.get(lot["status"], lot["status"])
        if lot["id"] == focus and lot["status"] == "QUEUED":
            label = "첫 선수" if state.get("status") == "READY" else "다음 준비"
        elif next_lot and lot["id"] == next_lot["id"]:
            label = "다음"
        items.append({"lot_id": lot["id"], "member_id": lot["member_id"], "number": index,
                      "label": label, "active": lot["id"] == focus, "done": lot["status"] == "SOLD", "player": player_data(lot)})
    unsold = sum(lot["status"] == "UNSOLD" for lot in lots)
    return {"kind": "queue", "items": items, "label": ("재경매 · " if attempt > 1 else "") + f"이번 순서 {len(active)}명" + (f" · 유찰 {unsold}명" if unsold else ""),
            "empty": "경매가 종료되었습니다." if state.get("status") in ("COMPLETED", "CANCELLED") else "경매 대상을 준비하고 있습니다."}


def render_queue(state, key, *, live_context=None):
    _renderer()(data={**queue_data(state), **({"live_context": live_context} if live_context else {})}, key=key, height="content", width="stretch")


def remaining_data(state):
    lots = [lot for lot in _latest_lots(state) if lot["status"] in ("QUEUED", "UNSOLD")]
    return {"kind": "remaining", "groups": [
        {"label": label, "players": [player_data(lot) for lot in lots if lot["status"] == status]}
        for status, label in (("QUEUED", "입찰 대기"), ("UNSOLD", "유찰 · 재경매 대기"))],
        "counts": [{"role": label, "count": sum(lot["role"] == role for lot in lots)} for role, label in ROLES.items()]}


def render_remaining(state, key, *, live_context=None):
    _renderer()(data={**remaining_data(state), **({"live_context": live_context} if live_context else {})}, key=key, height="content", width="stretch")


def live_companion_data(state, actor):
    """Public presentation for HTTP-driven cards; never include auth data."""
    teams = state.get("teams", [])
    member_id = actor.get("member_id") if actor else None
    cards = {str(team["id"]): team_data(team, member_id=member_id, index=index)
             for index, team in enumerate(teams)}
    return {"event_status": state.get("event", {}).get("status"),
            "queue": queue_data(state), "remaining": remaining_data(state), "teams": cards,
            "overview": {"kind": "overview", "teams": list(cards.values())},
            **bid_history_data(state)}


def bid_history_data(state):
    """Format only the bounded hot history; never rebuild roster/profile cards."""
    teams = state.get("teams", [])
    cancelled = {lot["id"] for lot in state.get("lots", []) if lot["status"] == "CANCELLED"}
    bids = [bid for bid in state.get("bids", []) if bid["lot_id"] not in cancelled][:30]
    names = {team["id"]: team["name"] for team in teams}
    def stamp(value):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(ZoneInfo("Asia/Seoul")).strftime("%H:%M:%S")
    return {"sound": {"kind": "sound", "event_id": state.get("event_id"), "bid_id": bids[0]["id"] if bids else None},
            "history": {"kind": "history", "bids": [
                {"id": bid["id"], "amount": f"{bid['amount']:,} P", "time": stamp(bid["created_at"]),
                 "team": bid.get("team_name") or names.get(bid["team_id"], ""), "captain": bid.get("riot_id", "")}
                for bid in bids]}}
