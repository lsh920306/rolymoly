"""Browser delivery for the same-origin auction API (no Streamlit rerun)."""

HTTP_JS = r"""
export function auctionApiUrl(path,{location=globalThis.location,backendBase=globalThis.__streamlit?.BACKEND_BASE_URL}={}) {
  if(!/^\/api\/auction\/(live|bid)$/.test(path))throw new Error('Invalid auction endpoint');
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
