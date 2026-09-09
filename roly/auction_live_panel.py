"""Local bid drafts and a single acknowledged Streamlit refresh channel.

The browser only submits immutable intentions. Authorization, deadlines, prices,
receipts and settlement remain in the existing server services.
"""
from hashlib import sha256
from uuid import uuid4

import streamlit as st

from .auction_components import CSS as STAGE_CSS, JS as STAGE_JS, stage_data


HTML = '<section class="live-panel"><div class="live-stage"><section class="auction-component"></section></div><div class="live-bid-controls"></div></section>'
CSS = STAGE_CSS + """
.live-panel { min-width:0; }
.live-bid-controls { margin-top:12px; }
.bid-metrics { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.bid-metric { min-width:0; background:#f5f7fa; border:1px solid #dce1e8; border-radius:11px; padding:9px 12px; }
.bid-label { display:block; color:#536176; font-size:12px; margin-bottom:6px; }
.bid-points { display:block; text-align:right; font-size:23px; line-height:1.3; font-weight:800; }
.bid-amount { width:100%; min-width:0; border:0; background:transparent; color:#142238; font-family:inherit; font-size:23px; font-weight:800; text-align:right; outline-offset:2px; }
.bid-amount:focus-visible { outline:2px solid #2269c7; border-radius:3px; }
.bid-increments { display:grid; grid-template-columns:repeat(8,minmax(0,1fr)); gap:5px; margin:8px 0; }
.bid-add { min-width:0; min-height:34px; padding:4px 2px; border:1px solid #ced6e3; border-radius:8px; background:#fff; color:#142238; font-family:inherit; font-size:12px; font-weight:700; cursor:pointer; }
.bid-add:focus-visible,.bid-submit:focus-visible { outline:3px solid #a9c5ff; outline-offset:2px; }
.bid-submit { width:100%; min-height:44px; padding:9px 12px; border:0; border-radius:10px; background:#2359e8; color:#fff; font-family:inherit; font-size:16px; font-weight:750; cursor:pointer; }
.bid-submit:disabled { background:#d9dde5; color:#657186; cursor:not-allowed; }
.bid-add:disabled,.bid-amount:disabled { color:#778397; cursor:not-allowed; }
.bid-feedback { min-height:32px; margin-top:8px; padding:7px 10px; border:1px solid #e0e5ed; border-radius:8px; background:#f7f9fc; color:#46566d; font-size:12px; line-height:1.4; }
.bid-feedback.error { color:#9f2424; background:#fff5f5; border-color:#edcaca; }
.bid-feedback.success { color:#176038; background:#f1fbf5; border-color:#cce8d6; }
.live-unavailable { padding:18px; border:1px solid #dce1e8; border-radius:12px; background:#fff; color:#536176; }
@container(max-width:420px) { .bid-increments{grid-template-columns:repeat(4,minmax(0,1fr));} .bid-points,.bid-amount{font-size:21px;} }
"""

# Named export makes the actual delivery/clock state machine executable in Node
# regression tests, without replacing Streamlit or contacting a real database.
CHANNEL_JS = r"""
const finite = value => typeof value === 'number' && Number.isFinite(value);
const integer = value => Number.isSafeInteger(value) && value >= 0;
const validCommand = value => value && typeof value.request_id === 'string' &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value.request_id) &&
  integer(value.lot_id) && integer(value.amount);

export function createLiveChannel({context, now, uuid, emit, readPending, writePending}) {
  let restored = null;
  try { restored = readPending(); } catch (_) {}
  const channel = {
    context, epoch:uuid(), sequence:0, outstanding:null, pending:validCommand(restored) ? {...restored} : null,
    sent:new Map(), lastReplySeq:0, lastContact:now(), nextAt:now(), timeouts:0,
    bestClock:null, message:'', messageStatus:'', messageLot:null, lastFrame:null,resolved:new Set(),metrics:{},
    commandTiming:null,lastCommandTiming:null,commandRoundTripMs:null,
    save() { try { writePending(this.pending); } catch (_) {} },
    send(force=false) {
      if(this.outstanding && !force) return null;
      const request={context:this.context,epoch:this.epoch,seq:++this.sequence,sent_ms:now(),
        command:this.pending ? {...this.pending} : null};
      this.outstanding=request;
      if(request.command && this.commandTiming?.request_id===request.command.request_id) this.commandTiming.attempts++;
      this.sent.set(request.seq,request);
      while(this.sent.size>32) this.sent.delete(this.sent.keys().next().value);
      emit(request);
      return request;
    },
    submit(lot_id,amount) {
      if(this.pending || !integer(lot_id) || !integer(amount)) return null;
      this.pending={request_id:uuid(),lot_id,amount};
      this.commandTiming={request_id:this.pending.request_id,lot_id,started:now(),attempts:0};
      this.timeouts=0;
      this.message='';this.messageStatus='';this.save();
      // A user command takes priority over an outstanding read. It remains in
      // every following envelope until its own durable receipt is acknowledged.
      return this.send(true);
    },
    receive(transport,serverNow) {
      if(!transport || transport.context!==this.context) return {fresh:false,matched:false};
      const echoed=transport.request;
      const staleReply=Boolean(echoed?.epoch===this.epoch && integer(echoed.seq) && echoed.seq<this.lastReplySeq) ||
        (typeof transport.frame_id==='number' && typeof this.lastFrame==='number' && transport.frame_id<this.lastFrame);
      const ack=transport.ack;
      if(!this.pending && !staleReply && ack?.status==='pending' && !this.resolved.has(ack.request_id) && validCommand(ack)) {
        this.pending={request_id:ack.request_id,lot_id:ack.lot_id,amount:ack.amount};this.save();
      }
      if(this.pending && ack && ack.request_id===this.pending.request_id &&
         ack.lot_id===this.pending.lot_id && ack.amount===this.pending.amount) {
        if(ack.status==='accepted' || ack.status==='rejected') {
          if(this.commandTiming?.request_id===ack.request_id) {
            this.lastCommandTiming={...this.commandTiming,status:ack.status,elapsed_ms:Math.max(0,now()-this.commandTiming.started)};
            // Learn only the original command's elapsed time. A retry's short
            // round trip must not lower the budget for a new server write.
            if(this.commandTiming.attempts===1) this.commandRoundTripMs=this.lastCommandTiming.elapsed_ms;
            this.commandTiming=null;
          }
          this.message=String(ack.message || (ack.status==='accepted' ? '입찰을 접수했습니다.' : '입찰이 접수되지 않았습니다.')).slice(0,500);
          this.messageStatus=ack.status==='accepted' ? 'success' : 'error';this.messageLot=ack.lot_id;
          this.resolved.add(this.pending.request_id);
          while(this.resolved.size>32)this.resolved.delete(this.resolved.values().next().value);
          this.pending=null;this.save();
        } else if(ack.status==='pending') {
          this.message=String(ack.message || '입찰 접수를 확인하고 있습니다.').slice(0,500);
          this.messageStatus='';
        }
      }
      const request=transport.request;
      const original=request && request.epoch===this.epoch ? this.sent.get(request.seq) : null;
      const matched=Boolean(original && original.sent_ms===request.sent_ms);
      let fresh=true;
      if(matched) {
        fresh=!staleReply && request.seq>=this.lastReplySeq;
        if(fresh) {
          const received=now();
          const rtt=Math.max(0,received-original.sent_ms);
          const processing=finite(transport.server_elapsed_ms) ? Math.max(0,transport.server_elapsed_ms) : 0;
          const rendering=finite(transport.snapshot_elapsed_ms) ? Math.max(0,transport.snapshot_elapsed_ms) : 0;
          const residual=Math.max(0,rtt-processing-rendering);
          this.metrics={round_trip_ms:rtt,server_elapsed_ms:processing,snapshot_elapsed_ms:rendering,residual_ms:residual,
            callback_elapsed_ms:finite(transport.callback_elapsed_ms) ? Math.max(0,transport.callback_elapsed_ms) : null,
            view_elapsed_ms:finite(transport.view_elapsed_ms) ? Math.max(0,transport.view_elapsed_ms) : null};
          if(finite(serverNow) && rtt<=30000 && processing+rendering<=rtt+100) {
            const sample={at:received,residual,offset:serverNow*1000+rendering+residual/2-received};
            if(!this.bestClock || received-this.bestClock.at>60000 || residual<=this.bestClock.residual) this.bestClock=sample;
          }
          this.lastReplySeq=request.seq;
          this.lastContact=received;
          if(this.outstanding?.seq===request.seq) {
            this.outstanding=null;this.timeouts=0;
            // A slow read already paid the polling interval. Keep one request
            // in flight, without adding another fixed delay after every reply.
            this.nextAt=this.pending ? received+1200 : Math.max(received,original.sent_ms+500);
          }
        }
        this.sent.delete(request.seq);
      } else if(request?.epoch===this.epoch && integer(request.seq) && request.seq<this.lastReplySeq) {
        fresh=false;
      }
      if(typeof transport.frame_id==='number' && typeof this.lastFrame==='number' && transport.frame_id<this.lastFrame) fresh=false;
      if(fresh && transport.frame_id!==undefined) this.lastFrame=transport.frame_id;
      return {fresh,matched};
    },
    estimatedServerNow() { return this.bestClock ? (now()+this.bestClock.offset)/1000 : null; },
    stale() { return !this.bestClock || now()-this.lastContact>5000; },
    pump(visible=true) {
      if(!visible) return null;
      if(this.outstanding) {
        const initial=this.outstanding.command ? Math.min(5000,Math.max(3000,(this.commandRoundTripMs || 0)*1.5+500)) : 2000;
        const limit=Math.min(10000,initial*(2**Math.min(this.timeouts,3)));
        if(now()-this.outstanding.sent_ms>=limit) { this.timeouts++;return this.send(true); }
      } else if(now()>=this.nextAt) return this.send();
      return null;
    }
  };
  channel.save();
  return channel;
}
"""

PANEL_JS = r"""
export default function({parentElement,data,setTriggerValue}) {
  const root=parentElement.querySelector('.live-panel');
  if(!root) return;
  const doc=root.ownerDocument;
  const view=doc.defaultView || globalThis;
  const now=()=>view.performance.now();
  const context=String(data?.transport?.context || '');
  if(!context) return;
  const old=parentElement._rolyLivePanel;
  old?.dispose?.();
  const memory=old && old.context===context ? old : {context,draft:0,draftLot:null,edited:false,stage:null,control:null};
  parentElement._rolyLivePanel=memory;
  const uuid=()=>view.crypto.randomUUID();
  memory.emit=setTriggerValue;
  // The bridge is replaced each render, while the channel and UUID survive.
  if(!memory.channel) {
    const storageKey='rolymoly.pending-bid.'+context;
    memory.channel= createLiveChannel({context,now,uuid,emit:request=>memory.emit('event',request),
      readPending:()=>JSON.parse(view.sessionStorage.getItem(storageKey) || 'null'),
      writePending:pending=>pending ? view.sessionStorage.setItem(storageKey,JSON.stringify(pending)) : view.sessionStorage.removeItem(storageKey)});
  }
  const live=memory.channel;
  const incoming=live.receive(data.transport,data.server_now);
  if(incoming.fresh) {
    memory.stage=data.stage || null;
    memory.control=data.control || null;
    memory.status=data.status || null;
    memory.lot=data.lot || null;
    memory.stateAvailable=Boolean(data.available);
  }
  const make=(tag,cls,text)=>{const node=doc.createElement(tag);node.className=cls;if(text!==undefined)node.textContent=String(text);return node;};
  const stageMount=root.querySelector('.live-stage');
  const stage=memory.stage;
  if(stage) {
    let seconds=stage.seconds;
    const calibrated=live.estimatedServerNow();
    if(stage.ticking && calibrated!==null && finite(stage.deadline)) seconds=Math.max(0,stage.deadline-calibrated);
    else if(stage.ticking) seconds=Math.max(0,Number(seconds || 0)-Math.max(0,Number(data.transport.snapshot_elapsed_ms || 0))/1000);
    memory.stageDispose=renderAuctionStage({parentElement:stageMount,data:{...stage,seconds}});
  } else {
    memory.stageDispose?.();memory.stageDispose=null;
    const target=stageMount.querySelector('.auction-component');
    target.replaceChildren(make('div','live-unavailable',data.available ? '다음 선수를 준비하고 있습니다.' : '경매 연결을 확인하고 있습니다.'));
  }
  // Local monotonic stamps record when fresh server data actually reaches
  // this DOM. They contain no account/session secrets and do not send events.
  if(incoming.fresh && memory.stateAvailable) {
    for(const [name,value] of [['Bid',stage?.bid_id],['Lot',memory.lot?.id]]) {
      const key='display'+name+'Id', identity=value==null ? '' : String(value);
      if(memory[key]!==identity) {memory[key]=identity;memory[key+'SeenAt']=now();}
      root.dataset[key]=identity;
      root.dataset['display'+name+'SeenAtMs']=String(memory[key+'SeenAt']);
    }
  }
  const controls=root.querySelector('.live-bid-controls');
  if(!memory.nodes || memory.nodes.root!==controls) {
    controls.replaceChildren();
    const metrics=make('div','bid-metrics');
    const balance=make('div','bid-metric');const balanceLabel=make('span','bid-label');const points=make('strong','bid-points');balance.append(balanceLabel,points);
    const amount=make('div','bid-metric');const label=make('label','bid-label','입찰할 포인트');
    const input=make('input','bid-amount');input.type='number';input.min='0';input.step='5';input.inputMode='numeric';input.setAttribute('aria-label','입찰할 포인트');
    const inputId='bid-amount-'+live.epoch;input.id=inputId;label.htmlFor=inputId;amount.append(label,input);metrics.append(balance,amount);
    const increments=make('div','bid-increments');const buttons=[];
    for(const value of [5,10,20,30,50,70,100]) {const button=make('button','bid-add','+'+value);button.type='button';button.dataset.increment=String(value);increments.append(button);buttons.push(button);}
    const reset=make('button','bid-add','초기화');reset.type='button';reset.setAttribute('aria-label','금액 초기화');increments.append(reset);
    const submit=make('button','bid-submit','입찰하기');submit.type='button';
    const feedback=make('div','bid-feedback');feedback.setAttribute('role','status');feedback.setAttribute('aria-live','polite');
    controls.append(metrics,increments,submit,feedback);
    memory.nodes={root:controls,metrics,balanceLabel,points,input,increments,buttons,reset,submit,feedback};
  }
  const nodes=memory.nodes;
  const lot=memory.lot;
  if(lot && lot.id!==memory.draftLot) {
    memory.draftLot=lot.id;memory.draft=integer(lot.highest_bid) ? lot.highest_bid : 0;memory.edited=false;
    nodes.input.value=String(memory.draft);live.message='';live.messageStatus='';
  }
  function remainingSeconds() {
    const shown=memory.stage;
    if(!shown?.ticking || !finite(shown.deadline)) return 0;
    const server=live.estimatedServerNow();
    if(server===null) return 0;
    let remaining=Math.max(0,shown.deadline-server);
    // The stage clamps an unchanged deadline so delayed snapshots cannot add
    // time back. Submission must use that exact visible clock as well.
    const clock=stageMount._rolyAuctionClock?.clock;
    const key=JSON.stringify([shown.event_id,shown.lot_id,shown.deadline,shown.phase]);
    if(clock?.key===key && clock.ticking) {
      remaining=Math.min(remaining,Math.max(0,clock.seconds-Math.max(0,now()-clock.received)/1000));
    }
    return remaining;
  }
  function allowEdit() {
    return Boolean(memory.stateAvailable && memory.control?.can_bid && memory.control.context===context &&
      memory.lot?.status==='OPEN' && memory.status==='RUNNING' && !live.pending && !live.stale() && remainingSeconds()>0);
  }
  function validAmount() {
    return integer(memory.draft) && memory.draft<=Number(memory.control?.remaining || 0) &&
      (memory.lot?.highest_bid===null || memory.lot?.highest_bid===undefined || memory.draft>memory.lot.highest_bid);
  }
  function refresh() {
    const control=memory.control;
    const editing=allowEdit();
    nodes.metrics.hidden=!control;nodes.increments.hidden=!control;nodes.submit.hidden=!control;
    nodes.balanceLabel.textContent='내 포인트'+(control?.team_name ? ' · '+control.team_name : '');
    nodes.points.textContent=Number(control?.remaining || 0).toLocaleString('ko-KR')+' P';
    nodes.input.disabled=!editing;
    for(const button of nodes.buttons) button.disabled=!editing;
    nodes.reset.disabled=!editing;
    nodes.submit.disabled=!editing || !validAmount();
    nodes.submit.textContent=live.pending ? '입찰 확인 중…' : (integer(memory.draft) ? memory.draft.toLocaleString('ko-KR')+' P 입찰하기' : '입찰하기');
    let message=live.messageLot===memory.lot?.id ? live.message : '', kind=message ? live.messageStatus : '';
    if(live.pending) {message='입찰 접수를 확인하고 있습니다.';kind='';}
    else if(!memory.stateAvailable || live.stale()) {message='연결을 확인하고 있습니다.';kind='';}
    // A definitive response explains this attempt even if the deadline just
    // passed or another captain has already raised the current highest bid.
    else if(message && kind) {}
    else if(memory.status==='PAUSED') {message='일시 정지 중입니다.';kind='';}
    else if(!control) {message='이 경매의 팀장만 입찰할 수 있습니다.';kind='';}
    else if(!editing) {message='다음 선수 입찰을 기다려 주세요.';kind='';}
    else if(!validAmount()) {message=memory.draft>control.remaining ? '사용 가능한 포인트를 확인해 주세요.' : '현재 최고 입찰가보다 높은 포인트를 입력해 주세요.';kind='';}
    nodes.feedback.textContent=message || '포인트를 선택한 뒤 입찰해 주세요.';
    nodes.feedback.className='bid-feedback'+(kind ? ' '+kind : '');
    root.dataset.pendingRequest=live.pending?.request_id || '';
    root.dataset.pollSequence=String(live.sequence);
    root.dataset.clockCalibrated=String(Boolean(live.bestClock));
    root.dataset.clockStale=String(live.stale());
    root.dataset.frameAgeMs=String(Math.max(0,now()-live.lastContact));
    root.dataset.roundTripMs=String(live.metrics.round_trip_ms ?? '');
    root.dataset.serverElapsedMs=String(live.metrics.server_elapsed_ms ?? '');
    root.dataset.snapshotElapsedMs=String(live.metrics.snapshot_elapsed_ms ?? '');
    root.dataset.callbackElapsedMs=String(live.metrics.callback_elapsed_ms ?? '');
    root.dataset.viewElapsedMs=String(live.metrics.view_elapsed_ms ?? '');
    root.dataset.serverNow=String(data.server_now ?? '');
    root.dataset.clientDraftChangeMs=String(memory.draftChangeMs ?? '');
    root.dataset.pollPending=String(Boolean(live.outstanding));
    root.dataset.lastBidRequestId=live.lastCommandTiming?.request_id || '';
    root.dataset.lastBidElapsedMs=String(live.lastCommandTiming?.elapsed_ms ?? '');
    root.dataset.lastBidStatus=live.lastCommandTiming?.status || '';
    root.dataset.lastBidAttempts=String(live.lastCommandTiming?.attempts ?? '');
    root.dataset.clientTimeOriginMs=String(view.performance.timeOrigin ?? '');
  }
  const recordDraftChange=started=>{memory.draftChangeMs=now()-started;root.dataset.clientDraftChangeMs=String(memory.draftChangeMs);};
  const setDraft=value=>{const started=now();memory.draft=value;memory.edited=true;nodes.input.value=String(value);live.message='';live.messageStatus='';refresh();recordDraftChange(started);};
  nodes.input.oninput=()=>{
    if(!allowEdit()) return;
    const started=now();const value=nodes.input.value.trim();memory.draft=value!=='' && /^\d+$/.test(value) ? Number(value) : NaN;memory.edited=true;live.message='';refresh();recordDraftChange(started);
  };
  for(const button of nodes.buttons) button.onclick=()=>{
    if(!allowEdit()) return;
    const base=Math.max(integer(memory.draft) ? memory.draft : 0,Number(memory.lot?.highest_bid || 0));
    const value=base+Number(button.dataset.increment);if(integer(value))setDraft(value);
  };
  nodes.reset.onclick=()=>{if(allowEdit())setDraft(Number(memory.lot?.highest_bid || 0));};
  nodes.submit.onclick=()=>{if(allowEdit() && validAmount()){live.submit(memory.lot.id,memory.draft);refresh();}};
  nodes.input.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();nodes.submit.onclick();}};
  const tick=()=>{if(parentElement.isConnected){live.pump(doc.visibilityState!=='hidden');refresh();}};
  const onReturn=()=>{if(doc.visibilityState!=='hidden'){live.nextAt=now();tick();}};
  doc.addEventListener('visibilitychange',onReturn);view.addEventListener('focus',onReturn);view.addEventListener('pageshow',onReturn);
  tick();const interval=view.setInterval(tick,100);
  const stageDispose=memory.stageDispose;
  let disposed=false;
  const dispose=()=>{
    // Both our next render and Streamlit may dispose the previous render. A
    // repeated call must never clear the new render's handlers or clock.
    if(disposed) return;
    disposed=true;
    view.clearInterval(interval);doc.removeEventListener('visibilitychange',onReturn);view.removeEventListener('focus',onReturn);view.removeEventListener('pageshow',onReturn);
    stageDispose?.();if(memory.stageDispose===stageDispose)memory.stageDispose=null;
    nodes.input.oninput=null;nodes.input.onkeydown=null;nodes.submit.onclick=null;nodes.reset.onclick=null;
    for(const button of nodes.buttons)button.onclick=null;
    if(memory.dispose===dispose)memory.dispose=null;
  };
  memory.dispose=dispose;
  return dispose;
}
"""

JS = STAGE_JS.replace("export default function(", "function renderAuctionStage(", 1) + CHANNEL_JS + PANEL_JS
COMPONENT_REVISION = sha256((HTML + "\0" + CSS + "\0" + JS).encode()).hexdigest()[:16]


def live_panel_data(state, *, control=None, transport):
    """Send presentation data only, never account tokens or database settings."""
    lot = state.get("current_lot") if state else None
    stage = stage_data(state) if lot else None
    return {
        **(stage or {}),
        "kind": "stage",
        "available": state is not None,
        "status": state.get("status") if state else None,
        "server_now": state.get("server_now") if state else None,
        "stage": stage,
        "lot": {"id": lot["id"], "status": lot["status"], "highest_bid": lot.get("highest_bid")} if lot else None,
        "control": {name: control.get(name) for name in ("team_name", "remaining", "can_bid", "context")} if control else None,
        "transport": {name: transport.get(name) for name in (
            "context", "frame_id", "request", "server_elapsed_ms", "snapshot_elapsed_ms",
            "callback_elapsed_ms", "view_elapsed_ms", "ack")},
    }


@st.cache_resource(scope="session", show_spinner=False)
def _register(scope, revision):
    return st.components.v2.component("auction_live_panel_" + revision, html=HTML, css=CSS, js=JS, isolate_styles=True)


def render_live_panel(state, *, key, control=None, transport, on_event_change):
    st.session_state.setdefault("_auction_live_panel_scope", uuid4().hex)
    return _register(st.session_state["_auction_live_panel_scope"], COMPONENT_REVISION)(
        data=live_panel_data(state, control=control, transport=transport), key=key,
        height="content", width="stretch", on_event_change=on_event_change,
    )
