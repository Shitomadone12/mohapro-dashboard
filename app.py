<div class="card block">
      <h2 class="sec-h">Ganacsiyada <span class="rt" id="trades-count">0</span></h2>
      <div class="tbl-wrap">
        <table class="tbl">
          <thead><tr><th></th><th>Lammaane</th><th>Nooc</th><th>Xeelad</th><th>P&amp;L</th><th>Xaalad</th></tr></thead>
          <tbody id="tradesBody"><tr><td colspan="6" class="ot-empty">Trade ma jiro</td></tr></tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- ===== SYMBOLS ===== -->
  <section class="pane" data-p="symbols">
    <div class="card block">
      <h2 class="sec-h">Symbols <span class="rt" id="sym-count">0 firfircoon</span></h2>
      <div id="symList"><div class="sym-empty">Symbol lama helin weli.<br>Marka bootku trade furo, halkan ayuu ka soo muuqan doonaa.</div></div>
      <div class="addrow">
        <input id="symInput" placeholder="Ku dar symbol, tusaale XAUUSD" maxlength="16">
        <button onclick="addSymbol()">Ku dar</button>
      </div>
    </div>
    <div class="card block">
      <h2 class="sec-h">Fiiro gaar ah</h2>
      <p style="font-size:13px;color:var(--ink-2);line-height:1.7">
        Damintu waxay amar u dirtaa bootka. EA-gu wuxuu qaataa markuu xiga poll-ka
        (3–10 sekan). Trade-yada horeba u furan ma xirmaan — isticmaal CLOSE.
      </p>
    </div>
  </section>

  <!-- ===== SIGNALS ===== -->
  <section class="pane" data-p="signals">
    <div class="card block">
      <h2 class="sec-h">Codso signal <span class="rt" id="sig-key"></span></h2>
      <p style="font-size:11.5px;color:var(--muted);font-weight:600;margin-bottom:9px">Lammaanaha</p>
      <div class="chips" id="sigSyms"></div>
      <p style="font-size:11.5px;color:var(--muted);font-weight:600;margin-bottom:9px">Muddada</p>
      <div class="exp" id="sigExp">
        <button data-e="5" class="on">5 min</button>
        <button data-e="15">15 min</button>
        <button data-e="60">1 saac</button>
      </div>
      <button class="bigbtn" id="sigGo">CODSO SIGNAL</button>
      <div id="sigOut"></div>
    </div>

    <div class="card block">
      <h2 class="sec-h">Signal-adii hore <span class="rt" id="sig-pending"></span></h2>
      <div id="sigHist"><div class="ot-empty">Signal weli lama codsan</div></div>
    </div>

    <div class="block">
      <div class="acc">
        <div style="font-size:11.5px;color:var(--orange);font-weight:700;margin-bottom:12px">Saxnaantaada dhabta ah</div>
        <div style="display:flex;align-items:flex-end;gap:13px">
          <div><div id="accVal" style="font-size:28px;font-weight:800;line-height:1">—</div>
               <div id="accN" style="font-size:10.5px;color:var(--muted);margin-top:3px">0 signal</div></div>
          <div style="flex:1">
            <div class="accbar"><i id="accBar" style="width:0%;background:var(--muted)"></i><span class="accmark" id="accMark" style="left:55.6%"></span></div>
            <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:5px">
              <span>0%</span><span id="accBE">break-even 55.6%</span><span>100%</span></div>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:9px;margin-top:13px">
          <span style="font-size:11.5px;color:var(--ink-2)">Payout broker-kaaga</span>
          <input id="payout" type="number" min="50" max="100" value="80" style="width:64px;background:var(--surface-2);border:1px solid var(--line);color:var(--ink);border-radius:8px;padding:7px 9px;font-size:13px;font-variant-numeric:tabular-nums">
          <span style="font-size:13px;color:var(--ink-2)">%</span>
        </div>
        <div id="accNote"></div>
      </div>
    </div>
  </section>

  <!-- ===== CHART ===== -->
  <section class="pane" data-p="chart">
    <div class="card block">
      <h2 class="sec-h">Chart <span class="rt" id="chartsym">GBPUSD · M5</span></h2>
      <div id="tvchart" style="height:340px;border-radius:10px;overflow:hidden"></div>
    </div>
  </section>
<!-- ===== JOURNAL ===== -->
  <section class="pane" data-p="journal">
    <div class="block">
      <h2 class="sec-h">Journal <span class="rt" id="j-store"></span></h2>
      <div class="chips" id="jSyms"></div>
      <div class="kpis" style="grid-template-columns:repeat(2,minmax(0,1fr));margin-bottom:10px">
        <div class="card kpi"><div class="lbl">Trade guud</div><div class="val num" id="jn_total">-</div><div class="lbl" id="jn_wl" style="margin-top:3px"></div></div>
        <div class="card kpi"><div class="lbl">Win rate</div><div class="val num or" id="jn_wr">-</div></div>
      </div>
      <div class="kpis" style="grid-template-columns:repeat(2,minmax(0,1fr))">
        <div class="card kpi accent"><div class="lbl">Profit factor</div><div class="val num" id="jn_pf">-</div></div>
        <div class="card kpi"><div class="lbl">Net</div><div class="val num" id="jn_net">-</div></div>
      </div>
      <div id="jn_verdict"></div>
    </div>

    <div class="card block">
      <h2 class="sec-h">Lammaane kasta</h2>
      <div id="jSymTable"><div class="ot-empty">Xog weli ma jirto</div></div>
    </div>

    <div class="card block">
      <h2 class="sec-h">Trade-yadii u dambeeyay <span class="rt" id="j-count"></span></h2>
      <div id="jList"><div class="ot-empty">Xog weli ma jirto</div></div>
    </div>

    <div class="block">
      <button style="width:100%;height:44px" id="jCsv">Soo dejiso CSV</button>
    </div>
  </section>

  <div class="foot">MOHA PRO · Bot Control · build <span id="buildTag">BUILD</span></div>
</div>

<nav class="navbar" id="nav">
  <button data-t="overview" class="active"><svg viewBox="0 0 24 24"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/></svg>Guud</button>
  <button data-t="symbols"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/></svg>Symbols</button>
  <button data-t="chart"><svg viewBox="0 0 24 24"><path d="M3 15l5-5 4 4 8-8"/></svg>Chart</button>
  <button data-t="journal"><svg viewBox="0 0 24 24"><path d="M4 4h16v16H4z"/><line x1="8" y1="9" x2="16" y2="9"/><line x1="8" y1="13" x2="16" y2="13"/></svg>Journal</button>
  <button data-t="signals"><svg viewBox="0 0 24 24"><polyline points="3 17 9 11 13 15 21 6"/><circle cx="9" cy="11" r="1.4"/></svg>Signals</button>
</nav>

<script>
const $=id=>document.getElementById(id);
const TOKEN=new URLSearchParams(location.search).get('token')||"MohaPro_Live_2026_MySecret";
const POLL_MS=5000;
let CUR_BOT=null;      // null = Dhammaan
let BOTS=[];
const money=n=>{const v=+n||0;return (v<0?'-$':'$')+Math.abs(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});};

/* clock */
function tick(){$('clock').textContent=new Date().toLocaleTimeString('en-GB');}
setInterval(tick,1000);tick();

/* ===== TABS ===== */
function showTab(t){
  document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.dataset.p===t));
  document.querySelectorAll('#tabs div').forEach(d=>d.classList.toggle('on',d.dataset.t===t));
  document.querySelectorAll('#nav button[data-t]').forEach(b=>b.classList.toggle('active',b.dataset.t===t));
  if(t==='chart')initChart(LAST_SYMBOL);
  if(t==='signals')loadSignals();
  if(t==='journal')loadJournal();
  window.scrollTo({top:0,behavior:'smooth'});
}
document.querySelectorAll('#tabs div').forEach(d=>d.addEventListener('click',()=>showTab(d.dataset.t)));
document.querySelectorAll('#nav button[data-t]').forEach(b=>b.addEventListener('click',()=>showTab(b.dataset.t)));

/* ===== CHART ===== */
let tvStarted=false,tvSym='',LAST_SYMBOL='GBPUSD';
function tvSymbolFor(raw){let s=(raw||'GBPUSD').toUpperCase().replace(/[^A-Z].*$/,'');if(s.length<6)s='GBPUSD';return 'FX:'+s.slice(0,6);}
function loadTV(cb){if(window.TradingView){cb();return;}if(!tvStarted){tvStarted=true;const s=document.createElement('script');s.src='https://s3.tradingview.com/tv.js';s.onload=cb;document.head.appendChild(s);}else{setTimeout(()=>loadTV(cb),300);}}
function initChart(raw){
  const sym=tvSymbolFor(raw);if(sym===tvSym)return;tvSym=sym;
  $('chartsym').textContent=sym.replace('FX:','')+' · M5';
  loadTV(()=>{const el=$('tvchart');if(!el||!window.TradingView)return;el.innerHTML='';
    new TradingView.widget({container_id:'tvchart',autosize:true,symbol:sym,interval:'5',timezone:'Etc/UTC',theme:'dark',style:'1',locale:'en',toolbar_bg:'#15151e',hide_side_toolbar:true,allow_symbol_change:true});});
}

/* ===== BANNER ===== */
(function(){const img=$('banner-img'),inp=$('img-input');
  try{const s=localStorage.getItem('moha_banner');if(s)img.src=s;}catch(e){}
  const open=()=>inp.click();
  $('change-btn').addEventListener('click',open);
  inp.addEventListener('change',e=>{const f=e.target.files&&e.target.files[0];if(!f)return;const r=new FileReader();
    r.onload=ev=>{img.src=ev.target.result;try{localStorage.setItem('moha_banner',ev.target.result);}catch(x){}};r.readAsDataURL(f);});
})();

/* ===== STRATEGY ===== */
document.querySelectorAll('.strat').forEach(el=>el.addEventListener('click',()=>{
  sendCmd('STRATEGY:'+el.dataset.s);
  document.querySelectorAll('.strat').forEach(x=>x.classList.remove('active'));el.classList.add('active');}));

/* ===== COMMANDS ===== */
async function sendCmd(cmd){
  if(!CUR_BOT && BOTS.length>1 && (cmd==='CLOSE_ALL'cmd==='STOP'cmd==='CLOSE_PROFIT')){
    if(!confirm(cmd+' waxay u socotaa DHAMMAAN botyada ('+BOTS.length+'). Sii wad?'))return;
  }
  const note=$('cmdNote');note.style.color='var(--muted)';note.textContent='Diraya '+cmd+'…';
  try{
    const r=await fetch('/admin/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:TOKEN,bot:(CUR_BOT||'*'),command:cmd})});
    const d=await r.json();
    if(d.ok){note.style.color='var(--good-ink)';
      note.textContent=cmd+' → '+(d.bots||[]).join(', ')+' (~3s gudahood)';}
    else{note.style.color='var(--bad-ink)';note.textContent=d.error||'Amarka lama aqbalin';}
  }catch(e){note.style.color='var(--bad-ink)';note.textContent='Server-ka lama gaari karin';}
}

/* ===== BOT SWITCHER ===== */
function renderBots(list){
  BOTS=list||[];
  const sw=$('botsw');
  if(!BOTS.length){sw.innerHTML='';$('botsBlock').classList.add('hide');$('acctBlock').classList.remove('hide');return;}
  if(CUR_BOT&&!BOTS.some(b=>b.bot===CUR_BOT))CUR_BOT=null;
  if(!CUR_BOT&&BOTS.length===1)CUR_BOT=BOTS[0].bot;   // hal bot = si toos ah u dooro

  sw.innerHTML='';
  const mk=(label,val,live)=>{
    const b=document.createElement('button');
    b.className=(val===CUR_BOT?'on':'');
    b.innerHTML='<span class="bdot'+(live?' live':'')+'"></span>'+esc(label);
    b.addEventListener('click',()=>{CUR_BOT=val;renderBots(BOTS);poll();});
    sw.appendChild(b);
  };
  if(BOTS.length>1)mk('Dhammaan',null,BOTS.some(b=>b.live));
  BOTS.forEach(b=>mk(b.bot,b.bot,b.live));

  const all=!CUR_BOT&&BOTS.length>1;
  $('botsBlock').classList.toggle('hide',!all);
  $('acctBlock').classList.toggle('hide',all);
  $('stratBlock').classList.toggle('hide',all);
  $('bots-live').textContent=BOTS.filter(b=>b.live).length+' nool';

  if(all){
    const box=$('botList');box.innerHTML='';
    BOTS.forEach(b=>{
      const row=document.createElement('div');row.className='botcard';
      const p=+b.profit||0;
      row.innerHTML='<div style="flex:1;min-width:0">'+
        '<div class="bn"><span class="bdot'+(b.live?' live':'')+'"></span>'+esc(b.bot)+'</div>'+
        '<div class="bm">'+(b.live?('Balance '+money(b.balance)+' · '+(b.opentrades||0)+' furan'):
          (b.reason==='stale'?'Duugoobay '+(b.age||0)+'s':'Xog ma jirto'))+'</div></div>'+
        '<div class="bp"><div class="v '+(p>=0?'up':'down')+'">'+(b.live?((p>=0?'+':'')+money(Math.abs(p))):'—')+'</div>'+
        '<div class="l">floating</div></div>';
row.addEventListener('click',()=>{CUR_BOT=b.bot;renderBots(BOTS);poll();});
      let lp=null;
      const startLP=()=>{lp=setTimeout(()=>{
        if(confirm(b.bot+' liiska ka saar? Haddii uu wali wax dirayo, wuu soo laaban doonaa.')){
          fetch('/admin/forget_bot',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({token:TOKEN,bot:b.bot})}).then(()=>{CUR_BOT=null;poll();});
        }},700);};
      const endLP=()=>{if(lp){clearTimeout(lp);lp=null;}};
      row.addEventListener('touchstart',startLP,{passive:true});
      ['touchend','touchmove','touchcancel'].forEach(e=>row.addEventListener(e,endLP));
      row.addEventListener('mousedown',startLP);
      ['mouseup','mouseleave'].forEach(e=>row.addEventListener(e,endLP));
      box.appendChild(row);
    });
  }
}

/* ===== SYMBOLS ===== */
let SYMS=[];
function renderSymbols(list){
  SYMS=list||[];const box=$('symList');
  const on=SYMS.filter(s=>s.enabled!==false).length;
  $('sym-count').textContent=on+' firfircoon';
  if(!SYMS.length){box.innerHTML='<div class="sym-empty">Symbol lama helin weli.<br>Marka bootku trade furo, halkan ayuu ka soo muuqan doonaa.</div>';return;}
  box.innerHTML='';
  SYMS.forEach(s=>{
    const row=document.createElement('div');row.className='symrow';
    const pnl=+s.open_pnl||0;
    const meta=s.open>0
      ? s.open+' furan · <b class="'+(pnl>=0?'pl-pos':'pl-neg')+'">'+(pnl>=0?'+':'')+money(Math.abs(pnl))+'</b>'
      : (s.enabled===false?'Damisan':'Bannaan');
    const strat=s.strategies&&s.strategies.length?' · '+s.strategies.join(', '):'';
    const who=s.bot?' · '+esc(s.bot):'';
    row.innerHTML='<div class="si"><div class="sn">'+esc(s.symbol)+'</div><div class="sm">'+meta+strat+who+'</div></div>';
    const sw=document.createElement('button');
    sw.className='sw'+(s.enabled===false?'':' on');
    sw.setAttribute('aria-label',(s.enabled===false?'Fur ':'Dami ')+s.symbol);
    sw.addEventListener('click',()=>{
      const turnOn=!sw.classList.contains('on');
      sw.classList.toggle('on',turnOn);
      const keep=CUR_BOT;if(s.bot)CUR_BOT=s.bot;
      sendCmd((turnOn?'SYMBOL_ON:':'SYMBOL_OFF:')+s.symbol);CUR_BOT=keep;
    });
    row.appendChild(sw);box.appendChild(row);
  });
}
function addSymbol(){
  const v=($('symInput').value||'').trim().toUpperCase();
  if(!v){$('symInput').focus();return;}
  sendCmd('SYMBOL_ON:'+v);$('symInput').value='';
  setTimeout(poll,600);
}

/* ===== TRADES ===== */
function esc(s){const d=document.createElement('div');d.textContent=(s==null?'':s);return d.innerHTML;}
function f5(v){return (+v||0).toFixed(5);}
function detHTML(t,open){
  return '<div class="det-grid">'+
    '<div class="dc"><span>Entry</span><b>'+f5(t.entry)+'</b></div>'+
    '<div class="dc"><span>'+(open?'Hadda':'Close')+'</span><b>'+f5(t.cur)+'</b></div>'+
    '<div class="dc"><span>Stop loss</span><b class="pl-neg">'+f5(t.sl)+'</b></div>'+
    '<div class="dc"><span>Take profit</span><b class="pl-pos">'+f5(t.tp)+'</b></div>'+
    '<div class="dc"><span>Lot</span><b>'+(+t.lot||0).toFixed(2)+'</b></div>'+
    '<div class="dc"><span>P&L</span><b class="'+((+t.profit0)>=0?'pl-pos':'pl-neg')+'">'+money(+t.profit0)+'</b></div>'+
  '</div>';
}
function renderTrades(trades){
  const tb=$('tradesBody'),cnt=$('trades-count');
  if(!trades||!trades.length){tb.innerHTML='<tr><td colspan="6" class="ot-empty">Trade ma jiro weli</td></tr>';cnt.textContent='0';return;}
  cnt.textContent=trades.length;tb.innerHTML='';
  trades.slice(0,20).forEach((t,i)=>{
    const p=+t.profit0,buy=(t.type'').toUpperCase()==='BUY',open=(t.st||'')==='OPEN';
    const tr=document.createElement('tr');tr.className='trow';
    tr.innerHTML='<td class="carcell"><span class="car" id="car'+i+'">&#9656;</span></td>'+
      '<td class="sym">'+esc(t.sym)+'</td>'+
'<td><span class="badge '+(buy?'buy':'sell')+'">'+esc(t.type)+'</span></td>'+
      '<td><span class="badge strat">'+esc(t.strat)+'</span></td>'+
      '<td class="'+(p>=0?'pl-pos':'pl-neg')+'">'+(p>=0?'+':'')+money(p)+'</td>'+
      '<td><span class="badge '+(open?'op':'cl')+'">'+(open?'FURAN':'XIRAN')+'</span></td>';
    tr.addEventListener('click',()=>{const d=$('det'+i),c=$('car'+i);const sh=d.style.display==='none';
      d.style.display=sh?'table-row':'none';if(c)c.innerHTML=sh?'&#9662;':'&#9656;';});
    tb.appendChild(tr);
    const dr=document.createElement('tr');dr.id='det'+i;dr.style.display='none';dr.className='detrow';
    dr.innerHTML='<td colspan="6" class="tdet">'+detHTML(t,open)+'</td>';
    tb.appendChild(dr);
  });
}

/* ===== JOURNAL ===== */
let J_SYM=null;
function jMoney(v){const n=+v||0;return (n>=0?'+':'-')+'$'+Math.abs(n).toFixed(2);}

function renderJournal(d){
  const st=d.stats, o=st.overall;
  $('j-store').textContent = st.persistent ? 'waaraya' : 'xusuusta';
  $('j-count').textContent = d.trades.length;
  $('jn_total').textContent = o.trades || '0';
  $('jn_wl').textContent    = o.wins+' W · '+o.losses+' L';
  $('jn_wr').textContent    = (o.winrate==null?'—':o.winrate+'%');
  $('jn_pf').textContent    = (o.trades?o.pf.toFixed(2):'—');
  $('jn_pf').className      = 'val num '+(o.pf>=1.3?'up':o.pf>=0.9?'or':'down');
  $('jn_net').textContent   = (o.trades?jMoney(o.net):'—');
  $('jn_net').className     = 'val num '+(o.net>=0?'up':'down');

  const cls = (st.verdict==='good')?'okbox':'warnbox';
  $('jn_verdict').innerHTML='<div class="'+cls+'" style="margin-top:12px">'+esc(st.message)+'</div>';

  const chips=$('jSyms');chips.innerHTML='';
  const mk=(label,val)=>{const b=document.createElement('button');
    b.className='chip2'+(val===J_SYM?' on':'');b.textContent=label;
    b.addEventListener('click',()=>{J_SYM=val;loadJournal();});chips.appendChild(b);};
  mk('Dhammaan',null);
  st.symbols.forEach(x=>mk(x.symbol,x.symbol));

  const t=$('jSymTable');
  if(!st.symbols.length){t.innerHTML='<div class="ot-empty">Xog weli ma jirto</div>';}
  else{
    t.innerHTML='';
    st.symbols.forEach(x=>{
      const row=document.createElement('div');row.className='symrow';
      row.innerHTML='<div class="si"><div class="sn">'+esc(x.symbol)+'</div>'+
        '<div class="sm">'+x.trades+' trade · '+(x.winrate==null?'—':x.winrate+'%')+
        ' · PF '+x.pf.toFixed(2)+'</div></div>'+
        '<div style="text-align:right"><div style="font-size:15px;font-weight:700" class="'+
        (x.net>=0?'pl-pos':'pl-neg')+'">'+jMoney(x.net)+'</div>'+
        '<div style="font-size:11px;color:var(--text-muted)">'+
        (x.trades<30?'sample yar':'sample ku filan')+'</div></div>';
      t.appendChild(row);
    });
  }

  const l=$('jList');
  if(!d.trades.length){l.innerHTML='<div class="ot-empty">Xog weli ma jirto</div>';return;}
  l.innerHTML='';
  d.trades.slice(0,40).forEach(r=>{
    const p=+r.profit0, buy=(r.side'').toUpperCase()==='BUY';
    const row=document.createElement('div');row.className='symrow';
    row.innerHTML='<div class="si"><div class="sn" style="font-size:14px">'+esc(r.symbol)+
      ' <span class="badge '+(buy?'buy':'sell')+'">'+esc(r.side)+'</span></div>'+
      '<div class="sm">'+esc(r.close_time'')+' · '+esc(r.strat'')+'</div></div>'+
      '<div style="text-align:right"><div style="font-size:15px;font-weight:700" class="'+
      (p>=0?'pl-pos':'pl-neg')+'">'+jMoney(p)+'</div>'+
      '<div style="font-size:11px;color:var(--text-muted)">#'+esc(r.ticket)+'</div></div>';
    l.appendChild(row);
  });
}

async function loadJournal(){
  try{
    const u='/journal?token='+encodeURIComponent(TOKEN)+(J_SYM?'&symbol='+encodeURIComponent(J_SYM):'');
    const r=await fetch(u,{cache:'no-store'});
    renderJournal(await r.json());
  }catch(e){}
}
$('jCsv').addEventListener('click',()=>{
  window.open('/journal.csv?token='+encodeURIComponent(TOKEN),'_blank');
});
/* ===== STATE ===== */
const DEMO={balance:10482.55,equity:10531.20,profit:182.55,winrate:76.2,drawdown:3.10,opentrades:2,symbol:"GBPUSD",
  trades:[
    {bot:"MOHA PRO V56",sym:"GBPUSD",type:"BUY",strat:"VSA",profit:64.20,st:"OPEN",entry:1.27140,cur:1.27204,sl:1.26950,tp:1.27520,lot:0.20},
    {sym:"USDJPY",type:"SELL",strat:"SR",profit:16.93,st:"OPEN",entry:157.320,cur:157.280,sl:157.520,tp:156.920,lot:0.15},
    {sym:"GBPUSD",type:"BUY",strat:"VSA",profit:88.30,st:"CLOSED",entry:1.26980,cur:1.27290,sl:1.26800,tp:1.27340,lot:0.20},
    {sym:"AUDUSD",type:"SELL",strat:"VSA",profit:137.09,st:"CLOSED",entry:0.66420,cur:0.66150,sl:0.66600,tp:0.66060,lot:0.30},
    {sym:"USDCAD",type:"SELL",strat:"SR",profit:-48.34,st:"CLOSED",entry:1.36540,cur:1.36680,sl:1.36700,tp:1.36180,lot:0.15}
  ],
  symbols:[
    {symbol:"GBPUSD",open:1,open_pnl:64.20,strategies:["VSA"],enabled:true},
    {symbol:"USDJPY",open:1,open_pnl:16.93,strategies:["SR"],enabled:true},
    {symbol:"AUDUSD",open:0,open_pnl:0,strategies:["VSA"],enabled:true},
    {symbol:"USDCAD",open:0,open_pnl:0,strategies:["SR"],enabled:false}
  ],
  bots:[{bot:"MOHA PRO GOLD",live:true,balance:10482.55,equity:10531.20,profit:81.13,opentrades:2,symbols:[]},
        {bot:"MOHA PRO V56",live:true,balance:6210.00,equity:6288.40,profit:78.40,opentrades:1,symbols:[]}],
  journal:{gainPct:18.4,balance:11842.55,equity:11905.20,today:82.55,week:1842.55,month:1842.55,year:1842.55,
    trades:1078,winRate:76.2,pf:1.62,pips:4820,avgWin:34,avgLoss:-21,best:137,worst:-50,dd:3.1,
    monthly:[320,540,-180,410,730,-90,560,880,210,-210,640,780]}};

const REASONS={
  no_data:"Bootku weli xog ma soo dirin. Fur /diag si aad u aragto sababta.",
  stale:"Xogtii ugu dambeysay way duugowday. Bootku ma shaqaynayo ama server-ku wuu hurday.",
  offline:"Server-ka lama gaari karin."
};

function setStatus(s,reason,age){
  const el=$('status'),bl=$('botline'),bs=$('botstate'),dm=$('dataMode');
  el.className='st-pill '+s;
  if(s==='on'){
    el.innerHTML='<span class="dot"></span>LIVE';bl.classList.add('run');bs.textContent='Shaqeynaya';
    dm.className='banner live';
    const noName=BOTS.length&&BOTS.every(b=>b.bot==='default');
    dm.innerHTML='Xog dhab ah — la cusboonaysiiyay '+(age!=null?age+'s ka hor':'hadda')+
      (noName?'<br><span style="color:var(--orange-2)">Bootku magac ma laha. Geli InpCloudBotName.</span>':'');
  }else{
    el.innerHTML='<span class="dot"></span>'+(s==='demo'?'DEMO':'OFFLINE');
    bl.classList.remove('run');bs.textContent='Joogsan';
    dm.className='banner demo';
    dm.innerHTML='Xog tusaale ah — lacagtaadu maaha.<br>'+(REASONS[reason]||REASONS.no_data)+
      ' <a href="/diag" target="_blank">Fur /diag</a>';
  }
}

function applyState(d,strict){
  // strict = xog dhab ah. Goob maqan waxay noqonaysaa "—", MARNABA lambar demo ah.
  const put=(id,val,fmt,cls)=>{
    const el=$(id);
    if(val==null||val===''){ if(strict){el.textContent='—';if(cls)el.className='val num';} return; }
    el.textContent=fmt(val); if(cls)el.className=cls(val);
  };
  put('k_balance',d.balance,money);
  put('k_equity',d.equity,money);
  put('k_profit',d.profit,v=>((+v>=0?'+':'')+money(Math.abs(+v))),v=>'val num '+(+v>=0?'up':'down'));
  put('k_wr',d.winrate,v=>(+v).toFixed(1)+'%');
  put('k_dd',d.drawdown,v=>(+v).toFixed(2)+'%');
  put('k_open',d.opentrades,v=>String(v));
  if(d.symbol){$('symbol').textContent=d.symbol;LAST_SYMBOL=d.symbol;}
  else if(strict)$('symbol').textContent='—';
  renderTrades(d.trades);
  renderSymbols(d.symbols);
  if(d.bots)renderBots(d.bots);
}

function showDemo(reason){applyState(DEMO,false);setStatus('demo',reason||'no_data');}

async function poll(){
  try{
    const url='/state?token='+encodeURIComponent(TOKEN)+(CUR_BOT?'&bot='+encodeURIComponent(CUR_BOT):'');
    const r=await fetch(url,{cache:'no-store'});
if(!r.ok)throw 0;
    const d=await r.json();
    if(d.live){applyState(d,true);setStatus('on','live',d.age);}
    else{showDemo(d.reason);}
  }catch(e){showDemo('offline');}
}
showDemo();poll();setInterval(poll,POLL_MS);
</script>
</body>
</html>
"""


_dashboard_cache = {"mtime": 0, "html": None}


def dashboard_html():
    try:
        mtime = os.path.getmtime(DASHBOARD_FILE)
        if _dashboard_cache["html"] is None or mtime != _dashboard_cache["mtime"]:
            with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
                _dashboard_cache["html"] = f.read()
            _dashboard_cache["mtime"] = mtime
        return _dashboard_cache["html"]
    except OSError:
        return EMBEDDED_HTML


@app.route("/")
@app.route("/admin")
def index():
    html = dashboard_html().replace("BUILD", BUILD)
    r = Response(html, mimetype="text/html")
    # Browser-ku HA hayn bog duug ah - taasi ayaa hore u dhibtay.
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    r.headers["X-Moha-Build"] = BUILD
    return r


@app.route("/version")
def version():
    return jsonify({"build": BUILD})


@app.route("/health")
def health():
    return jsonify({"ok": True, "build": BUILD,
                    "bots": sum(len(v) for v in STATES.values())})


if name == "main":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
