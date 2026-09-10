"""Browser delivery for the same-origin auction API (no Streamlit rerun)."""

HTTP_JS = r"""
export function auctionApiUrl(path,{location=globalThis.location,backendBase=globalThis.__streamlit?.BACKEND_BASE_URL}={}) {
  if(!/^\/api\/auction\/(live|bid|ws)$/.test(path))throw new Error('Invalid auction endpoint');
  if(!location)return path;
  const page=new URL(location.href);
  const injected=typeof backendBase==='string' && backendBase.length>0;
  const base=new URL(injected?backendBase:page.origin,page.origin);
  if(base.origin!==page.origin || base.username || base.password || base.search || base.hash)
    throw new Error('Auction endpoint must share the page origin');
  if(base.protocol!=='https:' && !(base.protocol==='http:' && ['localhost','127.0.0.1','[::1]'].includes(base.hostname)))
    throw new Error('Secure auction endpoint required');
  // Community Cloud serves the app below /~/+/. Its page gateway redirects
  // root-relative requests; bid requests must never follow that redirect.
  const prefix=injected?base.pathname.replace(/\/$/,''):(page.pathname.startsWith('/~/+/')?'/~/+':'');
  return new URL(prefix+path,page.origin).href;
}

export function auctionXsrfToken(cookieString) {
  if(cookieString===undefined) {
    try {cookieString=globalThis.document?.cookie;} catch (_) {return null;}
  }
  if(typeof cookieString!=='string')return null;
  const matches=cookieString.split(';').map(part=>part.trimStart())
    .filter(part=>part.startsWith('_streamlit_xsrf='));
  if(matches.length!==1)return null;
  let value=matches[0].slice('_streamlit_xsrf='.length);
  // Match Streamlit's POST convention: send the cookie's encoded value,
  // without percent-decoding or inventing a token when none is available.
  if(value.startsWith('"') && value.endsWith('"'))value=value.slice(1,-1);
  if(!/^[\x21-\x7e]{1,1024}$/.test(value) || /["\\;,]/.test(value))return null;
  return value;
}

export function createHttpDelivery({config,fetcher,readToken,onFrame,onAck,onError}) {
  const attempted=new Set();
  let stopped=false,latestRead=0;
  const terminalError=(message,reload=false)=>{if(reload)stopped=true;onError(message,reload);};
  return {
    stopped:()=>stopped,
    dispose:()=>{stopped=true;},
    async send(request,{restored=false}={}) {
      if(stopped)return;
      const isBid=Boolean(request.command),id=request.command?.request_id;
      const confirmOnly=isBid && (restored || attempted.has(id));
      if(isBid && !attempted.has(id) && attempted.size>=8192) {
        terminalError('입찰 확인 기록이 가득 찼습니다. 화면을 새로고침해 주세요.',true);return;
      }
      if(isBid)attempted.add(id);else latestRead=request.seq;
      let token;
      try { token=readToken(); } catch (_) {}
      if(typeof token!=='string' || token.length<20 || token.length>256) {
        terminalError('로그인 상태를 확인해 주세요. 화면을 새로고침해 주세요.',true);return;
      }
      let url;
      try { url=auctionApiUrl(isBid?config.bid_url:config.live_url); }
      catch (_) {terminalError('경매 연결 주소를 확인하지 못했습니다. 화면을 새로고침해 주세요.',true);return;}
      const xsrf=auctionXsrfToken();
      try {
        const response=await fetcher(url,{
          method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',
          ...(typeof globalThis.AbortSignal?.timeout==='function'?{signal:globalThis.AbortSignal.timeout(10000)}:{}),
          headers:{'Content-Type':'application/json','X-Rolymoly-Session':token,
                   'X-Rolymoly-Server-Epoch':config.epoch,...(xsrf?{'X-Xsrftoken':xsrf}:{})},
          body:JSON.stringify({...request,event_id:config.event_id,session_token:token,server_epoch:config.epoch,
                               ...(isBid?{confirm_only:confirmOnly}:{})})
        });
        const result=await response.json();
        if(stopped)return;
        if(!response.ok) {
          const error=result?.error;
          terminalError(typeof error?.message==='string'?error.message:'경매 연결을 확인하고 있습니다.',
                        Boolean(error?.reload_required));return;
        }
        if(result?.context!==request.context || result?.epoch!==config.epoch) {
          terminalError('경매 연결이 변경되었습니다. 화면을 새로고침해 주세요.',true);return;
        }
        const echo=isBid?result.request:result.panel?.transport?.request;
        const sameRequest=echo && ['context','epoch','seq','sent_ms'].every(key=>echo[key]===request[key]);
        const samePayload=isBid ? result.ack && ['request_id','lot_id','amount'].every(key=>result.ack[key]===request.command[key]) :
          result.panel?.transport?.context===request.context &&
          (result.panel.stage?.event_id==null || result.panel.stage.event_id===config.event_id);
        if(!sameRequest || !samePayload) {
          terminalError('입찰 응답을 확인하지 못했습니다. 화면을 새로고침해 주세요.',true);return;
        }
        if(isBid) onAck(result);
        else if(request.seq===latestRead) onFrame(result.panel);
      } catch (_) {
        // A network error is not a rejection. Keep the immutable UUID and
        // resolve it on the next attempt; never resubmit it as a new write.
        if(!stopped)terminalError('연결을 확인하고 있습니다. 입찰 접수 여부를 다시 확인합니다.');
      }
    }
  };
}
"""

PUSH_JS = r"""
export function auctionSocketUrl(path,options) {
  if(path!=='/api/auction/ws')throw new Error('Invalid auction socket endpoint');
  const url=new URL(auctionApiUrl(path,options));
  url.protocol=url.protocol==='https:'?'wss:':'ws:';
  return url.href;
}

// Reads use a single socket once its authenticated snapshot arrives. The
// independent HTTP writer owns UUID attempts and confirmation-only retries.
export function createPushDelivery({config,context,fetcher,readToken,makeRequest,onFrame,onAck,onError,onClock,
  now=()=>performance.now(),socketFactory=url=>new WebSocket(url),onMode=()=>{}}) {
  let socket=null,dead=false,terminal=false,ready=false,connectingAt=0,lastContact=0;
  let retryAt=0,failures=0,generation=0,lastPing=0,pings=new Set(),revision=-1,detailRevision=-1,views=null;
  const reads=new Map();
  const validRevision=value=>Number.isSafeInteger(value) && value>=0;
  const mergeViews=panel=>{
    if(Object.hasOwn(panel,'views'))views=panel.views===null?null:{...(views || {}),...panel.views};
    return {...panel,...(views!==null?{views}:{})};
  };
  const closeSocket=()=>{
    const old=socket;socket=null;ready=false;pings.clear();
    if(old){old.onopen=old.onmessage=old.onclose=old.onerror=null;try{old.close();}catch(_){}}
  };
  const fail=(message,reload=false)=>{
    if(dead || terminal)return;
    if(reload)terminal=true;
    closeSocket();retryAt=now()+Math.min(30000,1000*2**Math.min(failures++,5));
    onMode('http');onError(message,reload);
  };
  const http=createHttpDelivery({config,fetcher,readToken,onAck,
    onError:(message,reload)=>{if(reload)fail(message,true);else if(!ready)onError(message,false);},
    onFrame:panel=>{
      const seq=panel.transport?.request?.seq,started=reads.get(seq);reads.delete(seq);
      if(dead || terminal || ready || started!==generation)return;
      const next=panel.transport?.revision;
      if(validRevision(next) && next<revision)return;
      if(validRevision(next))revision=next;
      const detail=panel.transport?.detail_revision;
      if(validRevision(detail))detailRevision=detail;
      onFrame(mergeViews(panel),{push:false});
    }});
  function connect() {
    if(dead || terminal || socket || now()<retryAt)return;
    let url;
    try{url=auctionSocketUrl(config.ws_url);}catch(_){fail('경매 연결 주소를 확인하지 못했습니다. 화면을 새로고침해 주세요.',true);return;}
    let opened,subscriptionRequest=null;
    try{opened=socketFactory(url);}catch(_){fail('실시간 연결을 다시 확인하고 있습니다.');return;}
    socket=opened;connectingAt=now();onMode('connecting');
    opened.onopen=()=>{
      if(socket!==opened || dead || terminal)return;
      let token;try{token=readToken();}catch(_){}
      if(typeof token!=='string' || token.length<20 || token.length>256){fail('로그인 상태를 확인해 주세요. 화면을 새로고침해 주세요.',true);return;}
      const request=subscriptionRequest=makeRequest();
      try{opened.send(JSON.stringify({...request,command:null,type:'subscribe',event_id:config.event_id,
        session_token:token,server_epoch:config.epoch}));}catch(_){fail('실시간 연결을 다시 확인하고 있습니다.');}
    };
    opened.onmessage=event=>{
      if(socket!==opened || dead || terminal)return;
      let frame;try{frame=JSON.parse(event.data);}catch(_){fail('경매 응답을 확인하지 못했습니다.',true);return;}
      if(frame?.type==='error'){
        fail(typeof frame.error?.message==='string'?frame.error.message:'경매 연결을 확인하고 있습니다.',Boolean(frame.error?.reload_required));return;
      }
      if(frame?.epoch!==config.epoch || frame?.context!==context){fail('경매 연결이 변경되었습니다. 화면을 새로고침해 주세요.',true);return;}
      if(frame.type==='pong') {
        if(!ready || !pings.has(frame.sent_ms) || !Number.isFinite(frame.server_now))return;
        pings.delete(frame.sent_ms);lastContact=now();onClock(frame);return;
      }
      const snapshot=frame.type==='snapshot';
      if(!snapshot && frame.type!=='state')return;
      if(!validRevision(frame.revision) || !validRevision(frame.detail_revision) ||
        frame.panel?.transport?.context!==context ||
        !['available','status','stage','lot','control','server_now'].every(key=>Object.hasOwn(frame.panel,key)) ||
        (frame.panel.stage?.event_id!=null && frame.panel.stage.event_id!==config.event_id) ||
        (snapshot && (!subscriptionRequest || !frame.panel.transport.request ||
          !['context','epoch','seq','sent_ms'].every(key=>frame.panel.transport.request[key]===subscriptionRequest[key])))){
        fail('경매 상태를 확인하지 못했습니다. 화면을 새로고침해 주세요.',true);return;
      }
      // Subscribe sequence numbers belong to this browser, never the shared
      // state ordering. Each hot panel is complete, so revision gaps are safe.
      if(frame.revision<revision || (!snapshot && (!ready || frame.revision===revision)))return;
      if(snapshot){generation++;reads.clear();ready=true;failures=0;onMode('websocket');}
      revision=frame.revision;
      const panel={...frame.panel,transport:{...frame.panel.transport,revision,detail_revision:frame.detail_revision}};
      if(frame.detail_revision<detailRevision)delete panel.views;
      else detailRevision=frame.detail_revision;
      lastContact=now();
      onFrame(mergeViews(panel),{push:true,snapshot,revision,detailRevision});
    };
    opened.onclose=()=>{if(socket===opened)fail('실시간 연결을 다시 확인하고 있습니다.');};
    opened.onerror=()=>{if(socket===opened)fail('실시간 연결을 다시 확인하고 있습니다.');};
  }
  return {
    stopped:()=>dead || terminal || http.stopped(),
    streaming:()=>ready && !dead && !terminal,
    start:connect,
    tick() {
      if(dead || terminal)return;
      if(socket && now()-(ready?lastContact:connectingAt)>5000){fail('실시간 연결을 다시 확인하고 있습니다.');return;}
      if(!socket){connect();return;}
      if(ready && now()-lastPing>=2000){
        lastPing=now();pings.add(lastPing);
        while(pings.size>8)pings.delete(pings.values().next().value);
        try{socket.send(JSON.stringify({type:'ping',sent_ms:lastPing}));}catch(_){fail('실시간 연결을 다시 확인하고 있습니다.');}
      }
    },
    send(request,options) {
      if(dead || terminal)return;
      if(!request.command){
        if(ready)return;
        reads.set(request.seq,generation);while(reads.size>32)reads.delete(reads.keys().next().value);
      }
      return http.send(request,options);
    },
    dispose(){if(dead)return;dead=true;closeSocket();reads.clear();http.dispose();},
  };
}
"""
