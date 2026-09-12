//+------------------------------------------------------------------+
//|              MOHA PRO ULTIMATE - V56  (MT5 PORT)                  |
//|         MQL4 -> MQL5 via compatibility layer (code kept as-is)    |
//+------------------------------------------------------------------+
#property copyright "Copyright 2026, Moha Pro Forex"
#property version   "57.4"
#property description "MOHA PRO V57.0 - POC REVERSAL FIX: entry VAL/VAH (proximal) halkii POC (dhexda); SL/TP zone-based; POC laga reebay SLTP_FIXED; regime filter reversal-aware; rejection dhab ah"
#include <Trade/Trade.mqh>

//==================================================================
//  MQL4 COMPATIBILITY LAYER  (MT4 -> MT5)
//==================================================================
CTrade  __t;

// ---- constants ----
#define OP_BUY   0
#define OP_SELL  1
#define OP_BUYLIMIT 2
#define OP_SELLLIMIT 3
#define OP_BUYSTOP 4
#define OP_SELLSTOP 5
#define MODE_MAIN  0
#define MODE_SIGNAL 1
#define MODE_UPPER 1
#define MODE_LOWER 2
#define MODE_HIGH  1
#define MODE_LOW   2
#define SELECT_BY_POS    0
#define SELECT_BY_TICKET 1
#define MODE_TRADES  0
#define MODE_HISTORY 1
#define MODE_POINT   1
#define MODE_DIGITS  2
#define MODE_SPREAD  3
#define MODE_STOPLEVEL 4
#define MODE_LOTSIZE 5
#define MODE_TICKVALUE 6
#define MODE_TICKSIZE 7
#define MODE_LOTSTEP 8
#define MODE_MINLOT  9
#define MODE_MAXLOT  10
#define MODE_MARGINREQUIRED 11
#define MODE_BID 12
#define MODE_ASK 13
#define DoubleToStr DoubleToString
#define StringToTime StringToTime

#define Point  _Point
#define Digits _Digits
#define Bars   Bars(_Symbol,_Period)
double Bid(){ return SymbolInfoDouble(_Symbol,SYMBOL_BID); }
double Ask(){ return SymbolInfoDouble(_Symbol,SYMBOL_ASK); }
#define Bid Bid()
#define Ask Ask()

// ---- price/time series objects (so Close[i], High[i]... keep working) ----
class __CSeries{ public:
   int kind; // 0=Close 1=Open 2=High 3=Low 4=Time 5=Volume
   double operator[](const int i) const {
      if(kind==0){ double b[]; if(CopyClose(_Symbol,_Period,i,1,b)<1)return 0; return b[0]; }
      if(kind==1){ double b[]; if(CopyOpen (_Symbol,_Period,i,1,b)<1)return 0; return b[0]; }
      if(kind==2){ double b[]; if(CopyHigh (_Symbol,_Period,i,1,b)<1)return 0; return b[0]; }
      if(kind==3){ double b[]; if(CopyLow  (_Symbol,_Period,i,1,b)<1)return 0; return b[0]; }
      if(kind==4){ datetime t[]; if(CopyTime(_Symbol,_Period,i,1,t)<1)return 0; return (double)t[0]; }
      if(kind==5){ long v[]; if(CopyTickVolume(_Symbol,_Period,i,1,v)<1)return 0; return (double)v[0]; }
      return 0;
   }
};
__CSeries Close={0},Open={1},High={2},Low={3},Time={4},Volume={5};

// ---- time helpers ----
int __hr(){ MqlDateTime d; TimeToStruct(TimeCurrent(),d); return d.hour; }
int __mn(){ MqlDateTime d; TimeToStruct(TimeCurrent(),d); return d.min; }
int __dow(){ MqlDateTime d; TimeToStruct(TimeCurrent(),d); return d.day_of_week; }
int __day(){ MqlDateTime d; TimeToStruct(TimeCurrent(),d); return d.day; }
#define Hour       __hr
#define Minute     __mn
#define DayOfWeek  __dow
#define Day        __day

// ---- account / terminal ----
double AccountBalance(){ return AccountInfoDouble(ACCOUNT_BALANCE); }
double AccountEquity(){ return AccountInfoDouble(ACCOUNT_EQUITY); }
double AccountFreeMargin(){ return AccountInfoDouble(ACCOUNT_MARGIN_FREE); }
long   AccountNumber(){ return AccountInfoInteger(ACCOUNT_LOGIN); }
bool   IsTesting(){ return (bool)MQLInfoInteger(MQL_TESTER); }
bool   IsTradeAllowed(){ return (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) && MQLInfoInteger(MQL_TRADE_ALLOWED)); }
bool   RefreshRates(){ return true; }

// ---- MarketInfo ----
double MarketInfo(string s,int mode){
   switch(mode){
      case MODE_POINT:  return SymbolInfoDouble(s,SYMBOL_POINT);
      case MODE_DIGITS: return (double)SymbolInfoInteger(s,SYMBOL_DIGITS);
      case MODE_SPREAD: return (double)SymbolInfoInteger(s,SYMBOL_SPREAD);
      case MODE_STOPLEVEL: return (double)SymbolInfoInteger(s,SYMBOL_TRADE_STOPS_LEVEL);
      case MODE_LOTSIZE: return SymbolInfoDouble(s,SYMBOL_TRADE_CONTRACT_SIZE);
      case MODE_TICKVALUE: return SymbolInfoDouble(s,SYMBOL_TRADE_TICK_VALUE);
      case MODE_TICKSIZE: return SymbolInfoDouble(s,SYMBOL_TRADE_TICK_SIZE);
      case MODE_LOTSTEP: return SymbolInfoDouble(s,SYMBOL_VOLUME_STEP);
      case MODE_MINLOT: return SymbolInfoDouble(s,SYMBOL_VOLUME_MIN);
      case MODE_MAXLOT: return SymbolInfoDouble(s,SYMBOL_VOLUME_MAX);
      case MODE_MARGINREQUIRED: { double m=0; if(!OrderCalcMargin(ORDER_TYPE_BUY,s,1.0,SymbolInfoDouble(s,SYMBOL_ASK),m)) return 0; return m; }
      case MODE_BID: return SymbolInfoDouble(s,SYMBOL_BID);
      case MODE_ASK: return SymbolInfoDouble(s,SYMBOL_ASK);
   }
   return 0;
}

// ---- indicator handle cache ----
string __ihK[]; int __ihH[];
#define __IH_MISS -2147483640
int __ihFind(string key){ int n=ArraySize(__ihK); for(int i=0;i<n;i++) if(__ihK[i]==key) return __ihH[i]; return __IH_MISS; }
int __ihAdd(string key,int h){ int n=ArraySize(__ihK); ArrayResize(__ihK,n+1); ArrayResize(__ihH,n+1); __ihK[n]=key; __ihH[n]=h; return h; }
void __ihReleaseAll(){ for(int i=0;i<ArraySize(__ihH);i++) if(__ihH[i]!=INVALID_HANDLE && __ihH[i]!=__IH_MISS) IndicatorRelease(__ihH[i]); ArrayResize(__ihK,0); ArrayResize(__ihH,0); }
double __buf(int h,int bufi,int shift){ if(h==INVALID_HANDLE||h==__IH_MISS||h<0) return 0; double b[]; if(CopyBuffer(h,bufi,shift,1,b)<1) return 0; return b[0]; }
ENUM_TIMEFRAMES __tf(int tf){ if(tf==0) return _Period; return (ENUM_TIMEFRAMES)tf; }

double m4iMA(string s,int tf,int per,int msh,int meth,int ap,int sh){ string k="MA"+s+IntegerToString(tf)+"_"+IntegerToString(per)+"_"+IntegerToString(msh)+"_"+IntegerToString(meth)+"_"+IntegerToString(ap); int h=__ihFind(k); if(h==__IH_MISS) h=__ihAdd(k,iMA(s,__tf(tf),per,msh,(ENUM_MA_METHOD)meth,(ENUM_APPLIED_PRICE)ap)); return __buf(h,0,sh); }
double m4iRSI(string s,int tf,int per,int ap,int sh){ string k="RSI"+s+IntegerToString(tf)+"_"+IntegerToString(per)+"_"+IntegerToString(ap); int h=__ihFind(k); if(h==__IH_MISS) h=__ihAdd(k,iRSI(s,__tf(tf),per,(ENUM_APPLIED_PRICE)ap)); return __buf(h,0,sh); }
double m4iATR(string s,int tf,int per,int sh){ string k="ATR"+s+IntegerToString(tf)+"_"+IntegerToString(per); int h=__ihFind(k); if(h==__IH_MISS) h=__ihAdd(k,iATR(s,__tf(tf),per)); return __buf(h,0,sh); }
double m4iADX(string s,int tf,int per,int ap,int mode,int sh){ string k="ADX"+s+IntegerToString(tf)+"_"+IntegerToString(per); int h=__ihFind(k); if(h==__IH_MISS) h=__ihAdd(k,iADX(s,__tf(tf),per)); int bi=(mode==MODE_MAIN)?0:1; return __buf(h,bi,sh); }
double m4iBands(string s,int tf,int per,double dev,int bsh,int ap,int mode,int sh){ string k="BB"+s+IntegerToString(tf)+"_"+IntegerToString(per)+"_"+DoubleToString(dev,2); int h=__ihFind(k); if(h==__IH_MISS) h=__ihAdd(k,iBands(s,__tf(tf),per,bsh,dev,(ENUM_APPLIED_PRICE)ap)); int bi=(mode==MODE_UPPER)?1:((mode==MODE_LOWER)?2:0); return __buf(h,bi,sh); }
double m4iMACD(string s,int tf,int fast,int slow,int sig,int ap,int mode,int sh){ string k="MACD"+s+IntegerToString(tf)+"_"+IntegerToString(fast)+"_"+IntegerToString(slow)+"_"+IntegerToString(sig)+"_"+IntegerToString(ap); int h=__ihFind(k); if(h==__IH_MISS) h=__ihAdd(k,iMACD(s,__tf(tf),fast,slow,sig,(ENUM_APPLIED_PRICE)ap)); int bi=(mode==MODE_MAIN)?0:1; return __buf(h,bi,sh); }
long m4iVolume(string s,int tf,int sh){ long v[]; if(CopyTickVolume(s,__tf(tf),sh,1,v)<1) return 0; return v[0]; }
double m4iClose(string s,int tf,int sh){ double b[]; if(CopyClose(s,__tf(tf),sh,1,b)<1)return 0; return b[0]; }
double m4iOpen(string s,int tf,int sh){ double b[]; if(CopyOpen(s,__tf(tf),sh,1,b)<1)return 0; return b[0]; }
datetime m4iTime(string s,int tf,int sh){ datetime t[]; if(CopyTime(s,__tf(tf),sh,1,t)<1)return 0; return t[0]; }
int m4iBars(string s,int tf){ return iBars(s,__tf(tf)); }
int m4iHighest(string s,int tf,int mode,int count,int start){ double a[]; ENUM_TIMEFRAMES p=__tf(tf); int got=(mode==MODE_LOW)?CopyLow(s,p,start,count,a):CopyHigh(s,p,start,count,a); if(got<1)return -1; int bi=0; for(int i=1;i<got;i++){ if(mode==MODE_LOW){ if(a[i]<a[bi]) bi=i; } else { if(a[i]>a[bi]) bi=i; } } return start+bi; }
int m4iLowest(string s,int tf,int mode,int count,int start){ return m4iHighest(s,tf,MODE_LOW,count,start); }

// ---- ORDER POOL compatibility ----
ulong __selT=0; bool __selHist=false; ulong __histOut[];
int  m4OrdersTotal(){ return PositionsTotal(); }
// History = KALIYA closed trades (DEAL_ENTRY_OUT deals) - u dhigma MT4 orders
// b110 FIX: cache + xadidaad taariikheed + map position_id -> qiimaha FURITAANKA
#define HIST_LOOKBACK_DAYS 180          // 0 = taariikhda oo dhan
datetime __histBuiltAt=0; int __histDealsSeen=-1;
long __pidKey[]; double __pidOpen[]; datetime __pidOpenT[];
int __pidFind(long pid){
   int n=ArraySize(__pidKey); if(n<1) return -1;
   int lo=0,hi=n-1;
   while(lo<=hi){ int mid=(lo+hi)/2; if(__pidKey[mid]==pid) return mid; if(__pidKey[mid]<pid) lo=mid+1; else hi=mid-1; }
   for(int i=n-1;i>=0;i--) if(__pidKey[i]==pid) return i;   // fallback (pid aan kala horreyn)
   return -1;
}
void __buildHistOut(bool force=false){
   datetime from=(HIST_LOOKBACK_DAYS>0)?(TimeCurrent()-(datetime)HIST_LOOKBACK_DAYS*86400):0;
   if(!HistorySelect(from,TimeCurrent())) return;
   int td=HistoryDealsTotal();
   if(!force && td==__histDealsSeen && TimeCurrent()==__histBuiltAt) return;
   __histDealsSeen=td; __histBuiltAt=TimeCurrent();
   ArrayResize(__histOut,td); ArrayResize(__pidKey,td); ArrayResize(__pidOpen,td); ArrayResize(__pidOpenT,td);
   int nOut=0,nIn=0;
   for(int i=0;i<td;i++){
      ulong tk=HistoryDealGetTicket(i); if(tk==0) continue;
      long ent=HistoryDealGetInteger(tk,DEAL_ENTRY);
      if(ent==DEAL_ENTRY_OUT){ __histOut[nOut++]=tk; }
      else if(ent==DEAL_ENTRY_IN){
         __pidKey[nIn]=(long)HistoryDealGetInteger(tk,DEAL_POSITION_ID);
         __pidOpen[nIn]=HistoryDealGetDouble(tk,DEAL_PRICE);
         __pidOpenT[nIn]=(datetime)HistoryDealGetInteger(tk,DEAL_TIME);
         nIn++;
      }
   }
   ArrayResize(__histOut,nOut); ArrayResize(__pidKey,nIn); ArrayResize(__pidOpen,nIn); ArrayResize(__pidOpenT,nIn);
}
int  m4OrdersHistoryTotal(){ __buildHistOut(); return ArraySize(__histOut); }
bool m4OrderSelect(long a,int sel,int pool=MODE_TRADES){
   if(sel==SELECT_BY_TICKET){ if(PositionSelectByTicket((ulong)a)){ __selT=(ulong)a; __selHist=false; return true; }
      HistorySelect(0,TimeCurrent()); if(HistoryDealSelect((ulong)a)){ __selT=(ulong)a; __selHist=true; return true; } return false; }
   if(pool==MODE_TRADES){ ulong tk=PositionGetTicket((int)a); if(tk==0)return false; __selT=tk; __selHist=false; return true; }
   if((int)a<0 || (int)a>=ArraySize(__histOut)) return false; __selT=__histOut[(int)a]; __selHist=true; return true;
}
long   m4OrderTicket(){ return (long)__selT; }
int    m4OrderType(){ if(__selHist){ long dt=HistoryDealGetInteger(__selT,DEAL_TYPE); return (dt==DEAL_TYPE_BUY)?OP_SELL:OP_BUY; } long t=PositionGetInteger(POSITION_TYPE); return (t==POSITION_TYPE_BUY)?OP_BUY:OP_SELL; }
double m4OrderLots(){ if(__selHist) return HistoryDealGetDouble(__selT,DEAL_VOLUME); return PositionGetDouble(POSITION_VOLUME); }
double m4OrderOpenPrice(){ if(__selHist){ int k=__pidFind((long)HistoryDealGetInteger(__selT,DEAL_POSITION_ID)); if(k>=0) return __pidOpen[k]; return HistoryDealGetDouble(__selT,DEAL_PRICE); } return PositionGetDouble(POSITION_PRICE_OPEN); }
double m4OrderClosePrice(){ if(__selHist) return HistoryDealGetDouble(__selT,DEAL_PRICE); return PositionGetDouble(POSITION_PRICE_CURRENT); }
double m4OrderStopLoss(){ if(__selHist) return 0; return PositionGetDouble(POSITION_SL); }
double m4OrderTakeProfit(){ if(__selHist) return 0; return PositionGetDouble(POSITION_TP); }
double m4OrderProfit(){ if(__selHist) return HistoryDealGetDouble(__selT,DEAL_PROFIT); return PositionGetDouble(POSITION_PROFIT); }
double m4OrderCommission(){ if(__selHist) return HistoryDealGetDouble(__selT,DEAL_COMMISSION); return 0; }
double m4OrderSwap(){ if(__selHist) return HistoryDealGetDouble(__selT,DEAL_SWAP); return PositionGetDouble(POSITION_SWAP); }
string m4OrderSymbol(){ if(__selHist) return HistoryDealGetString(__selT,DEAL_SYMBOL); return PositionGetString(POSITION_SYMBOL); }
int    m4OrderMagicNumber(){ if(__selHist) return (int)HistoryDealGetInteger(__selT,DEAL_MAGIC); return (int)PositionGetInteger(POSITION_MAGIC); }
datetime m4OrderOpenTime(){ if(__selHist){ int k=__pidFind((long)HistoryDealGetInteger(__selT,DEAL_POSITION_ID)); if(k>=0) return __pidOpenT[k]; return (datetime)HistoryDealGetInteger(__selT,DEAL_TIME); } return (datetime)PositionGetInteger(POSITION_TIME); }
datetime m4OrderCloseTime(){ if(__selHist) return (datetime)HistoryDealGetInteger(__selT,DEAL_TIME); return 0; }
string m4OrderComment(){ if(__selHist) return HistoryDealGetString(__selT,DEAL_COMMENT); return PositionGetString(POSITION_COMMENT); }
long m4OrderSend(string s,int cmd,double vol,double price,int slip,double sl,double tp,string cmt="",int mg=0,datetime exp=0,color c=clrNONE){
   __t.SetExpertMagicNumber(mg); __t.SetDeviationInPoints(slip>0?slip:10);
   bool ok=false;
   switch(cmd){
      case OP_BUY:       ok=__t.Buy (vol,s,price,sl,tp,cmt); break;
      case OP_SELL:      ok=__t.Sell(vol,s,price,sl,tp,cmt); break;
      case OP_BUYLIMIT:  ok=__t.BuyLimit (vol,price,s,sl,tp,ORDER_TIME_GTC,exp,cmt); break;
      case OP_SELLLIMIT: ok=__t.SellLimit(vol,price,s,sl,tp,ORDER_TIME_GTC,exp,cmt); break;
      case OP_BUYSTOP:   ok=__t.BuyStop  (vol,price,s,sl,tp,ORDER_TIME_GTC,exp,cmt); break;
      case OP_SELLSTOP:  ok=__t.SellStop (vol,price,s,sl,tp,ORDER_TIME_GTC,exp,cmt); break;
      default: Print("m4OrderSend: nooc amar aan la aqoon cmd=",cmd); return -1;
   }
   if(!ok){ Print("m4OrderSend FAIL ret=",__t.ResultRetcode()," ",__t.ResultRetcodeDescription()); return -1; }
   return (long)__t.ResultOrder();
}
bool m4OrderClose(long ticket,double lots,double price,int slip,color c=clrNONE){ __t.SetDeviationInPoints(slip>0?slip:10); if(PositionSelectByTicket((ulong)ticket)){ double _pv=PositionGetDouble(POSITION_VOLUME); if(lots>0 && lots<_pv-0.0000001) return __t.PositionClosePartial((ulong)ticket,lots); } return __t.PositionClose((ulong)ticket); }
bool m4OrderModify(long ticket,double price,double sl,double tp,datetime exp,color c=clrNONE){ ulong _tk=(ulong)ticket; if(!PositionSelectByTicket(_tk)){ Print("m4OrderModify: position #",ticket," lama helin - la joojiyay (position kale lama taabanayo)"); return false; } double _cs=PositionGetDouble(POSITION_SL), _ct=PositionGetDouble(POSITION_TP); if(MathAbs(_cs-sl)<_Point && MathAbs(_ct-tp)<_Point) return true; return __t.PositionModify(_tk,sl,tp); }
//==================================================================
//  END COMPATIBILITY LAYER  -  MOHA PRO V56 BODY BELOW (unchanged)
//==================================================================

//+------------------------------------------------------------------+
//| ENUMS                                                            |
//+------------------------------------------------------------------+
enum ENUM_STRATEGY       { STRAT_SR=0, STRAT_BOLLINGER=1, STRAT_EMA=2, STRAT_SMC=3, STRAT_VSA=4, STRAT_POC=5 };   // b82: RSI/RSI1H saaray | b83: POC (Volume Profile) ku daray
enum ENUM_TRADE_TIMEFRAME{ TF_M1=1, TF_M5=5 };
enum ENUM_PROP_MODE      { PROP_NONE=0, PROP_FTMO=1, PROP_MFF=2, PROP_CUSTOM=3 };
enum ENUM_EXEC_MODE      { EXEC_INSTANT=0, EXEC_SMART=1, EXEC_LIMIT=2 };
enum ENUM_SLTP_MODE      { SLTP_FIXED=0, SLTP_ATR=1, SLTP_SMC_PRO=2 };   // b14: SL/TP source (hal doorasho cad)
enum ENUM_STRAT_GROUP    { GRP_SINGLE=0, GRP_TREND=1, GRP_REVERSAL=2, GRP_ALL=3 };   // b23: koox xeelad

//+------------------------------------------------------------------+
//| INPUTS                                                           |
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| INPUTS (la kala saaray - organized by section)                   |
//+------------------------------------------------------------------+

sinput string   Sec_01 = "======== SHATIGA & AMMAANKA ========";
//==================================================================
//  MAAMULKA TRADE-KA — DHAMMAAN HAL MEEL (v57.4)
//
//  Waxa la saxay: hore LABA nidaam ayaa isku mar SL-ka dhaqaajinayay
//  (EnableBreakEven + Enable_ATR_Trailing). Isla SL-ka ayey ku
//  dagaallamayeen. Hadda ATR trailing oo keliya — break-even-ku
//  wuxuu ku jiraa ATR_BE_Mult.
//
//  Wax kasta oo position-ka qaybiya ama trade-ka hore u xiraa waa
//  DAMMAN inta xogta la uruurinayo. Haddii kale ma garan karto
//  in TP/SL-kaagu shaqeeyay iyo in kale.
//==================================================================

input group "=== M1. SL / TP (aasaaska) ==="
// Nisbaddu waa lafdhabarta. Tan tijaabi ka hor wax kasta.
input ENUM_SLTP_MODE SLTP_Mode     = SLTP_FIXED;   // Halka SL/TP laga qaato: FIXED (pips go'an) / ATR / SMC_PRO
input int       StopLoss_Pips_Fixed = 30;   // SL: fogaanta (pips) - habka FIXED
input int       TakeProfit_Pips_Fixed = 80;   // TP: fogaanta (pips) - habka FIXED
input double    TargetProfitUSD    = 0;   // Bartilmaameedka faa'iidada ($) - 0 = damman

input group "=== M2. SL DHAQAAJINTA — hal nidaam oo keliya ==="
// Kaliya MID ha noqdo true: ATR / R-based BE / pip trailing.
input bool      Enable_ATR_Trailing   = true;   // Trailing ku saleysan ATR (on/off)
input double    ATR_BE_Mult           = 1.0;   // ATR: multiplier-ka break-even
input double    ATR_Trail_Start_Mult  = 1.5;   // ATR: multiplier-ka bilowga trail-ka
input double    ATR_Trail_Step_Mult   = 1.0;   // ATR: multiplier-ka tallaabada trail-ka
input bool      EnableBreakEven    = false;   // *** BREAK-EVEN (kan dhabta ah) - SL u dhaqaaji entry-ga ***
input double    BE_Trigger_R       = 1.0;   // Break-even: immisa R faa'iido kadib ayuu shaqeeyaa
input double    BE_Lock_R          = 0.5;   // Break-even: faa'iidada la xidhayo (R)
input double    FastBE_Trigger_R   = 1.5;   // Fast BE: immisa R faa'iido kadib
input double    FastBE_Lock_R      = 0.5;   // Fast BE: faa'iidada la xidhayo (R)
input bool      EnableTrailingStop = false;   // Trailing stop - SL sicirka raaca (on/off)
input int       TrailingStartPips  = 25;   // Trailing: pips-ka bilowga
input int       TrailingStepPips   = 10;   // Trailing: pips-ka tallaabada
input double    Trail_Start_R         = 2.0;   // Trail: R-ga bilowga
input double    Trail_Step_R          = 0.5;   // Trail: R-ga tallaabada

input group "=== M3. POSITION QAYBSANAANTA — dhammaan DAMMAN ==="
// Qayb-xidhid natiijada way ka dhigaysaa mid aan la xisaabin karin.
input bool      EnablePartialClose = false;   // Xidh qayb ka mid ah trade-ka (on/off)
input int       PartialClosePips   = 20;   // Qayb-xidhid: pips
input int       PartialClosePercent = 50;   // Qayb-xidhid: boqolkiiba
input bool      Enable_ScaleOut    = false;   // BASELINE: la damiyay   // Qaado faa'iido qayb ah +1R iyo +2R (on/off)
input int       ScaleOut_R1_Pct    = 40;   // Scale out +1R: boqolkiiba la xidhayo
input int       ScaleOut_R2_Pct    = 30;   // Scale out +2R: boqolkiiba la xidhayo
input bool      Prop_Scale_Out     = false;   // BASELINE: la damiyay   // Prop: qaado faa'iido qayb ah (on/off)
input bool      Enable_TP_Ladder   = false;   // TP Ladder - TP qaybsan + SL tallaabo (on/off) - HA SHIDIN
input double    TPL_TP1_R          = 1.5;   // TP Ladder: TP1 immisa R (xidh 50%, SL breakeven)
input double    TPL_TP2_R          = 4.0;   // TP Ladder: TP2 immisa R (xidh 25%)
input double    TPL_TP3_R          = 6.0;   // TP Ladder: TP3 immisa R (xidh inta hadhay)
input double    TPL_TP4_R          = 8.0;   // TP Ladder: TP4 immisa R
input int       TPL_TP1_Pct        = 50;   // TP Ladder: TP1 boqolkiiba
input int       TPL_TP2_Pct        = 25;   // TP Ladder: TP2 boqolkiiba
input int       TPL_TP3_Pct        = 25;   // TP Ladder: TP3 boqolkiiba
input int       TPL_TP4_Pct        = 0;   // TP Ladder: TP4 boqolkiiba
input bool      TPL_Trail_Final    = false;   // TP Ladder: trail qaybta u dambaysa (on/off)
input double    TPL_Trail_Step_R   = 0.7;   // TP Ladder: tallaabada trail-ka (R)

input group "=== M4. FAA'IIDO QUFULID — dhammaan DAMMAN ==="
// Xogta ayey jaraan — maalin wanaagsan waa la joojinayaa.
input bool      Enable_USD_ProfitLock = false;   // BASELINE: la damiyay   // Xidh faa'iidada marka ay $ gaadho (on/off)
input double    LockProfit_USD        = 20;   // Faa'iidada ($) ee la xidhayo
input double    LockProfit_KeepPct    = 50;   // Faa'iidada: boqolkiiba la haynayo
input double    LockProfit_Trigger_R  = 1.0;   // Faa'iidada: R-ga shaqaynaya
input bool      Enable_Daily_Profit_Lock   = false;   // BASELINE: la damiyay   // Xidh faa'iidada maalinlaha marka la gaadho (on/off)
input bool      Enable_Weekly_Profit_Lock  = false;   // BASELINE: la damiyay   // Xidh faa'iidada usbuucle marka la gaadho (on/off)

input group "=== M5. XIRITAAN HORE — damman, weekend mooyee ==="
// Trade ha xirin ka hor TP/SL — haddii kale nisbaddaadu macno ma laha.
input bool      Enable_Stagnant_Exit     = false;   // Xidh trade aan waxba qabanayn N shumac kadib (on/off)
input int       Stagnant_Bars            = 30;      // Immisa shumac trade-ku furan yahay ka hor hubinta
input double    Stagnant_Max_R           = 0.2;     // Xidh haddii faa'iidadu u dhaxayso -R iyo +R (trade taagan)
input bool      Close_Profit_Before_News = false;   // Xidh trade faa'iido leh warka ka hor (on/off)
input int       News_Exit_Minutes        = 15;      // Daqiiqado warka ka hor oo la xidhayo
input double    News_Exit_Min_Profit_USD = 0.0;     // Kaliya xidh haddii faa'iidadu ka badan tahay ($) - 0 = mid kasta
input bool      News_Exit_High_Only      = true;    // Kaliya wararka WEYN (false = Weyn + Dhexe)
input bool      News_Exit_Close_Losers   = false;   // Sidoo kale xidh trade khasaare leh (on/off)
input bool      CloseWeekend       = true;   // Xidh trade-yada dhammaadka usbuuca (on/off)

input string    License_Key        = "MOHA-PRO-V26-ULTIMATE";   // Furaha shatiga (license key)
input bool      Enable_AccountLock = true;   // Ku xir account gaar ah (on/off)
input int       Licensed_Account   = 0;   // Lambarka account-ka la ogolaaday (0 = mid kasta)
input string    Expiry_Date        = "2027.01.01";   // Taariikhda uu shatigu dhacayo
input string    Cloud_Auth_Token   = "MohaPro_Live_2026_MySecret";   // Furaha ammaanka ee dashboard-ka cloud-ka
input bool      Require_Signed_Commands = true;   // U baahan amaro saxeexan (on/off)
input bool      Enable_Candle_Sync = false;   // Ku xir shaqada bilowga shumaca cusub (on/off)
input ENUM_TIMEFRAMES Sync_TF      = PERIOD_M5;   // Timeframe-ka sync-ga shumaca

sinput string   Sec_02 = "======== DOORASHADA XEELADDA ========";
input ENUM_STRATEGY Select_Strategy    = STRAT_VSA;   // Xeeladda la ganacsanayo (SR/BB/EMA/SMC/VSA/POC)
input ENUM_STRAT_GROUP Strategy_Group  = GRP_SINGLE;   // Kooxda xeeladaha (SINGLE / TREND / REVERSAL / ALL)
input bool      Enable_Auto_Strategy   = false;   // Doorasho xeelad oo toos ah (on/off)
input bool      Enable_Multi_Strategy  = false;   // Isku mar ku ganacso dhammaan xeeladaha (on/off)
input bool      Enable_Conflict_Guard  = false;   // Ilaali is-hor-imaad (BUY & SELL isku mar) (on/off)
input bool      One_Trade_Per_Symbol   = true;   // Hal trade lammaanahiiba (on/off)
input ENUM_TRADE_TIMEFRAME Trade_Timeframe = TF_M1;   // Timeframe-ka ganacsiga (M1 ama M5)
input bool      Enable_Consensus       = false;   // U baahan in xeeladuhu isku raacaan (on/off)
input int       Consensus_Min_Agree    = 2;   // Immisa xeelad oo isku raacda ayaa loo baahan yahay
input bool      Enable_MTF_Confirm     = false;   // Xaqiijin timeframe badan - EMA50 (on/off)
input int       MTF_EMA_Period         = 50;   // MTF: muddada EMA-ga xaqiijinta
input bool      MTF_Require_H4         = false;   // BASELINE: la damiyay   // MTF: H4 waa inuu isku raaco (on/off)
input bool      MTF_Require_H1         = false;   // BASELINE: la damiyay   // MTF: H1 waa inuu isku raaco (on/off)
input bool      MTF_Require_M15        = false;   // BASELINE: la damiyay   // MTF: M15 waa inuu isku raaco (on/off)

//--- b106: HTF EMA200 BIG-TREND FILTER (H1 + H4) ------------------------------
// BUY  = sicirku waa inuu KA KOR maro EMA200 (EMA200 hoos)
// SELL = sicirku waa inuu KA HOOS maro EMA200 (EMA200 kor)
sinput string   Sec_E200 = "======== EMA200 JIHADA WEYN (H1 + H4) ========";
input bool      Enable_HTF_EMA200      = false;   // EMA200 H1+H4 filter jihada weyn (on/off)
input int       HTF_EMA200_Period      = 200;    // Muddada EMA200-ka (caadi = 200)
input bool      HTF_EMA200_Require_H1  = false;   // BASELINE: la damiyay   // U baahan H1 EMA200 (on/off)
input bool      HTF_EMA200_Require_H4  = false;  // U baahan H4 EMA200 (on/off) - FIX V56.1: H1+H4 labaduba isku mar waxay xanibayeen 6-da xeelad oo dhan (0 trades). Hadda H1 kaliya ayaa loo baahan yahay by default.
input double    HTF_EMA200_Buffer_Pips = 0.0;    // Fogaan (pips) sicirka & EMA200 - 0 = damman
input bool      HTF_EMA200_Block_If_NA = false;  // Xannib haddii xogta EMA200 la waayo (on/off)

input bool      Enable_Correlation_Filter = false;   // Filter lammaanayaal isku xidhan (on/off)
input string    Correlation_Groups     = "EURUSD,GBPUSD,AUDUSD,NZDUSD|USDJPY,USDCHF,USDCAD|EURJPY,GBPJPY,EURGBP";   // Kooxaha lammaanayaasha isku xidhan ( | kala saar kooxaha )
input int       Max_Correlated_Same_Dir = 2;   // Trade isku jiho ah oo ugu badan lammaanayaal isku xidhan

sinput string   Sec_03 = "======== XEERARKA RAACIDDA TRENDKA ========";
input bool      Enable_TrendFollow = false;   // Raac trendka - albaabka xeeladaha trend (on/off)
input bool      TF_Require_Volume  = false;   // BASELINE: la damiyay   // Trend: u baahan volume (on/off)
input double    TF_Volume_Ratio    = 1.0;   // Trend: saamiga volume-ka loo baahan yahay
input bool      TF_Require_MACD    = false;   // BASELINE: la damiyay   // Trend: u baahan MACD (on/off)
input int       MACD_Fast          = 12;   // MACD degdeg (fast)
input int       MACD_Slow          = 26;   // MACD gaabis (slow)
input int       MACD_Signal        = 9;   // MACD signal
input bool      Enable_Extension_Guard = false;   // Ha eryin spike - sicirka aad uga fog EMA50 (on/off)
input double    Max_Extension_ATR      = 2.5;   // Fogaanta ugu badan EMA50 (x ATR) - hoos = adag
input bool      Rev_Structural_SL  = true;   // Rogmasho: SL ku saleysan qaab-dhismeedka (on/off)
input int       Rev_SL_Lookback    = 12;   // Rogmasho SL: immisa shumac dib loo eegayo
input double    Rev_SL_Max_ATR     = 3.5;   // Rogmasho SL: fogaanta ugu badan (x ATR)


sinput string   Sec_QC = "======== TAYADA TRADE-KA - KONTOROOLKA ========";
// --- 1) SMC confluence  (caddaynta gelitaanka - adkeyn = tayo sare) ---
input bool      SMC_Require_BOS    = true;   // SMC: u baahan BOS - jab qaab-dhismeed (on/off)
input bool      SMC_Require_CHoCH  = true;   // SMC: u baahan CHoCH - beddelka jihada (on/off)
input bool      SMC_Require_FVG    = false;   // BASELINE: la damiyay   // SMC: u baahan FVG - farqi sicir (on/off)
input bool      SMC_Require_Sweep  = false;   // BASELINE: la damiyay   // SMC: u baahan sweep - liquidity la nadiifiyay (on/off)
input bool      SMC_Require_PremDisc= false;   // BASELINE: la damiyay   // SMC: u baahan Premium / Discount (on/off)
input bool      SMC_Require_LTF_CHoCH = false;   // BASELINE: la damiyay   // SMC: u baahan CHoCH timeframe hoose (on/off)
input bool      SMC_Use_HTF_Bias   = true;   // SMC: isticmaal jihada timeframe-ka sare (on/off)
input int       SMC_MinTouches     = 3;   // SMC: immisa jeer heerka la taabtay
input double    OB_Mitigation_Perc = 50.0;   // Order Block: boqolkiiba la buuxiyay (%)
// --- 2) Risk : Reward  (faa'iido vs khasaare) ---
input bool      Enforce_Min_RR     = true;   // Khasab ka dhig RR-ga ugu yar (on/off)
input double    Min_RR_Ratio       = 1.4;   // RR ugu yar - TP waa la kordhiyaa (2.0 = TP labanlaab SL)
input double    SMC_SL_Max_ATR     = 2.5;   // SMC: SL-ga ugu fog (x ATR)
input bool      Use_Liquidity_TP     = true;   // TP ku saleysan liquidity (on/off)
input double    Liquidity_TP_Min_R   = 2.0;   // Liquidity TP: R-ga ugu yar
// --- 3) Xaaladda suuqa  (market filter - ka fogow suuq xun) ---
input bool      Filter2_ADX_Strong   = false;   // ADX filter: xoogga trendka loo baahan yahay (on/off)
input double    Filter2_ADX_MinLevel = 20.0;   // ADX ugu yar (20 = trend caadi, sare = adag)
input bool      Filter_Low_Volatility = false;   // Ha ganacsan suuq aan dhaqaaqayn (on/off)
input double    Min_ATR_Pips       = 5.0;   // ATR ugu yar (pips) - hoos = suuq aamusan
input bool      EnableNewsFilter   = false;   // Jooji ganacsiga waqtiga wararka (on/off)
input int       MaxSpread          = 50;   // Spread ugu badan (points)
// --- 4) Wakhti / Session  (saacadaha ugu fiican) ---
input bool      Enable_Session_Filter     = false;   // Filter-ka session-ka suuqa (on/off)
input bool      Trade_Asian          = false;   // Ganacso session-ka Aasiya (on/off)
input bool      Trade_London         = true;   // Ganacso session-ka London (on/off)
input bool      Trade_NewYork        = true;   // Ganacso session-ka New York (on/off)
input bool      Trade_Overlap_Only   = false;   // Kaliya waqtiga London & New York isku dhacaan (on/off)
input bool      Use_Time_Filter    = false;   // Kaliya ganacso saacadaha aad dooratay (on/off)
input int       Start_Hour         = 8;   // Saacadda bilowga (waqtiga broker-ka)
input int       End_Hour           = 20;   // Saacadda dhammaadka (waqtiga broker-ka)
// --- 5) Tirada trade  (frequency & limits - trade yar oo wanaagsan) ---
input int       MinMinutesBetweenTrades = 0;   // Daqiiqado ugu yar oo u dhexeeya trade-yada
input int       Max_Trades_Per_Day      = 10;   // Trade ugu badan maalintii
input int       Max_Losses_Per_Day      = 4;   // Khasaare ugu badan maalintii
input int       Max_Consecutive_Losses  = 3;   // Khasaare isku xigta oo ugu badan
input int       Max_True_Consecutive_Losses = 0;   // Khasaare dhab ah oo isku xigta (0 = damman)
input int       Max_Open_Trades    = 3;   // Trade furan oo ugu badan
input double    Risk_Percent       = 1.0;   // Khatarta trade kasta (% haraaga)
input bool      Enable_Dynamic_Risk  = false;   // BASELINE: la damiyay   // Yaree khatarta khasaare isku xigta kadib (on/off)
input int       DynRisk_Loss_Trigger = 2;   // Immisa khasaare kadib ayaa khatarta la yareeyaa
input double    DynRisk_Reduced_Pct  = 0.5;   // Khatarta la yareeyay (%)


sinput string   SecG_Trend = "########  XEELADAHA TRENDKA (3): EMA / SMC / VSA  ########";
sinput string   Sec_06 = "======== XEELADDA: EMA ========";
input int       FastEMA            = 50;   // EMA degdeg (fast)
input int       SlowEMA            = 200;   // EMA gaabis (slow)
input double    EMA_SL_Multiplier  = 1.5;   // EMA: SL (x ATR)
input double    EMA_TP_Multiplier  = 3.0;   // EMA: TP (x ATR)
input bool      EMA_Avoid_Squeeze  = true;   // EMA: ka fogow marka labada EMA isku dhow yihiin (on/off)
input double    EMA_Min_Sep_ATR    = 0.3;   // EMA: kala fogaanta ugu yar (x ATR)
input bool      EMA_Require_Retest = true;   // EMA: sug dib-u-dhaca, ha gelin jajabka (on/off)
input int       EMA_Retest_MaxBars = 5;   // EMA: immisa shumac dib-u-dhaca la sugayo
input bool      EMA_Require_Volume = false;   // BASELINE: la damiyay   // EMA: u baahan volume (on/off)

sinput string   Sec_07 = "======== XEELADDA: SMC (Smart Money) ========";
input int       SMC_Lookback       = 100;   // SMC: immisa shumac dib loo eegayo
input double    SMC_StrengthBuffer = 20.0;   // SMC: xoogga ugu yar (buffer)
input double    SMC_SL_Multiplier  = 1.6;   // SMC: SL (x ATR)
input double    SMC_TP_Multiplier  = 3.2;   // SMC: TP (x ATR)
input int       SMC_SwingLen       = 3;   // SMC: dhererka swing-ga
input double    SMC_Equilibrium_Pct= 50.0;   // SMC: dhexda (equilibrium) %
input int       SMC_OB_MaxLookback = 20;   // SMC: Order Block - shumac ugu badan
input ENUM_TIMEFRAMES SMC_HTF_TF   = PERIOD_H4;   // SMC: timeframe-ka sare
input int       SMC_HTF_EMA        = 50;   // SMC: EMA-ga timeframe-ka sare
input bool      SMC_Structural_SL  = true;   // SMC: SL ku saleysan qaab-dhismeedka (on/off)
input ENUM_TIMEFRAMES SMC_LTF        = PERIOD_M5;   // SMC: timeframe-ka hoose

sinput string   Sec_08 = "======== XEELADDA: VSA (Volume Spread Analysis) ========";
input int       VSA_Lookback       = 50;   // VSA: immisa shumac celceliska volume-ka
input double    VSA_VolumeRatio    = 1.6;   // VSA: saamiga volume-ka signal-ka keena (1.6 = 1.6x celcelis)
input int       VSA_MinTouches     = 2;   // VSA: immisa jeer heerka la taabtay
input double    VSA_StrengthBuffer = 10.0;   // VSA: xoogga ugu yar (buffer)
input double    VSA_SL_Multiplier  = 1.4;   // VSA: SL (x ATR)
input double    VSA_TP_Multiplier  = 2.0;   // VSA: TP (x ATR)
// b89: VSA SUPER SCALPER (Gavin Holmes/Tom Williams) - lakab dheeraad ah, DEFAULT OFF (VSA +107% ma bedelin)
input bool      VSA_SuperScalp        = false;   // VSA: signal Super Scalper (on/off) - HA SHIDIN
input bool      VSA_RequireNextBarConfirm = false;   // VSA: sug xaqiijinta shumaca xiga (on/off)
input double    VSA_UltraVolRatio     = 2.0;   // b113: 3.0 aad bay u adag tahay M5   // VSA: saamiga volume-ka ultra
input double    VSA_WideSpreadRatio   = 1.5;   // VSA: saamiga shumaca ballaadhan
input double    VSA_NarrowSpreadRatio = 0.85;  // b113: 0.7 + volume sare = ku dhawaad suurtagal la'aan   // VSA: saamiga shumaca cidhiidhiga
long g_vsaBars=0, g_vsaVolHi=0, g_vsaSprNarrow=0, g_vsaSprWide=0, g_vsaClosePos=0, g_vsaSignals=0;  // b113 diagnostic
input bool      VSA_Diagnostic        = true;  // b113 VSA: tirakoob soo saar dhammaadka tijaabada
input double    VSA_ClosePos_Strong   = 0.60;  // b112 VSA: meesha close-ku ku yaal shumaca (0=hoose 1=sare)
input double    VSA_LowVolRatio       = 0.75;  // b112 VSA: volume hooseeya (No Supply/No Demand)
input bool      VSA_Use_NoSupplyDemand= false; // b112 VSA: shid No Supply / No Demand (daciif - context u baahan)
input bool      VSA_SS_RequireTrend   = true;   // VSA Super Scalp: u baahan trend (on/off)

sinput string   SecG_Rev = "########  XEELADAHA ROGMASHADA (2): SR / BB  ########";
sinput string   Sec_04 = "======== XEELADDA: SR (Support / Resistance) ========";
input ENUM_TIMEFRAMES SR_Zone_TF   = PERIOD_H1;   // SR: timeframe-ka zone-yada
input int       SR_Lookback        = 100;   // SR: immisa shumac dib loo eegayo
input double    SR_ZonePips        = 5.0;   // SR: ballaca zone-ka (pips)
input int       SR_MinTouches      = 3;   // SR: taabasho ugu yar
input int       SR_Exact_Touches   = 3;   // SR: taabasho saxda ah
input double    SR_StrengthBuffer  = 15.0;   // SR: xoogga ugu yar (buffer)
input double    SR_SL_Multiplier   = 1.5;   // SR: SL (x ATR)
input double    SR_TP_Multiplier   = 3.0;   // SR: TP (x ATR)
input bool      SR_Require_Rejection = true;   // SR: u baahan diidmo shumac (rejection) (on/off)
input bool      SR_Require_Volume    = false;   // BASELINE: la damiyay   // SR: u baahan volume (on/off)
input bool      SR_Break_Retest      = false;   // SR: jab + dib-u-tijaabin (on/off)
input int       SR_BreakRetest_MaxBars = 10;   // SR: immisa shumac dib-u-tijaabinta la sugayo

sinput string   Sec_05 = "======== XEELADDA: BB (Bollinger Bands) ========";
input int       BB_Period          = 20;   // BB: muddada
input double    BB_Deviation       = 2.2;   // BB: kala fogaanta (deviation)
input int       BB_MinTouches      = 2;   // BB: taabasho ugu yar
input double    BB_StrengthBuffer  = 10.0;   // BB: xoogga ugu yar (buffer)
input double    BB_SL_Multiplier   = 1.2;   // BB: SL (x ATR)
input double    BB_TP_Multiplier   = 2.5;   // BB: TP (x ATR)
input bool      BB_Require_CloseBack = true;   // BB: shumacu waa inuu gudaha ku soo laabtaa (on/off)
input double    BB_Max_ADX           = 20.0;   // BB: ADX ugu badan - ha ka horyimaadin trend xoog leh

sinput string   Sec_POC = "======== XEELADDA: POC (Volume Profile) ========";
input bool      POC_Static     = true;   // POC: mid go'an (on/off)
input int       POC_Lookback   = 120;   // POC: immisa shumac dib loo eegayo
input int       POC_Bins       = 120;   // POC: tirada qaybaha (bins) - v57: 30 = POC khaldan ilaa nus bin
input double    POC_VA_Percent = 70.0;   // POC: Value Area (%)
input bool      POC_Reversion  = true;   // POC: ku noqo dhexda (on/off)
input double    POC_Buffer_ATR = 0.5;   // POC: buffer (x ATR)
input int       POC_Breakout_Vol_SMA = 20;   // POC: SMA-da volume-ka jabka
input double    POC_SL_ATR     = 0.5;   // POC: SL (x ATR, VAL/VAH hoostooda) - v57

// b82: STRATEGY RSI1H la saaray - parameters-kii waa la qariyay (gudaha, MAAHA input) si aysan uga muuqan liiska inputs-ka MT4
int       RSI_1H_Period      = 14;
int       RSI_1H_Overbought  = 70;
int       RSI_1H_Oversold    = 30;
bool      RSI_1H_M5_Confirm  = true;
bool      RSI_1H_Require_SR  = false;
bool      RSI_1H_Fresh_Only  = true;
int       RSI_1H_Entry_Window = 20;

sinput string   Sec_10 = "======== MAAREYNTA LACAGTA (SL/TP/LOT/KHATAR) ========";
input int       ATR_Period_Core    = 14;   // Muddada ATR-ka aasaasiga ah
input int       Safety_Buffer_Pips = 5;   // Buffer-ka ammaanka (pips)
input bool      Auto_Lot           = true;   // Lot toos ah oo ka yimaada khatarta % (on/off)
input double    InitialLot         = 0.01;   // Lot-ka bilowga (marka Auto Lot damman yahay)
input int       MagicNumber        = 20260407;   // Magic number - aqoonsiga bot-ka
input double    Max_Total_Drawdown_Pct = 10.0;   // Drawdown-ka guud ee ugu badan (%)

sinput string   Sec_11 = "======== MAAREYNTA TRADE-KA (BE / TRAIL / QAYB) ========";
input bool      Enable_Stealth_Mode = false;   // Hab qarsoodi - broker-ku SL/TP ha arkin (on/off)
sinput string   Sec_TPL = "======== TP LADDER (jaranjarada TP) ========";
// b90: FAST BREAK-EVEN - shaqeeya XITAA marka TP-Ladder shidan yahay. Hal mar SL ka saara khasaare-zone (ma la dagaallamo ladder).
input bool      Enable_FastBE      = false;   // FAST BE - kaliya TP Ladder la isticmaalo - HA SHIDIN
// b90: LOOSE MODE - dabci filter-yada guud (Market-Regime + ADX Filter2 + confirmations) si trade dhaqso u shaqeeyaan. DEFAULT OFF (VSA +107% ma bedelin).
input bool      Loose_Entry_Mode   = false;   // HAB DEBECSAN: trade badan, filter yar (on/off)
// b92: SIMPLE MODE - HAB FUDUD oo aad SL/TP go'an adigu maamusho. true = (1) SL/TP = FIXED pips (StopLoss_Pips_Fixed/TakeProfit_Pips_Fixed, ama Control-Panel manual) (2) DAMIN automatic-ga OO DHAN: TP-Ladder/Fast BE/BreakEven/Trailing/ScaleOut/PartialClose/USD-Lock (SL/TP MA taabto entry kadib) (3) dabci trades (sida Loose). Adigaa gacanta ku haya.
input bool      Simple_Mode        = false;   // HAB FUDUD: SL/TP go'an, maamul automatic ah ma jiro (on/off)

sinput string   Sec_12 = "======== FILTER-YADA ========";
int       RSI_M5_Period      = 14;   // b82: qarsoon (gudaha) - RSI-confirm filter dormant (Use_Higher_RSI_Confirm=false)
double    RSI_M5_Overbought  = 70;
double    RSI_M5_Oversold    = 30;
input bool      Enable_Market_Regime = false;   // Kaliya ganacso xaaladda suuqa ee saxda ah (on/off)
input int       Regime_Lookback    = 50;   // Xaaladda suuqa: immisa shumac dib loo eegayo
input int       Filter2_ADX_Period   = 14;   // Muddada ADX-ga filter-ka
input bool      Enable_Dynamic_Spread = false;   // Xadka spread-ka oo is-beddelaya (on/off)
input double    DynSpread_ATR_Mult    = 0.15;   // Spread firfircoon: multiplier ATR
input int       DynSpread_Min_Cap     = 10;   // Spread firfircoon: xadka ugu hooseeya
input int       DynSpread_Max_Cap     = 60;   // Spread firfircoon: xadka ugu sarreeya

sinput string   Sec_13 = "======== SESSION-YADA & WAQTIGA ========";
input bool      One_Trade_Per_H1_Bar = false;   // b111: hal trade saacaddii (H1). false = xannib ma jiro
input bool      AllowMultiplePerBar = true;   // Ogolow trade badan shumac kasta (on/off)
input int       Session_Broker_GMT_Offset = 2;   // Farqiga GMT ee broker-kaaga (saacado)
input bool      Enable_NewBar_Only     = true;   // Kaliya xisaabi signal marka shumac cusub furmo (on/off)

sinput string   Sec_14 = "======== XADKA AMMAANKA ========";
input double    Daily_Loss_Limit_Percent = 3.0;   // Xadka khasaaraha maalinlaha (%)
input bool      Enable_ExtraSafety      = true;   // Ammaan dheeraad ah (on/off)
input double    Daily_Profit_Target_Pct    = 4.0;   // Bartilmaameedka faa'iidada maalinlaha (%)
input double    Weekly_Profit_Target_Pct   = 8.0;   // Bartilmaameedka faa'iidada usbuucle (%)

sinput string   Sec_15 = "======== FILTER-KA WARARKA (NEWS) ========";
input string    News_Calendar_URL  = "https://nfs.faireconomy.media/ff_calendar_thisweek.json";   // URL-ka kalandarka wararka
input int       News_Broker_GMT_Offset = 2;   // Farqiga GMT ee wararka (saacado)
input int       News_Refresh_Minutes = 240;   // Daqiiqado la cusboonaysiiyo wararka
input bool      News_Filter_High   = true;   // Xannib wararka saameyn WEYN leh (on/off)
input bool      News_Filter_Medium = true;   // Xannib wararka saameyn DHEXE leh (on/off)
input bool      News_Filter_Low    = false;   // Xannib wararka saameyn YAR leh (on/off)
input int       MinutesBeforeNews  = 30;   // Daqiiqado warka ka hor oo la joojinayo
input int       MinutesAfterNews   = 30;   // Daqiiqado warka ka dib oo la joojinayo
input bool      News_Block_If_Fetch_Fails = false;   // Xannib haddii wararka la soo dejin waayo (on/off)
// ---- b100: NEWS EXIT + STAGNANT EXIT ----

sinput string   Sec_16 = "======== PROP FIRM ========";
input ENUM_PROP_MODE PropMode      = PROP_NONE;   // Habka Prop Firm (NONE = damman)
input double    Prop_Max_Daily_DD  = 5.0;   // Prop: drawdown maalinle ugu badan (%)
input double    Prop_Max_Total_DD  = 10.0;   // Prop: drawdown guud ugu badan (%)
input double    Prop_Min_Trading_Days = 4;   // Prop: maalmo ganacsi ugu yar
input bool      Prop_No_Weekend    = true;   // Prop: ha ganacsan dhammaadka usbuuca (on/off)
input bool      Prop_No_News       = true;   // Prop: ha ganacsan waqtiga wararka (on/off)
input double    Prop_Consistency_Max = 40.0;   // Prop: xeerka is-waafaqidda ugu badan (%)

sinput string   Sec_17 = "======== FULINTA AMARADA ========";
input bool      Enable_ECN_StopFallback = true;   // ECN: SL/TP kadib dir haddii la diido (on/off)
input ENUM_EXEC_MODE ExecMode      = EXEC_SMART;   // Habka fulinta (SMART / MARKET / LIMIT)
input int       MaxRetries         = 3;   // Isku day mar kale oo ugu badan
input int       RetryDelayMs       = 500;   // Daahitaanka isku dayga (ms)
input int       MaxSlippagePips    = 3;   // Slippage ugu badan (pips)
input bool      UseVirtualOrders   = false;   // Amaro virtual ah - SL/TP xasuusta ku hay (on/off)
input double    Limit_Offset_Pips  = 3.0;   // Limit order: fogaanta (pips)

sinput string   Sec_18 = "======== MAAREYNTA PORTFOLIO ========";
input bool      Enable_PortfolioMgmt = true;   // Maareynta portfolio (on/off)
input double    Max_Portfolio_Risk = 5.0;   // Khatarta guud ee ugu badan (%)
input double    Equity_Protection_Pct = 15.0;   // Ilaalinta equity-ga (%)

sinput string   Sec_19 = "======== XIRIIRKA INTERNET-KA ========";
input bool      Enable_Connection_Guard = true;   // Ilaali xiriirka internet-ka (on/off)
input bool      Enable_Reconnect_Alert  = true;   // Digniin marka xiriirku dib u soo noqdo (on/off)

sinput string   Sec_20 = "======== TELEGRAM ========";
input bool      EnableTelegram     = true;   // Dir digniinaha Telegram (on/off)
input string    TG_BotToken        = "8751505211:AAHnCiLzRG3ceLTMwgUlfCTvusKWouoJxj4";   // Telegram: token-ka bot-ka
input string    TG_ChatID          = "1113150632";   // Telegram: Chat ID
input bool      TG_DailySummary    = true;   // Telegram: soo koobid maalinle (on/off)
input bool      TG_WeeklySummary   = true;   // Telegram: soo koobid usbuucle (on/off)
input bool      TG_DrawdownAlert   = true;   // Telegram: digniin drawdown (on/off)
input double    TG_DrawdownAlertPct = 2.0;   // Telegram: boqolkiiba drawdown-ka digniinta
input bool      TG_TradeDetails    = true;   // Telegram: faahfaahinta trade-ka (on/off)
input bool      TG_ErrorAlerts     = true;   // Telegram: digniin qalad (on/off)
input bool      TG_TradeCloseAlert    = true;   // Telegram: digniin xidhitaanka trade (on/off)
input bool      TG_NewsAlert       = true;   // Telegram: digniin warar (on/off)
input bool      TG_RejectedAlerts  = true;   // Telegram: digniin trade la diiday (on/off)
input int       TG_Reject_Cooldown_Min = 30;   // Telegram: daqiiqado u dhexeeya digniinaha diidmada
input int       TG_Error_Cooldown_Sec  = 300;   // Telegram: ilbiriqsiyo u dhexeeya digniinaha qaladka

sinput string   Sec_21 = "======== DASHBOARD-KA CLOUD-KA ========";
input bool      EnableCloudDashboard = true;   // Dashboard-ka cloud-ka (on/off)
input string    CloudDashboardURL    = "https://mohapro-dashboard-1.onrender.com/update";   // URL-ka dashboard-ka
input string    CloudCommandURL      = "https://mohapro-dashboard-1.onrender.com/api/commands";   // URL-ka amarada
input string    Cloud_Bot_Name       = "MOHA PRO V56";   // Magaca bootka ee dashboard-ka (gaar u ah EA kasta)
input int       Cloud_History_Count  = 100;   // Immisa trade oo xiran ayaa dashboard loo diraa (journal)
input int       Cloud_History_Secs   = 60;    // Immisa sekan kasta journal-ka la cusboonaysiiyo

sinput string   Sec_22 = "======== MUUQAALKA & DASHBOARD-KA ========";
input string    BrandName          = "MOHA PRO ULTIMATE";   // Magaca panel-ka
input string    Watermark_Text     = "MOHA PRO - PROPERTY OF MOHA";   // Qoraalka watermark-ka
input int       Watermark_Size     = 40;   // Cabbirka watermark-ka
input color     Watermark_Color    = C'35,35,35';   // Midabka watermark-ka
input bool      Show_Trade_Signals = true;   // Tus calaamadaha signal-ka (on/off)
input int       Marker_Size        = 2;   // Cabbirka calaamadda
input color     Buy_Marker_Color   = clrLime;   // Midabka calaamadda BUY
input color     Sell_Marker_Color  = clrRed;   // Midabka calaamadda SELL
input color     SR_Line_Color      = clrGray;   // Midabka xariiqda SR
input bool      Enable_Chart_Screenshot = true;   // Qaado sawirka chart-ka (on/off)
input int       Screenshot_Width      = 1024;   // Ballaca sawirka
input int       Screenshot_Height     = 768;   // Dhererka sawirka
input bool      EnableNewDashboard = true;   // Dashboard-ka cusub (on/off)
input bool      Show_Dynamic_Zones = true;   // Tus zone-yada firfircoon (on/off)
input bool      Show_Trend_Lines   = true;   // Tus xariiqaha trendka (on/off)
input bool      SR_Draw_Zones      = true;   // Sawir zone-yada SR (on/off)
input bool      Panel_Show_Selected_Only = true;   // Panel: kaliya tus xeeladda la doortay (on/off)
input bool      Show_Trade_Markers = true;   // Tus calaamadaha trade-ka (on/off)
input bool      Show_CurrencyMeter = false;   // Tus cabbirka xoogga lacagaha (on/off)

sinput string   Sec_23 = "======== DIIWAANKA (JOURNAL) ========";
input bool      EnableJournal      = true;   // Diiwaanka trade-yada CSV (on/off)
input string    Journal_Filename   = "MohaPro_Journal.csv";   // Magaca faylka diiwaanka
input bool      Enable_Debug_Log   = true;   // Diiwaanka debug-ga (on/off)

sinput string   Sec_EMAViz = "======== MUUQAAL: XARIIQAHA EMA + FILTER ENTRY ========";
input bool      Show_EMA_Lines   = true;   // Tus xariiqaha EMA (on/off)
input int       Viz_EMA_Fast     = 50;   // Muuqaal: EMA degdeg
input int       Viz_EMA_Slow     = 200;   // Muuqaal: EMA gaabis
input color     Viz_EMA_Fast_Clr = clrDeepSkyBlue;   // Muuqaal: midabka EMA degdeg
input color     Viz_EMA_Slow_Clr = clrOrange;   // Muuqaal: midabka EMA gaabis
input int       Viz_EMA_Width    = 2;   // Muuqaal: dhumucda xariiqda EMA
input int       Viz_EMA_Bars     = 1000;   // Muuqaal: immisa shumac EMA la sawirayo

//+------------------------------------------------------------------+
//| b18: RETIRED / INTERNAL - laga saaray input panel-ka (lama beddesho)|
//| Qiimahoodu waa go'an; dhaqankii botku waa isku mid (identical).    |
//+------------------------------------------------------------------+
int       Strategy_Trials        = 20;
bool      Enable_Martingale  = false;
double    MrtMultiplier      = 1.2;
int       MaxMrtLevels       = 5;
double    Max_Martingale_Lot_Pct_Balance = 5.0;
int       BreakEvenPips      = 15;   // (V60: lama isticmaalo - hadda R-based)
int       BreakEven_Lock_Pips = 2;   // (V60: lama isticmaalo - hadda R-based)
input bool Use_Zone_Filter_For_Entry = false;  // FILTER MEEL: kaliya gal zone SR (BUY support / SELL resistance) (on/off)
bool      Enable_AI_Patterns     = false;  // nadiifin: pattern score is-dul-saaran
int       AI_Pattern_Min_Score   = 2;
bool      Use_Higher_RSI_Confirm = false;  // nadiifin: RSI-confirm wuu burinayay trend-ka
bool      Enable_Structure_Filter = false;  // nadiifin: SMC engine ayaa structure hubiya
int       Structure_SwingLen      = 3;      // Xoogga swing-ka structure-ka
int       Structure_Lookback      = 100;    // Immisa candle dib loo eego
bool      Structure_Use_ADX       = true;   // Ku dar ADX strength gate
int       Structure_ADX_Period    = 14;
double    Structure_ADX_Min       = 20.0;   // Haddii ADX ka hooseeyo = range (dir=0)
double    Metric_W_Expectancy   = 1.0;      // Miisaanka Expectancy
double    Metric_W_ProfitFactor = 10.0;     // Miisaanka Profit Factor
double    Metric_W_WinRate      = 0.5;      // Miisaanka Win Rate
bool      Enable_AI_MultiFactor = false;  // nadiifin: is-dul-saaran
int       AI_MTF_RSI_Period     = 14;     // RSI-ga timeframe sare (MTF)
bool      Enable_Entry_Confirmation = false; // nadiifin: 3-confluence is-dul-saaran
input int Min_Entry_Confirmations   = 3;   // Xaqiijin ugu yar oo entry loo baahan yahay (1-6)
bool      EnableTrendFilter  = false;  // nadiifin: TrendFollow EMA gate ayaa beddelay
// FIX V56.1: Filter1_MarketRegime waa la saaray - wuxuu ahaa hidden internal flag oo hardcoded=false, taasoo sababaysay in Market Regime filter-ku uusan waligiis shaqeynayn xitaa marka Enable_Market_Regime (input-ka la arko) uu yahay TRUE. Tani ayaa u ogolaatay VSA (iyo xeelado kale) inay ka ganacsadaan suuqa RANGING-ga ah.
bool      Filter3_HTF_Trend    = false; // nadiifin: EMA gate ayaa trend hubiya
bool      Filter4_AvoidMiddle  = false; // nadiifin
ENUM_TIMEFRAMES Filter3_HTF_TF = PERIOD_H4;
int       Filter3_HTF_EMA      = 200;
bool      Filter3_AllowM5Reverse = true;
double    Filter4_ZonePercent  = 15.0;

//+------------------------------------------------------------------+
//| STRUCTS                                                          |
//+------------------------------------------------------------------+
struct StratPerf {
   int    wins, losses, total;
   double winRate, totalPnL, avgRR;
   double grossProfit, grossLoss, profitFactor, expectancy, avgWin, avgLoss;   // QAYBTA 5 FIX (V37)
};
struct TradeRecord {
   long     ticket; string symbol; int type; double lot, openPrice, sl, tp, closePrice, profit, rr;
   datetime openTime, closeTime; string reason, strategy;
   double   entryScore, exitScore;   // QAYBTA 12 FIX (V39)
};
// QAYBTA 12 FIX (V39): store AI entry score per ticket
struct EntryScoreRec { long ticket; int score; };
EntryScoreRec g_EntryScores[];
struct CurrencyStrength { string name; double strength; };
struct DailyPnL { datetime day; double pnl; };
struct VirtualOrderState { long ticket; double vsl; double vtp; bool partialDone; };
struct ScaleOutState { long ticket; bool r1Done; bool r2Done; };
struct TPLadderState { long ticket; int level; double rDist; double origLot; };   // b63: TP-ladder per-trade state

// ---- QAYBTA 1 FIX: News Event struct (dynamic, real-time) ----
struct NewsEvt { string country; string impact; datetime time; string title; };

//+------------------------------------------------------------------+
//| GLOBAL VARIABLES                                                 |
//+------------------------------------------------------------------+
bool     IsAuthorized        = false; bool IsLicenseExpired = false;
double   dailyStartBalance   = 0; double weeklyStartBalance = 0;
datetime lastTradeTime       = 0; datetime lastTradeBarTime = 0;
string   G_Syms[8]           = {"AUD","CAD","CHF","EUR","GBP","JPY","NZD","USD"};
int      Signal_Counts[8];
int      totalWins = 0, totalLosses = 0;
double   winRatePercent = 0; int missedOpportunities = 0;
int      totalSignalsToday = 0; int lastDay = 0;
datetime lastWeekStart = 0; double todayClosedProfit = 0; double weeklyClosedProfit = 0;
double   maxEquityDrawdown = 0; double peakEquity = 0; int propTradingDays = 0;
bool     drawdownAlertSent = false; double totalOpenRisk = 0; int journalTradeCount = 0;
// ---- QAYBTA 14 FIX (V34): Safety counters ----
int      tradesOpenedToday = 0; int lossesToday = 0; int consecutiveLosses = 0;
int      trueConsecutiveLosses = 0;   // b43 FIX: isku-xigxiga khasaaraha ee CROSS-DAY ah - lama reset-garayo maalin kasta (consecutiveLosses maalin kasta wuu reset-garmayaa, taasoo u ogolaan jirtay 3+3+3...=10 khasaare isku xigta)
bool     dailyProfitLocked = false; bool weeklyProfitLocked = false;
int      lastCountedHistoryTotal = 0;
bool     g_wasConnected = true;   // QAYBTA 15 FIX (V39): connection state
bool     g_newsAlertSent = false;   // b52 FIX: news-active Telegram alert - hal mar kaliya ilaa news dhammaato
datetime g_lastRejectAlertTime = 0; // b52 FIX: cooldown ee "trade la diiday" alert-ka
datetime g_lastErrTgAlert = 0;      // b61 FIX: cooldown ee "TRADE-OP FAILED" Telegram alert (ka hortag buuq)
// ---- V42: Modern dashboard palette ----
color MC_GOLD  = C'224,182,77';  color MC_GREEN = C'46,204,113';  color MC_RED = C'224,82,79';
color MC_INFO  = C'88,160,235';  color MC_INK   = C'238,238,245'; color MC_MUT = C'140,140,160';
color MC_BG    = C'18,18,26';     color MC_PANEL = C'24,24,34';    color MC_DIV = C'44,44,60';
color MC_HDR   = C'30,30,44';     color MC_TRACK = C'34,34,46';
CurrencyStrength currStrength[8]; DailyPnL dailyPnLHistory[];
StratPerf stratStats[7];
int   currentActiveStrategy = 0; datetime lastStrategySwitch = 0;
datetime barTime_M1 = 0, barTime_M5 = 0, barTime_H1 = 0, barTime_H4 = 0, barTime_Sync = 0;
datetime lastTradeBarTime_1H = 0;
int      errorCount = 0; datetime lastErrorTime = 0; int consecutiveErrors = 0; int consecutiveWebFails = 0;
ScaleOutState scaleOutStates[];
TPLadderState tpLadder[];   // b63
VirtualOrderState virtualStates[];
int      stratSignalCount[7]; int stratCurrentSignal[7]; datetime lastSignalBar[7];
long     RRR_Tickets[]; string RRR_SL_Names[]; string RRR_TP_Names[];
// ---- V56: ON-CHART CONTROL PANEL runtime overrides (badhamo chart-ka, uma baahnid F7) ----
int      g_SelStrat = -1;    // -1 = Select_Strategy input; 0..6 = badhan la gujiyay
double   g_UserLot  = 0;     // 0 = AUTO (risk %); >0 = fixed lot
int      g_UserSL   = 0;     // 0 = AUTO (ATR/fixed); >0 = SL pips
int      g_UserTP   = 0;     // 0 = AUTO; >0 = TP pips
bool     g_BE_On    = true;  // b99: Break-even master switch (Control Panel button)
bool     g_Trail_On = false; // b99: Trailing master switch (Control Panel button)
bool     g_TPL_On   = false; // b101: TP-Ladder master switch (Control Panel button)
bool     g_TP_On[4];         // b101: TP1/TP2/TP3/TP4 on/off (Control Panel buttons)
void __tpAutoCalc();   // b105: forward declaration
bool     g_TP_Auto  = true;  // b105: TP1-4 si toos ah uga xisaabi SL/TP (Control Panel badhanka)
double   g_TP_R[4];          // b102: TP1..TP4 heerka R - panel +/- badhamo
double   g_SMC_SL   = 0;     // b11: SMC structural SL level (swing invalidation)

// ---- QAYBTA 1 FIX: News Calendar globals ----
NewsEvt  g_NewsEvents[];
datetime g_LastNewsFetch  = 0;
bool     g_NewsDataValid  = false;
int      g_NewsFetchFails = 0;

//+------------------------------------------------------------------+
//| HELPER FUNCTIONS                                                 |
//+------------------------------------------------------------------+
string EnumToStr_Strategy(int s) { string names[7] = {"SR","BB","EMA","SMC","VSA","POC","-"}; if(s>=0&&s<=6) return names[s]; return "---"; }
string EnumToStr_Prop(int mode) { string names[4] = {"OFF","FTMO","MFF","CUSTOM"}; if(mode>=0&&mode<=3) return names[mode]; return "OFF"; }
void LogTradeOpFailure(string opName, long ticket, int err) { errorCount++; consecutiveErrors++; lastErrorTime=TimeCurrent(); string msg="TRADE-OP FAILED ["+opName+"] Ticket="+IntegerToString(ticket)+" Err="+IntegerToString(err); Print(msg); if(TG_ErrorAlerts&&EnableTelegram&&TimeCurrent()-g_lastErrTgAlert>=TG_Error_Cooldown_Sec){ g_lastErrTgAlert=TimeCurrent(); SendTelegram("MOHA PRO: "+msg); } }   // b61 FIX: time-cooldown (halkii consecutiveErrors%5 oo dib u bilaabmaya - buuq keenayay)

//+------------------------------------------------------------------+
//| 4 SMART FILTERS                                                  |
//+------------------------------------------------------------------+
// b21: kala-saar noocyada xeeladaha - TREND (EMA/SMC/VSA) vs REVERSAL (SR/BB/RSI1H)
bool IsTrendStrat(int i){ return (i==STRAT_EMA||i==STRAT_SMC||i==STRAT_VSA); }
bool IsReversalStrat(int i){ return (i==STRAT_SR||i==STRAT_BOLLINGER||i==STRAT_POC); }   // b83: POC = mean-reversion (range)
// b23: xeeladdu ma ku jirtaa kooxda la doortay?
bool InGroup(int i){ if(i>=6) return false; if(Strategy_Group==GRP_TREND) return IsTrendStrat(i); if(Strategy_Group==GRP_REVERSAL) return IsReversalStrat(i); return true; }   // b83: idx 0..5 (POC ku jira)
bool IsMarketCleanForTrading(int signalType, int stratIdx, string &whyOut)
{
   whyOut = "";
   // v57 FIX #4: hore wuxuu KHASBAYAY dhammaan xeeladaha inay TRENDING ku ganacsadaan.
   // POC/SR/BB waa mean-reversion - taasi waxay ka dhigaysay inay trend la dagaallamaan.
   if(!Loose_Entry_Mode && !Simple_Mode && Enable_Market_Regime) {
      int regime = GetMarketRegime();
      if(IsReversalStrat(stratIdx)) {
         if(regime == 1) { whyOut="Reversal (SR/BB/POC): suuqu waa TRENDING - ha fade-gareyn"; return false; }
      } else {
         if(regime != 1) { whyOut="Trend (EMA/SMC/VSA): suuqu waa RANGING (aan trend ahayn)"; return false; }
      }
   }
   if(!Loose_Entry_Mode && !Simple_Mode && Filter2_ADX_Strong) {   // b90/b92: Loose/Simple -> ka bood ADX Filter2
      double adx = m4iADX(Symbol(), 0, Filter2_ADX_Period, PRICE_CLOSE, MODE_MAIN, 1);
      if(IsReversalStrat(stratIdx)) { if(adx >= Filter2_ADX_MinLevel*2.0) { whyOut="ADX aad u sareeya reversal-ka ("+DoubleToString(adx,1)+")"; return false; } }   // b21 REVERSAL xeer: ha fade-gareyn trend aad u xoog badan (ADX aad u sarreeya)
      else                          { if(adx <  Filter2_ADX_MinLevel)     { whyOut="ADX aad u hooseeya ("+DoubleToString(adx,1)+" < "+DoubleToString(Filter2_ADX_MinLevel,1)+")"; return false; } }   // b21 TREND xeer: waxaa loo baahan yahay trend xoog leh
   }
   if(Filter3_HTF_Trend) {
      double htfEma = m4iMA(Symbol(), Filter3_HTF_TF, Filter3_HTF_EMA, 0, MODE_EMA, PRICE_CLOSE, 1);
      if(htfEma <= 0) { whyOut="Filter3 HTF EMA xog la'aan"; return false; }
      if(!Filter3_AllowM5Reverse) { if(signalType == OP_BUY && Close[1] < htfEma) { whyOut="Filter3: BUY laakiin qiimaha EMA hoosteeda"; return false; } if(signalType == OP_SELL && Close[1] > htfEma) { whyOut="Filter3: SELL laakiin qiimaha EMA kore"; return false; } }
      else { double m5Ema = m4iMA(Symbol(), PERIOD_M5, 50, 0, MODE_EMA, PRICE_CLOSE, 1); if(m5Ema > 0) { if(signalType == OP_BUY && Close[1] < htfEma && Close[1] < m5Ema) { whyOut="Filter3 M5reverse: BUY diiday"; return false; } if(signalType == OP_SELL && Close[1] > htfEma && Close[1] > m5Ema) { whyOut="Filter3 M5reverse: SELL diiday"; return false; } } }
   }
   if(Filter4_AvoidMiddle) {
      double bbUpper = m4iBands(Symbol(), 0, 20, 2.0, 0, PRICE_CLOSE, MODE_UPPER, 1);
      double bbLower = m4iBands(Symbol(), 0, 20, 2.0, 0, PRICE_CLOSE, MODE_LOWER, 1);
      if(bbUpper > 0 && bbLower > 0 && bbUpper > bbLower) {
         double range = bbUpper - bbLower; double middle = (bbUpper + bbLower) / 2.0;
         double zoneHalf = range * (Filter4_ZonePercent / 100.0) / 2.0;
         if(MathAbs(Close[1] - middle) < zoneHalf) { whyOut="Filter4: qiimaha dhexda BB dhexdiisa ku jira"; return false; }
      }
   }
   return true;
}
int MTF_TF_Dir(ENUM_TIMEFRAMES tf){   // b49 (Qodob 8): 1=bull, -1=bear, 0=neutral/unavailable (aan xannibin)
   double e=m4iMA(Symbol(),tf,MTF_EMA_Period,0,MODE_EMA,PRICE_CLOSE,1);
   double c=m4iClose(Symbol(),tf,1);
   if(e<=0||c<=0) return 0;
   if(c>e) return 1;
   if(c<e) return -1;
   return 0;
}
bool MTF_ConfirmationPassed(int sig){   // b49 (Qodob 8): waterfall H4(Trend)->H1(Direction)->M15(Setup) - dhammaan waa inay isku raacaan jihada signal-ka
   if(!Enable_MTF_Confirm) return true;
   if(sig!=OP_BUY && sig!=OP_SELL) return true;
   int want=(sig==OP_BUY)?1:-1;
   if(MTF_Require_H4)  { int d4=MTF_TF_Dir(PERIOD_H4);  if(d4!=0  && d4!=want)  return false; }
   if(MTF_Require_H1)  { int d1=MTF_TF_Dir(PERIOD_H1);  if(d1!=0  && d1!=want)  return false; }
   if(MTF_Require_M15) { int d15=MTF_TF_Dir(PERIOD_M15);if(d15!=0 && d15!=want) return false; }
   return true;
}
bool Correlation_SymbolInGroup(string sym, string group){   // b50 (Qodob 9)
   string parts[]; int n=StringSplit(group,',',parts);
   for(int i=0;i<n;i++){ string p=parts[i]; StringTrimLeft(p); StringTrimRight(p); if(p==sym) return true; }
   return false;
}
bool Correlation_FindGroup(string sym, string &outGroup){   // b50 (Qodob 9)
   string groups[]; int gn=StringSplit(Correlation_Groups,'|',groups);
   for(int i=0;i<gn;i++){ if(Correlation_SymbolInGroup(sym,groups[i])){ outGroup=groups[i]; return true; } }
   return false;
}
bool CorrelationFilterPassed(int sig){   // b50 FIX (Qodob 9): xannib xad-dhaaf lammaanayaal isku xidhan (dhammaan symbols-ka account-ka, maaha kaliya chart-kan)
   if(!Enable_Correlation_Filter) return true;
   if(sig!=OP_BUY && sig!=OP_SELL) return true;
   string grp; if(!Correlation_FindGroup(Symbol(),grp)) return true;   // symbol-kan koox lama qeexin - filter-ku ma khusayso
   int cnt=0;
   for(int i=m4OrdersTotal()-1;i>=0;i--){
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(m4OrderType()!=OP_BUY && m4OrderType()!=OP_SELL) continue;
      if(!Correlation_SymbolInGroup(m4OrderSymbol(),grp)) continue;
      if(m4OrderType()==sig) cnt++;
   }
   if(cnt>=Max_Correlated_Same_Dir) return false;
   return true;
}
bool IsNewBar(ENUM_TIMEFRAMES tf) {
   datetime cur = m4iTime(Symbol(), tf, 0); if(cur <= 0) return false;
   if(tf == PERIOD_M1) { if(cur == barTime_M1) return false; barTime_M1 = cur; return true; }
   if(tf == PERIOD_M5) { if(cur == barTime_M5) return false; barTime_M5 = cur; return true; }
   if(tf == PERIOD_H1) { if(cur == barTime_H1) return false; barTime_H1 = cur; return true; }
   if(tf == PERIOD_H4) { if(cur == barTime_H4) return false; barTime_H4 = cur; return true; }
   if(cur == barTime_Sync) return false; barTime_Sync = cur; return true;
}
bool ValidateLogin() {
   if(License_Key != "MOHA-PRO-V26-ULTIMATE") { Alert("LICENSE KHALAD: Key saxan ma ahan!"); return false; }
   if(Enable_AccountLock && Licensed_Account > 0) { if((int)AccountNumber() != Licensed_Account) { Alert("LICENSE: Account number " + IntegerToString((int)AccountNumber()) + " u ogolaan la'yahay!"); return false; } }
   if(StringLen(Expiry_Date) >= 10) { datetime expiry = StringToTime(Expiry_Date + " 23:59:59"); if(expiry > 0 && TimeCurrent() > expiry) { IsLicenseExpired = true; Alert("LICENSE DHAMMAATAY: " + Expiry_Date); return false; } }
   if(Cloud_Auth_Token == "CHANGE_ME_LONG_RANDOM_SECRET" && EnableCloudDashboard) Alert("SECURITY: Cloud_Auth_Token weli waa default-ka.");
   return true;
}
double GetPipSize(string sym) { double p = MarketInfo(sym, MODE_POINT); int d = (int)MarketInfo(sym, MODE_DIGITS); if(d == 2 || d == 3 || d == 5) return p * 10; return p; }

//==================================================================
//  b106: EMA200 BIG-TREND FILTER (H1 + H4)
//  Sicirka hadda vs EMA200 ee bar-ka dhammaaday (shift 1 = repaint la'aan)
//   1 = sicirku EMA200 KA KOR yahay  -> BUY  la ogolyahay
//  -1 = sicirku EMA200 KA HOOS yahay -> SELL la ogolyahay
//   0 = xog la'aan / buffer dhexdiisa (neutral)
//==================================================================
int HTF_EMA200_Dir(ENUM_TIMEFRAMES tf){
   double ema = m4iMA(Symbol(), tf, HTF_EMA200_Period, 0, MODE_EMA, PRICE_CLOSE, 1);
   double px  = Bid;
   if(ema <= 0.0 || px <= 0.0) return 0;
   double buf = (HTF_EMA200_Buffer_Pips > 0.0) ? HTF_EMA200_Buffer_Pips * GetPipSize(Symbol()) : 0.0;
   if(px > ema + buf) return  1;
   if(px < ema - buf) return -1;
   return 0;   // buffer-ka dhexdiisa = neutral
}
bool HTF_EMA200_Passed(int sig, string &why){
   why = "";
   if(!Enable_HTF_EMA200) return true;
   if(sig != OP_BUY && sig != OP_SELL) return true;
   int want = (sig == OP_BUY) ? 1 : -1;

   if(HTF_EMA200_Require_H1){
      int d1 = HTF_EMA200_Dir(PERIOD_H1);
      if(d1 == 0){ if(HTF_EMA200_Block_If_NA){ why = "H1 EMA200: xog la'aan / neutral"; return false; } }
      else if(d1 != want){ why = (sig==OP_BUY) ? "H1: sicirku EMA200 hoostiisa" : "H1: sicirku EMA200 kor"; return false; }
   }
   if(HTF_EMA200_Require_H4){
      int d4 = HTF_EMA200_Dir(PERIOD_H4);
      if(d4 == 0){ if(HTF_EMA200_Block_If_NA){ why = "H4 EMA200: xog la'aan / neutral"; return false; } }
      else if(d4 != want){ why = (sig==OP_BUY) ? "H4: sicirku EMA200 hoostiisa" : "H4: sicirku EMA200 kor"; return false; }
   }
   return true;
}
bool IsTradingTime() { if(!Use_Time_Filter) return true; int h = Hour(), dow = DayOfWeek(); if(dow == 0 || dow == 6) return false; if(PropMode != PROP_NONE && Prop_No_Weekend && dow == 5 && h >= 20) return false; if(Start_Hour < End_Hour) return (h >= Start_Hour && h < End_Hour); if(Start_Hour > End_Hour) return (h >= Start_Hour || h < End_Hour); return true; }
// QAYBTA 11 FIX (V39): current session name for dashboard
string GetSessionName(){ int g=(Hour()-Session_Broker_GMT_Offset+24)%24; if(g>=13&&g<17) return "LDN/NY OVERLAP"; if(g>=8&&g<17) return "LONDON"; if(g>=13&&g<22) return "NEW YORK"; if(g>=0&&g<9) return "ASIAN"; return "OFF-HOURS"; }
// ---- QAYBTA 7 FIX (V35): Session filter (Asian/London/NY/Overlap in GMT) ----
bool InSessionWindow(){
   if(!Enable_Session_Filter) return true;
   int g = (Hour() - Session_Broker_GMT_Offset + 24) % 24;   // GMT hour
   bool asian   = (g>=0  && g<9);    // Tokyo  ~00-09 GMT
   bool london  = (g>=8  && g<17);   // London ~08-17 GMT
   bool newyork = (g>=13 && g<22);   // NY     ~13-22 GMT
   bool overlap = (g>=13 && g<17);   // London/NY overlap
   if(Trade_Overlap_Only) return overlap;
   bool ok=false;
   if(Trade_Asian   && asian)   ok=true;
   if(Trade_London  && london)  ok=true;
   if(Trade_NewYork && newyork) ok=true;
   return ok;
}
int CountByMagicRange(int s, int e) { int c=0; for(int i=0;i<m4OrdersTotal();i++) if(m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) if(m4OrderSymbol()==Symbol() && m4OrderMagicNumber()>=s && m4OrderMagicNumber()<=e) c++; return c; }
int CountAllOrders() { int c=0; for(int i=0;i<m4OrdersTotal();i++) if(m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES) && m4OrderSymbol()==Symbol()) c++; return c; }
int GetMagicForStrategy(int idx) { return MagicNumber + 100 + idx; }
// ---- QAYBTA 8 FIX (V36): dynamic max-spread based on ATR volatility ----
int GetMaxAllowedSpread(){
   if(!Enable_Dynamic_Spread) return MaxSpread;
   double _p=GetPipSize(Symbol());
   double atr=m4iATR(Symbol(),0,ATR_Period_Core,1);
   if(atr<=0 || atr>100000 || _p<=0) return MaxSpread;
   double atrPips  = atr/_p;
   double allowPts = atrPips * DynSpread_ATR_Mult * (_p/Point);   // pips -> points
   int pts = (int)MathRound(allowPts);
   if(pts < DynSpread_Min_Cap) pts = DynSpread_Min_Cap;
   if(pts > DynSpread_Max_Cap) pts = DynSpread_Max_Cap;
   return pts;
}
int GetMarketRegime() {
   // FIX V56.1b: default-ka hore ee "dhexdhexaad" (ADX u dhexeeya 20-25) wuxuu ahaa TRENDING (1) - taasi
   // ayaa u ogolaanaysay in ganacsi laga qaato zone-yo choppy/ranging ah (sida sawirka la soo diray).
   // Hadda default-ku waa RANGING (0) - haddii aan si cad loo xaqiijin karin in trend xoog leh jiro, laguma ganacsan doono.
   if(!Enable_Market_Regime) return 1;
   if(Bars < Regime_Lookback + 5) return -1;
   double adx  = m4iADX(Symbol(),0,14,PRICE_CLOSE,MODE_MAIN,1);
   double atr1 = m4iATR(Symbol(),0,ATR_Period_Core,1);
   double atr5 = m4iATR(Symbol(),0,ATR_Period_Core,10);
   bool chartTrending = (adx>25 && atr1>atr5*0.9);
   bool chartRanging  = (adx<20 && !(atr1>atr5*0.9));
   if(chartRanging) return 0;
   if(!chartTrending) return 0;   // FIX: dhexdhexaad = RANGING hadda (horay waxay ahayd TRENDING)
   // FIX V56.1b: xaqiiji H1-ADX sidoo kale - si aan loo qaadan ganacsi trend gaaban oo M1/M5 ku dhacay
   // halka H1-ka guud ahaan uu weli yahay ranging/choppy
   double adxH1 = m4iADX(Symbol(),PERIOD_H1,14,PRICE_CLOSE,MODE_MAIN,1);
   if(adxH1>0 && adxH1<18) return 0;
   return 1;
}

//+------------------------------------------------------------------+
//| AI Candlestick Pattern Engine                                    |
//+------------------------------------------------------------------+
struct PatternResult { int signal; int score; string name; };
bool IsPinBar(int idx, int type) { double o=Open[idx], h=High[idx], l=Low[idx], c=Close[idx]; double body=MathAbs(c-o), range=h-l; if(range<Point) return false; double upperWick=h-MathMax(o,c), lowerWick=MathMin(o,c)-l; if(type==OP_BUY) return (lowerWick>body*2.0 && lowerWick>upperWick*2.0 && body<range*0.35); if(type==OP_SELL) return (upperWick>body*2.0 && upperWick>lowerWick*2.0 && body<range*0.35); return false; }
bool IsEngulfing(int idx, int type) { if(idx+1>=Bars) return false; double o0=Open[idx],c0=Close[idx],o1=Open[idx+1],c1=Close[idx+1]; if(type==OP_BUY) return (c1<o1 && c0>o0 && c0>o1 && o0<c1); if(type==OP_SELL) return (c1>o1 && c0<o0 && c0<o1 && o0>c1); return false; }
bool IsDoji(int idx) { double o=Open[idx],h=High[idx],l=Low[idx],c=Close[idx]; double body=MathAbs(c-o), range=h-l; if(range<Point) return false; return (body < range*0.1); }
bool IsInsideBar(int idx) { if(idx+1>=Bars) return false; return (High[idx]<High[idx+1] && Low[idx]>Low[idx+1]); }
bool IsMorningStar(int idx, int type) { if(idx+2>=Bars) return false; double b0=MathAbs(Close[idx]-Open[idx]), b1=MathAbs(Close[idx+1]-Open[idx+1]), b2=MathAbs(Close[idx+2]-Open[idx+2]); bool star=(b1<b2*0.3); if(type==OP_BUY) return (Close[idx+2]<Open[idx+2] && star && Close[idx]>Open[idx] && Close[idx]>(Open[idx+2]+Close[idx+2])/2.0); if(type==OP_SELL) return (Close[idx+2]>Open[idx+2] && star && Close[idx]<Open[idx] && Close[idx]<(Open[idx+2]+Close[idx+2])/2.0); return false; }
bool IsHammer(int idx, int type) { double o=Open[idx],h=High[idx],l=Low[idx],c=Close[idx]; double body=MathAbs(c-o), range=h-l; double lw=MathMin(o,c)-l, uw=h-MathMax(o,c); if(range<Point) return false; if(type==OP_BUY) return (lw>body*2.5 && uw<body*0.5 && body<range*0.4); if(type==OP_SELL) return (uw>body*2.5 && lw<body*0.5 && body<range*0.4); return false; }
// QAYBTA 3 FIX (V38): AI score = candlesticks + trend + momentum + volume + volatility + structure + MTF
PatternResult GetAIPatternScore(int signalType) {
   PatternResult res; res.signal=signalType; res.score=0; res.name="";
   if(!Enable_AI_Patterns) { res.score=99; return res; }
   string patterns="";
   // --- Candlestick patterns ---
   if(IsPinBar(1,signalType))     { res.score+=3; patterns+="PinBar+"; }
   if(IsEngulfing(1,signalType))  { res.score+=3; patterns+="Engulf+"; }
   if(IsHammer(1,signalType))     { res.score+=2; patterns+="Hammer+"; }
   if(IsMorningStar(1,signalType)){ res.score+=3; patterns+="Star+"; }
   if(IsDoji(2))                  { res.score+=1; patterns+="Doji+"; }
   if(IsInsideBar(1))             { res.score+=1; patterns+="IB+"; }
   // --- Momentum (RSI extreme) ---
   double rsi = m4iRSI(Symbol(),0,RSI_1H_Period,PRICE_CLOSE,1);
   if(signalType==OP_BUY  && rsi<45) { res.score+=1; patterns+="RSI_OV+"; }
   if(signalType==OP_SELL && rsi>55) { res.score+=1; patterns+="RSI_OB+"; }
   // --- Volume ---
   long v1=m4iVolume(Symbol(),0,1); long v3L=(m4iVolume(Symbol(),0,2)+m4iVolume(Symbol(),0,3)+m4iVolume(Symbol(),0,4)); double v3=(double)v3L/3.0;
   if(v3>0 && v1>v3*1.3) { res.score+=1; patterns+="HiVol+"; }
   // --- QAYBTA 3 FIX (V38): multi-factor confluence ---
   if(Enable_AI_MultiFactor){
      double e50=m4iMA(Symbol(),0,50,0,MODE_EMA,PRICE_CLOSE,1), e200=m4iMA(Symbol(),0,200,0,MODE_EMA,PRICE_CLOSE,1);
      if(e50>0 && e200>0){ if(signalType==OP_BUY && e50>e200){ res.score+=2; patterns+="Trend+"; } if(signalType==OP_SELL && e50<e200){ res.score+=2; patterns+="Trend+"; } }
      int st=GetStructureTrend();
      if(signalType==OP_BUY  && st==1) { res.score+=2; patterns+="Struct+"; }
      if(signalType==OP_SELL && st==-1){ res.score+=2; patterns+="Struct+"; }
      double rsiPrev=m4iRSI(Symbol(),0,RSI_1H_Period,PRICE_CLOSE,2);
      if(signalType==OP_BUY  && rsi>rsiPrev){ res.score+=1; patterns+="Mom+"; }
      if(signalType==OP_SELL && rsi<rsiPrev){ res.score+=1; patterns+="Mom+"; }
      double atrNow=m4iATR(Symbol(),0,ATR_Period_Core,1), atrOld=m4iATR(Symbol(),0,ATR_Period_Core,10);
      if(atrNow>0 && atrOld>0 && atrNow>atrOld){ res.score+=1; patterns+="Volat+"; }
      ENUM_TIMEFRAMES htf=(Trade_Timeframe==TF_M1)?PERIOD_M5:PERIOD_H1;
      double htfRsi=m4iRSI(Symbol(),htf,AI_MTF_RSI_Period,PRICE_CLOSE,1);
      if(signalType==OP_BUY  && htfRsi<50){ res.score+=2; patterns+="MTF+"; }
      if(signalType==OP_SELL && htfRsi>50){ res.score+=2; patterns+="MTF+"; }
   }
   if(StringLen(patterns)>0) res.name=StringSubstr(patterns,0,StringLen(patterns)-1);
   return res;
}
// QAYBTA 6 FIX (V38): count independent entry confirmations (0-6)
int CountEntryConfirmations(int type){
   int c=0;
   double e50=m4iMA(Symbol(),0,50,0,MODE_EMA,PRICE_CLOSE,1), e200=m4iMA(Symbol(),0,200,0,MODE_EMA,PRICE_CLOSE,1);
   if(e50>0 && e200>0){ if(type==OP_BUY && e50>e200) c++; if(type==OP_SELL && e50<e200) c++; }
   int st=GetStructureTrend(); if((type==OP_BUY && st==1)||(type==OP_SELL && st==-1)) c++;
   if(IsPinBar(1,type)||IsEngulfing(1,type)||IsHammer(1,type)||IsMorningStar(1,type)) c++;
   double rsi=m4iRSI(Symbol(),0,RSI_1H_Period,PRICE_CLOSE,1);
   if((type==OP_BUY && rsi<45)||(type==OP_SELL && rsi>55)) c++;
   long v1=m4iVolume(Symbol(),0,1); double v3=(double)(m4iVolume(Symbol(),0,2)+m4iVolume(Symbol(),0,3)+m4iVolume(Symbol(),0,4))/3.0;
   if(v3>0 && v1>v3*1.3) c++;
   ENUM_TIMEFRAMES htf=(Trade_Timeframe==TF_M1)?PERIOD_M5:PERIOD_H1;
   double htfRsi=m4iRSI(Symbol(),htf,RSI_M5_Period,PRICE_CLOSE,1);
   if((type==OP_BUY && htfRsi<50)||(type==OP_SELL && htfRsi>50)) c++;
   return c;
}

//+------------------------------------------------------------------+
//| Currency Strength Meter                                          |
//+------------------------------------------------------------------+
void UpdateCurrencyStrength() {
   if(IsTesting()) return;   // b13: ha soo dejin 8 pairs tester-ka (backtest degdeg badan; panel horeba la saaray)
   string bases[8]={"AUD","CAD","CHF","EUR","GBP","JPY","NZD","USD"}; string pairs[8]={"AUDUSD","USDCAD","USDCHF","EURUSD","GBPUSD","USDJPY","NZDUSD","EURUSD"}; bool inv[8]={false,true,true,false,false,true,false,true};
   for(int i=0;i<8;i++) { double r=m4iRSI(pairs[i],PERIOD_H1,14,PRICE_CLOSE,1); if(r<=0 || r>=100) r=50; double s=inv[i]?(100.0-r):r; currStrength[i].name=bases[i]; currStrength[i].strength=s; }
   for(int i=0;i<7;i++) for(int j=0;j<7-i;j++) if(currStrength[j].strength<currStrength[j+1].strength) { CurrencyStrength tmp=currStrength[j]; currStrength[j]=currStrength[j+1]; currStrength[j+1]=tmp; }
}
double GetCurrencyStrength(string ccy){ for(int i=0;i<8;i++) if(currStrength[i].name==ccy) return currStrength[i].strength; return 50.0; }

//+------------------------------------------------------------------+
//| Prop Firm Checks                                                 |
//+------------------------------------------------------------------+
void RecordDailyPnLForConsistency(double pnlDelta) { datetime today=StringToTime(TimeToString(TimeCurrent(),TIME_DATE)); int n=ArraySize(dailyPnLHistory); if(n>0 && dailyPnLHistory[n-1].day==today) { dailyPnLHistory[n-1].pnl+=pnlDelta; } else { ArrayResize(dailyPnLHistory,n+1); dailyPnLHistory[n].day=today; dailyPnLHistory[n].pnl=pnlDelta; } }
bool BreachesConsistencyRule() { int n=ArraySize(dailyPnLHistory); if(n<2) return false; double total=0,best=-1e18; for(int i=0;i<n;i++){ total+=dailyPnLHistory[i].pnl; if(dailyPnLHistory[i].pnl>best) best=dailyPnLHistory[i].pnl; } if(total<=0) return false; double share=(best/total)*100.0; return (share>Prop_Consistency_Max); }
bool PropFirmCheck() { if(PropMode==PROP_NONE) return true; double bal=AccountBalance(), eq=AccountEquity(); if(bal<=0) return true; double dailyDD=(dailyStartBalance>0)?(dailyStartBalance-eq)/dailyStartBalance*100.0:0; if(dailyDD>=Prop_Max_Daily_DD){ ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"PROP: DAILY DD LIMIT!"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrRed); if(EnableTelegram) SendTelegram("PROP: Daily DD "+DoubleToString(dailyDD,2)+"% - Paused"); return false; } double totalDD=(peakEquity>0)?(peakEquity-eq)/peakEquity*100.0:0; if(totalDD>=Prop_Max_Total_DD){ ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"PROP: MAX TOTAL DD!"); if(EnableTelegram) SendTelegram("PROP: Total DD "+DoubleToString(totalDD,2)+"% - Stopped"); return false; } if(Prop_No_Weekend && DayOfWeek()==5 && Hour()>=20){ CloseAllTrades("Prop: Weekend close"); return false; } if(Prop_No_News && IsNewsActive()){ ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"PROP: NEWS BLACKOUT"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrOrange); return false; } if(BreachesConsistencyRule()){ ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"PROP: CONSISTENCY BREACH"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrOrange); if(EnableTelegram) SendTelegram("PROP: Consistency rule breached."); return false; } return true; }
// ---- QAYBTA 14 FIX (V34): Advanced safety gate ----
bool SafetyChecksPassed(){
   if(!Enable_ExtraSafety) return true;
   if(Enable_Weekly_Profit_Lock && weeklyStartBalance>0){
      double wkPct=(AccountBalance()-weeklyStartBalance)/weeklyStartBalance*100.0;
      if(wkPct>=Weekly_Profit_Target_Pct && !weeklyProfitLocked){ weeklyProfitLocked=true; if(EnableTelegram) SendTelegram("SAFETY: Weekly profit "+DoubleToString(wkPct,2)+"% - trading locked (week)."); }
   }
   if(weeklyProfitLocked){ ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"WEEKLY PROFIT LOCK"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrLime); return false; }
   if(Enable_Daily_Profit_Lock && dailyStartBalance>0){
      double dyPct=(AccountBalance()-dailyStartBalance)/dailyStartBalance*100.0;
      if(dyPct>=Daily_Profit_Target_Pct && !dailyProfitLocked){ dailyProfitLocked=true; if(EnableTelegram) SendTelegram("SAFETY: Daily profit "+DoubleToString(dyPct,2)+"% - trading locked (today)."); }
   }
   if(dailyProfitLocked){ ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"DAILY PROFIT LOCK"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrLime); return false; }
   if(Max_Trades_Per_Day>0 && tradesOpenedToday>=Max_Trades_Per_Day){ ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"MAX TRADES/DAY"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrOrange); return false; }
   if(Max_Losses_Per_Day>0 && lossesToday>=Max_Losses_Per_Day){ ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"MAX LOSSES/DAY"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrRed); return false; }
   if(Max_Consecutive_Losses>0 && consecutiveLosses>=Max_Consecutive_Losses){ ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"CONSEC LOSS STOP ("+IntegerToString(consecutiveLosses)+")"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrRed); return false; }
   if(Max_True_Consecutive_Losses>0 && trueConsecutiveLosses>=Max_True_Consecutive_Losses){ ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"CROSS-DAY LOSS STOP ("+IntegerToString(trueConsecutiveLosses)+")"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrRed); return false; }   // b43 FIX: xayiraad aan maalin kasta dib u bilaabmayn (ka duwan Max_Consecutive_Losses)
   return true;
}
void CheckPropMinTradingDaysWarning(){ if(PropMode==PROP_NONE) return; if(propTradingDays<(int)Prop_Min_Trading_Days) ObjectSetString(0,"Moha_Prop",OBJPROP_TEXT,"Prop: "+EnumToStr_Prop((int)PropMode)+" (Days "+IntegerToString(propTradingDays)+"/"+IntegerToString((int)Prop_Min_Trading_Days)+")"); }
void CloseAllTrades(string reason){ long tickets[]; int cnt=0; for(int i=0;i<m4OrdersTotal();i++){ if(m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES) && m4OrderSymbol()==Symbol()){ int mg=m4OrderMagicNumber(); if(mg>=MagicNumber+100 && mg<=MagicNumber+106){ ArrayResize(tickets,cnt+1); tickets[cnt++]=m4OrderTicket(); } } } for(int i=0;i<cnt;i++){ if(!m4OrderSelect(tickets[i],SELECT_BY_TICKET)) continue; double cp=(m4OrderType()==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK); bool closed=m4OrderClose(tickets[i],m4OrderLots(),cp,3,clrWhite); if(!closed) LogTradeOpFailure("CloseAllTrades",tickets[i],GetLastError()); } if(cnt>0) Print("Closed ",cnt," trades | Reason: ",reason); }
bool PortfolioRiskCheck(double newLot){ if(!Enable_PortfolioMgmt) return true; double bal=AccountBalance(); if(bal<=0) return true; double eqPct=(AccountEquity()/bal)*100.0; if(eqPct < (100.0 - Equity_Protection_Pct)) { CloseAllTrades("Equity Protection"); return false; } double totalRisk=0; for(int i=0;i<m4OrdersTotal();i++){ if(!m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue; if(m4OrderStopLoss()<=0) continue; double _p=GetPipSize(m4OrderSymbol()), slD=MathAbs(m4OrderOpenPrice()-m4OrderStopLoss()); double tv=MarketInfo(m4OrderSymbol(),MODE_TICKVALUE), ts=MarketInfo(m4OrderSymbol(),MODE_TICKSIZE); if(ts>0 && _p>0) totalRisk += (slD/ts)*tv*m4OrderLots(); } double newSL=StopLoss_Pips_Fixed*GetPipSize(Symbol()); double nTV=MarketInfo(Symbol(),MODE_TICKVALUE), nTS=MarketInfo(Symbol(),MODE_TICKSIZE); if(nTS>0) totalRisk += (newSL/nTS)*nTV*newLot; double riskPct=(totalRisk/bal)*100.0; if(riskPct>Max_Portfolio_Risk) { Print("Portfolio risk exceeds max"); return false; } return true; }
// QAYBTA 12 FIX (V39): entry-score storage per ticket
void StoreEntryScore(long ticket,int score){ for(int i=0;i<ArraySize(g_EntryScores);i++) if(g_EntryScores[i].ticket==ticket){ g_EntryScores[i].score=score; return; } int sz=ArraySize(g_EntryScores); ArrayResize(g_EntryScores,sz+1); g_EntryScores[sz].ticket=ticket; g_EntryScores[sz].score=score; }
int GetEntryScore(long ticket){ for(int i=0;i<ArraySize(g_EntryScores);i++) if(g_EntryScores[i].ticket==ticket) return g_EntryScores[i].score; return 0; }
// QAYBTA 13 FIX (V39): save chart screenshot to MQL4/Files
void SaveTradeScreenshot(string tag){ if(!Enable_Chart_Screenshot) return; string fn="MohaPro_"+tag+"_"+Symbol()+"_"+IntegerToString((int)TimeCurrent())+".gif"; ChartScreenShot(0,fn,Screenshot_Width,Screenshot_Height); }
void JournalWriteHeader(){ if(!EnableJournal) return; int h=FileOpen(Journal_Filename,FILE_WRITE|FILE_CSV|FILE_ANSI,','); if(h==INVALID_HANDLE){ Print("JOURNAL ERROR"); return; } FileWrite(h,"Ticket","Symbol","Type","Lot","OpenTime","CloseTime","OpenPrice","ClosePrice","SL","TP","Profit","RR","Strategy","Reason","EntryScore","ExitScore_R"); FileClose(h); }
void JournalWrite(TradeRecord &tr){ if(!EnableJournal) return; int h=FileOpen(Journal_Filename,FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI,','); if(h==INVALID_HANDLE){ Print("JOURNAL ERROR"); return; } FileSeek(h,0,SEEK_END); FileWrite(h,IntegerToString(tr.ticket),tr.symbol,(tr.type==OP_BUY?"BUY":"SELL"),DoubleToString(tr.lot,2),TimeToString(tr.openTime),TimeToString(tr.closeTime),DoubleToString(tr.openPrice,5),DoubleToString(tr.closePrice,5),DoubleToString(tr.sl,5),DoubleToString(tr.tp,5),DoubleToString(tr.profit,2),DoubleToString(tr.rr,2),tr.strategy,tr.reason,DoubleToString(tr.entryScore,0),DoubleToString(tr.exitScore,2)); FileClose(h); journalTradeCount++; RecordDailyPnLForConsistency(tr.profit); }
void JournalLogClosedTrade(long ticket){ if(!m4OrderSelect(ticket,SELECT_BY_TICKET,MODE_HISTORY)) return; TradeRecord tr;   /* b72 FIX: EnableJournal-ka laga saaray shuruudda hore - fariinta win/loss Telegram wax bay ku xirnayd journal (haddii journal off = fariin lama helo). Hadda mar walba way dirmaysaa. */ tr.ticket=ticket; tr.symbol=m4OrderSymbol(); tr.type=m4OrderType(); tr.lot=m4OrderLots(); tr.openPrice=m4OrderOpenPrice(); tr.closePrice=m4OrderClosePrice(); tr.sl=m4OrderStopLoss(); tr.tp=m4OrderTakeProfit(); tr.profit=m4OrderProfit()+m4OrderCommission()+m4OrderSwap(); tr.openTime=m4OrderOpenTime(); tr.closeTime=m4OrderCloseTime(); int _mgStrat=m4OrderMagicNumber()-(MagicNumber+100); tr.strategy=EnumToStr_Strategy((_mgStrat>=0&&_mgStrat<=6)?_mgStrat:currentActiveStrategy); tr.reason=m4OrderComment();   /* b56 FIX: journal magaca xeelada = magic number-ka trade-ka (xeelada dhabta ah) */ double slD=MathAbs(tr.openPrice-tr.sl), tpD=MathAbs(tr.openPrice-tr.tp); tr.rr=(slD>0)?tpD/slD:0;
   // QAYBTA 12 FIX (V39): entry score (stored at open) + realized R exit score
   tr.entryScore=GetEntryScore(ticket); double _pp=GetPipSize(tr.symbol); double slPips=(_pp>0)?MathAbs(tr.openPrice-tr.sl)/_pp:0; double movePips=(_pp>0)?(((tr.type==OP_BUY)?(tr.closePrice-tr.openPrice):(tr.openPrice-tr.closePrice))/_pp):0; tr.exitScore=(slPips>0)?movePips/slPips:0;
   if(EnableJournal) JournalWrite(tr);   // b72: journal-write kaliya ayaa EnableJournal ku xiran - fariinta Telegram hoose way ka madax bannaan tahay
   // QAYBTA 13 FIX (V39): close alert + screenshot
   if(TG_TradeCloseAlert && EnableTelegram){ bool _win=(tr.profit>=0); int _tot=totalWins+totalLosses; double _wr=(_tot>0)?(double)totalWins/_tot*100.0:0; string _hdr=_win?"💹〔 NATIIJADA TRADE-KA 〕💹":"🔻〔 NATIIJADA TRADE-KA 〕🔻"; string _res=_win?"✅ GUUL":"❌ KHASAARE"; string _line=_win?"💵 Faa'iido:  +$":"💸 Khasaare:  -$"; string _bal=_win?"📈 Balance:  $":"📉 Balance:  $"; string m=_hdr+"\n┏━━━━━━━━━━━━━━━┓\n   "+_res+"  ·  "+tr.symbol+"\n   📊 "+tr.strategy+"   ·   🕐 "+TimeToString(TimeCurrent(),TIME_MINUTES)+"\n┗━━━━━━━━━━━━━━━┛\n🏆 Win "+IntegerToString(totalWins)+" | Loss "+IntegerToString(totalLosses)+"   ("+DoubleToString(_wr,0)+"%)\n"+_line+DoubleToString(MathAbs(tr.profit),2)+"\n"+_bal+DoubleToString(AccountBalance(),2)+"\n━━━━━━━━━━━━━━━━\n🌐 @MOHAPROLIVE_BOT"; SendTelegram(m); }
   SaveTradeScreenshot("CLOSE"); }
string GetErrMsg(int code){ switch(code){ case 0: return "No error"; case 128: return "Timeout"; case 129: return "Invalid price"; case 130: return "Invalid stops"; case 131: return "Invalid volume"; case 134: return "Not enough money"; default: return "Error "+IntegerToString(code); } }
bool HandleOrderError(int error,int &retries){ consecutiveErrors++; errorCount++; Print("OrderError[",error,"]: ",GetErrMsg(error)," Retry: ",retries); if(consecutiveErrors>10){ if(EnableTelegram) SendTelegram("EA: "+IntegerToString(consecutiveErrors)+" errors!"); consecutiveErrors=0; } switch(error){ case 128: case 142: case 143: Sleep(RetryDelayMs*2); retries--; return true; case 135: case 138: case 136: Sleep(RetryDelayMs); retries--; return true; default: Sleep(RetryDelayMs); retries--; return (retries>0); } }
long SmartOrderSend(string sym, int type, double lot, double price, int slip, double sl, double tp, string comment, int magic){
   if(ExecMode == EXEC_LIMIT){ double _p=GetPipSize(sym); double limPrice=(type==OP_BUY)?price-Limit_Offset_Pips*_p:price+Limit_Offset_Pips*_p; int limType=(type==OP_BUY)?OP_BUYLIMIT:OP_SELLLIMIT; long ticketL=m4OrderSend(sym,limType,lot,NormalizeDouble(limPrice,(int)MarketInfo(sym,MODE_DIGITS)),slip,sl,tp,comment,magic,0,(type==OP_BUY?clrBlue:clrRed)); if(ticketL<=0) LogTradeOpFailure("SmartOrderSend(LIMIT)",-1,GetLastError()); return ticketL; }
   long ticket=-1; int retries=(ExecMode==EXEC_INSTANT)?1:MaxRetries; while(retries>0){ if(!IsTradeAllowed()){ Sleep(500); retries--; continue; } RefreshRates(); double execPrice=(type==OP_BUY)?MarketInfo(sym,MODE_ASK):MarketInfo(sym,MODE_BID); ticket=m4OrderSend(sym,type,lot,execPrice,slip,sl,tp,comment,magic,0,(type==OP_BUY?clrBlue:clrRed)); if(ticket>0){ consecutiveErrors=0; break; } int err=GetLastError(); if(Enable_ECN_StopFallback && (err==130||err==145) && (sl!=0||tp!=0)){ RefreshRates(); double ep2=(type==OP_BUY)?MarketInfo(sym,MODE_ASK):MarketInfo(sym,MODE_BID); long t2=m4OrderSend(sym,type,lot,ep2,slip,0,0,comment,magic,0,(type==OP_BUY?clrBlue:clrRed)); if(t2>0){ if(m4OrderSelect(t2,SELECT_BY_TICKET)){ bool mo=m4OrderModify(t2,m4OrderOpenPrice(),sl,tp,0,clrGold); if(!mo) LogTradeOpFailure("ECN_ModifyStops",t2,GetLastError()); } consecutiveErrors=0; ticket=t2; break; } err=GetLastError(); } if(ExecMode==EXEC_INSTANT){ LogTradeOpFailure("SmartOrderSend(INSTANT)",-1,err); break; } if(!HandleOrderError(err,retries)){ LogTradeOpFailure("SmartOrderSend(SMART)",-1,err); break; } } return ticket; }
void ManageScaleOut(){ if(!Enable_ScaleOut) return; for(int i=0;i<m4OrdersTotal();i++){ if(!m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue; int mg=m4OrderMagicNumber(); if(mg<MagicNumber+100||mg>MagicNumber+106||m4OrderSymbol()!=Symbol()) continue; long ticket=m4OrderTicket(); double open=m4OrderOpenPrice(), sl=m4OrderStopLoss(); if(sl<=0) continue; double rDist=MathAbs(open-sl); double curr=(m4OrderType()==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK); double pnlR=(m4OrderType()==OP_BUY)?(curr-open)/rDist:(open-curr)/rDist; int stIdx=-1; for(int j=0;j<ArraySize(scaleOutStates);j++) if(scaleOutStates[j].ticket==ticket){ stIdx=j; break; } if(stIdx==-1){ int sz=ArraySize(scaleOutStates); ArrayResize(scaleOutStates,sz+1); scaleOutStates[sz].ticket=ticket; scaleOutStates[sz].r1Done=false; scaleOutStates[sz].r2Done=false; stIdx=sz; } if(pnlR>=1.0 && !scaleOutStates[stIdx].r1Done){ double closeLot=NormalizeDouble(m4OrderLots()*ScaleOut_R1_Pct/100.0,2); if(closeLot>=MarketInfo(Symbol(),MODE_MINLOT)){ double cp=(m4OrderType()==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK); bool closed=m4OrderClose(ticket,closeLot,cp,3,clrCyan); if(!closed) LogTradeOpFailure("ScaleOut_R1",ticket,GetLastError()); scaleOutStates[stIdx].r1Done=true; } else scaleOutStates[stIdx].r1Done=true; } if(pnlR>=2.0 && !scaleOutStates[stIdx].r2Done){ double closeLot=NormalizeDouble(m4OrderLots()*ScaleOut_R2_Pct/100.0,2); if(closeLot>=MarketInfo(Symbol(),MODE_MINLOT)){ double cp=(m4OrderType()==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK); bool closed=m4OrderClose(ticket,closeLot,cp,3,clrAqua); if(!closed) LogTradeOpFailure("ScaleOut_R2",ticket,GetLastError()); scaleOutStates[stIdx].r2Done=true; } else scaleOutStates[stIdx].r2Done=true; } } }
// b85: haddii TP-Ladder partial-close-ku guuldareysto (tusaale Err 4753 MT5 compat), hal mar kaliya isku day -> ka hortag SPAM + retry aan dhammaad lahayn
long g_tpFailT[]; int g_tpFailL[];
bool TPCloseFailed(long t,int l){ for(int i=0;i<ArraySize(g_tpFailT);i++) if(g_tpFailT[i]==t && g_tpFailL[i]==l) return true; return false; }
void TPCloseFailAdd(long t,int l){ int s=ArraySize(g_tpFailT); ArrayResize(g_tpFailT,s+1); ArrayResize(g_tpFailL,s+1); g_tpFailT[s]=t; g_tpFailL[s]=l; }
// b90: FAST BREAK-EVEN - marka +FastBE_Trigger_R la gaadho, SL -> open+FastBE_Lock_R (BUY) / open-lock (SELL).
// Kaliya marka SL weli khasaare-zone ku jiro (atRisk) -> hal mar, KA HORTAG ladder-fight. Shaqeeya xitaa TP-Ladder shidan.
void ManageFastBreakEven(){
   if(!Enable_FastBE) return;
   for(int i=0;i<m4OrdersTotal();i++){
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      int mg=m4OrderMagicNumber();
      if(mg<MagicNumber+100||mg>MagicNumber+106||m4OrderSymbol()!=Symbol()) continue;
      double open=m4OrderOpenPrice(), sl=m4OrderStopLoss(); if(sl<=0) continue;
      double rDist=MathAbs(open-sl); if(rDist<=0) continue;
      int type=m4OrderType();
      bool atRisk=(type==OP_BUY)?(sl<open):(sl>open);   // SL weli khasaare-zone? haddii kale (BE horeba) -> ha taaban
      if(!atRisk) continue;
      double curr=(type==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK);
      double pnlR=(type==OP_BUY)?(curr-open)/rDist:(open-curr)/rDist;
      if(pnlR<FastBE_Trigger_R) continue;
      double lock=FastBE_Lock_R*rDist;
      double newSL=(type==OP_BUY)?open+lock:open-lock;
      bool valid=(type==OP_BUY)?(newSL<curr):(newSL>curr);   // SL ha dhaafin market
      if(!valid) continue;
      long tk=m4OrderTicket();
      bool mod=m4OrderModify(tk,open,newSL,m4OrderTakeProfit(),0,clrLime);
      if(!mod) LogTradeOpFailure("FastBE",tk,GetLastError());
      else if(Enable_Debug_Log) Print("DEBUG FastBE: ticket ",tk," SL -> ",DoubleToString(newSL,Digits)," (breakeven +",DoubleToString(FastBE_Lock_R,2),"R)");
   }
}
void ManageTPLadder(){   // b63: TP1/2/3/4 + SL step (TP1->breakeven, TP2->TP1, TP3->TP2, TP4->TP3)
   if(!g_TPL_On) return;   // b101: panel badhanka LADDER
   __tpAutoCalc();   // b105: haddii AUTO, heerarka TP ka xisaabi SL/TP
   // prune states of closed tickets (ka hortag leak)
   for(int j=ArraySize(tpLadder)-1;j>=0;j--){ if(!m4OrderSelect(tpLadder[j].ticket,SELECT_BY_TICKET,MODE_TRADES)){ int last=ArraySize(tpLadder)-1; tpLadder[j]=tpLadder[last]; ArrayResize(tpLadder,last); } }
   double tpR[4]; tpR[0]=g_TP_R[0]; tpR[1]=g_TP_R[1]; tpR[2]=g_TP_R[2]; tpR[3]=g_TP_R[3];   // b102: panel values
   int tpPct[4]; tpPct[0]=TPL_TP1_Pct; tpPct[1]=TPL_TP2_Pct; tpPct[2]=TPL_TP3_Pct; tpPct[3]=TPL_TP4_Pct;
   for(int z=0;z<4;z++) if(!g_TP_On[z]) tpPct[z]=0;   // b101: heerka la damiyay -> qayb ma gooyo (SL step wuu socdaa)
   for(int i=m4OrdersTotal()-1;i>=0;i--){
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES) || m4OrderSymbol()!=Symbol()) continue;
      int mg=m4OrderMagicNumber(); if(mg<MagicNumber+100||mg>MagicNumber+106) continue;
      int type=m4OrderType(); if(type!=OP_BUY&&type!=OP_SELL) continue;
      long ticket=m4OrderTicket(); double open=m4OrderOpenPrice();
      int si=-1; for(int j=0;j<ArraySize(tpLadder);j++) if(tpLadder[j].ticket==ticket){ si=j; break; }
      if(si==-1){ double d=MathAbs(open-m4OrderStopLoss()); if(d<=0) continue; int sz=ArraySize(tpLadder); ArrayResize(tpLadder,sz+1); tpLadder[sz].ticket=ticket; tpLadder[sz].level=0; tpLadder[sz].rDist=d; tpLadder[sz].origLot=m4OrderLots(); si=sz; }
      double rDist=tpLadder[si].rDist; if(rDist<=0) continue;
      double curr=(type==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK);
      double pnlR=(type==OP_BUY)?(curr-open)/rDist:(open-curr)/rDist;
      int maxLvl=TPL_Trail_Final?3:4;   // b64: haddii Trail_Final, TP1/TP2/TP3 kaliya partial+step; qaybta 4aad way daba socotaa (trail)
      for(int L=tpLadder[si].level; L<maxLvl; L++){
         if(pnlR < tpR[L]) break;
         // 1. qabo qayb - partial close - % of original lot
         if(tpPct[L]>0){
            double closeLot=NormalizeDouble(tpLadder[si].origLot*tpPct[L]/100.0,2);
            double curLot=m4OrderLots();
            if((L==3 && !TPL_Trail_Final) || closeLot>=curLot) closeLot=curLot;   // TP4 (ma aha trail) ama rounding -> xir inta hadhay
            if(closeLot>=MarketInfo(Symbol(),MODE_MINLOT) && !TPCloseFailed(ticket,L)){ double cp=(type==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK); bool cl=m4OrderClose(ticket,closeLot,cp,3,clrCyan); if(!cl){ TPCloseFailAdd(ticket,L); LogTradeOpFailure("TPLadder_TP"+IntegerToString(L+1),ticket,GetLastError()); } }   // b85: guuldarro -> hal mar log, ka dib iska boodo (SL-step breakeven weli wuu shaqeynayaa)
         }
         // 2. step SL: TP1 L=0 -> breakeven; TP2+ L>=1 -> qiimaha TP L-1
         double newSL=(L==0)?open:(open+((type==OP_BUY)? tpR[L-1]*rDist : -tpR[L-1]*rDist));
         newSL=NormalizeDouble(newSL,Digits);
         if(m4OrderSelect(ticket,SELECT_BY_TICKET,MODE_TRADES)){
            bool better=(type==OP_BUY)?(newSL>m4OrderStopLoss()):(m4OrderStopLoss()==0||newSL<m4OrderStopLoss());
            if(better && MathAbs(m4OrderStopLoss()-newSL)>_Point){ bool m=m4OrderModify(ticket,m4OrderOpenPrice(),newSL,m4OrderTakeProfit(),0,clrGold); if(!m) LogTradeOpFailure("TPLadder_SL"+IntegerToString(L+1),ticket,GetLastError()); else if(Enable_Debug_Log) Print("DEBUG TP-LADDER: TP",L+1," hit -> SL ",DoubleToString(newSL,Digits)); }
         }
         tpLadder[si].level=L+1;
      }
      // b64: RUNNER TRAIL - marka TP3 la gaadho (level>=3) & Trail_Final, qaybta ugu dambaysa ha daba socoto (guul weyn fog u ordaan)
      if(TPL_Trail_Final && tpLadder[si].level>=3){
         double tstep=TPL_Trail_Step_R*rDist; if(tstep<=0) tstep=0.5*rDist;
         double nsl=(type==OP_BUY)?curr-tstep:curr+tstep; nsl=NormalizeDouble(nsl,Digits);
         double minStopT=MarketInfo(Symbol(),MODE_STOPLEVEL)*Point;
         if(m4OrderSelect(ticket,SELECT_BY_TICKET,MODE_TRADES)){
            bool betterT=(type==OP_BUY)?(nsl>m4OrderStopLoss()):(m4OrderStopLoss()==0||nsl<m4OrderStopLoss());
            bool validT=(type==OP_BUY)?(nsl<curr-minStopT):(nsl>curr+minStopT);
            if(betterT && validT && MathAbs(m4OrderStopLoss()-nsl)>_Point){ bool mt=m4OrderModify(ticket,m4OrderOpenPrice(),nsl,m4OrderTakeProfit(),0,clrGold); if(!mt) LogTradeOpFailure("TPLadder_Trail",ticket,GetLastError()); else if(Enable_Debug_Log) Print("DEBUG TP-LADDER RUNNER trail -> SL ",DoubleToString(nsl,Digits)); }
         }
      }
   }
}
void UpdateStrategyStats(){ for(int i=0;i<7;i++){ stratStats[i].wins=stratStats[i].losses=stratStats[i].total=0; stratStats[i].winRate=stratStats[i].totalPnL=stratStats[i].avgRR=0; stratStats[i].grossProfit=stratStats[i].grossLoss=stratStats[i].profitFactor=stratStats[i].expectancy=stratStats[i].avgWin=stratStats[i].avgLoss=0; } int ht=m4OrdersHistoryTotal(); for(int i=0;i<ht;i++){ if(!m4OrderSelect(i,SELECT_BY_POS,MODE_HISTORY)) continue; int mg=m4OrderMagicNumber(); if(m4OrderSymbol()==Symbol() && mg>=MagicNumber+100 && mg<=MagicNumber+106){ int idx=mg-(MagicNumber+100); if(idx<0||idx>6) continue; double p=m4OrderProfit()+m4OrderCommission()+m4OrderSwap(); if(p>0){ stratStats[idx].wins++; stratStats[idx].grossProfit+=p; } else { stratStats[idx].losses++; stratStats[idx].grossLoss+=MathAbs(p); } stratStats[idx].total++; stratStats[idx].totalPnL+=p; double slD=MathAbs(m4OrderOpenPrice()-m4OrderStopLoss()), tpD=MathAbs(m4OrderOpenPrice()-m4OrderTakeProfit()); if(slD>0) stratStats[idx].avgRR += tpD/slD; } } for(int i=0;i<7;i++){ if(stratStats[i].total>0){ stratStats[i].winRate=(double)stratStats[i].wins/stratStats[i].total*100.0; stratStats[i].avgRR/=stratStats[i].total; stratStats[i].avgWin=(stratStats[i].wins>0)?stratStats[i].grossProfit/stratStats[i].wins:0; stratStats[i].avgLoss=(stratStats[i].losses>0)?stratStats[i].grossLoss/stratStats[i].losses:0; stratStats[i].profitFactor=(stratStats[i].grossLoss>0)?stratStats[i].grossProfit/stratStats[i].grossLoss:((stratStats[i].grossProfit>0)?999.0:0.0); double wr=stratStats[i].winRate/100.0; stratStats[i].expectancy=wr*stratStats[i].avgWin-(1.0-wr)*stratStats[i].avgLoss; } } }
// QAYBTA 5 FIX (V37): composite score = Expectancy + Profit Factor + Win Rate (weighted)
int SelectBestStrategy(){ int bestIdx=(int)Select_Strategy; double bestScore=-1e18; for(int i=0;i<7;i++){ if(stratStats[i].total<Strategy_Trials) continue; double pf=stratStats[i].profitFactor; if(pf>3.0) pf=3.0; double score=stratStats[i].expectancy*Metric_W_Expectancy + pf*Metric_W_ProfitFactor + stratStats[i].winRate*Metric_W_WinRate; if(score>bestScore){ bestScore=score; bestIdx=i; } } return bestIdx; }
void AutoSwitchStrategy(){ if(!Enable_Auto_Strategy) return; static datetime lastCheck=0; if(TimeCurrent()-lastCheck<3600) return; lastCheck=TimeCurrent(); UpdateStrategyStats(); int newStrat=SelectBestStrategy(); if(newStrat!=currentActiveStrategy){ currentActiveStrategy=newStrat; if(EnableTelegram) SendTelegram("Strategy bedel: "+EnumToStr_Strategy(newStrat)+" WinRate: "+DoubleToString(stratStats[newStrat].winRate,1)+"%"); } }

//+------------------------------------------------------------------+
//| QAYBTA 1 FIX -- REAL-TIME NEWS CALENDAR (WebRequest + JSON)      |
//+------------------------------------------------------------------+
// Generic JSON string-field extractor. Waxay u shaqaysaa kuwa qaabkoodu
// yahay "key":"value" (dhammaan xogta ForexFactory feed-ku waa strings).
string JsonGetStringField(string obj, string key) {
   string search = "\"" + key + "\":\"";
   int p = StringFind(obj, search);
   if(p < 0) return "";
   p += StringLen(search);
   int e = StringFind(obj, "\"", p);
   if(e < 0) return "";
   return StringSubstr(obj, p, e - p);
}

// U kala jar array-ga JSON-ka ({...},{...},...) hal-hal object.
int SplitJsonObjects(string json, string &objects[]) {
   ArrayResize(objects, 0);
   int depth = 0, start = -1, cnt = 0;
   int len = StringLen(json);
   for(int i = 0; i < len; i++) {
      ushort c = StringGetCharacter(json, i);
      if(c == '{') { if(depth == 0) start = i; depth++; }
      else if(c == '}') {
         depth--;
         if(depth == 0 && start >= 0) {
            ArrayResize(objects, cnt + 1);
            objects[cnt] = StringSubstr(json, start, i - start + 1);
            cnt++;
            start = -1;
         }
      }
   }
   return cnt;
}

// Ka beddel taariikh ISO8601 (tusaale "2026-07-08T12:30:00-04:00") datetime
// oo ku habboon server-ka broker-ka, iyadoo la adeegsanayo News_Broker_GMT_Offset.
datetime ParseNewsDateTime(string iso, int brokerGmtOffsetHours) {
   if(StringLen(iso) < 19) return 0;
   int y  = (int)StringToInteger(StringSubstr(iso, 0, 4));
   int mo = (int)StringToInteger(StringSubstr(iso, 5, 2));
   int d  = (int)StringToInteger(StringSubstr(iso, 8, 2));
   int h  = (int)StringToInteger(StringSubstr(iso, 11, 2));
   int mi = (int)StringToInteger(StringSubstr(iso, 14, 2));
   int se = (int)StringToInteger(StringSubstr(iso, 17, 2));
   if(y < 2000 || mo < 1 || mo > 12 || d < 1 || d > 31) return 0;
   string dateStr = StringFormat("%04d.%02d.%02d %02d:%02d:%02d", y, mo, d, h, mi, se);
   datetime localEventTime = StringToTime(dateStr);
   if(localEventTime <= 0) return 0;

   int offSign = 0, offH = 0, offM = 0;
   if(StringLen(iso) >= 25) {
      string offPart = StringSubstr(iso, 19, 6); // "-04:00" or "+02:00"
      ushort sChar = StringGetCharacter(offPart, 0);
      if(sChar == '-') offSign = -1; else if(sChar == '+') offSign = 1;
      if(offSign != 0) { offH = (int)StringToInteger(StringSubstr(offPart, 1, 2)); offM = (int)StringToInteger(StringSubstr(offPart, 4, 2)); }
   }
   int offsetSeconds = offSign * (offH * 3600 + offM * 60);
   datetime utcTime    = localEventTime - offsetSeconds;
   datetime brokerTime = utcTime + brokerGmtOffsetHours * 3600;
   return brokerTime;
}

// Soo qaad calendar-ka xogta wararka toos ah (real-time) internetka.
// b28: SHARED NEWS CACHE - hal chart soo dejiya, kuwa kale fayl ka akhriya (429 xalliya, 7-chart)
string NewsCacheFile(){ return "MohaPro_News_Cache.txt"; }
void WriteNewsCache(){
   int h=FileOpen(NewsCacheFile(),FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(h==INVALID_HANDLE) return;
   FileWriteString(h,IntegerToString((int)TimeCurrent())+"\r\n");
   for(int i=0;i<ArraySize(g_NewsEvents);i++)
      FileWriteString(h,g_NewsEvents[i].country+"\t"+g_NewsEvents[i].impact+"\t"+IntegerToString((int)g_NewsEvents[i].time)+"\t"+g_NewsEvents[i].title+"\r\n");
   FileClose(h);
}
bool ReadNewsCache(int maxAgeSec){
   if(!FileIsExist(NewsCacheFile(),FILE_COMMON)) return false;
   int h=FileOpen(NewsCacheFile(),FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(h==INVALID_HANDLE) return false;
   string tsLine=FileReadString(h);
   datetime ts=(datetime)StringToInteger(tsLine);
   if(maxAgeSec>0 && (TimeCurrent()-ts)>maxAgeSec){ FileClose(h); return false; }
   ArrayResize(g_NewsEvents,0); int cnt=0;
   while(!FileIsEnding(h)){
      string line=FileReadString(h);
      if(StringLen(line)<5) continue;
      string p[]; int np=StringSplit(line,(ushort)'\t',p);
      if(np<4) continue;
      ArrayResize(g_NewsEvents,cnt+1);
      g_NewsEvents[cnt].country=p[0]; g_NewsEvents[cnt].impact=p[1];
      g_NewsEvents[cnt].time=(datetime)StringToInteger(p[2]); g_NewsEvents[cnt].title=p[3];
      cnt++;
   }
   FileClose(h);
   return (cnt>0);
}
void FetchNewsCalendar() {
   if(IsTesting()) { g_NewsDataValid=false; return; }   // V40: no WebRequest in Strategy Tester
   // b28: isku day SHARED CACHE horta - haddii fresh, ha soo dejin (WebRequest la'aan = 429 la'aan)
   if(ReadNewsCache((int)(News_Refresh_Minutes*60))){
      g_LastNewsFetch=TimeCurrent(); g_NewsDataValid=true; g_NewsFetchFails=0;
      Print("News calendar loaded from SHARED CACHE: ",ArraySize(g_NewsEvents)," events");
      return;
   }
   char postData[], result[];
   string resultHeaders;
   ResetLastError();
   int res = WebRequest("GET", News_Calendar_URL, "", 5000, postData, result, resultHeaders);
   if(res != 200) {
      // b28: fetch fashilay (429...) -> fallback SHARED CACHE (xitaa duqoobay) si news aan u lumin
      if(ReadNewsCache(0)){
         g_LastNewsFetch=TimeCurrent(); g_NewsDataValid=true;
         Print("News fetch http=",res," -> fallback SHARED CACHE: ",ArraySize(g_NewsEvents)," events");
         return;
      }
      g_NewsDataValid = false;
      g_NewsFetchFails++;
      g_LastNewsFetch = TimeCurrent();   // V43: backoff - ha isku dayin ilaa News_Refresh_Minutes (ka fogow 429)
      Print("NEWS CALENDAR FETCH FAILED | http=", res, " err=", GetLastError(),
            " | Next retry in ", News_Refresh_Minutes, " min | URL: ", News_Calendar_URL);
      if(EnableTelegram && TG_ErrorAlerts && TimeCurrent()-g_lastErrTgAlert>=TG_Error_Cooldown_Sec){   // b62: time-cooldown (halkii %5) - news 429 waa benign, ha buuqin
         g_lastErrTgAlert=TimeCurrent();
         SendTelegram("MOHA PRO: News calendar fetch fashilmay (http=" + IntegerToString(res) + "). Ganacsigu wuu socdaa (news filter kaliya ayaa hakad ah).");
      }
      return;
   }
   string json = CharArrayToString(result);
   string objs[];
   int n = SplitJsonObjects(json, objs);
   ArrayResize(g_NewsEvents, n);
   int validCount = 0;
   for(int i = 0; i < n; i++) {
      string country  = JsonGetStringField(objs[i], "country");
      string impact   = JsonGetStringField(objs[i], "impact");
      string dateStr  = JsonGetStringField(objs[i], "date");
      string title    = JsonGetStringField(objs[i], "title");
      if(StringLen(country) == 0 || StringLen(dateStr) == 0) continue;
      datetime dt = ParseNewsDateTime(dateStr, News_Broker_GMT_Offset);
      if(dt <= 0) continue;
      g_NewsEvents[validCount].country = country;
      g_NewsEvents[validCount].impact  = impact;
      g_NewsEvents[validCount].time    = dt;
      g_NewsEvents[validCount].title   = title;
      validCount++;
   }
   ArrayResize(g_NewsEvents, validCount);
   g_LastNewsFetch  = TimeCurrent();
   g_NewsDataValid  = true;
   g_NewsFetchFails = 0;
   WriteNewsCache();   // b28: kaydi shared cache si chart-yada kale u helaan (fetch ma sameynayaan)
   Print("News calendar loaded: ", validCount, " events (", News_Calendar_URL, ")");
}

//+------------------------------------------------------------------+
//| SIGNAL FUNCTIONS  - STRATEGY SPECIFIC ZONE LOGIC                 |
//+------------------------------------------------------------------+
bool IsHigherRSIConfirmed(int sig){
   if(!Use_Higher_RSI_Confirm) return true;
   ENUM_TIMEFRAMES htf=(Trade_Timeframe==TF_M1)?PERIOD_M5:PERIOD_H1;
   int per=(Trade_Timeframe==TF_M1)?RSI_M5_Period:RSI_1H_Period;
   double ob=(Trade_Timeframe==TF_M1)?RSI_M5_Overbought:RSI_1H_Overbought;
   double os=(Trade_Timeframe==TF_M1)?RSI_M5_Oversold:RSI_1H_Oversold;
   double rsi=m4iRSI(Symbol(),htf,per,PRICE_CLOSE,1);
   if(rsi<1.0||rsi>99.0) return false;
   if(sig==OP_BUY) return (rsi<=os);
   if(sig==OP_SELL) return (rsi>=ob);
   return false;
}

// ---- ZONE STRENGTH CHECK (PER-STRATEGY) ----
bool IsZoneStrong(int type, double price, int minTouches, double buffer) {
   if(!Use_Zone_Filter_For_Entry) return true;
   if(minTouches <= 0) return true;
   int touches = 0; double _p = GetPipSize(Symbol());
   int maxB = MathMin(100, Bars-1);
   for(int i = 1; i < maxB; i++) {
      if(type==OP_BUY && MathAbs(Low[i]-price) < buffer*_p) touches++;
      if(type==OP_SELL && MathAbs(High[i]-price) < buffer*_p) touches++;
   }
   return (touches >= minTouches);
}

// ---- 1. SR STRATEGY (Classic Swing Points - 100 bars) ----
// V56: tiri taabashooyinka DHABTA ah ee heerka (mid kastaa = soo-dhawaansho cusub oo bannaan ka soo galay)
int SR_CountTouches(double level, bool isSupport, int lookback){
   double _p=GetPipSize(Symbol()); double band=SR_StrengthBuffer*_p; if(band<=0) band=15*_p;
   int touches=0; bool inside=false; int maxB=MathMin(lookback, Bars-2);
   for(int i=maxB; i>=1; i--){
      double px=isSupport?Low[i]:High[i];
      bool near=(MathAbs(px-level)<=band);
      if(near && !inside){ touches++; inside=true; }
      else if(!near){ inside=false; }
   }
   return touches;
}
// b44 FIX: S/R DHAB AH - xariiq isku-le'eg oo swing points (fractal) badani taabteen, maaha hal candle min/max 8-saacadood (sida diagram-ka la i tusay: support/resistance = heer isugu jira dhawr swing point, maaha dhibic kaliya)
bool SR_FindClusteredLevel(bool findSupport, int lookback, double &level, int &touchCount){
   double _p=GetPipSize(Symbol()); double band=SR_StrengthBuffer*_p; if(band<=0) band=15*_p;
   int fLen=3; int maxI=MathMin(lookback,Bars-fLen-2); if(maxI<fLen+2) return false;
   double pr[]; int n=0;
   for(int i=fLen+1;i<=maxI;i++){
      if(findSupport){ if(SMC_IsSwingLow(i,fLen)){ ArrayResize(pr,n+1); pr[n]=Low[i]; n++; } }
      else           { if(SMC_IsSwingHigh(i,fLen)){ ArrayResize(pr,n+1); pr[n]=High[i]; n++; } }
   }
   if(n==0) return false;
   int bestCnt=0; double bestPrice=0;
   for(int a=0;a<n;a++){ int cnt=0; double sum=0; for(int b=0;b<n;b++){ if(MathAbs(pr[b]-pr[a])<=band){ cnt++; sum+=pr[b]; } } if(cnt>bestCnt){ bestCnt=cnt; bestPrice=sum/cnt; } }
   if(bestCnt<1) return false;
   level=bestPrice; touchCount=bestCnt; return true;
}
bool SR_IsSwingLowTF(ENUM_TIMEFRAMES tf,int i,int len){ double lo=iLow(Symbol(),tf,i); if(lo<=0) return false; for(int k=1;k<=len;k++){ if(iLow(Symbol(),tf,i+k)<lo||iLow(Symbol(),tf,i-k)<lo) return false; } return true; }   // b68: swing detection timeframe-aware
bool SR_IsSwingHighTF(ENUM_TIMEFRAMES tf,int i,int len){ double hi=iHigh(Symbol(),tf,i); if(hi<=0) return false; for(int k=1;k<=len;k++){ if(iHigh(Symbol(),tf,i+k)>hi||iHigh(Symbol(),tf,i-k)>hi) return false; } return true; }
bool SR_FindClusteredZone(bool findSupport, int lookback, double &zoneLo, double &zoneHi, double &level, int &touchCount){   // b65/b68: ZONE (band hi/lo) + touch count, ka timeframe SARE (SR_Zone_TF)
   ENUM_TIMEFRAMES tf=(SR_Zone_TF==PERIOD_CURRENT)?(ENUM_TIMEFRAMES)Period():SR_Zone_TF;   // b68: SR zones ka TF sare (H4)
   double _p=GetPipSize(Symbol()); double band=SR_StrengthBuffer*_p; if(band<=0) band=15*_p;
   int fLen=3; int tfBars=m4iBars(Symbol(),tf); int maxI=MathMin(lookback,tfBars-fLen-2); if(maxI<fLen+2) return false;
   double pr[]; int n=0;
   for(int i=fLen+1;i<=maxI;i++){
      if(findSupport){ if(SR_IsSwingLowTF(tf,i,fLen)){ ArrayResize(pr,n+1); pr[n]=iLow(Symbol(),tf,i); n++; } }
      else           { if(SR_IsSwingHighTF(tf,i,fLen)){ ArrayResize(pr,n+1); pr[n]=iHigh(Symbol(),tf,i); n++; } }
   }
   if(n==0) return false;
   int bestCnt=0; double bestSum=0, bestLo=0, bestHi=0;
   for(int a=0;a<n;a++){ int cnt=0; double sum=0, lo=pr[a], hi=pr[a]; for(int b=0;b<n;b++){ if(MathAbs(pr[b]-pr[a])<=band){ cnt++; sum+=pr[b]; if(pr[b]<lo) lo=pr[b]; if(pr[b]>hi) hi=pr[b]; } } if(cnt>bestCnt){ bestCnt=cnt; bestSum=sum; bestLo=lo; bestHi=hi; } }
   if(bestCnt<1) return false;
   level=bestSum/bestCnt; touchCount=bestCnt; zoneLo=bestLo; zoneHi=bestHi;
   // b67: band = BALLACA DHABTA ah ee taabashooyinka (min->max) - ADAPTIVE (weyn=weyn, yar=yar). Kaliya minimum yar (2 pip) muuqaal ahaan.
   double minW=2*_p; if(zoneHi-zoneLo < minW){ zoneLo=level-minW*0.5; zoneHi=level+minW*0.5; }
   return true;
}
int CheckSR_Signal(){ if(Bars<SR_Lookback+5)return -1; int bN=MathMin(SR_Lookback,Bars-1); double _p=GetPipSize(Symbol());
   double sLo=0,sHi=0,sLvl=0, rLo=0,rHi=0,rLvl=0; int loT=0,hiT=0;
   bool haveLo=SR_FindClusteredZone(true,bN,sLo,sHi,sLvl,loT), haveHi=SR_FindClusteredZone(false,bN,rLo,rHi,rLvl,hiT); if(!haveLo&&!haveHi)return -1;
   // b77: BREAK & RETEST mode (continuation) - jab heerka, ka dib retest, gal jihada jabka
   if(SR_Break_Retest){
      int maxB=MathMin(SR_BreakRetest_MaxBars,Bars-3); double band=SR_ZonePips*_p;
      // BUY: resistance kor loo jabsaday -> qiimuhu dib ugu soo laabtay (retest) -> bood kor
      if(haveHi){
         bool broke=false; for(int j=2;j<=maxB;j++){ if(Close[j] > rHi + band){ broke=true; break; } }
         bool retest = (Close[1] >= rLo - band && Close[1] <= rHi + band);
         bool bull   = (Close[1]>Open[1]) && ((MathMin(Open[1],Close[1])-Low[1]) >= MathAbs(Close[1]-Open[1]));   // pin diidmo hoose
         if(broke && retest && bull){ if(SR_Require_Volume && m4iVolume(NULL,0,1)<=m4iVolume(NULL,0,2)) return -1; return OP_BUY; }
      }
      // SELL: support hoos loo jabsaday -> retest -> bood hoos
      if(haveLo){
         bool broke2=false; for(int j=2;j<=maxB;j++){ if(Close[j] < sLo - band){ broke2=true; break; } }
         bool retest2 = (Close[1] >= sLo - band && Close[1] <= sHi + band);
         bool bear    = (Close[1]<Open[1]) && ((High[1]-MathMax(Open[1],Close[1])) >= MathAbs(Close[1]-Open[1]));  // pin diidmo sare
         if(broke2 && retest2 && bear){ if(SR_Require_Volume && m4iVolume(NULL,0,1)<=m4iVolume(NULL,0,2)) return -1; return OP_SELL; }
      }
      return -1;
   }
   if(haveLo && Close[1] <= sHi + SR_ZonePips*_p && Close[1] >= sLo - SR_ZonePips*_p){ if(SR_Exact_Touches>0 && loT<SR_Exact_Touches) return -1; if(Close[1]<=Open[1]) return -1; if(SR_Require_Rejection && (MathMin(Open[1],Close[1])-Low[1]) < MathAbs(Close[1]-Open[1])) return -1; if(SR_Require_Volume && m4iVolume(NULL,0,1)<=m4iVolume(NULL,0,2)) return -1; return OP_BUY; }   // b65: price ku jira support ZONE + pin rejection + volume
   if(haveHi && Close[1] >= rLo - SR_ZonePips*_p && Close[1] <= rHi + SR_ZonePips*_p){ if(SR_Exact_Touches>0 && hiT<SR_Exact_Touches) return -1; if(Close[1]>=Open[1]) return -1; if(SR_Require_Rejection && (High[1]-MathMax(Open[1],Close[1])) < MathAbs(Close[1]-Open[1])) return -1; if(SR_Require_Volume && m4iVolume(NULL,0,1)<=m4iVolume(NULL,0,2)) return -1; return OP_SELL; }   // b65: price ku jira resistance ZONE + pin rejection + volume
   return -1; }

// ---- 2. BOLLINGER STRATEGY ----
int CheckBB_Signal(){ double bU1=m4iBands(NULL,0,BB_Period,BB_Deviation,0,PRICE_CLOSE,MODE_UPPER,1), bL1=m4iBands(NULL,0,BB_Period,BB_Deviation,0,PRICE_CLOSE,MODE_LOWER,1); double bU2=m4iBands(NULL,0,BB_Period,BB_Deviation,0,PRICE_CLOSE,MODE_UPPER,2), bL2=m4iBands(NULL,0,BB_Period,BB_Deviation,0,PRICE_CLOSE,MODE_LOWER,2); if(bU1<=0||bL1<=0)return -1; double bbRsi=m4iRSI(NULL,0,14,PRICE_CLOSE,1);
   if(bbRsi<1.0||bbRsi>99.0) return -1;   // b111 FIX: __buf() wuxuu 0 soo celiyaa marka xogtu jirin. 0<=40 = BUY been ah
   if(BB_Max_ADX>0){ double bbAdx=m4iADX(NULL,0,14,PRICE_CLOSE,MODE_MAIN,1); if(bbAdx<=0) return -1; if(bbAdx>BB_Max_ADX) return -1; }   // b111: ADX 0 = xog la'aan, ha aqbalin   // b46: BB waa mean-reversion, ha shaqeyn suuq TRENDING (ADX sare)
   if(BB_Require_CloseBack){ if(Close[2]>bU2 && Close[1]<=bU1 && bbRsi>=55) return OP_SELL; if(Close[2]<bL2 && Close[1]>=bL1 && bbRsi<=45) return OP_BUY; return -1; }   // b33: ka baxo band-ka -> ku soo laabo gudaha (reversion confirm)
   if(Close[1]>bU1 && Close[2]<=bU1 && bbRsi>=60)return OP_SELL; if(Close[1]<bL1 && Close[2]>=bL1 && bbRsi<=40)return OP_BUY; return -1; }

// ---- 3. EMA STRATEGY ----
int CheckEMA_Signal(){ double f1=m4iMA(NULL,0,FastEMA,0,MODE_EMA,PRICE_CLOSE,1), s1=m4iMA(NULL,0,SlowEMA,0,MODE_EMA,PRICE_CLOSE,1); if(f1<=0||s1<=0)return -1;
   if(EMA_Avoid_Squeeze){ double eatr=m4iATR(NULL,0,ATR_Period_Core,1); if(eatr>0 && eatr<100000 && MathAbs(Close[1]-s1) < EMA_Min_Sep_ATR*eatr) return -1; }   // b38 FIX: hore waxa la qiimeyn jiray f1-s1 (fast/slow) oo AJIS ah marka isku-jarka (cross) uu dhici karo - ta ku dhow 0 waqti kasta, taasoo xanibi jirtay 100% EMA signal-ka. Hadda waxaa la qiimeynayaa Close-ka fogaanta EMA slow-ga (xoogga jabinta dhabta ah)
   int dir=-1; if(f1>s1&&Close[1]>s1)dir=OP_BUY; else if(f1<s1&&Close[1]<s1)dir=OP_SELL; if(dir==-1)return -1;
   if(!EMA_Require_Retest){ double f2=m4iMA(NULL,0,FastEMA,0,MODE_EMA,PRICE_CLOSE,2), s2=m4iMA(NULL,0,SlowEMA,0,MODE_EMA,PRICE_CLOSE,2); if(dir==OP_BUY&&f2<=s2)return OP_BUY; if(dir==OP_SELL&&f2>=s2)return OP_SELL; return -1; }
   // b48 FIX (Qodob 10): False Breakout Filter - sug in cross-ku dhaco muddo dhawaan ah (ugu badan EMA_Retest_MaxBars), kadibna price-gu retest sameeyo (dib ugu soo laabo EMA fast-ka) ka hor gelitaanka - halkii la geli lahaa isla-markiiba jajabka (false breakout aad u badan)
   int maxB=MathMin(EMA_Retest_MaxBars,Bars-3); int crossBar=-1;
   for(int i=2;i<=maxB+1;i++){
      double fi=m4iMA(NULL,0,FastEMA,0,MODE_EMA,PRICE_CLOSE,i), si=m4iMA(NULL,0,SlowEMA,0,MODE_EMA,PRICE_CLOSE,i);
      double fn=m4iMA(NULL,0,FastEMA,0,MODE_EMA,PRICE_CLOSE,i+1), sn=m4iMA(NULL,0,SlowEMA,0,MODE_EMA,PRICE_CLOSE,i+1);
      if(dir==OP_BUY  && fi>si && fn<=sn){ crossBar=i; break; }
      if(dir==OP_SELL && fi<si && fn>=sn){ crossBar=i; break; }
   }
   if(crossBar==-1) return -1;
   double eatr2=m4iATR(NULL,0,ATR_Period_Core,1); if(eatr2<=0||eatr2>100000) return -1; double retBand=EMA_Min_Sep_ATR*eatr2;
   bool hadRetest=false;
   for(int j=crossBar-1;j>=1;j--){ double fj=m4iMA(NULL,0,FastEMA,0,MODE_EMA,PRICE_CLOSE,j); if(MathAbs(Close[j]-fj)<=retBand){ hadRetest=true; break; } }
   if(!hadRetest) return -1;
   if(EMA_Require_Volume && m4iVolume(NULL,0,1)<=m4iVolume(NULL,0,2)) return -1;
   return dir; }

// ---- 4. SMC STRATEGY (QAYBTA 2 FIX V34: REAL SMC ENGINE) ----
// Fractal swing detection
bool SMC_IsSwingHigh(int i,int len){
   if(i-len < 1 || i+len >= Bars) return false;
   double v=High[i];
   for(int k=1;k<=len;k++){ if(High[i+k]>v) return false; if(High[i-k]>v) return false; }
   return true;
}
bool SMC_IsSwingLow(int i,int len){
   if(i-len < 1 || i+len >= Bars) return false;
   double v=Low[i];
   for(int k=1;k<=len;k++){ if(Low[i+k]<v) return false; if(Low[i-k]<v) return false; }
   return true;
}
// Most recent confirmed swing (scan newest -> oldest)
bool SMC_LastSwingHigh(int len,int lb,int &idx,double &price){
   int maxI=MathMin(lb,Bars-len-1);
   for(int i=len+1;i<=maxI;i++){ if(SMC_IsSwingHigh(i,len)){ idx=i; price=High[i]; return true; } }
   return false;
}
bool SMC_LastSwingLow(int len,int lb,int &idx,double &price){
   int maxI=MathMin(lb,Bars-len-1);
   for(int i=len+1;i<=maxI;i++){ if(SMC_IsSwingLow(i,len)){ idx=i; price=Low[i]; return true; } }
   return false;
}
// Break of Structure / CHoCH: a later candle closed beyond the last swing level
bool SMC_BosUp(int len,int lb){
   int shIdx; double shP;
   if(!SMC_LastSwingHigh(len,lb,shIdx,shP)) return false;
   for(int j=shIdx-1;j>=1;j--){ if(Close[j] > shP) return true; }
   return false;
}
bool SMC_BosDown(int len,int lb){
   int slIdx; double slP;
   if(!SMC_LastSwingLow(len,lb,slIdx,slP)) return false;
   for(int j=slIdx-1;j>=1;j--){ if(Close[j] < slP) return true; }
   return false;
}
// Fair Value Gap (3-candle imbalance) within lb bars
bool SMC_HasBullFVG(int lb){
   int maxI=MathMin(lb,Bars-3);
   for(int i=1;i<=maxI;i++){ if(Low[i] > High[i+2]) return true; }
   return false;
}
bool SMC_HasBearFVG(int lb){
   int maxI=MathMin(lb,Bars-3);
   for(int i=1;i<=maxI;i++){ if(High[i] < Low[i+2]) return true; }
   return false;
}
// Liquidity sweep: wick took out prior swing then closed back inside
bool SMC_BullSweep(int len,int lb){
   int slIdx; double slP;
   if(!SMC_LastSwingLow(len,lb,slIdx,slP)) return false;
   for(int j=slIdx-1;j>=1;j--){ if(Low[j] < slP && Close[j] > slP) return true; }
   return false;
}
bool SMC_BearSweep(int len,int lb){
   int shIdx; double shP;
   if(!SMC_LastSwingHigh(len,lb,shIdx,shP)) return false;
   for(int j=shIdx-1;j>=1;j--){ if(High[j] > shP && Close[j] < shP) return true; }
   return false;
}
// Order block: last opposite-colour candle before the impulse
bool SMC_FindBullOB(int maxLook,double &obLo,double &obHi){
   int maxI=MathMin(maxLook,Bars-2);
   for(int i=1;i<=maxI;i++){ if(Close[i] < Open[i]){ obLo=Low[i]; obHi=High[i]; return true; } }
   return false;
}
bool SMC_FindBearOB(int maxLook,double &obLo,double &obHi){
   int maxI=MathMin(maxLook,Bars-2);
   for(int i=1;i<=maxI;i++){ if(Close[i] > Open[i]){ obLo=Low[i]; obHi=High[i]; return true; } }
   return false;
}
// ---- b11: SMC PROFESSIONAL helpers (HTF bias + CHoCH) ----
int SMC_HTF_Dir(){   // 1=bull, -1=bear, 0=neutral/off
   if(!SMC_Use_HTF_Bias) return 0;
   double e=m4iMA(Symbol(),SMC_HTF_TF,SMC_HTF_EMA,0,MODE_EMA,PRICE_CLOSE,1);
   double c=m4iClose(Symbol(),SMC_HTF_TF,1);
   if(e<=0||c<=0) return 0;
   return (c>e)?1:-1;
}
bool SMC_CHoCH_Up(int len,int lb){ double h1,h2; if(!SMC_TwoSwingHighs(len,lb,h1,h2)) return false; return (h1<h2 && Close[1]>h1); }   // jab lower-high (rogmasho kor)
bool SMC_CHoCH_Down(int len,int lb){ double l1,l2; if(!SMC_TwoSwingLows(len,lb,l1,l2)) return false; return (l1>l2 && Close[1]<l1); }   // jab higher-low (rogmasho hoos)
// ---- b12: CHoCH timeframe hoose (5M entry trigger) ----
bool SMC_LTF_CHoCH(int type){
   if(!SMC_Require_LTF_CHoCH) return true;
   ENUM_TIMEFRAMES tf=SMC_LTF; int len=SMC_SwingLen; int maxB=MathMin(40, m4iBars(Symbol(),tf)-len-2);
   if(maxB<len+3) return true;
   double c1=m4iClose(Symbol(),tf,1); double vals[2]; int cnt=0;
   if(type==OP_BUY){
      for(int i=len+1;i<=maxB && cnt<2;i++){ bool sh=true; double h=iHigh(Symbol(),tf,i); for(int j=1;j<=len;j++){ if(iHigh(Symbol(),tf,i-j)>h || iHigh(Symbol(),tf,i+j)>h){ sh=false; break; } } if(sh){ vals[cnt]=h; cnt++; } }
      if(cnt<2) return false; return (vals[0]<vals[1] && c1>vals[0]);   // jab lower-high LTF
   } else {
      for(int i=len+1;i<=maxB && cnt<2;i++){ bool sl=true; double l=iLow(Symbol(),tf,i); for(int j=1;j<=len;j++){ if(iLow(Symbol(),tf,i-j)<l || iLow(Symbol(),tf,i+j)<l){ sl=false; break; } } if(sl){ vals[cnt]=l; cnt++; } }
      if(cnt<2) return false; return (vals[0]>vals[1] && c1<vals[0]);   // jab higher-low LTF
   }
}
// ---- b12: raadi liquidity target (prior swing) TP ahaan ----
bool FindLiquidityTarget(int type, double entry, int lookback, double &target){
   int maxB=MathMin(lookback, Bars-SMC_SwingLen-2); double best=0; bool found=false;
   for(int i=SMC_SwingLen+1; i<=maxB; i++){
      if(type==OP_BUY && SMC_IsSwingHigh(i,SMC_SwingLen)){ double h=High[i]; if(h>entry && (!found || h<best)){ best=h; found=true; } }
      if(type==OP_SELL && SMC_IsSwingLow(i,SMC_SwingLen)){ double l=Low[i]; if(l<entry && (!found || l>best)){ best=l; found=true; } }
   }
   if(found){ target=best; return true; } return false;
}
int CheckSMC_Signal() {
   int lb = MathMin(SMC_Lookback, Bars-SMC_SwingLen-2);
   if(lb < SMC_SwingLen+3 || Bars < SMC_Lookback+5) return -1;
   double _p = GetPipSize(Symbol());

   int shIdx, slIdx; double shP, slP;
   if(!SMC_LastSwingHigh(SMC_SwingLen, lb, shIdx, shP)) return -1;
   if(!SMC_LastSwingLow (SMC_SwingLen, lb, slIdx, slP)) return -1;
   int htfDir = SMC_HTF_Dir();   // b11: HTF trend bias

   // Premium / Discount range
   double rngHi = MathMax(shP, slP), rngLo = MathMin(shP, slP);
   double range = rngHi - rngLo;
   if(range < 2*_p) return -1;
   double eq = rngLo + range*(SMC_Equilibrium_Pct/100.0);
   bool inDiscount = (Close[1] < eq);
   bool inPremium  = (Close[1] > eq);

   bool bosUp   = SMC_BosUp  (SMC_SwingLen, lb);
   bool bosDown = SMC_BosDown(SMC_SwingLen, lb);

   double obLo=0, obHi=0;

   // ---------- BUY setup: HTF bull -> CHoCH -> BOS -> discount -> FVG -> sweep -> OB ----------
   bool buyOK = true;
   if(SMC_Use_HTF_Bias    && htfDir==-1)                          buyOK=false;   // b11: HTF trend
   if(buyOK && SMC_Require_CHoCH && !SMC_CHoCH_Up(SMC_SwingLen,lb)) buyOK=false;  // b11: CHoCH
   if(buyOK && SMC_Require_BOS      && !bosUp)                     buyOK=false;
   if(buyOK && SMC_Require_PremDisc && !inDiscount)               buyOK=false;
   if(buyOK && SMC_Require_FVG      && !SMC_HasBullFVG(SMC_OB_MaxLookback)) buyOK=false;
   if(buyOK && SMC_Require_Sweep    && !SMC_BullSweep(SMC_SwingLen, lb))    buyOK=false;
   if(buyOK && SMC_Require_LTF_CHoCH && !SMC_LTF_CHoCH(OP_BUY))            buyOK=false;   // b12: 5M CHoCH entry
   if(buyOK){
      // b111 FIX: g_SMC_SL waxaa la dejiyaa KALIYA marka trade dhab ah la furayo.
      // Hore: waa la dejin jiray halkan, kadibna haddii qiimuhu OB-ga kuma jirin
      // return ma dhicin -> qiimo qudhun ah ayaa u hadhay trade-ka SMC ee xiga.
      if(SMC_FindBullOB(SMC_OB_MaxLookback, obLo, obHi)){
         double mitig = MathMax(2*_p, (obHi-obLo)*(OB_Mitigation_Perc/100.0));
         if(Close[1] <= obHi+mitig && Close[1] >= obLo-mitig){
            g_SMC_SL = (obLo>0 && obLo<slP) ? obLo : slP;
            return OP_BUY;
         }
      } else { g_SMC_SL = slP; return OP_BUY; }
   }

   // ---------- SELL setup: HTF bear -> CHoCH -> BOS -> premium -> FVG -> sweep -> OB ----------
   bool sellOK = true;
   if(SMC_Use_HTF_Bias    && htfDir==1)                           sellOK=false;   // b11: HTF trend
   if(sellOK && SMC_Require_CHoCH && !SMC_CHoCH_Down(SMC_SwingLen,lb)) sellOK=false; // b11: CHoCH
   if(sellOK && SMC_Require_BOS      && !bosDown)                  sellOK=false;
   if(sellOK && SMC_Require_PremDisc && !inPremium)               sellOK=false;
   if(sellOK && SMC_Require_FVG      && !SMC_HasBearFVG(SMC_OB_MaxLookback)) sellOK=false;
   if(sellOK && SMC_Require_Sweep    && !SMC_BearSweep(SMC_SwingLen, lb))    sellOK=false;
   if(sellOK && SMC_Require_LTF_CHoCH && !SMC_LTF_CHoCH(OP_SELL))           sellOK=false;   // b12: 5M CHoCH entry
   if(sellOK){
      // b111 FIX: sida BUY-ga - g_SMC_SL kaliya marka trade la furayo
      if(SMC_FindBearOB(SMC_OB_MaxLookback, obLo, obHi)){
         double mitig2 = MathMax(2*_p, (obHi-obLo)*(OB_Mitigation_Perc/100.0));
         if(Close[1] >= obLo-mitig2 && Close[1] <= obHi+mitig2){
            g_SMC_SL = (obHi>shP) ? obHi : shP;
            return OP_SELL;
         }
      } else { g_SMC_SL = shP; return OP_SELL; }
   }

   return -1;
}

// ---- QAYBTA 4 FIX (V37): MARKET STRUCTURE TREND (HH/HL/LH/LL + ADX) ----
bool SMC_TwoSwingHighs(int len,int lb,double &h1,double &h2){
   int c=0; double vals[2]; int maxI=MathMin(lb,Bars-len-1);
   for(int i=len+1;i<=maxI;i++){ if(SMC_IsSwingHigh(i,len)){ vals[c]=High[i]; c++; if(c==2){ h1=vals[0]; h2=vals[1]; return true; } } }
   return false;
}
bool SMC_TwoSwingLows(int len,int lb,double &l1,double &l2){
   int c=0; double vals[2]; int maxI=MathMin(lb,Bars-len-1);
   for(int i=len+1;i<=maxI;i++){ if(SMC_IsSwingLow(i,len)){ vals[c]=Low[i]; c++; if(c==2){ l1=vals[0]; l2=vals[1]; return true; } } }
   return false;
}
// Returns 1 = uptrend (HH+HL), -1 = downtrend (LH+LL), 0 = range/weak
int GetStructureTrend(){
   int len=Structure_SwingLen, lb=Structure_Lookback;
   double h1,h2,l1,l2; bool hh=false,hl=false,lh=false,ll=false;
   if(SMC_TwoSwingHighs(len,lb,h1,h2)){ hh=(h1>h2); lh=(h1<h2); }
   if(SMC_TwoSwingLows (len,lb,l1,l2)){ hl=(l1>l2); ll=(l1<l2); }
   int dir=0;
   if(hh && hl) dir=1;
   else if(lh && ll) dir=-1;
   if(Structure_Use_ADX){ double adx=m4iADX(Symbol(),0,Structure_ADX_Period,PRICE_CLOSE,MODE_MAIN,1); if(adx<Structure_ADX_Min) dir=0; }
   return dir;
}
// ---- 5. VSA STRATEGY ----
// b89: celceliska spread ee 'n' bar laga bilaabo bar 'start' (loo isticmaalo wide/narrow classification)
double VSA_AvgSpread(int start,int n){ if(n<1)n=1; double s=0; int c=0; for(int k=start;k<start+n;k++){ double sp=High[k]-Low[k]; if(sp>0){ s+=sp; c++; } } return (c>0)?s/c:0; }

// b89: VSA SUPER SCALPER - calaamadaha PDF-ka (Gavin Holmes) oo la code-gareeyay.
// Signal bar = index 2 (dhammaystiran), confirmation bar = index 1. Return OP_BUY/OP_SELL kaliya marka la xaqiijiyo.
int CheckVSA_SuperScalp(){
   int lb=MathMax(2,VSA_Lookback); if(Bars<lb+6) return -1;
   int s=2;   // signal bar (waxaa ku xiga bar[1] oo xaqiijin ah)
   long vsL=m4iVolume(NULL,0,s); if(vsL<=0) return -1; double vs=(double)vsL;
   long sumL=0; for(int k=s+1;k<s+1+lb;k++) sumL+=m4iVolume(NULL,0,k); if(sumL<=0) return -1;
   double avgV=(double)sumL/lb;
   double vP1=(double)m4iVolume(NULL,0,s+1), vP2=(double)m4iVolume(NULL,0,s+2);
   bool high      = (vs > avgV*VSA_VolumeRatio);
   bool ultraHigh = (vs > avgV*VSA_UltraVolRatio);
   bool lowVol    = (vP1>0 && vP2>0 && vs<vP1 && vs<vP2);
   double rng=High[s]-Low[s]; if(rng<=0) return -1;
   double avgSp=VSA_AvgSpread(s+1,lb);
   bool wide   = (avgSp>0 && rng > avgSp*VSA_WideSpreadRatio);
   bool narrow = (avgSp>0 && rng < avgSp*VSA_NarrowSpreadRatio);
   double cp=(Close[s]-Low[s])/rng;
   bool closeUp=(cp>0.66), closeDown=(cp<0.33);
   bool upBar=(Close[s]>Open[s]), downBar=(Close[s]<Open[s]);
   int stTrend=GetStructureTrend();
   bool upTrend=(stTrend>0), downTrend=(stTrend<0);
   bool trendOK = !VSA_SS_RequireTrend;   // haddii trend lagama-maarmaan yahay, kolba pass

   // ---- Signs of Strength -> BUY ----
   bool buySig=false;
   if(downBar && narrow && lowVol && (upTrend||trendOK))                     buySig=true; // No Supply
   if(downBar && ultraHigh && cp>=0.40 && (downTrend||trendOK))              buySig=true; // Stopping Volume
   if(wide && (high||ultraHigh) && closeUp && Low[s]<Low[s+1])               buySig=true; // Shakeout
   if(wide && ultraHigh && closeUp && (downTrend||trendOK))                  buySig=true; // Selling Climax
   // ---- Signs of Weakness -> SELL ----
   bool sellSig=false;
   if(upBar && narrow && lowVol && (downTrend||trendOK))                     sellSig=true; // No Demand
   if(wide && (high||ultraHigh) && closeDown && High[s]>High[s+1])           sellSig=true; // Upthrust
   if(wide && ultraHigh && closeDown && (upTrend||trendOK))                  sellSig=true; // Buying Climax

   if(buySig && sellSig) return -1;   // iska hor imaad -> ha galin
   if(buySig){  if(!VSA_RequireNextBarConfirm || Close[1]>Close[s]) return OP_BUY;  }
   if(sellSig){ if(!VSA_RequireNextBarConfirm || Close[1]<Close[s]) return OP_SELL; }
   return -1;
}

//+------------------------------------------------------------------+
//| b112: VSA DHAB AH (Wyckoff) - Volume + Spread + Close position    |
//|                                                                   |
//| Kii hore wuxuu ahaa MOMENTUM oo keliya:                           |
//|    volume sare + qiime kor -> BUY                                 |
//| Taasi maaha VSA. VSA waxay eegtaa SADDEX shay wada jira:           |
//|    1. Volume  - intee?                                            |
//|    2. Spread  - shumacu intee buu dheer yahay? (High-Low)          |
//|    3. Close   - xagee buu ku xirmay shumaca dhexdiisa?             |
//|                                                                   |
//| Signal bar = shift 2 (dhammaystiran), confirm bar = shift 1        |
//+------------------------------------------------------------------+
int CheckVSA_Signal(){
   if(VSA_SuperScalp) return CheckVSA_SuperScalp();

   int lb = MathMax(5, VSA_Lookback);
   if(Bars < lb+5) return -1;

   // Signal bar: haddii xaqiijin loo baahan yahay -> shift 2, haddii kale -> shift 1
   int sig = VSA_RequireNextBarConfirm ? 2 : 1;

   long vSigL = m4iVolume(NULL,0,sig); if(vSigL<=0) return -1;
   double vSig = (double)vSigL;

   // celceliska volume-ka (ka bilow shumaca sig+1)
   long sumL=0; int cnt=0;
   for(int k=sig+1; k<sig+1+lb; k++){ long vk=m4iVolume(NULL,0,k); if(vk>0){ sumL+=vk; cnt++; } }
   if(cnt<3 || sumL<=0) return -1;
   double avgVol = (double)sumL/cnt;
   if(avgVol<=0) return -1;

   // celceliska spread-ka
   double avgSpread = VSA_AvgSpread(sig+1, lb);
   if(avgSpread<=0) return -1;

   double hi=High[sig], lo=Low[sig], cl=Close[sig], op=Open[sig];
   double spread = hi-lo;
   if(spread<=0) return -1;

   double volR     = vSig/avgVol;              // saamiga volume-ka
   double sprR     = spread/avgSpread;         // saamiga spread-ka
   double closePos = (cl-lo)/spread;           // 0 = hoose, 1 = sare

   bool upBar   = (cl>op);
   bool downBar = (cl<op);

   double strong = VSA_ClosePos_Strong;        // tusaale 0.65
   double weak   = 1.0-strong;                 // tusaale 0.35

   int sigDir = -1;

   // b113 diagnostic
   if(VSA_Diagnostic){
      g_vsaBars++;
      if(volR >= VSA_VolumeRatio)        g_vsaVolHi++;
      if(sprR <= VSA_NarrowSpreadRatio)  g_vsaSprNarrow++;
      if(sprR >= VSA_WideSpreadRatio)    g_vsaSprWide++;
      if(closePos>=strong || closePos<=weak) g_vsaClosePos++;
   }

   //=================== CALAAMADAHA BULLISH ===================
   // 1. STOPPING VOLUME / SELLING CLIMAX
   //    volume SARE + shumac hoos + close SARE
   //    -> iib badan ayaa jiray laakiin waa la nuugay = demand
   //    b113: shuruudda spread-ka waa la saaray. volume iyo spread aad bay
   //    isugu xidhan yihiin - saddexda isku-darkoodu wuu waayay signal.
   if(volR >= VSA_UltraVolRatio && downBar && closePos >= strong)
      sigDir = OP_BUY;

   // 2. ABSORPTION (Supply nuugid)
   //    volume SARE + spread CIDHIIDHI + close SARE
   else if(volR >= VSA_VolumeRatio && sprR <= VSA_NarrowSpreadRatio
           && closePos >= strong)
      sigDir = OP_BUY;

   // 3. NO SUPPLY (daciif - context u baahan)
   else if(VSA_Use_NoSupplyDemand && volR <= VSA_LowVolRatio
           && sprR <= VSA_NarrowSpreadRatio && downBar)
      sigDir = OP_BUY;

   //=================== CALAAMADAHA BEARISH ===================
   // 1. BUYING CLIMAX
   else if(volR >= VSA_UltraVolRatio && upBar && closePos <= weak)
      sigDir = OP_SELL;

   // 2. UPTHRUST / DISTRIBUTION
   else if(volR >= VSA_VolumeRatio && sprR <= VSA_NarrowSpreadRatio
           && closePos <= weak)
      sigDir = OP_SELL;

   // 3. NO DEMAND (daciif)
   else if(VSA_Use_NoSupplyDemand && volR <= VSA_LowVolRatio
           && sprR <= VSA_NarrowSpreadRatio && upBar)
      sigDir = OP_SELL;

   if(sigDir == -1) return -1;
   if(VSA_Diagnostic) g_vsaSignals++;

   //=================== XAQIIJINTA ===================
   if(VSA_RequireNextBarConfirm){
      // shumaca xiga (shift 1) waa inuu raaco jihada
      if(sigDir==OP_BUY  && !(Close[1] > Close[2] && Close[1] > Open[1])) return -1;
      if(sigDir==OP_SELL && !(Close[1] < Close[2] && Close[1] < Open[1])) return -1;
   }

   return sigDir;
}

// ---- POC / VOLUME PROFILE STRATEGY (b83, upgraded b88) ----
// STATIC POC + Value Area (VAH/VAL) laga xisaabiyo maalintii hore. Reversal(bounce+rejection) / Breakout(VA+volume).
double g_lastPOC=0, g_pocVAH=0, g_pocVAL=0;   // b88: POC + Value Area High/Low (static maalin kasta)
int    g_pocDay=-1;                            // b88: maalinta la xisaabiyay (recompute marka maalin cusub)
// b88: dhis profile-ka -> POC + VAH/VAL. Static=barkii maalintii hore; else=POC_Lookback bars.
void ComputePOCProfile(){
   int bins=POC_Bins; if(bins<5)bins=5; if(bins>200)bins=200;
   int lo_i=1, hi_i=1;
   if(POC_Static){
      datetime todayStart=StringToTime(TimeToString(TimeCurrent(),TIME_DATE));
      datetime yStart=todayStart-86400;
      lo_i=-1; hi_i=-1;
      for(int i=1;i<Bars;i++){
         datetime bt=m4iTime(NULL,0,i);
         if(bt>=todayStart) continue;   // ka bood bar-yada maanta (developing)
         if(bt<yStart) break;           // ka hor shalay -> jooji
         if(lo_i==-1) lo_i=i;           // bar-ka ugu cusub ee shalay
         hi_i=i;                        // bar-ka ugu duug ee shalay
      }
      if(lo_i==-1){ lo_i=1; hi_i=MathMin(120,Bars-2); }   // fallback
   } else { lo_i=1; hi_i=MathMin(POC_Lookback,Bars-2); }
   if(hi_i<lo_i+10) hi_i=MathMin(lo_i+120,Bars-2);
   double hi=-1e18, lo=1e18;
   for(int i=lo_i;i<=hi_i;i++){ double h=High[i], l=Low[i]; if(h>hi)hi=h; if(l<lo)lo=l; }
   double rng=hi-lo; if(rng<=0) return;
   double binSize=rng/bins; if(binSize<=0) return;
   double vol[]; ArrayResize(vol,bins); ArrayInitialize(vol,0.0); double total=0;
   for(int i=lo_i;i<=hi_i;i++){
      long bv=m4iVolume(NULL,0,i); if(bv<=0) continue;
      int b0=(int)MathFloor((Low[i]-lo)/binSize), b1=(int)MathFloor((High[i]-lo)/binSize);
      if(b0<0)b0=0; if(b1>bins-1)b1=bins-1; if(b1<b0)b1=b0;
      double share=(double)bv/(double)(b1-b0+1);
      for(int b=b0;b<=b1;b++){ vol[b]+=share; total+=share; }
   }
   if(total<=0) return;
   int pocBin=0; double pocVol=-1;
   for(int b=0;b<bins;b++){ if(vol[b]>pocVol){ pocVol=vol[b]; pocBin=b; } }
   g_lastPOC=lo+(pocBin+0.5)*binSize;
   // Value Area: ka fido POC bin ilaa POC_VA_Percent% volume-ka la daboolo -> VAH, VAL
   double target=total*POC_VA_Percent/100.0, acc=vol[pocBin];
   int up=pocBin, dn=pocBin;
   while(acc<target && (up<bins-1 || dn>0)){
      double vUp=(up<bins-1)?vol[up+1]:-1.0, vDn=(dn>0)?vol[dn-1]:-1.0;
      if(vUp>=vDn){ if(up<bins-1){ up++; acc+=vol[up]; } else if(dn>0){ dn--; acc+=vol[dn]; } }
      else        { if(dn>0){ dn--; acc+=vol[dn]; } else if(up<bins-1){ up++; acc+=vol[up]; } }
   }
   g_pocVAH=lo+(up+1.0)*binSize;
   g_pocVAL=lo+(dn*1.0)*binSize;
}
int CheckPOC_Signal(){
   if(Bars<50) return -1;
   int curDay=Day();
   if(!POC_Static || curDay!=g_pocDay || g_lastPOC<=0){ ComputePOCProfile(); g_pocDay=curDay; }
   if(g_lastPOC<=0 || g_pocVAH<=0 || g_pocVAL<=0) return -1;
   double atr=m4iATR(NULL,0,ATR_Period_Core,1); if(atr<=0||atr>1e6) return -1;
   double buf=POC_Buffer_ATR*atr, c1=Close[1];
   // v57 FIX #3: rejection DHAB ah. "||c1>o1" waa la saaray - shumac cagaaran KELIYA
   // ma aha diidmo; wuxuu u ogolaanayay ~50% shumac kasta inuu signal noqdo.
   bool bullRej=(IsPinBar(1,OP_BUY) ||IsEngulfing(1,OP_BUY) ||IsHammer(1,OP_BUY));
   bool bearRej=(IsPinBar(1,OP_SELL)||IsEngulfing(1,OP_SELL)||IsHammer(1,OP_SELL));
   if(POC_Reversion){
      // v57 FIX #1: entry-gu wuxuu ahaa POC = DHEXDA Value Area (proximal khaldan).
      // Khatar: nus range (POC->VAL->SL). Faa'iido: nus kale (POC->VAH). = 1:1 aan la badbaadin karin.
      // BUY  -> VAL (salka)  = proximal dhabta ah, SL cidhiidhi, TP = POC (magnet)
      if(Low[1]  <= g_pocVAL+buf && c1 > g_pocVAL && bullRej) return OP_BUY;
      // SELL -> VAH (dusha) = proximal dhabta ah
      if(High[1] >= g_pocVAH-buf && c1 < g_pocVAH && bearRej) return OP_SELL;
      return -1;
   } else {
      // BREAKOUT continuation: close ka baxsan Value Area + volume > SMA
      long v1=m4iVolume(NULL,0,1); double vsma=0; int n=MathMax(2,POC_Breakout_Vol_SMA);
      for(int k=2;k<n+2;k++) vsma+=(double)m4iVolume(NULL,0,k); vsma/=n;
      bool volOK=(vsma>0 && (double)v1>vsma);
      if(c1>g_pocVAH && volOK) return OP_BUY;   // baxay VAH + volume -> continuation kor
      if(c1<g_pocVAL && volOK) return OP_SELL;  // baxay VAL + volume -> continuation hoos
      return -1;
   }
}

// ---- 6. RSI STRATEGY ----
int CheckRSI_Signal(){ return -1; }   // TIRTIRAY: plain RSI (buuq badan) la saaray gebi ahaan - isticmaal RSI1H

// ---- 7. RSI 1H STRATEGY ----
// b26: MTF RSI REVERSAL - 1H RSI(30/70)=aasaas, gelitaan 5M candle rogmasho, SR confluence ikhtiyaari
int CheckRSI_1H_Signal(){
   double rsi=m4iRSI(Symbol(),PERIOD_H1,RSI_1H_Period,PRICE_CLOSE,1);   // 1H RSI = xoogga/aasaaska
   int dir=-1;
   if(rsi>=RSI_1H_Overbought) dir=OP_SELL;        // 1H son sare -> SELL bias
   else if(rsi<=RSI_1H_Oversold) dir=OP_BUY;      // 1H son hoose -> BUY bias
   if(dir==-1) return -1;
   // b29: entry KALIYA saacadaha cusub ee 1H (dhawaan ka dib 1H close) - RSI waa CLOSED 1H (shift 1)
   if(RSI_1H_Fresh_Only && Minute() >= RSI_1H_Entry_Window) return -1;
   if(RSI_1H_M5_Confirm){                          // gelitaanka lagu sameeyo 5M (5m keligiis waa diif)
      if(m4iBars(Symbol(),PERIOD_M5)<3) return -1;
      double c5=m4iClose(Symbol(),PERIOD_M5,1), o5=m4iOpen(Symbol(),PERIOD_M5,1);
      if(dir==OP_SELL && c5>=o5) return -1;        // sug 5M candle hoos u socda
      if(dir==OP_BUY  && c5<=o5) return -1;        // sug 5M candle kor u socda
   }
   if(RSI_1H_Require_SR){ if(CheckSR_Signal()!=dir) return -1; }   // SR reversal confluence
   return dir;
}

// ---- WRAPPER TO GET SIGNAL WITH STRATEGY-SPECIFIC FILTERS ----
// ---- b97: CHART PATTERN CONFIRMATION ----
// REVERSAL patterns (Double Top/Bottom) -> reversal strats (SR/BB/POC). CONTINUATION (pullback-in-trend/flag) -> trend strats (EMA/SMC/VSA).
input bool   Require_Pattern_Confirm = false;  // Kaliya ganacso marka qaab chart uu xaqiijiyo (on/off)
input double Pattern_Tol_ATR         = 0.5;    // Qaab: dulqaadka Double Top / Bottom (x ATR)
input int    Pattern_Lookback        = 40;     // Qaab: immisa shumac la baadhayo
double __patATR(){ double a=m4iATR(NULL,0,ATR_Period_Core,1); if(a<=0||a>1e6) a=10*GetPipSize(Symbol()); return a; }
bool IsDoubleBottom(){
   double atr=__patATR(); double tol=Pattern_Tol_ATR*atr; int len=3; int lb=MathMax(15,Pattern_Lookback);
   double l1=0,l2=0; int i1=-1,i2=-1,found=0;
   for(int i=len+1;i<lb && i<Bars-len-1;i++){ if(SMC_IsSwingLow(i,len)){ if(found==0){ l1=Low[i]; i1=i; found=1; } else { l2=Low[i]; i2=i; found=2; break; } } }
   if(found<2) return false;
   if(MathAbs(l1-l2)>tol) return false;                    // two lows ~ isle heer
   double peak=-1e18; for(int k=i1;k<=i2;k++) if(High[k]>peak) peak=High[k];
   if(peak-MathMax(l1,l2) < atr*0.6) return false;          // trough dhab ah dhexdooda
   if(Close[1]<=Close[2]) return false;                     // hadda kor u kacaya (confirm)
   return true;
}
bool IsDoubleTop(){
   double atr=__patATR(); double tol=Pattern_Tol_ATR*atr; int len=3; int lb=MathMax(15,Pattern_Lookback);
   double h1=0,h2=0; int i1=-1,i2=-1,found=0;
   for(int i=len+1;i<lb && i<Bars-len-1;i++){ if(SMC_IsSwingHigh(i,len)){ if(found==0){ h1=High[i]; i1=i; found=1; } else { h2=High[i]; i2=i; found=2; break; } } }
   if(found<2) return false;
   if(MathAbs(h1-h2)>tol) return false;
   double trough=1e18; for(int k=i1;k<=i2;k++) if(Low[k]<trough) trough=Low[k];
   if(MathMin(h1,h2)-trough < atr*0.6) return false;
   if(Close[1]>=Close[2]) return false;                     // hadda hoos u dhacaya
   return true;
}
bool IsBullContinuation(){
   int st=GetStructureTrend();
   double e50=m4iMA(NULL,0,50,0,MODE_EMA,PRICE_CLOSE,1), e200=m4iMA(NULL,0,200,0,MODE_EMA,PRICE_CLOSE,1);
   bool up=(st==1)||(e50>0&&e200>0&&e50>e200); if(!up) return false;
   bool pullback=false; for(int k=2;k<=6 && k+1<Bars;k++) if(Close[k]<Close[k+1]){ pullback=true; break; }
   return (pullback && Close[1]>Close[2]);                  // pullback -> resume up (flag/continuation)
}
bool IsBearContinuation(){
   int st=GetStructureTrend();
   double e50=m4iMA(NULL,0,50,0,MODE_EMA,PRICE_CLOSE,1), e200=m4iMA(NULL,0,200,0,MODE_EMA,PRICE_CLOSE,1);
   bool dn=(st==-1)||(e50>0&&e200>0&&e50<e200); if(!dn) return false;
   bool pullback=false; for(int k=2;k<=6 && k+1<Bars;k++) if(Close[k]>Close[k+1]){ pullback=true; break; }
   return (pullback && Close[1]<Close[2]);
}
bool PassPatternConfirm(int sig, int stratIdx){
   if(!Require_Pattern_Confirm) return true;
   if(stratIdx<0) return true;
   bool rev=IsReversalStrat(stratIdx), trend=IsTrendStrat(stratIdx);
   if(sig==OP_BUY){  if(rev) return IsDoubleBottom(); if(trend) return IsBullContinuation(); }
   else if(sig==OP_SELL){ if(rev) return IsDoubleTop(); if(trend) return IsBearContinuation(); }
   return true;
}

int GetCurrentStrategySignal(int &minT, double &buf) {
   int strat = (g_SelStrat>=0) ? g_SelStrat : ((Enable_Auto_Strategy && currentActiveStrategy>=0) ? currentActiveStrategy : (int)Select_Strategy);  // V56: control panel override
   int sig = -1; minT = 0; buf = 0;
   switch(strat) {
      case STRAT_SR: sig=CheckSR_Signal(); minT=SR_MinTouches; buf=SR_StrengthBuffer; break;
      case STRAT_BOLLINGER: sig=CheckBB_Signal(); minT=BB_MinTouches; buf=BB_StrengthBuffer; break;
      case STRAT_EMA: sig=CheckEMA_Signal(); break;
      case STRAT_SMC: sig=CheckSMC_Signal(); minT=SMC_MinTouches; buf=SMC_StrengthBuffer; break;
      case STRAT_VSA: sig=CheckVSA_Signal(); minT=VSA_MinTouches; buf=VSA_StrengthBuffer; break;
      case STRAT_POC: sig=CheckPOC_Signal(); break;   // b83
      // b82: RSI/RSI1H laga saaray
      default: sig=-1;
   }
   if(sig != -1 && !IsHigherRSIConfirmed(sig)) sig = -1;
   if(sig != -1 && Use_Zone_Filter_For_Entry && minT > 0 && !IsZoneStrong(sig, Close[1], minT, buf)) sig = -1;
   if(sig != -1 && !PassPatternConfirm(sig, strat)) sig = -1;   // b97: pattern confirmation
   return sig;
}

// ---- V53: dhammaan 7-da xeelad way shaqeeyaan. IsGoodStrat = kaliya calaamad "recommended" panel-ka ----
// EMA + SMC waa kuwa lagu taliyay (dahab panel-ka), laakiin dhammaan 7-da way trade-gareeyaan.
bool IsGoodStrat(int i){ return (i==STRAT_EMA || i==STRAT_SMC); }

// ---- MULTI-STRATEGY LOOP WITH PER-STRATEGY FILTERS (dhammaan 7-da) ----
bool GetBestMultiSignal(int &sig, int &stratIdx, int &minT, double &buf) {
   for(int i = 0; i < 7; i++) {
      if(!InGroup(i)) continue;   // b23: kaliya kooxda la doortay (TREND/REVERSAL/ALL)
      int s = -1; int t=0; double b=0;
      switch(i) {
         case STRAT_SR: s=CheckSR_Signal(); t=SR_MinTouches; b=SR_StrengthBuffer; break;
         case STRAT_BOLLINGER: s=CheckBB_Signal(); t=BB_MinTouches; b=BB_StrengthBuffer; break;
         case STRAT_EMA: s=CheckEMA_Signal(); break;
         case STRAT_SMC: s=CheckSMC_Signal(); t=SMC_MinTouches; b=SMC_StrengthBuffer; break;
         case STRAT_VSA: s=CheckVSA_Signal(); t=VSA_MinTouches; b=VSA_StrengthBuffer; break;
         case STRAT_POC: s=CheckPOC_Signal(); break;   // b83
         // b82: RSI/RSI1H laga saaray
      }
      if(s != -1 && !IsHigherRSIConfirmed(s)) s = -1;
      if(s != -1 && Use_Zone_Filter_For_Entry && t > 0 && !IsZoneStrong(s, Close[1], t, b)) s = -1;
      if(s != -1 && !PassPatternConfirm(s, i)) s = -1;   // b97: pattern confirmation
      if(s != -1) { sig = s; stratIdx = i; minT = t; buf = b; return true; }
   }
   return false;
}

// ---- QAYBTA 10 FIX (V35): CONSENSUS - dhawr strategy waa inay isku raacaan ----
bool GetConsensusSignal(int &sig, int &stratIdx, int &minT, double &buf) {
   int buyVotes=0, sellVotes=0, firstBuyIdx=-1, firstSellIdx=-1;
   int bt=0; double bb=0; int st=0; double sb=0;
   for(int i = 0; i < 7; i++) {
      if(!InGroup(i)) continue;   // b23: kaliya kooxda la doortay
      int s = -1; int t=0; double b=0;
      switch(i) {
         case STRAT_SR: s=CheckSR_Signal(); t=SR_MinTouches; b=SR_StrengthBuffer; break;
         case STRAT_BOLLINGER: s=CheckBB_Signal(); t=BB_MinTouches; b=BB_StrengthBuffer; break;
         case STRAT_EMA: s=CheckEMA_Signal(); break;
         case STRAT_SMC: s=CheckSMC_Signal(); t=SMC_MinTouches; b=SMC_StrengthBuffer; break;
         case STRAT_VSA: s=CheckVSA_Signal(); t=VSA_MinTouches; b=VSA_StrengthBuffer; break;
         case STRAT_POC: s=CheckPOC_Signal(); break;   // b83
         // b82: RSI/RSI1H laga saaray
      }
      if(s != -1 && !IsHigherRSIConfirmed(s)) s = -1;
      if(s != -1 && Use_Zone_Filter_For_Entry && t > 0 && !IsZoneStrong(s, Close[1], t, b)) s = -1;
      if(s != -1 && !PassPatternConfirm(s, i)) s = -1;   // b97: pattern confirmation
      if(s == OP_BUY)  { buyVotes++;  if(firstBuyIdx==-1){  firstBuyIdx=i;  bt=t; bb=b; } }
      else if(s == OP_SELL) { sellVotes++; if(firstSellIdx==-1){ firstSellIdx=i; st=t; sb=b; } }
   }
   // U baahan consensus + iska hor imaad la'aan (dhinaca badnaadaa mudan)
   if(buyVotes >= Consensus_Min_Agree && buyVotes > sellVotes) { sig=OP_BUY;  stratIdx=firstBuyIdx;  minT=bt; buf=bb; return true; }
   if(sellVotes>= Consensus_Min_Agree && sellVotes > buyVotes) { sig=OP_SELL; stratIdx=firstSellIdx; minT=st; buf=sb; return true; }
   return false;
}
void UpdateAllStrategySignals() {
   for(int i=0;i<7;i++) {
      int s=-1;
      switch(i){ case STRAT_SR:s=CheckSR_Signal();break; case STRAT_BOLLINGER:s=CheckBB_Signal();break; case STRAT_EMA:s=CheckEMA_Signal();break; case STRAT_SMC:s=CheckSMC_Signal();break; case STRAT_VSA:s=CheckVSA_Signal();break; case STRAT_POC:s=CheckPOC_Signal();break; }   // b83: POC ku daran
      if(s != -1 && !IsHigherRSIConfirmed(s)) s = -1;
      stratCurrentSignal[i] = s;
      if(s != -1) { datetime curBar = m4iTime(Symbol(), 0, 0); if(curBar != lastSignalBar[i]) { stratSignalCount[i]++; lastSignalBar[i] = curBar; } }
   }
}

void GetStrategySLTPMultipliers(double &slM, double &tpM, int s){   // b31 FIX: isticmaal xeelada DHABTA (stratIdx), maaha Select_Strategy oo keliya
   switch(s){ case STRAT_SR:slM=SR_SL_Multiplier;tpM=SR_TP_Multiplier;break; case STRAT_BOLLINGER:slM=BB_SL_Multiplier;tpM=BB_TP_Multiplier;break; case STRAT_EMA:slM=EMA_SL_Multiplier;tpM=EMA_TP_Multiplier;break; case STRAT_SMC:slM=SMC_SL_Multiplier;tpM=SMC_TP_Multiplier;break; case STRAT_VSA:slM=VSA_SL_Multiplier;tpM=VSA_TP_Multiplier;break; case STRAT_POC:slM=1.3;tpM=2.0;break; default:slM=1.5;tpM=3.0;break; }   // b83: POC (mean-revert, TP dhow)
}

//+------------------------------------------------------------------+
//| Lot sizing                                                       |
//+------------------------------------------------------------------+
double GetEffectiveRiskPercent(){   // b57: anti-martingale - marka khasaare isku xigta (trueConsecutiveLosses = ma reset-garo maalin kasta, kaliya GUUL) gaadho trigger-ka, risk hoos u dhig
   if(Enable_Dynamic_Risk && DynRisk_Loss_Trigger>0 && trueConsecutiveLosses>=DynRisk_Loss_Trigger && DynRisk_Reduced_Pct>0)
      return DynRisk_Reduced_Pct;
   return Risk_Percent;
}
double GetSmartLot(){
   double lot=InitialLot; double _p=GetPipSize(Symbol());
   if(g_UserLot>0){   // V56: Control panel lot (fixed) - ka hor auto/martingale
      return NormalizeLot(g_UserLot);   // b110 FIX
   }
   if(Auto_Lot){
      // V45: ROBUST AUTO-LOT (risk % of balance) - pip value fallback si aan 0.01 loogu noqon
      double riskAmt=AccountBalance()*(GetEffectiveRiskPercent()/100.0);   // b57: risk dynamic (anti-martingale)
      double slPipsD=StopLoss_Pips_Fixed;
      if(SLTP_Mode!=SLTP_FIXED){ double atr=m4iATR(Symbol(),0,ATR_Period_Core,1); if(atr<=0||atr>100000) atr=10*_p; slPipsD=atr/_p; if(slPipsD<10) slPipsD=10; }  // b14: lot-sizing SL estimate
      double spPips=(_p>0)?MarketInfo(Symbol(),MODE_SPREAD)*Point/_p:0;
      double totSL=slPipsD+spPips;
      double tv=MarketInfo(Symbol(),MODE_TICKVALUE), ts=MarketInfo(Symbol(),MODE_TICKSIZE);
      double pipVal;                                     // qiimaha 1 pip ee 1.0 lot (account currency)
      if(tv>0 && ts>0) pipVal=(tv/ts)*_p;
      else { double contract=MarketInfo(Symbol(),MODE_LOTSIZE); pipVal=(contract>0)?contract*_p:10.0; }  // fallback
      if(pipVal<=0) pipVal=10.0;
      if(totSL>0 && riskAmt>0){ lot=riskAmt/(totSL*pipVal); if(Enable_Debug_Log) Print("DEBUG LOT: ",DoubleToString(lot,2),"  | risk $",DoubleToString(riskAmt,2)," | SL ",DoubleToString(slPipsD,0),"p | bal $",DoubleToString(AccountBalance(),2)); }
   }
   if(Enable_Martingale){ int losses=0; double lastLot=InitialLot; for(int i=m4OrdersHistoryTotal()-1;i>=0;i--){ if(m4OrderSelect(i,SELECT_BY_POS,MODE_HISTORY)&&m4OrderMagicNumber()>=MagicNumber&&m4OrderMagicNumber()<=MagicNumber+110&&m4OrderSymbol()==Symbol()){ if(m4OrderProfit()<0){ losses++; lastLot=m4OrderLots(); if(losses>=MaxMrtLevels)break; } else break; } } if(losses>0 && losses<MaxMrtLevels) lot=lastLot*MrtMultiplier; else if(losses>=MaxMrtLevels) lot=InitialLot; double marginPerLot=MarketInfo(Symbol(),MODE_MARGINREQUIRED); if(marginPerLot>0 && AccountBalance()>0){ double maxLotByCap=(AccountBalance()*(Max_Martingale_Lot_Pct_Balance/100.0))/marginPerLot; if(lot>maxLotByCap) lot=maxLotByCap; } }
   return NormalizeLot(lot);   // b110 FIX
}
bool ValidateSLTP(double &sl,double &tp,int type,double price){
   double _p=GetPipSize(Symbol());
   double stopsLv =(double)SymbolInfoInteger(Symbol(),SYMBOL_TRADE_STOPS_LEVEL)*_Point;
   double freezeLv=(double)SymbolInfoInteger(Symbol(),SYMBOL_TRADE_FREEZE_LEVEL)*_Point;
   double min_stop=MathMax(stopsLv,freezeLv);
   double min_dist=MathMax(10*_p,min_stop)+Safety_Buffer_Pips*_p;
   double bid=SymbolInfoDouble(Symbol(),SYMBOL_BID), ask=SymbolInfoDouble(Symbol(),SYMBOL_ASK);
   if(type==OP_BUY){
      if(sl<=0||sl>=price) sl=price-min_dist;
      if(sl>bid-min_dist)  sl=bid-min_dist;
      if(tp<=0||tp<=price) tp=price+min_dist;
      if(tp<bid+min_dist)  tp=bid+min_dist;      // b110 FIX: TP sidoo kale StopLevel wuu ixtiraamayaa
   } else {
      if(sl<=0||sl<=price) sl=price+min_dist;
      if(sl<ask+min_dist)  sl=ask+min_dist;
      if(tp<=0||tp>=price) tp=price-min_dist;
      if(tp>ask-min_dist)  tp=ask-min_dist;      // b110 FIX
   }
   sl=NormalizeDouble(sl,_Digits); tp=NormalizeDouble(tp,_Digits);
   if(MathAbs(sl-price)<min_dist-_p*0.5) return false;
   if(MathAbs(tp-price)<min_dist-_p*0.5) return false;
   return true;
}
int __lotDigits(){ double st=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_STEP); if(st<=0) return 2; int d=0; double v=st; while(v<1.0 && d<8){ v*=10.0; d++; } return d; }
double NormalizeLot(double lot){   // b110 FIX: LotStep 0.001 ee crypto/indices ma burburayo
   double st=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_STEP);
   double mn=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN), mx=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MAX);
   if(st>0) lot=MathFloor(lot/st+1e-8)*st;
   lot=MathMax(lot,mn); lot=MathMin(lot,mx);
   return NormalizeDouble(lot,__lotDigits());
}
bool HasEnoughMargin(double lot){ double mReq=MarketInfo(Symbol(),MODE_MARGINREQUIRED)*lot; return (AccountFreeMargin()>=mReq*1.2); }

//+------------------------------------------------------------------+
//| Telegram / Web                                                   |
//+------------------------------------------------------------------+
string UrlEncode(string s){ string r=""; for(int i=0;i<StringLen(s);i++){ ushort c=StringGetCharacter(s,i); if((c>='A'&&c<='Z')||(c>='a'&&c<='z')||(c>='0'&&c<='9')||c=='-'||c=='_'||c=='.'||c=='~') r+=ShortToString(c); else if(c==' ') r+="%20"; else r+="%"+StringFormat("%02X",c); } return r; }
void SendTelegram(string msg){ if(IsTesting()) return; if(!EnableTelegram||StringLen(TG_BotToken)<5||StringLen(TG_ChatID)<3) return; string url="https://api.telegram.org/bot"+TG_BotToken+"/sendMessage", headers="Content-Type: application/x-www-form-urlencoded\r\n", body="chat_id="+TG_ChatID+"&text="+UrlEncode(msg)+"&parse_mode=HTML"; uchar postData[], result[]; string rHdrs; int bodyLen=StringToCharArray(body,postData,0,WHOLE_ARRAY,CP_UTF8)-1; if(bodyLen<=0)return; ArrayResize(postData,bodyLen); int res=WebRequest("POST",url,headers,5000,postData,result,rHdrs); if(res==-1){ consecutiveWebFails++; Print("TELEGRAM FAILED err=", GetLastError()); } else { consecutiveWebFails=0; } }
void SendTelegramDailySummary(){ if(!TG_DailySummary)return; CalculateWinRate(); int _tot=totalWins+totalLosses; double _wr=(_tot>0)?(double)totalWins/_tot*100.0:0; string sNamesD[7]={"SR","BB","EMA","SMC","VSA","POC","-"}; int _sIdx=(g_SelStrat>=0)?g_SelStrat:(int)Select_Strategy; string _sName=(_sIdx>=0&&_sIdx<7)?sNamesD[_sIdx]:"?"; string msg="🏅〔 WARBIXIN MAALINLE 〕🏅\n┏━━━━━━━━━━━━━━━┓\n   📅 "+TimeToString(TimeCurrent(),TIME_DATE)+"\n┗━━━━━━━━━━━━━━━┛\n🏆 Win "+IntegerToString(totalWins)+" | Loss "+IntegerToString(totalLosses)+"   ("+DoubleToString(_wr,0)+"%)\n💰 Faa'iido maanta:  $"+DoubleToString(todayClosedProfit,2)+"\n📈 Balance:  $"+DoubleToString(AccountBalance(),2)+"\n📊 Xeelad:  "+_sName+"   |   🔄 Open:  "+IntegerToString(CountAllOrders())+"   |   💱 "+Symbol()+"\n━━━━━━━━━━━━━━━━\n🌐 @MOHAPROLIVE_BOT"; SendTelegram(msg); }
void SendTelegramWeeklySummary(){ if(!TG_WeeklySummary)return; CalculateWinRate(); string msg="MOHA PRO - TODOBAADKA\nHaraaga: $"+DoubleToString(AccountBalance(),2)+"\nFaa'iidada: $"+DoubleToString(weeklyClosedProfit,2)+"\nWin Rate: "+DoubleToString(winRatePercent,1)+"%\nWins: "+IntegerToString(totalWins)+" Losses: "+IntegerToString(totalLosses); SendTelegram(msg); }
string DailySummaryLockFile(){ return "MohaPro_DailySummary_Lock.txt"; }   // b52 FIX: FILE_COMMON = la wadaago dhammaan charts/EA (isku PC), si "Maalin cusub" aan loogu diri mar walba chart kasta
bool DailySummaryClaimedToday(){
   int h=FileOpen(DailySummaryLockFile(),FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(h==INVALID_HANDLE) return false;
   string dLine=FileReadString(h); FileClose(h);
   return (dLine==TimeToString(TimeCurrent(),TIME_DATE));
}
void DailySummaryClaimToday(){
   int h=FileOpen(DailySummaryLockFile(),FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(h==INVALID_HANDLE) return;
   FileWriteString(h,TimeToString(TimeCurrent(),TIME_DATE));
   FileClose(h);
}
void TG_AlertRejected(string reason){   // b52 FIX: fariin "trade la diiday" - cooldown si aan buuq loo helin
   // FIX V56.1: Journal-ka had iyo jeer ku qor (Telegram on/off korkeeda kama xirna) - si aad Expert tab-ka uga aragto sababta trade-ku loo diiday
   Print("[MOHA REJECT] ", Symbol(), " | ", reason);
   if(!EnableTelegram || !TG_RejectedAlerts) return;
   if(TimeCurrent()-g_lastRejectAlertTime < TG_Reject_Cooldown_Min*60) return;
   g_lastRejectAlertTime=TimeCurrent();
   SendTelegram("⚠️ TRADE LA DIIDAY\n💱 "+Symbol()+"\n📋 Sababta: "+reason);
}

// ---- QAYBTA 1 FIX: real-time news check, per-currency + per-impact ----
bool IsNewsActive() {
   if(!EnableNewsFilter) return false;
   if(IsTesting()) return false;   // V40: WebRequest unavailable in Strategy Tester -> don't block backtest
   if(TimeCurrent() - g_LastNewsFetch >= News_Refresh_Minutes * 60) FetchNewsCalendar();   // V43: respect timer even on failure (no 429 hammering)
   if(!g_NewsDataValid) return News_Block_If_Fetch_Fails; // AMMAAN: haddii aan xog la helin, ku fasax input-ka

   string symClean = StringSubstr(Symbol(), 0, 6);
   string baseCcy  = StringSubstr(symClean, 0, 3);
   string quoteCcy = StringSubstr(symClean, 3, 3);
   datetime now = TimeCurrent();

   for(int i = 0; i < ArraySize(g_NewsEvents); i++) {
      if(g_NewsEvents[i].country != baseCcy && g_NewsEvents[i].country != quoteCcy) continue;
      bool impactOk = (g_NewsEvents[i].impact == "High"   && News_Filter_High)
                    || (g_NewsEvents[i].impact == "Medium" && News_Filter_Medium)
                    || (g_NewsEvents[i].impact == "Low"    && News_Filter_Low);
      if(!impactOk) continue;
      datetime s = g_NewsEvents[i].time - MinutesBeforeNews * 60;
      datetime e = g_NewsEvents[i].time + MinutesAfterNews * 60;
      if(now >= s && now <= e) return true;
   }
   return false;
}

//+------------------------------------------------------------------+
//| Smart Order                                                      |
//+------------------------------------------------------------------+
// ---- TREND-FOLLOW GATE: EMA trend filter + Volume confirmation (xeer cad) ----
bool TrendFollow_OK(int type){
   if(!Enable_TrendFollow) return true;
   // 1) EMA trend filter: EMA50 vs EMA200 + qiimaha vs EMA50
   double e50 =m4iMA(Symbol(),0,FastEMA,0,MODE_EMA,PRICE_CLOSE,1);
   double e200=m4iMA(Symbol(),0,SlowEMA,0,MODE_EMA,PRICE_CLOSE,1);
   if(e50<=0||e200<=0) return false;
   if(type==OP_BUY  && !(e50>e200 && Close[1]>e50)) return false;   // uptrend + qiime kor EMA50
   if(type==OP_SELL && !(e50<e200 && Close[1]<e50)) return false;   // downtrend + qiime hoos EMA50
   // 2) Volume (VSA) confirmation: xoogga trend-ku ha taageero
   if(TF_Require_Volume){
      long v0=m4iVolume(Symbol(),0,1); int lb=MathMax(2,VSA_Lookback); long sum=0;
      for(int k=2;k<lb+2;k++) sum+=m4iVolume(Symbol(),0,k);
      double avg=(sum>0)?(double)sum/lb:0;
      if(avg>0 && (double)v0 < avg*TF_Volume_Ratio) return false;   // volume daciif -> ha gelin
   }
   // 3) MACD momentum confirmation (b27): main vs signal waa inuu raaco jihada
   if(TF_Require_MACD){
      double mMain=m4iMACD(Symbol(),0,MACD_Fast,MACD_Slow,MACD_Signal,PRICE_CLOSE,MODE_MAIN,1);
      double mSig =m4iMACD(Symbol(),0,MACD_Fast,MACD_Slow,MACD_Signal,PRICE_CLOSE,MODE_SIGNAL,1);
      if(type==OP_BUY  && !(mMain>mSig)) return false;   // BUY: MACD momentum kor
      if(type==OP_SELL && !(mMain<mSig)) return false;   // SELL: MACD momentum hoos
   }
   return true;
}
// b47 FIX (Qodob 7): Multi-Strategy/ALL mode - xeelado kala duwan (tusaale EMA=BUY, RSI1H=SELL) isku waqti furan kara, isu diidan - xannib
bool HasOpposingOpenPosition(int type){
   for(int i=m4OrdersTotal()-1;i>=0;i--){
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(m4OrderSymbol()!=Symbol()) continue;
      int mg=m4OrderMagicNumber(); if(mg<MagicNumber+100||mg>MagicNumber+106) continue;
      if(m4OrderType()==OP_BUY  && type==OP_SELL) return true;
      if(m4OrderType()==OP_SELL && type==OP_BUY)  return true;
   }
   return false;
}
// b81: Tiri trade-yada MOHA (xeelad kasta) ee furan lammaanahan (symbol-kan). Magic range = strategy-gated.
int CountMohaOnSymbol() {
   int c=0;
   for(int i=0;i<m4OrdersTotal();i++){
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      if(m4OrderSymbol()!=Symbol()) continue;
      int mg=m4OrderMagicNumber();
      if(mg>=MagicNumber+100 && mg<=MagicNumber+106) c++;
   }
   return c;
}
void SmartOrder(int type, double lot, int stratIdx, string note) {
   // b81 XEER: hal trade / lammaan. Haddii lammaan uu HORE u leeyahay trade MOHA ah (xeelad kasta) -> jooji, xeelad kale ma furto.
   if(One_Trade_Per_Symbol && CountMohaOnSymbol()>=1){ missedOpportunities++; TG_AlertRejected("One-Trade-Per-Symbol: lammaanahan trade ayuu hore u leeyahay - xeelad kale lama furayo"); return; }
   if(Enable_Conflict_Guard && HasOpposingOpenPosition(type)){ missedOpportunities++; TG_AlertRejected("Conflict Guard - xeelad kale isu diidan furan"); return; }   // b47: xeelad kale meel ka dhow buu isu diidan yahay
   if(IsTrendStrat(stratIdx) && !TrendFollow_OK(type)){ missedOpportunities++; return; }   // b21: trend-gate KALIYA xeeladaha trend (EMA/SMC/VSA); reversal(SR/BB/RSI1H) waa ka reeban
   // b30: EXTENSION GUARD - ha chase-garayn spike (qiime aad uga fog EMA50). Trend-strats KALIYA.
   if(IsTrendStrat(stratIdx) && Enable_Extension_Guard){
      double eg_ema=m4iMA(Symbol(),0,FastEMA,0,MODE_EMA,PRICE_CLOSE,1);
      double eg_atr=m4iATR(Symbol(),0,ATR_Period_Core,1);
      if(eg_ema>0 && eg_atr>0 && eg_atr<100000){
         if(MathAbs(Close[1]-eg_ema) > Max_Extension_ATR*eg_atr){ missedOpportunities++; return; }  // aad u durugsan -> sug dib-u-dhac
      }
   }
   int magic=GetMagicForStrategy(stratIdx);
   if(CountByMagicRange(magic,magic)>=2||CountAllOrders()>=Max_Open_Trades||(int)MarketInfo(Symbol(),MODE_SPREAD)>GetMaxAllowedSpread()||!HasEnoughMargin(lot)||!PortfolioRiskCheck(lot)) return;
   
   // ============================================================
   // TREND FILTER - LABADA EMA (1H iyo 4H)
   // ============================================================
   if(EnableTrendFilter){
      double ema1H = m4iMA(Symbol(), PERIOD_H1, 200, 0, MODE_EMA, PRICE_CLOSE, 1);
      double ema4H = m4iMA(Symbol(), PERIOD_H4, 200, 0, MODE_EMA, PRICE_CLOSE, 1);
      if(ema1H <= 0 || ema4H <= 0) { missedOpportunities++; return; }
      
      // BUY oggolow haddii qiimuhu ka sareeyo labada EMA
      if(type == OP_BUY && (Close[1] < ema1H || Close[1] < ema4H)) {
         missedOpportunities++;
         return;
      }
      // SELL oggolow haddii qiimuhu ka hooseeyo labada EMA
      if(type == OP_SELL && (Close[1] > ema1H || Close[1] > ema4H)) {
         missedOpportunities++;
         return;
      }
   }
   
   // QAYBTA 4 FIX (V37): market-structure trend filter
   if(Enable_Structure_Filter){
      int st=GetStructureTrend();
      if(type==OP_BUY  && st==-1){ missedOpportunities++; return; }
      if(type==OP_SELL && st==1 ){ missedOpportunities++; return; }
   }
   // QAYBTA 6 FIX (V38): require minimum entry confirmations
   if(Enable_Entry_Confirmation){
      if(CountEntryConfirmations(type) < ((Loose_Entry_Mode||Simple_Mode)?1:Min_Entry_Confirmations)){ missedOpportunities++; return; }   // b90/b92: Loose/Simple -> 1 confirmation kaliya
   }
   double _p=GetPipSize(Symbol()), price=(type==OP_BUY)?MarketInfo(Symbol(),MODE_ASK):MarketInfo(Symbol(),MODE_BID), sl=0,tp=0;
   bool useRealSLTP=!Enable_Stealth_Mode;
   if(useRealSLTP){
      // ===== b14: SL/TP MODE (hal doorasho cad) =====
      // v57 FIX #5 (MUHIIM): POC waa laga reebay FIXED-ka. SLTP_Mode default-ku waa SLTP_FIXED,
      // taasoo macnaheedu yahay in koodhka SL/TP ee POC-ga (else-ka hoose) uusan WALIGIIS shaqeyn -
      // trade kastaa wuxuu isticmaalayay 30/80 pip go'an oo aan la xiriirin VAL/VAH/POC.
      if((Simple_Mode || SLTP_Mode==SLTP_FIXED) && stratIdx!=STRAT_POC){          // b92: Simple_Mode -> khasab FIXED pips | pip go'an cad
         if(type==OP_BUY){ sl=price-StopLoss_Pips_Fixed*_p; tp=price+TakeProfit_Pips_Fixed*_p; }
         else            { sl=price+StopLoss_Pips_Fixed*_p; tp=price-TakeProfit_Pips_Fixed*_p; }
      } else {                            // ATR ama SMC_PRO (base = ATR)
         double atr=m4iATR(Symbol(),0,ATR_Period_Core,1); if(atr<=0||atr>100000) atr=10*_p; double slM,tpM; GetStrategySLTPMultipliers(slM,tpM,stratIdx);
         if(type==OP_BUY){ sl=price-atr*slM; tp=price+atr*tpM; } else { sl=price+atr*slM; tp=price-atr*tpM; }
         if(SLTP_Mode==SLTP_SMC_PRO && stratIdx==STRAT_SMC){   // structural SL + liquidity TP
            if(SMC_Structural_SL && g_SMC_SL>0){ if(type==OP_BUY && g_SMC_SL<price) sl=g_SMC_SL-Safety_Buffer_Pips*_p; else if(type==OP_SELL && g_SMC_SL>price) sl=g_SMC_SL+Safety_Buffer_Pips*_p; }
            if(SMC_SL_Max_ATR>0){ double maxD=SMC_SL_Max_ATR*atr; if(MathAbs(price-sl)>maxD){ sl=(type==OP_BUY)?price-maxD:price+maxD; } }   // b15: xadka khasaaraha
            if(Use_Liquidity_TP){ double lqTP=0; if(FindLiquidityTarget(type,price,SMC_Lookback,lqTP)){ double slD=MathAbs(price-sl), tpD=MathAbs(lqTP-price); if(slD>0 && tpD>=slD*Liquidity_TP_Min_R){ tp=(type==OP_BUY)?lqTP-Safety_Buffer_Pips*_p:lqTP+Safety_Buffer_Pips*_p; } } }
         }
         // b32: REVERSAL structural SL - dhig SL ka baxsan swing (heerka la fade-garay), maaha ATR cidhiidhi
         if(IsReversalStrat(stratIdx) && Rev_Structural_SL){
            int rlb=MathMax(3,Rev_SL_Lookback);
            double swLo=Low[m4iLowest(NULL,0,MODE_LOW,rlb,1)];
            double swHi=High[m4iHighest(NULL,0,MODE_HIGH,rlb,1)];
            double rbuf=Safety_Buffer_Pips*_p;
            if(type==OP_BUY){ double sSL=swLo-rbuf; if(sSL<sl) sl=sSL; }   // ka hooseeya swing low
            else            { double sSL=swHi+rbuf; if(sSL>sl) sl=sSL; }   // ka sarreeya swing high
            double rmaxD=Rev_SL_Max_ATR*atr; if(rmaxD>0 && MathAbs(price-sl)>rmaxD){ sl=(type==OP_BUY)?price-rmaxD:price+rmaxD; }   // cap
         }
         // v57 FIX #2: SL salka/dusha HOOSTOODA (maaha POC), TP = POC (magnet dhow).
         // Hore: SL=POC-1.5ATR halka entry-gu POC yahay -> SL ballaaran, TP fog = RR 1:1.
         if(stratIdx==STRAT_POC && g_lastPOC>0 && g_pocVAL>0 && g_pocVAH>0){
            if(type==OP_BUY){ sl=g_pocVAL-POC_SL_ATR*atr; if(g_lastPOC>price+_p) tp=g_lastPOC; }
            else            { sl=g_pocVAH+POC_SL_ATR*atr; if(g_lastPOC<price-_p) tp=g_lastPOC; }
         }
      }
      // ===== Control panel manual override (sare ugu muhiimsan) =====
      if(g_UserSL>0){ if(type==OP_BUY) sl=price-g_UserSL*_p; else sl=price+g_UserSL*_p; }
      if(g_UserTP>0){ if(type==OP_BUY) tp=price+g_UserTP*_p; else tp=price-g_UserTP*_p; }
      // ===== R:R enforce (haddii TP gacan lama dhigin) =====
      if(Enforce_Min_RR && Min_RR_Ratio>0 && g_UserTP<=0){ double slDist=MathAbs(price-sl); double wantTP=slDist*Min_RR_Ratio; if(type==OP_BUY){ if((tp-price)<wantTP) tp=price+wantTP; } else { if((price-tp)<wantTP) tp=price-wantTP; } }
      if(!Simple_Mode && g_TPL_On && g_UserTP<=0){ double slD4=MathAbs(price-sl); if(slD4>0){ double capR=TPL_Trail_Final?10.0:g_TP_R[3]; tp=(type==OP_BUY)?price+capR*slD4:price-capR*slD4; } }   // b63/b64/b92: TP-ladder - TP = TP4 heerka (Simple_Mode -> daa TP go'an)
      if(!ValidateSLTP(sl,tp,type,price))return;
      // b40 FIX: Auto-Lot wuxuu isticmaalay ATR qiyaas SL AYAA lot-ka lagu xisaabiyay - kadibna structural SL (SMC/b32 Reversal) ayaa SL dhabta ah ka ballaadhin kara qiyaastaas, taasoo khasaaraha dollar-ka uga weynayn 1% Risk_Percent la dejiyay (sida trade-yadii $1465/$1486 ee la arkay). Halkan lot-ka waxaa dib loogu xisaabinayaa SL-ka DHABTA AH ee la go'aamiyay.
      if(Auto_Lot && g_UserLot<=0){
         double finalSLDist=MathAbs(price-sl);
         if(finalSLDist>0){
            double tvR=MarketInfo(Symbol(),MODE_TICKVALUE), tsR=MarketInfo(Symbol(),MODE_TICKSIZE);
            double pipValR; if(tvR>0&&tsR>0) pipValR=(tvR/tsR)*_p; else { double contractR=MarketInfo(Symbol(),MODE_LOTSIZE); pipValR=(contractR>0)?contractR*_p:10.0; }
            if(pipValR<=0) pipValR=10.0;
            double slPipsFinal=finalSLDist/_p, riskAmtR=AccountBalance()*(GetEffectiveRiskPercent()/100.0);   // b57: risk dynamic (anti-martingale)
            if(slPipsFinal>0){
               lot=NormalizeLot(riskAmtR/(slPipsFinal*pipValR));   // b110 FIX
               // b110 FIX: lot-ku wuu isbeddelay -> DIB u hubi margin/portfolio (hore waa la dhaafi jiray)
               if(!HasEnoughMargin(lot) || !PortfolioRiskCheck(lot)){ missedOpportunities++; return; }
               if(Enable_Debug_Log) Print("DEBUG LOT-RECALC (final SL ",DoubleToString(slPipsFinal,0),"p): lot -> ",DoubleToString(lot,2));
            }
         }
      } }
   PatternResult pat=GetAIPatternScore(type); if(Enable_AI_Patterns&&pat.score<AI_Pattern_Min_Score&&pat.score!=99){ missedOpportunities++; return; }
   string stNames[7]={"SR","BB","EMA","SMC","VSA","POC","-"}; string comment="MP|"+stNames[stratIdx]+"|"+pat.name; if(StringLen(comment)>31) comment=StringSubstr(comment,0,31);   // PRO (V41): MT4 comment max 31 chars
   double sendSl=sl, sendTp=tp; if(UseVirtualOrders){ sendSl=0; sendTp=0; }
   int slippagePoints=(int)MathRound(MaxSlippagePips*(_p/Point)); if(slippagePoints<1) slippagePoints=1;
   long ticket=SmartOrderSend(Symbol(),type,lot,price,slippagePoints,sendSl,sendTp,comment,magic);
   if(ticket>0){ tradesOpenedToday++; StoreEntryScore(ticket,pat.score); lastTradeTime=TimeCurrent(); lastTradeBarTime=m4iTime(Symbol(),0,0); lastTradeBarTime_1H=m4iTime(Symbol(),PERIOD_H1,0); if(UseVirtualOrders) GetOrCreateVirtualState(ticket,sl,tp); if(Show_Trade_Markers) DrawRRBox(ticket); if(EnableTelegram&&TG_TradeDetails){ double _pp=GetPipSize(Symbol()); double slp=(_pp>0)?MathAbs(price-sl)/_pp:0,tpp=(_pp>0)?MathAbs(tp-price)/_pp:0; double rr=(slp>0)?tpp/slp:0; string dir=(type==OP_BUY)?"🟢 BUY":"🔴 SELL"; string msg="⚡〔 MOHA PRO · SIGNAL 〕⚡\n┏━━━━━━━━━━━━━━━┓\n  "+dir+"  ·  "+Symbol()+"\n  📊 "+stNames[stratIdx]+"   ·   🕐 "+TimeToString(TimeCurrent(),TIME_MINUTES)+"\n┗━━━━━━━━━━━━━━━┛\n📍 Entry:  "+DoubleToString(price,Digits)+"\n🛑 SL:  "+DoubleToString(sl,Digits)+"   ("+DoubleToString(slp,0)+"p)\n🎯 TP:  "+DoubleToString(tp,Digits)+"   ("+DoubleToString(tpp,0)+"p)\n⚖ R:R  1:"+DoubleToString(rr,1)+"    💰 "+DoubleToString(lot,2)+" lot\n━━━━━━━━━━━━━━━━\n🌐 @MOHAPROLIVE_BOT"; SendTelegram(msg); } SaveTradeScreenshot("OPEN"); }
}

int GetOrCreateVirtualState(long ticket, double vsl=0, double vtp=0){ for(int j=0;j<ArraySize(virtualStates);j++) if(virtualStates[j].ticket==ticket) return j; int sz=ArraySize(virtualStates); ArrayResize(virtualStates,sz+1); virtualStates[sz].ticket=ticket; virtualStates[sz].vsl=vsl; virtualStates[sz].vtp=vtp; virtualStates[sz].partialDone=false; return sz; }
void PruneVirtualStates(){ for(int j=ArraySize(virtualStates)-1;j>=0;j--){ if(!m4OrderSelect(virtualStates[j].ticket,SELECT_BY_TICKET,MODE_TRADES)){ int last=ArraySize(virtualStates)-1; virtualStates[j]=virtualStates[last]; ArrayResize(virtualStates,last); } } }

void ManageActiveTrades() {
   double _p=GetPipSize(Symbol()), min_stop=MarketInfo(Symbol(),MODE_STOPLEVEL)*Point, spread=MarketInfo(Symbol(),MODE_SPREAD)*Point;
   // ---- QAYBTA 9 FIX (V36): ATR-based BE/Trailing distances (pips) ----
   double bePips=BreakEvenPips, trStartPips=TrailingStartPips, trStepPips=TrailingStepPips;
   if(Enable_ATR_Trailing){ double atrMg=m4iATR(Symbol(),0,ATR_Period_Core,1); if(atrMg>0 && atrMg<100000 && _p>0){ double atrPips=atrMg/_p; bePips=atrPips*ATR_BE_Mult; trStartPips=atrPips*ATR_Trail_Start_Mult; trStepPips=atrPips*ATR_Trail_Step_Mult; if(trStepPips<1) trStepPips=1; if(trStartPips<trStepPips) trStartPips=trStepPips; } }
   for(int i=m4OrdersTotal()-1;i>=0;i--){
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES)||m4OrderSymbol()!=Symbol()) continue;
      int mg=m4OrderMagicNumber(); if(mg<MagicNumber+100||mg>MagicNumber+106) continue;
      if(!Simple_Mode && EnablePartialClose){   // b92: Simple_Mode -> ha gooyn
         long ticketPC=m4OrderTicket(); double openPC=m4OrderOpenPrice(); double currPC=(m4OrderType()==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK);
         double profitPipsPC=(m4OrderType()==OP_BUY)?(currPC-openPC)/_p:(openPC-currPC)/_p;
         int vsIdx=GetOrCreateVirtualState(ticketPC);
         if(!virtualStates[vsIdx].partialDone && profitPipsPC>=PartialClosePips){
            double closeLotPC=NormalizeDouble(m4OrderLots()*PartialClosePercent/100.0,2);
            if(closeLotPC>=MarketInfo(Symbol(),MODE_MINLOT) && closeLotPC<m4OrderLots()){ bool closedPC=m4OrderClose(ticketPC,closeLotPC,currPC,3,clrYellow); if(!closedPC) LogTradeOpFailure("PartialClose",ticketPC,GetLastError()); }
            virtualStates[vsIdx].partialDone=true;
         }
      }
      if(UseVirtualOrders && m4OrderStopLoss()==0 && m4OrderTakeProfit()==0){
         long ticketVO=m4OrderTicket(); int vIdx=-1; for(int j=0;j<ArraySize(virtualStates);j++) if(virtualStates[j].ticket==ticketVO){ vIdx=j; break; }
         if(vIdx>=0 && (virtualStates[vIdx].vsl>0 || virtualStates[vIdx].vtp>0)){
            double currVO=(m4OrderType()==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK);
            bool hitSL=(m4OrderType()==OP_BUY)?(virtualStates[vIdx].vsl>0&&currVO<=virtualStates[vIdx].vsl):(virtualStates[vIdx].vsl>0&&currVO>=virtualStates[vIdx].vsl);
            bool hitTP=(m4OrderType()==OP_BUY)?(virtualStates[vIdx].vtp>0&&currVO>=virtualStates[vIdx].vtp):(virtualStates[vIdx].vtp>0&&currVO<=virtualStates[vIdx].vtp);
            if(hitSL||hitTP){ bool closedVO=m4OrderClose(ticketVO,m4OrderLots(),currVO,3,clrWhite); if(!closedVO) LogTradeOpFailure("VirtualOrderClose",ticketVO,GetLastError()); }
         }
         continue;
      }
      if(Enable_Stealth_Mode&&SLTP_Mode==SLTP_FIXED&&m4OrderStopLoss()==0&&m4OrderTakeProfit()==0){
         double open=m4OrderOpenPrice(), curr=(m4OrderType()==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK);
         long ticketNow=m4OrderTicket();
         if(m4OrderType()==OP_BUY){ if(curr<=open-StopLoss_Pips_Fixed*_p||curr>=open+TakeProfit_Pips_Fixed*_p){ bool closed=m4OrderClose(ticketNow,m4OrderLots(),curr,3,clrWhite); if(!closed) LogTradeOpFailure("StealthClose_BUY",ticketNow,GetLastError()); } }
         else { if(curr>=open+StopLoss_Pips_Fixed*_p+spread||curr<=open-TakeProfit_Pips_Fixed*_p){ bool closed=m4OrderClose(ticketNow,m4OrderLots(),curr,3,clrWhite); if(!closed) LogTradeOpFailure("StealthClose_SELL",ticketNow,GetLastError()); } }
         continue;
      }
      if(!g_TPL_On && g_BE_On){   // b99: BE waxaa maamula badhanka CONTROL PANEL (g_BE_On) - wuu la shaqeeyaa FIXED iyo ATR labadaba, xitaa Simple_Mode
         // V60: CAQLI-GAL Break-Even - ku saleysan R (SL distance), maaha 15p->2p
         double slDist=MathAbs(m4OrderOpenPrice()-m4OrderStopLoss());
         if(slDist<=0) slDist=StopLoss_Pips_Fixed*_p;   // fallback (stealth/virtual)
         if(slDist>0){
            double trigDist=slDist*BE_Trigger_R;   // faa'iido loo baahan yahay ka hor BE (default 1R)
            double lockDist=slDist*BE_Lock_R;       // inta faa'iido la xirayo (default 0.5R)
            double curr2=(m4OrderType()==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK);
            double beTrig=m4OrderOpenPrice()+(m4OrderType()==OP_BUY?trigDist:-trigDist);
            double lockSL=NormalizeDouble(m4OrderOpenPrice()+(m4OrderType()==OP_BUY?lockDist:-lockDist),Digits);
            bool met=(m4OrderType()==OP_BUY&&curr2>beTrig)||(m4OrderType()==OP_SELL&&curr2<beTrig);
            bool needMove=(m4OrderType()==OP_BUY&&(m4OrderStopLoss()<lockSL||m4OrderStopLoss()==0))||(m4OrderType()==OP_SELL&&(m4OrderStopLoss()>lockSL||m4OrderStopLoss()==0));
            if(met&&needMove){ long ticketBE=m4OrderTicket(); bool mod=m4OrderModify(ticketBE,m4OrderOpenPrice(),lockSL,m4OrderTakeProfit(),0,clrGold); if(!mod) LogTradeOpFailure("BreakEvenModify",ticketBE,GetLastError()); else if(Enable_Debug_Log) Print("DEBUG BE: ticket ",ticketBE," SL -> ",DoubleToString(lockSL,Digits)," (breakeven +",DoubleToString(BE_Lock_R,1),"R)"); }  // V60: lock 0.5R
         }
      }
      // b17: DOLLAR PROFIT LOCK - marka faa'iido ($) gaaro, ILAALI SL laakiin HA XIRIN (trade sii socdo)
      if(!Simple_Mode && !g_TPL_On && Enable_USD_ProfitLock && LockProfit_USD>0){   // b63/b92: TP-ladder ama Simple_Mode -> USD-lock waa la damiyaa
         double prof=m4OrderProfit();   // floating $ (excl comm/swap)
         // b42 FIX: LockProfit_USD ($20) waa qiimo GO'AN aan la xiriirin account-ka/R-ka - account weyn (tusaale $99k, ~$990=1R) ayuu $20 ku xannibi jiray guulaha (2% R kaliya) ka hor inta ay ordi karin. Hadda xadka ugu yar ee lock-ku bilaabmayo waa R(SL dhabta ah) DHABTA AH x LockProfit_Trigger_R, LockProfit_USD-na waa dhalka ugu yar (account yar u ilaalin).
         double slDistPL=MathAbs(m4OrderOpenPrice()-m4OrderStopLoss()); double trigUSD=LockProfit_USD;
         if(slDistPL>0){ double tvPL=MarketInfo(Symbol(),MODE_TICKVALUE), tsPL=MarketInfo(Symbol(),MODE_TICKSIZE); if(tvPL>0&&tsPL>0){ double dollarR=m4OrderLots()*slDistPL*(tvPL/tsPL); if(dollarR*LockProfit_Trigger_R>trigUSD) trigUSD=dollarR*LockProfit_Trigger_R; } }
         if(prof>=trigUSD){
            double open3=m4OrderOpenPrice();
            double cur3=(m4OrderType()==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK);
            double diff=cur3-open3;
            if(MathAbs(diff)>_p*0.5){
               double perUnit=prof/diff;                          // $ per price unit
               double lockUSD=prof*(LockProfit_KeepPct/100.0);    // ilaali % faa'iido
               if(lockUSD<0.01) lockUSD=0.01;
               double lockPrice=NormalizeDouble(open3+lockUSD/perUnit,Digits);
               if(m4OrderType()==OP_BUY){ if(lockPrice>m4OrderStopLoss() && lockPrice<cur3-min_stop){ long tk=m4OrderTicket(); bool m=m4OrderModify(tk,open3,lockPrice,m4OrderTakeProfit(),0,clrGold); if(!m) LogTradeOpFailure("USDLock_BUY",tk,GetLastError()); else if(Enable_Debug_Log) Print("DEBUG USD-LOCK: SL -> ",DoubleToString(lockPrice,Digits)," (locked $",DoubleToString(lockUSD,2),")"); } }
               else { if((m4OrderStopLoss()==0||lockPrice<m4OrderStopLoss()) && lockPrice>cur3+min_stop){ long tk2=m4OrderTicket(); bool m2=m4OrderModify(tk2,open3,lockPrice,m4OrderTakeProfit(),0,clrGold); if(!m2) LogTradeOpFailure("USDLock_SELL",tk2,GetLastError()); else if(Enable_Debug_Log) Print("DEBUG USD-LOCK: SL -> ",DoubleToString(lockPrice,Digits)," (locked $",DoubleToString(lockUSD,2),")"); } }
            }
         }
      }
      if(!g_TPL_On && g_Trail_On){   // b99: TRAIL waxaa maamula badhanka CONTROL PANEL (g_Trail_On) - FIXED iyo ATR labadaba
         double open=m4OrderOpenPrice(), slDistTS=MathAbs(open-m4OrderStopLoss());
         // b41 FIX: hore trailing-ku wuxuu isticmaali jiray ATR go'an (1.5x/1x) aan la xidhiidhin R-ka trade-ka - xeladaha SL ballaadhan (structural, tusaale 3x ATR) trailing-ku wuxuu qabsan jiray guusha 0.15-0.3R kaliya (SL-heerkiisu ka fog yahay 1x ATR aad u yar). Hadda R-ka DHABTA AH (SL-ka trade-kan) ayaa lagu xisaabinayaa.
         double start=(slDistTS>0)?slDistTS*Trail_Start_R:trStartPips*_p;
         double step =(slDistTS>0)?slDistTS*Trail_Step_R :trStepPips*_p;
         double curr3=(m4OrderType()==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK);
         if(m4OrderType()==OP_BUY&&curr3-open>start){ double nsl=NormalizeDouble(curr3-step,Digits); if(nsl>m4OrderStopLoss()+step&&nsl<curr3-min_stop){ long ticketTS=m4OrderTicket(); bool mod=m4OrderModify(ticketTS,open,nsl,m4OrderTakeProfit(),0,clrGold); if(!mod) LogTradeOpFailure("TrailingModify_BUY",ticketTS,GetLastError()); else if(Enable_Debug_Log) Print("DEBUG TRAIL: SL -> ",DoubleToString(nsl,Digits)); } }
         if(m4OrderType()==OP_SELL&&open-curr3>start){ double nsl=NormalizeDouble(curr3+step,Digits); if((m4OrderStopLoss()==0||nsl<m4OrderStopLoss()-step)&&nsl>curr3+min_stop){ long ticketTS2=m4OrderTicket(); bool mod=m4OrderModify(ticketTS2,open,nsl,m4OrderTakeProfit(),0,clrGold); if(!mod) LogTradeOpFailure("TrailingModify_SELL",ticketTS2,GetLastError()); else if(Enable_Debug_Log) Print("DEBUG TRAIL: SL -> ",DoubleToString(nsl,Digits)); } }
      }
   }
   ManageNewsAndStagnantExit();   // b100: xir trade faa'iido leh ka hor news / xir trade istaagay
   if(!Simple_Mode) ManageFastBreakEven();   // b90/b92: fast BE (Simple_Mode -> ma taabto SL/TP)
   if(!Simple_Mode && g_TPL_On) ManageTPLadder();   // b63/b92: TP-ladder (Simple_Mode -> daa SL/TP go'an)
   else if(!Simple_Mode && (Enable_ScaleOut || (PropMode!=PROP_NONE && Prop_Scale_Out))) ManageScaleOut();   // b92: Simple_Mode -> ma gooyn
}

void CheckMoneyProfit(){ if(TargetProfitUSD<=0)return; double tot=todayClosedProfit; for(int i=0;i<m4OrdersTotal();i++){ if(m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES)&&m4OrderSymbol()==Symbol()){ int mg=m4OrderMagicNumber(); if(mg>=MagicNumber+100&&mg<=MagicNumber+106) tot+=m4OrderProfit()+m4OrderCommission()+m4OrderSwap(); } } if(tot<TargetProfitUSD)return; CloseAllTrades("Target reached"); if(EnableTelegram) SendTelegram("TARGET GAADHY: $"+DoubleToString(tot,2)); }
void UpdateSignalStats(){ string sym=Symbol(); for(int i=0;i<8;i++) if(StringFind(sym,G_Syms[i])>=0){ Signal_Counts[i]++; break; } }
void DrawTradeMarker(int type,string sN){ string name="M_Sig_"+sN+"_"+TimeToString(TimeCurrent()); double _p=GetPipSize(Symbol()); datetime bT=m4iTime(Symbol(),0,0); double price=(type==OP_BUY)?iLow(Symbol(),0,0)-5*_p:iHigh(Symbol(),0,0)+5*_p; if(ObjectCreate(0,name,OBJ_ARROW,0,bT,price)){ ObjectSetInteger(0,name,OBJPROP_ARROWCODE,(type==OP_BUY?233:234)); ObjectSetInteger(0,name,OBJPROP_COLOR,(type==OP_BUY?Buy_Marker_Color:Sell_Marker_Color)); ObjectSetInteger(0,name,OBJPROP_WIDTH,Marker_Size); } }
void CalculateWinRate(){ totalWins=totalLosses=0; int ht=m4OrdersHistoryTotal(); for(int i=ht-1;i>=0;i--){ if(m4OrderSelect(i,SELECT_BY_POS,MODE_HISTORY)){ int mg=m4OrderMagicNumber(); if(mg>=MagicNumber+100&&mg<=MagicNumber+106){ if(m4OrderProfit()>0) totalWins++; else if(m4OrderProfit()<0) totalLosses++; } } } int tT=totalWins+totalLosses; winRatePercent=(tT>0)?((double)totalWins/tT)*100.0:0.0; }

void DrawRRBox(long ticket){ if(!m4OrderSelect(ticket,SELECT_BY_TICKET,MODE_TRADES)) return; double sl=m4OrderStopLoss(), tp=m4OrderTakeProfit(); if(sl==0||tp==0)return; double op=m4OrderOpenPrice(); string sym=m4OrderSymbol(); string slN="RR_SL_"+IntegerToString(ticket), tpN="RR_TP_"+IntegerToString(ticket); if(ObjectFind(0,slN)>=0) ObjectDelete(0,slN); if(ObjectFind(0,tpN)>=0) ObjectDelete(0,tpN); datetime bt=m4iTime(sym,0,0), et=bt+(PeriodSeconds()*5); if(ObjectCreate(0,slN,OBJ_RECTANGLE,0,bt,op,et,sl)){ ObjectSetInteger(0,slN,OBJPROP_COLOR,clrRed); ObjectSetInteger(0,slN,OBJPROP_BACK,true); ObjectSetInteger(0,slN,OBJPROP_FILL,true); } if(ObjectCreate(0,tpN,OBJ_RECTANGLE,0,bt,op,et,tp)){ ObjectSetInteger(0,tpN,OBJPROP_COLOR,clrGreen); ObjectSetInteger(0,tpN,OBJPROP_BACK,true); ObjectSetInteger(0,tpN,OBJPROP_FILL,true); } AddRRNames(ticket,slN,tpN); }
void AddRRNames(long t,string slN,string tpN){ for(int i=0;i<ArraySize(RRR_Tickets);i++) if(RRR_Tickets[i]==t) return; int sz=ArraySize(RRR_Tickets); ArrayResize(RRR_Tickets,sz+1); ArrayResize(RRR_SL_Names,sz+1); ArrayResize(RRR_TP_Names,sz+1); RRR_Tickets[sz]=t; RRR_SL_Names[sz]=slN; RRR_TP_Names[sz]=tpN; }
void CleanRRBoxes(){ for(int i=ArraySize(RRR_Tickets)-1;i>=0;i--){ long t=RRR_Tickets[i]; if(m4OrderSelect(t,SELECT_BY_TICKET,MODE_TRADES)) continue; if(ObjectFind(0,RRR_SL_Names[i])>=0) ObjectDelete(0,RRR_SL_Names[i]); if(ObjectFind(0,RRR_TP_Names[i])>=0) ObjectDelete(0,RRR_TP_Names[i]); int last=ArraySize(RRR_Tickets)-1; RRR_Tickets[i]=RRR_Tickets[last]; RRR_SL_Names[i]=RRR_SL_Names[last]; RRR_TP_Names[i]=RRR_TP_Names[last]; ArrayResize(RRR_Tickets,last); ArrayResize(RRR_SL_Names,last); ArrayResize(RRR_TP_Names,last); } }

//+------------------------------------------------------------------+
//| DASHBOARD                                                        |
//+------------------------------------------------------------------+
void CreateRect(string n,int x,int y,int w,int h,color bg,color border=clrNONE){ ObjectDelete(0,n); if(ObjectCreate(0,n,OBJ_RECTANGLE_LABEL,0,0,0)){ ObjectSetInteger(0,n,OBJPROP_XDISTANCE,x); ObjectSetInteger(0,n,OBJPROP_YDISTANCE,y); ObjectSetInteger(0,n,OBJPROP_XSIZE,w); ObjectSetInteger(0,n,OBJPROP_YSIZE,h); ObjectSetInteger(0,n,OBJPROP_BGCOLOR,bg); ObjectSetInteger(0,n,OBJPROP_BORDER_TYPE,BORDER_FLAT); ObjectSetInteger(0,n,OBJPROP_COLOR,(border==clrNONE)?bg:border); ObjectSetInteger(0,n,OBJPROP_CORNER,CORNER_LEFT_UPPER); ObjectSetInteger(0,n,OBJPROP_SELECTABLE,false); ObjectSetInteger(0,n,OBJPROP_HIDDEN,true); } }
void CreateLabel(string n,int x,int y,string t,color c,int s,bool b=false){ ObjectDelete(0,n); if(ObjectCreate(0,n,OBJ_LABEL,0,0,0)){ ObjectSetInteger(0,n,OBJPROP_XDISTANCE,x); ObjectSetInteger(0,n,OBJPROP_YDISTANCE,y); ObjectSetString(0,n,OBJPROP_TEXT,t); ObjectSetInteger(0,n,OBJPROP_COLOR,c); ObjectSetInteger(0,n,OBJPROP_FONTSIZE,s); ObjectSetString(0,n,OBJPROP_FONT,b?"Segoe UI Semibold":"Segoe UI"); ObjectSetInteger(0,n,OBJPROP_CORNER,CORNER_LEFT_UPPER); ObjectSetInteger(0,n,OBJPROP_SELECTABLE,false); ObjectSetInteger(0,n,OBJPROP_BACK,false); ObjectSetInteger(0,n,OBJPROP_HIDDEN,true); } }
// ============================================================
// V56: ON-CHART CONTROL PANEL (badhamo la gujin karo - uma baahnid F7)
// ============================================================
// ================= b100: NEWS EXIT + STAGNANT EXIT =================
// Daqiiqadaha ka hadhay news-ka soo socda ee lamaanahan khuseeya. -1 = news lama hayo / lama helin.
int MinutesToNextNews(){
   if(!EnableNewsFilter) return -1;
   if(IsTesting()) return -1;                       // WebRequest ma jiro Strategy Tester
   if(TimeCurrent()-g_LastNewsFetch >= News_Refresh_Minutes*60) FetchNewsCalendar();
   if(!g_NewsDataValid) return -1;
   string symClean=StringSubstr(Symbol(),0,6);
   string baseCcy=StringSubstr(symClean,0,3), quoteCcy=StringSubstr(symClean,3,3);
   datetime now=TimeCurrent(); int best=-1;
   for(int i=0;i<ArraySize(g_NewsEvents);i++){
      if(g_NewsEvents[i].country!=baseCcy && g_NewsEvents[i].country!=quoteCcy) continue;
      bool impactOk=(g_NewsEvents[i].impact=="High") || (!News_Exit_High_Only && g_NewsEvents[i].impact=="Medium");
      if(!impactOk) continue;
      if(g_NewsEvents[i].time<=now) continue;       // waqtigii dhaafay
      int mins=(int)((g_NewsEvents[i].time-now)/60);
      if(best<0 || mins<best) best=mins;
   }
   return best;
}
void ManageNewsAndStagnantExit(){
   if(!Close_Profit_Before_News && !Enable_Stagnant_Exit) return;
   int mins=(Close_Profit_Before_News)?MinutesToNextNews():-1;
   bool newsSoon=(mins>=0 && mins<=News_Exit_Minutes);
   int barSecs=Period()*60; if(barSecs<=0) barSecs=60;
   for(int i=m4OrdersTotal()-1;i>=0;i--){
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES)||m4OrderSymbol()!=Symbol()) continue;
      int mg=m4OrderMagicNumber(); if(mg<MagicNumber+100||mg>MagicNumber+106) continue;
      long   tk=m4OrderTicket();
      double prof=m4OrderProfit()+m4OrderSwap()+m4OrderCommission();
      double cp=(m4OrderType()==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK);
      string why="";
      if(newsSoon){
         if(prof>0 && prof>=News_Exit_Min_Profit_USD) why="NewsExit(profit $"+DoubleToString(prof,2)+", news in "+IntegerToString(mins)+"m)";
         else if(News_Exit_Close_Losers)              why="NewsExit(all, news in "+IntegerToString(mins)+"m)";
      }
      if(why=="" && Enable_Stagnant_Exit){
         int barsOpen=(int)((TimeCurrent()-m4OrderOpenTime())/barSecs);
         if(barsOpen>=Stagnant_Bars){
            double rDist=MathAbs(m4OrderOpenPrice()-m4OrderStopLoss());
            if(rDist<=0) rDist=StopLoss_Pips_Fixed*GetPipSize(Symbol());
            if(rDist>0){
               double pnlR=(m4OrderType()==OP_BUY)?(cp-m4OrderOpenPrice())/rDist:(m4OrderOpenPrice()-cp)/rDist;
               if(pnlR<Stagnant_Max_R && pnlR>-Stagnant_Max_R) why="StagnantExit("+IntegerToString(barsOpen)+" bars, "+DoubleToString(pnlR,2)+"R)";
            }
         }
      }
      if(why=="") continue;
      bool closed=m4OrderClose(tk,m4OrderLots(),cp,3,clrAqua);
      if(!closed) LogTradeOpFailure("NewsStagnantExit",tk,GetLastError());
      else { Print("b100 ",why," -> closed ticket ",tk); if(EnableTelegram) SendTelegram("EXIT: "+why+"  ·  "+Symbol()); }
   }
}
void OnChartEvent(const int id,const long &lparam,const double &dparam,const string &sparam){
   if(id!=CHARTEVENT_OBJECT_CLICK) return;
   if(StringFind(sparam,"CP_Strat_")==0){ int idx=(int)StringToInteger(StringSubstr(sparam,9)); if(idx>=0&&idx<7) g_SelStrat=idx; CP_Refresh(); return; }
   if(sparam=="CP_LotUp"){   g_UserLot=(g_UserLot<=0)?0.01:g_UserLot+0.01; CP_Refresh(); return; }
   if(sparam=="CP_LotDn"){   g_UserLot=(g_UserLot<=0.01)?0:g_UserLot-0.01; CP_Refresh(); return; }
   if(sparam=="CP_LotAuto"){ g_UserLot=0; CP_Refresh(); return; }
   if(sparam=="CP_SLUp"){    g_UserSL=(g_UserSL<=0)?20:g_UserSL+5; CP_Refresh(); return; }
   if(sparam=="CP_SLDn"){    g_UserSL=(g_UserSL<=5)?0:g_UserSL-5; CP_Refresh(); return; }
   if(sparam=="CP_TPUp"){    g_UserTP=(g_UserTP<=0)?40:g_UserTP+5; CP_Refresh(); return; }
   if(sparam=="CP_TPDn"){    g_UserTP=(g_UserTP<=5)?0:g_UserTP-5; CP_Refresh(); return; }
   if(sparam=="CP_BE"){    g_BE_On=!g_BE_On;       ObjectSetInteger(0,"CP_BE",OBJPROP_STATE,false);    CP_Refresh(); return; }   // b99
   if(sparam=="CP_TRAIL"){ g_Trail_On=!g_Trail_On; ObjectSetInteger(0,"CP_TRAIL",OBJPROP_STATE,false); CP_Refresh(); return; }   // b99
   if(sparam=="CP_LAD"){ g_TPL_On=!g_TPL_On; ObjectSetInteger(0,"CP_LAD",OBJPROP_STATE,false); CP_Refresh(); return; }   // b101
   if(StringFind(sparam,"CP_TP")==0 && StringLen(sparam)==6){ int ti=(int)StringToInteger(StringSubstr(sparam,5))-1; if(ti>=0&&ti<4) g_TP_On[ti]=!g_TP_On[ti]; ObjectSetInteger(0,sparam,OBJPROP_STATE,false); CP_Refresh(); return; }   // b101
   if(sparam=="CP_TPAUTO"){ g_TP_Auto=!g_TP_Auto; if(g_TP_Auto) __tpAutoCalc(); ObjectSetInteger(0,sparam,OBJPROP_STATE,false); CP_Refresh(); return; }   // b105
   if(StringFind(sparam,"CP_TPm")==0){ int tm=(int)StringToInteger(StringSubstr(sparam,6))-1; if(tm>=0&&tm<4){ g_TP_Auto=false; g_TP_R[tm]-=0.1; if(g_TP_R[tm]<0.1) g_TP_R[tm]=0.1; __tpOrderFix(tm); } ObjectSetInteger(0,sparam,OBJPROP_STATE,false); CP_Refresh(); return; }   // b104: tallaabo 0.1R + kala-horreyn
   if(StringFind(sparam,"CP_TPp")==0){ int tp2=(int)StringToInteger(StringSubstr(sparam,6))-1; if(tp2>=0&&tp2<4){ g_TP_Auto=false; g_TP_R[tp2]+=0.1; if(g_TP_R[tp2]>20.0) g_TP_R[tp2]=20.0; __tpOrderFix(tp2); } ObjectSetInteger(0,sparam,OBJPROP_STATE,false); CP_Refresh(); return; }   // b104: tallaabo 0.1R + kala-horreyn
}
void CreateButton(string n,int x,int y,int w,int h,string t,color bg,color txt,int fs=8){
   ObjectDelete(0,n);
   if(ObjectCreate(0,n,OBJ_BUTTON,0,0,0)){
      ObjectSetInteger(0,n,OBJPROP_XDISTANCE,x); ObjectSetInteger(0,n,OBJPROP_YDISTANCE,y);
      ObjectSetInteger(0,n,OBJPROP_XSIZE,w); ObjectSetInteger(0,n,OBJPROP_YSIZE,h);
      ObjectSetString(0,n,OBJPROP_TEXT,t); ObjectSetInteger(0,n,OBJPROP_COLOR,txt);
      ObjectSetInteger(0,n,OBJPROP_BGCOLOR,bg); ObjectSetInteger(0,n,OBJPROP_FONTSIZE,fs);
      ObjectSetString(0,n,OBJPROP_FONT,"Segoe UI Semibold");
      ObjectSetInteger(0,n,OBJPROP_CORNER,CORNER_LEFT_UPPER);
      ObjectSetInteger(0,n,OBJPROP_BORDER_COLOR,MC_DIV);
      ObjectSetInteger(0,n,OBJPROP_HIDDEN,true);
   }
}
// b103: 1R = SL-ka pips-ka ah (panel SL, ama ATR, ama FIXED) - si TP-yada loo tuso pips
void __tpOrderFix(int k){   // b104: TP1<TP2<TP3<TP4 - kor u kac khasab ah
   for(int i=0;i<4;i++){ if(g_TP_R[i]<0.1) g_TP_R[i]=0.1; if(g_TP_R[i]>20.0) g_TP_R[i]=20.0; }
   for(int a=k+1;a<4;a++)  if(g_TP_R[a]<=g_TP_R[a-1]) g_TP_R[a]=g_TP_R[a-1]+0.1;
   for(int b=k-1;b>=0;b--) if(g_TP_R[b]>=g_TP_R[b+1]) g_TP_R[b]=g_TP_R[b+1]-0.1;
   for(int c=0;c<4;c++){ if(g_TP_R[c]<0.1) g_TP_R[c]=0.1; if(g_TP_R[c]>20.0) g_TP_R[c]=20.0; }
}
double __slPipsNow(){
   if(g_UserSL>0) return (double)g_UserSL;
   if(!Simple_Mode && SLTP_Mode!=SLTP_FIXED){
      double a=m4iATR(NULL,0,ATR_Period_Core,1), pp=GetPipSize(Symbol());
      if(a>0 && a<100000 && pp>0){ double slM=1.0,tpM=2.0; int si=(g_SelStrat>=0)?g_SelStrat:(int)Select_Strategy; GetStrategySLTPMultipliers(slM,tpM,si); double v=(a*slM)/pp; if(v>=5.0) return v; }
   }
   return (double)StopLoss_Pips_Fixed;
}
double __tpPipsNow(){   // b105: masaafada TP-ga weyn ee pips
   if(g_UserTP>0) return (double)g_UserTP;
   if(!Simple_Mode && SLTP_Mode!=SLTP_FIXED){
      double a=m4iATR(NULL,0,ATR_Period_Core,1), pp=GetPipSize(Symbol());
      if(a>0 && a<100000 && pp>0){ double slM=1.0,tpM=2.0; int si=(g_SelStrat>=0)?g_SelStrat:(int)Select_Strategy; GetStrategySLTPMultipliers(slM,tpM,si); double v=(a*tpM)/pp; if(v>=5.0) return v; }
   }
   return (double)TakeProfit_Pips_Fixed;
}
void __tpAutoCalc(){   // b105: TP1/TP2/TP3/TP4 = qayb ka mid ah TP-ga weyn (si toos ah)
   if(!g_TP_Auto) return;
   double slP=__slPipsNow(), tpP=__tpPipsNow();
   if(slP<=0 || tpP<=0) return;
   double mainR=tpP/slP;
   if(mainR<0.5) mainR=0.5; if(mainR>20.0) mainR=20.0;
   g_TP_R[0]=MathRound(mainR*0.40*10.0)/10.0;
   g_TP_R[1]=MathRound(mainR*0.65*10.0)/10.0;
   g_TP_R[2]=MathRound(mainR*0.90*10.0)/10.0;
   g_TP_R[3]=MathRound(mainR*10.0)/10.0;
   __tpOrderFix(3);
}
void CP_Refresh(){
   if(IsTesting()) return;
   __tpAutoCalc();   // b105: TP1-4 ku salee SL/TP hadda
   int act=(g_SelStrat>=0)?g_SelStrat:(int)Select_Strategy;
   for(int i=0;i<7;i++){
      string bn="CP_Strat_"+IntegerToString(i); bool on=(i==act);
      ObjectSetInteger(0,bn,OBJPROP_BGCOLOR,on?MC_GOLD:MC_HDR);
      ObjectSetInteger(0,bn,OBJPROP_COLOR,on?MC_BG:MC_INK);
      ObjectSetInteger(0,bn,OBJPROP_STATE,false);
   }
   ObjectSetString(0,"CP_LotVal",OBJPROP_TEXT,(g_UserLot>0)?DoubleToString(g_UserLot,2):"AUTO");
   ObjectSetString(0,"CP_SLVal", OBJPROP_TEXT,(g_UserSL>0)?IntegerToString(g_UserSL)+"p":(Simple_Mode?IntegerToString(StopLoss_Pips_Fixed)+"p FIX":"AUTO"));   // b92: Simple_Mode -> tus FIXED pips
   ObjectSetString(0,"CP_TPVal", OBJPROP_TEXT,(g_UserTP>0)?IntegerToString(g_UserTP)+"p":(Simple_Mode?IntegerToString(TakeProfit_Pips_Fixed)+"p FIX":"AUTO"));   // b92: Simple_Mode -> tus FIXED pips
   ObjectSetString(0,"CP_BE",OBJPROP_TEXT,g_BE_On?"BE: ON":"BE: OFF");
   ObjectSetInteger(0,"CP_BE",OBJPROP_COLOR,g_BE_On?MC_GREEN:MC_MUT);
   ObjectSetString(0,"CP_TRAIL",OBJPROP_TEXT,g_Trail_On?"TRAIL: ON":"TRAIL: OFF");
   ObjectSetInteger(0,"CP_TRAIL",OBJPROP_COLOR,g_Trail_On?MC_GREEN:MC_MUT);
   ObjectSetString(0,"CP_LAD",OBJPROP_TEXT,g_TPL_On?"LAD:ON":"LAD:OFF");
   ObjectSetInteger(0,"CP_LAD",OBJPROP_COLOR,g_TPL_On?MC_GOLD:MC_MUT);
   for(int t=0;t<4;t++){
      string sn=IntegerToString(t+1), on="CP_TP"+sn;
      ObjectSetString(0,on,OBJPROP_TEXT,g_TP_On[t]?("TP"+sn+" "+DoubleToString(g_TP_R[t],1)+"R="+IntegerToString((int)MathRound(g_TP_R[t]*__slPipsNow()))+"p"):("TP"+sn+" OFF"));   // b103: R + pips
      ObjectSetInteger(0,on,OBJPROP_COLOR,(g_TPL_On&&g_TP_On[t])?MC_GREEN:MC_MUT);
      ObjectSetInteger(0,on,OBJPROP_STATE,false);
      ObjectSetInteger(0,"CP_TPm"+sn,OBJPROP_STATE,false);
      ObjectSetInteger(0,"CP_TPp"+sn,OBJPROP_STATE,false);
   }
   ObjectSetString(0,"CP_TPAUTO",OBJPROP_TEXT,g_TP_Auto?"AUTO TP":"MANUAL");   // b105
   ObjectSetInteger(0,"CP_TPAUTO",OBJPROP_COLOR,g_TP_Auto?MC_GREEN:MC_GOLD);
   ObjectSetInteger(0,"CP_TPAUTO",OBJPROP_STATE,false);
   string btns[7]={"CP_LotDn","CP_LotUp","CP_LotAuto","CP_SLDn","CP_SLUp","CP_TPDn","CP_TPUp"};
   for(int k=0;k<7;k++) ObjectSetInteger(0,btns[k],OBJPROP_STATE,false);
   ChartRedraw();
}
void CP_CreatePanel(){
   if(IsTesting()) return;
   string sN[7]={"SR","BB","EMA","SMC","VSA","POC","-"};
   CreateRect("CP_BG",292,286,384,278,MC_BG,MC_DIV);
   CreateRect("CP_Acc",292,286,384,3,MC_GOLD);
   CreateRect("CP_HDR",292,289,384,22,MC_HDR);
   CreateLabel("CP_Title",302,294,"CONTROL PANEL - GUJI SI AAD U DOORATO",MC_GOLD,9,true);
   CreateLabel("CP_LStrat",302,318,"XEELAD:",MC_MUT,8,true);
   int bx6[6]={300,353,406,459,512,565}; int bc=0;
   for(int i=0;i<7;i++){ if(i>=6) continue; /* b83: 6 xeelad (idx 0..5, POC ku jira) */ CreateButton("CP_Strat_"+IntegerToString(i),bx6[bc],332,51,22,sN[i],MC_HDR,MC_INK,8); bc++; }   // b83: 6 badhan (SR/BB/EMA/SMC/VSA/POC)
   CreateLabel("CP_LLot",302,367,"LOT:",MC_INK,9,true);
   CreateButton("CP_LotDn",344,364,26,22,"-",MC_HDR,MC_GOLD,11);
   CreateLabel("CP_LotVal",378,367,"AUTO",MC_GOLD,9,true);
   CreateButton("CP_LotUp",432,364,26,22,"+",MC_HDR,MC_GOLD,11);
   CreateButton("CP_LotAuto",466,364,60,22,"AUTO",MC_HDR,MC_INFO,8);
   CreateLabel("CP_LSL",302,397,"SL:",MC_INK,9,true);
   CreateButton("CP_SLDn",344,394,26,22,"-",MC_HDR,MC_GOLD,11);
   CreateLabel("CP_SLVal",378,397,"AUTO",MC_GREEN,9,true);
   CreateButton("CP_SLUp",432,394,26,22,"+",MC_HDR,MC_GOLD,11);
   CreateLabel("CP_LTP",302,427,"TP:",MC_INK,9,true);
   CreateButton("CP_TPDn",344,424,26,22,"-",MC_HDR,MC_GOLD,11);
   CreateLabel("CP_TPVal",378,427,"AUTO",MC_GREEN,9,true);
   CreateButton("CP_TPUp",432,424,26,22,"+",MC_HDR,MC_GOLD,11);
   CreateButton("CP_BE",302,450,110,22,"BE: ON",MC_HDR,MC_GREEN,8);
   CreateButton("CP_TRAIL",420,450,110,22,"TRAIL: OFF",MC_HDR,MC_MUT,8);
   CreateButton("CP_LAD",580,478,86,22,"LAD:OFF",MC_HDR,MC_MUT,8);
   CreateButton("CP_TPAUTO",580,504,86,22,"AUTO TP",MC_HDR,MC_GREEN,8);   // b105
   int _tpx[4]={302,442,302,442}; int _tpy[4]={478,478,504,504};
   for(int t=0;t<4;t++){
      string sn=IntegerToString(t+1);
      CreateButton("CP_TPm"+sn,_tpx[t],     _tpy[t],20,22,"-",MC_HDR,MC_GOLD,10);
      CreateButton("CP_TP"+sn, _tpx[t]+22,  _tpy[t],86,22,"TP"+sn,MC_HDR,MC_GREEN,8);
      CreateButton("CP_TPp"+sn,_tpx[t]+110, _tpy[t],20,22,"+",MC_HDR,MC_GOLD,10);
   }
   CreateLabel("CP_Hint",302,532,"TP1-4: -/+ beddel R, guji magaca si aad u damiso/u shido.",MC_MUT,7);
   CP_Refresh();
}
void CreateDashboard(){
   // ---- V42 MODERN LEFT PANEL ----
   CreateRect("Moha_BG",10,28,272,662,MC_BG,MC_DIV);
   CreateRect("Moha_TopAccent",10,28,272,3,MC_GOLD);
   CreateLabel("Moha_Title",20,44,BrandName,MC_GOLD,12,true);
   CreateLabel("Moha_Sub",20,62,"BUILD b105 · AUTO TP ladder from SL/TP · 2026.07.30",MC_INFO,7,true);
   int y=86;
   CreateLabel("Moha_Lic",20,y,"● LICENSE ACTIVE",MC_GREEN,8,true); y+=18;
   CreateLabel("Moha_Exp",20,y,"Expires: "+Expiry_Date,MC_MUT,8); y+=24;
   CreateRect("Moha_Sep1",16,y,260,1,MC_DIV); y+=10;
   CreateLabel("Moha_Bal",20,y,"Balance: $0.00",MC_INK,9); y+=20;
   CreateLabel("Moha_Eq",20,y,"Equity: $0.00",MC_INK,9); y+=20;
   CreateLabel("Moha_Prf",20,y,"Profit: $0.00",MC_GREEN,9,true); y+=20;
   CreateLabel("Moha_DD",20,y,"Drawdown: 0.00%",MC_RED,9); y+=20;
   CreateLabel("Moha_Spr",20,y,"Spread: 0",MC_MUT,9); y+=20;
   CreateLabel("Moha_WR",20,y,"Win Rate: 0.00%",MC_GREEN,9,true); y+=20;
   CreateLabel("Moha_Ops",20,y,"Open Trades: 0",MC_INFO,9); y+=20;
   CreateLabel("Moha_Miss",20,y,"Missed: 0",MC_MUT,8); y+=18;
   CreateLabel("Moha_Err",20,y,"Errors: 0",MC_MUT,8); y+=18;
   CreateLabel("Moha_News",20,y,"News: ---",MC_GOLD,8); y+=18;
   CreateLabel("Moha_Risk",20,y,"Risk/Trade: 0.0%",MC_INFO,8); y+=18;
   CreateLabel("Moha_PF",20,y,"Profit Factor: 0.00",MC_GREEN,8); y+=18;
   CreateLabel("Moha_Sess",20,y,"Session: ---",MC_GOLD,8); y+=22;
   CreateRect("Moha_Sep2",16,y,260,1,MC_DIV); y+=10;
   CreateRect("Moha_Acc1",16,y+1,3,12,MC_GOLD); CreateLabel("Moha_Strat",26,y,"Strategy: ---",MC_GOLD,9,true); y+=20;
   CreateLabel("Moha_Reg",20,y,"Regime: ---",MC_INK,8); y+=18;
   CreateLabel("Moha_Prop",20,y,"Prop Mode: OFF",MC_MUT,8); y+=18;
   CreateLabel("Moha_AI",20,y,"AI Patterns: OFF",MC_MUT,8); y+=22;
   CreateRect("Moha_StatusBG",16,y,260,22,MC_HDR); CreateLabel("Moha_Status",26,y+4,"INITIALIZING...",MC_GOLD,9,true); y+=30;
   CreateRect("Moha_HdrBG",16,y,260,18,MC_HDR);
   CreateLabel("H_T1",22,y+2,"TYPE",MC_GOLD,8,true); CreateLabel("H_T2",72,y+2,"SYMBOL",MC_GOLD,8,true); CreateLabel("H_T3",152,y+2,"RESULT",MC_GOLD,8,true); CreateLabel("H_T4",228,y+2,"$",MC_GOLD,8,true); y+=20;
   for(int i=0;i<8;i++){ string idx=IntegerToString(i); CreateLabel("His_Type_"+idx,22,y,"-",MC_INK,8); CreateLabel("His_Sym_"+idx,72,y,"-",MC_INK,8); CreateLabel("His_Res_"+idx,152,y,"-",MC_INK,8); CreateLabel("His_Prof_"+idx,228,y,"-",MC_INK,8); y+=17; }
   // ---- STRATEGY PANEL ----
   CreateRect("Strat_BG",292,28,384,234,MC_BG,MC_DIV); CreateRect("Strat_TopAccent",292,28,384,3,MC_GOLD); CreateRect("Strat_HDR",292,31,384,26,MC_HDR); CreateLabel("Strat_Title",302,38,"STRATEGY PERFORMANCE & SIGNALS",MC_GOLD,9,true);
   string sN[7]={"SR","BB","EMA","SMC","VSA","POC","-"}; int sy=68;
   CreateLabel("Strat_H1",302,sy,"STRAT",MC_MUT,8,true); CreateLabel("Strat_HSig",342,sy,"SIGNAL",MC_MUT,8,true); CreateLabel("Strat_HCnt",382,sy,"COUNT",MC_MUT,8,true); CreateLabel("Strat_H2",422,sy,"WR%",MC_MUT,8,true); CreateLabel("Strat_H3",472,sy,"PnL",MC_MUT,8,true); CreateLabel("Strat_H4",522,sy,"RR",MC_MUT,8,true); sy+=18;
   for(int i=0;i<7;i++){ if(i>=6) continue; /* b83: 6 xeelad (idx 0..5, POC ku jira) */ string idx=IntegerToString(i); CreateLabel("Strat_Name_"+idx,302,sy,sN[i],MC_INK,8,true); CreateLabel("Strat_Sig_"+idx,342,sy,"WAIT",MC_MUT,8,true); CreateLabel("Strat_Cnt_"+idx,382,sy,"0",MC_INFO,8,true); CreateLabel("Strat_WR_"+idx,422,sy,"---%",MC_INFO,8); CreateLabel("Strat_PnL_"+idx,472,sy,"$0",MC_INK,8); CreateLabel("Strat_RR_"+idx,522,sy,"0.0",MC_INK,8); sy+=18; }   // 6 xeelad (plain RSI la tirtiray)
   sy+=6; CreateLabel("ActiveStrat",302,sy,"Active:",MC_GOLD,9,true); CreateLabel("ActiveStratVal",362,sy,"---",MC_GREEN,9,true);
   if(Show_CurrencyMeter){ CreateRect("CS_BG",292,290,384,112,MC_BG,MC_DIV); CreateRect("CS_TopAccent",292,290,384,3,MC_GREEN); CreateRect("CS_HDR",292,293,384,22,MC_HDR); CreateLabel("CS_Title",302,298,"CURRENCY STRENGTH",MC_GREEN,9,true); int csStartX=299,csSpacing=46; for(int i=0;i<8;i++){ int cx=csStartX+i*csSpacing; string idx=IntegerToString(i); CreateLabel("CS_Sym_"+idx,cx,320,G_Syms[i],MC_INK,7,true); CreateRect("CS_Bar_"+idx,cx+3,335,18,60,MC_TRACK); CreateRect("CS_Fill_"+idx,cx+3,380,18,15,MC_MUT); CreateLabel("CS_Val_"+idx,cx-2,400,"50",MC_INK,7); } }
}
void UpdateDashboardValues(){
   ObjectSetString(0,"Moha_Bal",OBJPROP_TEXT,"Balance: $"+DoubleToString(AccountBalance(),2));
   ObjectSetString(0,"Moha_Eq",OBJPROP_TEXT,"Equity: $"+DoubleToString(AccountEquity(),2));
   double tp=todayClosedProfit; for(int i=0;i<m4OrdersTotal();i++){ if(m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES)&&m4OrderSymbol()==Symbol()){ int mg=m4OrderMagicNumber(); if(mg>=MagicNumber+100&&mg<=MagicNumber+106) tp+=m4OrderProfit()+m4OrderCommission()+m4OrderSwap(); } }
   ObjectSetString(0,"Moha_Prf",OBJPROP_TEXT,"Profit: $"+DoubleToString(tp,2)); ObjectSetInteger(0,"Moha_Prf",OBJPROP_COLOR,(tp>=0)?MC_GREEN:MC_RED);
   double dd=(peakEquity>0)?(peakEquity-AccountEquity())/peakEquity*100.0:0; ObjectSetString(0,"Moha_DD",OBJPROP_TEXT,"Drawdown: "+DoubleToString(MathMax(0,dd),2)+"%");
   ObjectSetString(0,"Moha_Spr",OBJPROP_TEXT,"Spread: "+IntegerToString((int)MarketInfo(Symbol(),MODE_SPREAD)));
   CalculateWinRate(); ObjectSetString(0,"Moha_WR",OBJPROP_TEXT,"Win Rate: "+DoubleToString(winRatePercent,2)+"% ("+IntegerToString(totalWins)+"W/"+IntegerToString(totalLosses)+"L)");
   ObjectSetString(0,"Moha_Ops",OBJPROP_TEXT,"Open Trades: "+IntegerToString(CountAllOrders()));
   ObjectSetString(0,"Moha_Miss",OBJPROP_TEXT,"Missed: "+IntegerToString(missedOpportunities));
   ObjectSetString(0,"Moha_Err",OBJPROP_TEXT,"Errors: "+IntegerToString(errorCount));
   // QAYBTA 11 FIX (V39): News / Risk% / Profit Factor / Session
   string newsTxt=(!EnableNewsFilter)?"OFF":(!g_NewsDataValid?"NO DATA":(IsNewsActive()?"ACTIVE - BLOCK":"CLEAR"));
   color newsCol=(newsTxt=="ACTIVE - BLOCK")?MC_GOLD:((newsTxt=="CLEAR")?MC_GREEN:MC_MUT);
   ObjectSetString(0,"Moha_News",OBJPROP_TEXT,"News: "+newsTxt); ObjectSetInteger(0,"Moha_News",OBJPROP_COLOR,newsCol);
   double _effRisk=GetEffectiveRiskPercent(); ObjectSetString(0,"Moha_Risk",OBJPROP_TEXT,"Risk/Trade: "+DoubleToString(_effRisk,1)+"%"+((_effRisk<Risk_Percent)?" (DYN-)":"")); ObjectSetInteger(0,"Moha_Risk",OBJPROP_COLOR,(_effRisk<Risk_Percent)?MC_GOLD:MC_INFO);   // b57: tus risk dynamic marka hoos loo dhigo
   double gpAll=0,glAll=0; for(int si=0;si<7;si++){ gpAll+=stratStats[si].grossProfit; glAll+=stratStats[si].grossLoss; } double pfAll=(glAll>0)?gpAll/glAll:((gpAll>0)?999.0:0.0);
   ObjectSetString(0,"Moha_PF",OBJPROP_TEXT,"Profit Factor: "+DoubleToString(pfAll,2)); ObjectSetInteger(0,"Moha_PF",OBJPROP_COLOR,(pfAll>=1.0)?MC_GREEN:MC_RED);
   ObjectSetString(0,"Moha_Sess",OBJPROP_TEXT,"Session: "+GetSessionName());
   string sNames[7]={"SR","BB","EMA","SMC","VSA","POC","-"}; int actS=(g_SelStrat>=0)?g_SelStrat:((Enable_Auto_Strategy&&currentActiveStrategy>=0)?currentActiveStrategy:(int)Select_Strategy);   // b56 FIX: label-ka "Strategy:"/"Active:" = xeelada DHABTA la doortay (panel), maaha Select_Strategy oo keliya
   ObjectSetString(0,"Moha_Strat",OBJPROP_TEXT,"Strategy: "+(Enable_Multi_Strategy?"MULTI (7)":sNames[actS])+(Simple_Mode?" · SIMPLE":(Loose_Entry_Mode?" · LOOSE":"")));   // b92: tus xaalada mode
   ObjectSetString(0,"Moha_Reg",OBJPROP_TEXT,"Regime: "+(GetMarketRegime()==1?"TRENDING":"RANGING"));
   ObjectSetString(0,"Moha_Prop",OBJPROP_TEXT,"Prop: "+EnumToStr_Prop((int)PropMode));
   CheckPropMinTradingDaysWarning();
   ObjectSetString(0,"Moha_AI",OBJPROP_TEXT,"AI Patterns: "+(Enable_AI_Patterns?"ON":"OFF"));
   ObjectSetString(0,"ActiveStratVal",OBJPROP_TEXT,sNames[actS]);
   // ---- Strategy Performance panel = xog DHAB ah (6 xeelad, plain RSI la tirtiray) ----
   for(int spi=0;spi<7;spi++){
      if(spi>=6) continue;   // b83: 6 xeelad (POC ku jira)
      string sidx=IntegerToString(spi);
      if(Panel_Show_Selected_Only && !Enable_Multi_Strategy && spi!=actS){   // b70: single-mode - xeeladaha aan la dooran = blank (kaliya tus xeelada firfircoon, tusaale SR)
         ObjectSetString (0,"Strat_Sig_"+sidx,OBJPROP_TEXT,"-");   ObjectSetInteger(0,"Strat_Sig_"+sidx,OBJPROP_COLOR,MC_MUT);
         ObjectSetString (0,"Strat_Cnt_"+sidx,OBJPROP_TEXT,"-");
         ObjectSetString (0,"Strat_WR_"+sidx,OBJPROP_TEXT,"---%");
         ObjectSetString (0,"Strat_PnL_"+sidx,OBJPROP_TEXT,"-");   ObjectSetInteger(0,"Strat_PnL_"+sidx,OBJPROP_COLOR,MC_MUT);
         ObjectSetString (0,"Strat_RR_"+sidx,OBJPROP_TEXT,"-");
         ObjectSetInteger(0,"Strat_Name_"+sidx,OBJPROP_COLOR,MC_MUT);
         continue;
      }
      int cs=stratCurrentSignal[spi];
      string sigTxt=(cs==OP_BUY)?"BUY":((cs==OP_SELL)?"SELL":"WAIT");
      color  sigCol=(cs==OP_BUY)?MC_GREEN:((cs==OP_SELL)?MC_RED:MC_MUT);
      ObjectSetString (0,"Strat_Sig_"+sidx,OBJPROP_TEXT,sigTxt);
      ObjectSetInteger(0,"Strat_Sig_"+sidx,OBJPROP_COLOR,sigCol);
      ObjectSetString (0,"Strat_Cnt_"+sidx,OBJPROP_TEXT,IntegerToString(stratSignalCount[spi]));
      ObjectSetString (0,"Strat_WR_"+sidx,OBJPROP_TEXT,(stratStats[spi].total>0)?DoubleToString(stratStats[spi].winRate,0)+"%":"---%");
      ObjectSetString (0,"Strat_PnL_"+sidx,OBJPROP_TEXT,"$"+DoubleToString(stratStats[spi].totalPnL,0));
      ObjectSetInteger(0,"Strat_PnL_"+sidx,OBJPROP_COLOR,(stratStats[spi].totalPnL>=0)?MC_GREEN:MC_RED);
      ObjectSetString (0,"Strat_RR_"+sidx,OBJPROP_TEXT,DoubleToString(stratStats[spi].avgRR,1));
      color nmCol=IsGoodStrat(spi)?MC_GOLD:MC_INK;   // kuwa kale = INK (la akhriyi karo, maaha madow)
      ObjectSetInteger(0,"Strat_Name_"+sidx,OBJPROP_COLOR,nmCol);
   }
   int ht=m4OrdersHistoryTotal(), disp=0; for(int i=ht-1;i>=0&&disp<8;i--){ if(m4OrderSelect(i,SELECT_BY_POS,MODE_HISTORY)){ int mg=m4OrderMagicNumber(); if(mg>=MagicNumber+100&&mg<=MagicNumber+106){ string idx=IntegerToString(disp); bool isWin=(m4OrderProfit()>=0); ObjectSetString(0,"His_Type_"+idx,OBJPROP_TEXT,(m4OrderType()==OP_BUY?"BUY":"SELL")); ObjectSetInteger(0,"His_Type_"+idx,OBJPROP_COLOR,(m4OrderType()==OP_BUY)?MC_GREEN:MC_RED); ObjectSetString(0,"His_Sym_"+idx,OBJPROP_TEXT,m4OrderSymbol()); ObjectSetString(0,"His_Res_"+idx,OBJPROP_TEXT,(isWin?"WIN":"LOSS")); ObjectSetInteger(0,"His_Res_"+idx,OBJPROP_COLOR,isWin?MC_GREEN:MC_RED); ObjectSetString(0,"His_Prof_"+idx,OBJPROP_TEXT,(m4OrderProfit()>=0?"+$":"-$")+DoubleToString(MathAbs(m4OrderProfit()),2)); ObjectSetInteger(0,"His_Prof_"+idx,OBJPROP_COLOR,isWin?MC_GREEN:MC_RED); disp++; } } } for(int i=disp;i<8;i++){ string idx=IntegerToString(i); ObjectSetString(0,"His_Type_"+idx,OBJPROP_TEXT,"-"); ObjectSetString(0,"His_Sym_"+idx,OBJPROP_TEXT,"-"); ObjectSetString(0,"His_Res_"+idx,OBJPROP_TEXT,"-"); ObjectSetString(0,"His_Prof_"+idx,OBJPROP_TEXT,"-"); }
   if(Show_CurrencyMeter){ for(int i=0;i<8;i++){ double s=currStrength[i].strength; int barH=(int)((s/100.0)*60); if(barH<0) barH=0; if(barH>60) barH=60; color cc=(s>=65)?MC_GREEN:(s<=35?MC_RED:MC_GOLD); string idx=IntegerToString(i); ObjectSetInteger(0,"CS_Fill_"+idx,OBJPROP_YSIZE,barH); ObjectSetInteger(0,"CS_Fill_"+idx,OBJPROP_YDISTANCE,395-barH); ObjectSetInteger(0,"CS_Fill_"+idx,OBJPROP_BGCOLOR,cc); ObjectSetString(0,"CS_Val_"+idx,OBJPROP_TEXT,IntegerToString((int)s)); ObjectSetInteger(0,"CS_Val_"+idx,OBJPROP_COLOR,cc); } }
   ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"LIVE SCANNING... "+Symbol());
}
void CreateWatermark(){ ObjectDelete(0,"Moha_Watermark"); if(ObjectCreate(0,"Moha_Watermark",OBJ_LABEL,0,0,0)){ ObjectSetInteger(0,"Moha_Watermark",OBJPROP_XDISTANCE,300); ObjectSetInteger(0,"Moha_Watermark",OBJPROP_YDISTANCE,200); ObjectSetString(0,"Moha_Watermark",OBJPROP_TEXT,Watermark_Text); ObjectSetInteger(0,"Moha_Watermark",OBJPROP_COLOR,Watermark_Color); ObjectSetInteger(0,"Moha_Watermark",OBJPROP_FONTSIZE,Watermark_Size); ObjectSetString(0,"Moha_Watermark",OBJPROP_FONT,"Impact"); } }
void DrawDynamicZones(ENUM_STRATEGY strat){ if(Bars<50)return; string zU="M_Dyn_Zone_Up", zD="M_Dyn_Zone_Dn"; double pU=0,pD=0; color zC=clrGray; int bN; switch(strat){ case STRAT_SR: bN=MathMin(SR_Lookback,Bars-1); if(bN<2)return; { double clHi=0,clLo=0; int tHi=0,tLo=0; if(SR_FindClusteredLevel(false,bN,clHi,tHi)) pU=clHi; if(SR_FindClusteredLevel(true,bN,clLo,tLo)) pD=clLo; } zC=clrOrangeRed; break; /* b44 FIX: chart-ka wuxuu hadda tusayaa isla level-ka bot-ku ku ganacsado (swing cluster), maaha kii hore */ case STRAT_BOLLINGER: pU=m4iBands(NULL,0,BB_Period,BB_Deviation,0,PRICE_CLOSE,MODE_UPPER,1); pD=m4iBands(NULL,0,BB_Period,BB_Deviation,0,PRICE_CLOSE,MODE_LOWER,1); zC=clrAqua; break; case STRAT_EMA: pU=m4iMA(NULL,0,SlowEMA,0,MODE_EMA,PRICE_CLOSE,1); pD=m4iMA(NULL,0,FastEMA,0,MODE_EMA,PRICE_CLOSE,1); zC=clrYellow; break; case STRAT_SMC: { int lookback=MathMin(SMC_Lookback,Bars-5); int maxIdx=-1; double maxR=0; for(int i=1;i<=lookback;i++){ double r=High[i]-Low[i]; if(r>maxR){ maxR=r; maxIdx=i; } } if(maxIdx!=-1){ pU=High[maxIdx]; pD=Low[maxIdx]; } zC=clrDeepPink; } break; case STRAT_VSA: zC=clrMediumPurple; break; case STRAT_POC: pU=g_pocVAH; pD=g_pocVAL; zC=clrGold; break; default: return; } if(pU>0 && pU<1e6){ if(ObjectFind(0,zU)==-1) ObjectCreate(0,zU,OBJ_HLINE,0,0,pU); else ObjectMove(0,zU,0,0,pU); ObjectSetInteger(0,zU,OBJPROP_COLOR,zC); ObjectSetInteger(0,zU,OBJPROP_STYLE,STYLE_DOT); } if(pD>0 && pD<1e6){ if(ObjectFind(0,zD)==-1) ObjectCreate(0,zD,OBJ_HLINE,0,0,pD); else ObjectMove(0,zD,0,0,pD); ObjectSetInteger(0,zD,OBJPROP_COLOR,zC); ObjectSetInteger(0,zD,OBJPROP_STYLE,STYLE_DOT); } }
void ApplyPremiumTheme(){ ChartSetInteger(0,CHART_MODE,CHART_CANDLES); ChartSetInteger(0,CHART_COLOR_BACKGROUND,C'10,10,18'); ChartSetInteger(0,CHART_COLOR_FOREGROUND,clrWhite); ChartSetInteger(0,CHART_COLOR_CANDLE_BEAR,C'220,40,40'); ChartSetInteger(0,CHART_COLOR_CANDLE_BULL,C'0,200,100'); ChartSetInteger(0,CHART_COLOR_CHART_UP,C'0,200,100'); ChartSetInteger(0,CHART_COLOR_CHART_DOWN,C'220,40,40'); ChartSetInteger(0,CHART_SHOW_GRID,false); }
void Wrapper_DrawManualSR(){ if(Bars<2)return; int bN=MathMin(SR_Lookback,Bars-1); if(bN<2)return;
   // b65: SR = ZONE (band) + touch count (T:N) - sida indicator professional
   double sLo,sHi,sLvl, rLo,rHi,rLvl; int loT=0,hiT=0;
   int leftBars=MathMin(bN,300); datetime tL=m4iTime(Symbol(),0,leftBars); datetime tR=m4iTime(Symbol(),0,0)+PeriodSeconds()*8;
   ObjectDelete(0,"WR_ResLine"); ObjectDelete(0,"WR_SupLine");   // nadiifi xariiqyihii hore (b44)
   if(!SR_Draw_Zones){   // fallback: xariiq keliya (sida hore)
      double clHi=0,clLo=0; int tHi=0,tLo=0; bool hHi=SR_FindClusteredLevel(false,bN,clHi,tHi), hLo=SR_FindClusteredLevel(true,bN,clLo,tLo);
      if(hHi){ ObjectDelete(0,"WR_ResLine"); ObjectCreate(0,"WR_ResLine",OBJ_HLINE,0,0,clHi); ObjectSetInteger(0,"WR_ResLine",OBJPROP_COLOR,SR_Line_Color); }
      if(hLo){ ObjectDelete(0,"WR_SupLine"); ObjectCreate(0,"WR_SupLine",OBJ_HLINE,0,0,clLo); ObjectSetInteger(0,"WR_SupLine",OBJPROP_COLOR,SR_Line_Color); }
      return;
   }
   if(SR_FindClusteredZone(false,bN,rLo,rHi,rLvl,hiT)){   // RESISTANCE zone
      ObjectDelete(0,"WR_ResZone"); if(ObjectCreate(0,"WR_ResZone",OBJ_RECTANGLE,0,tL,rHi,tR,rLo)){ ObjectSetInteger(0,"WR_ResZone",OBJPROP_COLOR,clrTomato); ObjectSetInteger(0,"WR_ResZone",OBJPROP_BACK,true); ObjectSetInteger(0,"WR_ResZone",OBJPROP_FILL,true); ObjectSetInteger(0,"WR_ResZone",OBJPROP_SELECTABLE,false); }
      ObjectDelete(0,"WR_ResTxt"); if(ObjectCreate(0,"WR_ResTxt",OBJ_TEXT,0,tR,rHi)){ ObjectSetString(0,"WR_ResTxt",OBJPROP_TEXT," R  T:"+IntegerToString(hiT)); ObjectSetInteger(0,"WR_ResTxt",OBJPROP_COLOR,clrTomato); ObjectSetInteger(0,"WR_ResTxt",OBJPROP_FONTSIZE,10); ObjectSetInteger(0,"WR_ResTxt",OBJPROP_SELECTABLE,false); }
   }
   if(SR_FindClusteredZone(true,bN,sLo,sHi,sLvl,loT)){   // SUPPORT zone
      ObjectDelete(0,"WR_SupZone"); if(ObjectCreate(0,"WR_SupZone",OBJ_RECTANGLE,0,tL,sHi,tR,sLo)){ ObjectSetInteger(0,"WR_SupZone",OBJPROP_COLOR,clrLimeGreen); ObjectSetInteger(0,"WR_SupZone",OBJPROP_BACK,true); ObjectSetInteger(0,"WR_SupZone",OBJPROP_FILL,true); ObjectSetInteger(0,"WR_SupZone",OBJPROP_SELECTABLE,false); }
      ObjectDelete(0,"WR_SupTxt"); if(ObjectCreate(0,"WR_SupTxt",OBJ_TEXT,0,tR,sLo)){ ObjectSetString(0,"WR_SupTxt",OBJPROP_TEXT," S  T:"+IntegerToString(loT)); ObjectSetInteger(0,"WR_SupTxt",OBJPROP_COLOR,clrLimeGreen); ObjectSetInteger(0,"WR_SupTxt",OBJPROP_FONTSIZE,10); ObjectSetInteger(0,"WR_SupTxt",OBJPROP_SELECTABLE,false); }
   }
}   // b65: xariiq -> ZONE (band) dhab ah + touch count
void ND_CreatePanel(){ if(IsStopped())return; int x=292,y=410; CreateRect("ND_MainBG",x,y,384,166,MC_BG,MC_DIV); CreateRect("ND_TopAccent",x,y,384,3,MC_INFO); CreateRect("ND_Header",x,y+3,384,22,MC_HDR); CreateLabel("ND_Title",x+96,y+8,"CURRENCY POWER · RSI H1",MC_INFO,8,true); for(int i=0;i<8;i++){ int xP=x+(i*46)+8; string idx=IntegerToString(i); CreateLabel("ND_Sym_"+G_Syms[i],xP+4,y+30,G_Syms[i],MC_INK,8,true); CreateRect("ND_BarBG_"+idx,xP+8,y+45,16,80,MC_TRACK); CreateRect("ND_BarFill_"+idx,xP+8,y+45,16,0,MC_INFO); CreateLabel("ND_Val_"+idx,xP+2,y+132,"WAIT",MC_INK,7,true); CreateLabel("ND_Count_"+idx,xP+8,y+147,"0",MC_INFO,7,true); } }
void ND_UpdateStats(){ string pairMap[8][2]={{"AUD","AUDUSD"},{"CAD","USDCAD"},{"CHF","USDCHF"},{"EUR","EURUSD"},{"GBP","GBPUSD"},{"JPY","USDJPY"},{"NZD","NZDUSD"},{"USD","EURUSD"}}; bool isQuote[8]={false,true,true,false,false,true,false,true}; for(int i=0;i<8;i++){ double r=m4iRSI(pairMap[i][1],PERIOD_H1,RSI_1H_Period,PRICE_CLOSE,1); if(r<=0||r>=100)r=50; if(isQuote[i])r=100.0-r; int h=(int)((r/100.0)*80); if(h<0)h=0; if(h>80)h=80; string idx=IntegerToString(i); ObjectSetInteger(0,"ND_BarFill_"+idx,OBJPROP_YSIZE,h); ObjectSetInteger(0,"ND_BarFill_"+idx,OBJPROP_YDISTANCE,455+(80-h)); color c=(r>=65)?MC_GREEN:(r<=35?MC_RED:MC_GOLD); ObjectSetInteger(0,"ND_BarFill_"+idx,OBJPROP_BGCOLOR,c); ObjectSetString(0,"ND_Val_"+idx,OBJPROP_TEXT,(r>=65)?"BUY":(r<=35?"SELL":"WAIT")); ObjectSetInteger(0,"ND_Val_"+idx,OBJPROP_COLOR,c); ObjectSetString(0,"ND_Count_"+idx,OBJPROP_TEXT,IntegerToString(Signal_Counts[i])); } }

//+------------------------------------------------------------------+
//| CHECK COMMANDS FROM ADMIN PANEL                                  |
//+------------------------------------------------------------------+
//--- magaca bootka URL-ka ku habboon (meel bannaan -> %20)
string CloudUrlEncName(string t)
{
   string o=""; int n=StringLen(t);
   for(int i=0;i<n;i++)
   {
      ushort c=StringGetCharacter(t,i);
      if((c>='A'&&c<='Z')||(c>='a'&&c<='z')||(c>='0'&&c<='9')||c=='-'||c=='_'||c=='.')
         o+=ShortToString(c);
      else if(c==' ') o+="%20";
      else o+=StringFormat("%%%02X",c);
   }
   return(o);
}

//+------------------------------------------------------------------+
//| v57.3: TRADE JOURNAL -> cloud                                     |
//| Trade-yada la xiray, TICKET la socda, si server-ku u kaydiyo.     |
//| Ticket-ku wuxuu ka dhigayaa dedup mid sahlan - isla trade dib     |
//| looma tirinayo, xitaa haddii 100 jeer la diro.                    |
//+------------------------------------------------------------------+
string JsonEsc(string t)
{
   string o=""; int n=StringLen(t);
   for(int i=0;i<n;i++)
   {
      ushort c=StringGetCharacter(t,i);
      if(c=='"')       o+="\\\"";
      else if(c=='\\') o+="\\\\";
      else if(c=='\n' || c=='\r') o+=" ";
      else             o+=ShortToString(c);
   }
   return(o);
}

void SendClosedTrades()
{
   if(IsTesting()) return;
   if(!EnableCloudDashboard) return;
   if(Cloud_Auth_Token=="CHANGE_ME_LONG_RANDOM_SECRET") return;

   string arr=""; int n=0;
   int ht=m4OrdersHistoryTotal();

   //--- kuwa ugu dambeeyay ayaa marka hore la eegayaa
   for(int i=ht-1; i>=0 && n<40; i--)
   {
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_HISTORY)) continue;
      if(m4OrderSymbol()!=Symbol()) continue;
      int mg=m4OrderMagicNumber();
      if(mg<MagicNumber+100 || mg>MagicNumber+300) continue;
      int ty=m4OrderType();
      if(ty!=OP_BUY && ty!=OP_SELL) continue;

      double prof=m4OrderProfit()+m4OrderCommission()+m4OrderSwap();
      string ot=TimeToString(m4OrderOpenTime(),TIME_DATE|TIME_SECONDS);
      string ct=TimeToString(m4OrderCloseTime(),TIME_DATE|TIME_SECONDS);
      StringReplace(ot,".","-"); StringReplace(ct,".","-");

      if(n>0) arr+=",";
      arr+="{\"ticket\":"+IntegerToString(m4OrderTicket())
          +",\"symbol\":\""+Symbol()+"\""
          +",\"side\":\""+(ty==OP_BUY?"BUY":"SELL")+"\""
          +",\"strat\":\""+JsonEsc(m4OrderComment())+"\""
          +",\"lot\":"+DoubleToString(m4OrderLots(),2)
          +",\"entry\":"+DoubleToString(m4OrderOpenPrice(),Digits)
          +",\"exitp\":"+DoubleToString(m4OrderClosePrice(),Digits)
          +",\"sl\":"+DoubleToString(m4OrderStopLoss(),Digits)
          +",\"tp\":"+DoubleToString(m4OrderTakeProfit(),Digits)
          +",\"profit\":"+DoubleToString(prof,2)
          +",\"open_time\":\""+ot+"\""
          +",\"close_time\":\""+ct+"\"}";
      n++;
   }
   if(n==0) return;

   string json="{\"token\":\""+Cloud_Auth_Token+"\",\"bot\":\""+Cloud_Bot_Name
              +"\",\"trades\":["+arr+"]}";

   string url=CloudDashboardURL;
   int pos=StringFind(url,"/update");
   if(pos>0) url=StringSubstr(url,0,pos);
   url+="/trades";

   uchar post[], result[];
   string headers="Content-Type: application/json\r\nAuthorization: Bearer "+Cloud_Auth_Token+"\r\n";
   string rh="";
   StringToCharArray(json,post,0,StringLen(json),CP_UTF8);
   ResetLastError();
   int code=WebRequest("POST",url,headers,8000,post,result,rh);
   if(code!=200 && Enable_Debug_Log)
      PrintFormat("JOURNAL post -> %d err %d", code, GetLastError());
}

void CheckCloudCommands(){
   if(IsTesting()) return;   // V40: no WebRequest in Strategy Tester
   if(Cloud_Auth_Token == "CHANGE_ME_LONG_RANDOM_SECRET") return;
   // Amarrada bootkan oo keliya (haddii kale labada bot amar bay wadaagaan)
   string cmdUrl=CloudCommandURL+"?bot="+CloudUrlEncName(Cloud_Bot_Name); uchar postData[], result[]; string headers="Content-Type: application/json\r\nAuthorization: Bearer "+Cloud_Auth_Token+"\r\n"; string resultHeaders;
   int res=WebRequest("GET",cmdUrl,headers,3000,postData,result,resultHeaders);
   if(res!=200){ consecutiveWebFails++; if(res==-1) Print("CLOUD CMD FAILED err=", GetLastError()); return; }
   consecutiveWebFails=0; string response=CharArrayToString(result);
   if(Require_Signed_Commands){ if(StringFind(response,"\"token\":\""+Cloud_Auth_Token+"\"")<0){ Print("SECURITY: Invalid token - IGNORED."); if(EnableTelegram&&TG_ErrorAlerts) SendTelegram("SECURITY: invalid cloud command."); return; } }
   if(StringFind(response,"START")>=0) { IsAuthorized=true; Print("Admin: SHID"); }
   if(StringFind(response,"STOP")>=0) { IsAuthorized=false; Print("Admin: DAM"); }
   if(StringFind(response,"CLOSE_ALL")>=0){ CloseAllTrades("Admin: XIDH"); Print("Admin: XIDH DHAMMAAN"); }
   if(StringFind(response,"CLOSE_PROFIT")>=0){ for(int i=m4OrdersTotal()-1;i>=0;i--){ if(m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES)&&m4OrderSymbol()==Symbol()&&m4OrderProfit()>0){ long ticketCP=m4OrderTicket(); double cp=(m4OrderType()==OP_BUY)?MarketInfo(Symbol(),MODE_BID):MarketInfo(Symbol(),MODE_ASK); bool closed=m4OrderClose(ticketCP,m4OrderLots(),cp,3,clrWhite); if(!closed) LogTradeOpFailure("Admin_ClosePROFIT",ticketCP,GetLastError()); } } Print("Admin: XIDH FAA'IIDO"); }
   if(StringFind(response,"STRATEGY:SR")>=0) { currentActiveStrategy=STRAT_SR; Print("Admin: Strategy > SR"); }
   if(StringFind(response,"STRATEGY:BB")>=0) { currentActiveStrategy=STRAT_BOLLINGER; Print("Admin: Strategy > BB"); }
   if(StringFind(response,"STRATEGY:EMA")>=0) { currentActiveStrategy=STRAT_EMA; Print("Admin: Strategy > EMA"); }
   if(StringFind(response,"STRATEGY:SMC")>=0) { currentActiveStrategy=STRAT_SMC; Print("Admin: Strategy > SMC"); }
   if(StringFind(response,"STRATEGY:VSA")>=0) { currentActiveStrategy=STRAT_VSA; Print("Admin: Strategy > VSA"); }
   if(StringFind(response,"STRATEGY:POC")>=0) { currentActiveStrategy=STRAT_POC; Print("Admin: Strategy > POC"); }   // b83
   // b82: STRATEGY:RSI amar laga saaray (RSI/RSI1H waa la tirtiray)
}
// b84: JOURNAL (sida MyFxBook) -> P&L bille (12 bilood) + tirakoob guud -> dashboard. Taariikh keliya, trading kuma saameyn.
string BuildJournalJSON(){
   double mon[12]; ArrayInitialize(mon,0.0);
   int trades=0,wins=0,losses=0; double gp=0,gl=0,best=-1e18,worst=1e18,pips=0;
   double pToday=0,pWeek=0,pMonth=0,pYear=0;
   string curDS = TimeToString(TimeCurrent(),TIME_DATE);   // "YYYY.MM.DD" (MQL4+MQL5)
   int curYr=(int)StringToInteger(StringSubstr(curDS,0,4));
   int curMo=(int)StringToInteger(StringSubstr(curDS,5,2));
   int curKey=curYr*12+curMo;
   datetime todayStart=StringToTime(curDS);
   datetime weekStart =todayStart-(DayOfWeek()-1)*86400;
   datetime monthStart=StringToTime(StringSubstr(curDS,0,7)+".01");
   datetime yearStart =StringToTime(StringSubstr(curDS,0,4)+".01.01");
   int ht=m4OrdersHistoryTotal();
   for(int i=0;i<ht;i++){
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_HISTORY)) continue;
      int mg=m4OrderMagicNumber(); if(mg<MagicNumber+100||mg>MagicNumber+106) continue;
      int ot=m4OrderType(); if(ot!=OP_BUY&&ot!=OP_SELL) continue;
      double p=m4OrderProfit()+m4OrderCommission()+m4OrderSwap();
      datetime ct=m4OrderCloseTime();
      trades++;
      if(p>0){ wins++; gp+=p; } else { losses++; gl+=MathAbs(p); }
      if(p>best)best=p; if(p<worst)worst=p;
      double ps=GetPipSize(m4OrderSymbol());
      if(ps>0){ double dd=(ot==OP_BUY)?(m4OrderClosePrice()-m4OrderOpenPrice()):(m4OrderOpenPrice()-m4OrderClosePrice()); pips+=dd/ps; }
      if(ct>=todayStart)pToday+=p; if(ct>=weekStart)pWeek+=p; if(ct>=monthStart)pMonth+=p; if(ct>=yearStart)pYear+=p;
      string cds=TimeToString(ct,TIME_DATE);
      int y=(int)StringToInteger(StringSubstr(cds,0,4)); int m=(int)StringToInteger(StringSubstr(cds,5,2));
      int off=curKey-(y*12+m); if(off>=0 && off<12) mon[11-off]+=p;
   }
   double winRate=(trades>0)?(double)wins/trades*100.0:0;
   double pf=(gl>0)?gp/gl:((gp>0)?999.0:0);
   double avgWin=(wins>0)?gp/wins:0, avgLoss=(losses>0)?gl/losses:0;
   if(best<-1e17)best=0; if(worst>1e17)worst=0;
   double bal=AccountBalance();
   double gainPct=((bal-pYear)>0)?(pYear/(bal-pYear))*100.0:0;
   string j="{";
   j+="\"gainPct\":"+DoubleToString(gainPct,2)+",";
   j+="\"balance\":"+DoubleToString(bal,2)+",";
   j+="\"equity\":"+DoubleToString(AccountEquity(),2)+",";
   j+="\"today\":"+DoubleToString(pToday,2)+",";
   j+="\"week\":"+DoubleToString(pWeek,2)+",";
   j+="\"month\":"+DoubleToString(pMonth,2)+",";
   j+="\"year\":"+DoubleToString(pYear,2)+",";
   j+="\"trades\":"+IntegerToString(trades)+",";
   j+="\"winRate\":"+DoubleToString(winRate,1)+",";
   j+="\"pf\":"+DoubleToString(pf,2)+",";
   j+="\"pips\":"+DoubleToString(pips,0)+",";
   j+="\"avgWin\":"+DoubleToString(avgWin,2)+",";
   j+="\"avgLoss\":"+DoubleToString(avgLoss,2)+",";
   j+="\"best\":"+DoubleToString(best,2)+",";
   j+="\"worst\":"+DoubleToString(worst,2)+",";
   j+="\"dd\":"+DoubleToString(maxEquityDrawdown,1)+",";
   j+="\"monthly\":[";
   for(int k=0;k<12;k++){ if(k>0)j+=","; j+=DoubleToString(mon[k],2); }
   j+="]}";
   return j;
}

//+------------------------------------------------------------------+
//  BuildHistoryJSON - journal buuxa (ilaa Cloud_History_Count trade)
//  MT5 history-ga ayaa ah ilaha xogta. Server database uma baahna -
//  xogtu weligeed ma lumeyso, xitaa marka Render dib u bilaabmo.
//+------------------------------------------------------------------+
string CloudEscJ(string t)
{
   string o=""; int n=StringLen(t);
   for(int i=0;i<n;i++){
      ushort c=StringGetCharacter(t,i);
      if(c=='"') o+="\\\""; else if(c=='\\') o+="\\\\";
      else if(c=='\n'||c=='\r') o+=" "; else o+=ShortToString(c);
   }
   return(o);
}

string BuildHistoryJSON()
{
   int want = MathMax(10, MathMin(300, Cloud_History_Count));
   string arr="["; int cnt=0;

   for(int i=m4OrdersHistoryTotal()-1; i>=0 && cnt<want; i--)
   {
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_HISTORY)) continue;
      int mg=m4OrderMagicNumber();
      if(mg<MagicNumber+100 || mg>MagicNumber+300) continue;
      if(m4OrderType()!=OP_BUY && m4OrderType()!=OP_SELL) continue;

      string sym = m4OrderSymbol();
      int    dg  = (int)SymbolInfoInteger(sym,SYMBOL_DIGITS);
      double pt  = SymbolInfoDouble(sym,SYMBOL_POINT);
      bool   buy = (m4OrderType()==OP_BUY);
      double op  = m4OrderOpenPrice();
      double cp  = m4OrderClosePrice();
      double pl  = m4OrderProfit()+m4OrderCommission()+m4OrderSwap();
      double pts = (pt>0) ? ((buy ? cp-op : op-cp)/pt) : 0.0;
      int    idx = mg-(MagicNumber+100); if(idx<0||idx>6) idx=0;

      if(cnt>0) arr+=",";
      arr+="{\"sym\":\""      + CloudEscJ(sym) + "\""
          +",\"type\":\""     + (buy?"BUY":"SELL") + "\""
          +",\"strat\":\""    + CloudEscJ(EnumToStr_Strategy(idx)) + "\""
          +",\"lot\":"        + DoubleToString(m4OrderLots(),2)
          +",\"open\":"       + DoubleToString(op,dg)
          +",\"close\":"      + DoubleToString(cp,dg)
          +",\"points\":"     + DoubleToString(pts,1)
          +",\"profit\":"     + DoubleToString(pl,2)
          +",\"otime\":"      + IntegerToString((int)m4OrderOpenTime())
          +",\"ctime\":"      + IntegerToString((int)m4OrderCloseTime())
          +"}";
      cnt++;
   }
   arr+="]";
   return(arr);
}

string BuildTradesJSON(){   // b78: liiska ganacsiyada (open + closed dhaw) -> dashboard (sym/type/strat/profit/st)
   string arr="["; int cnt=0;
   for(int i=m4OrdersTotal()-1;i>=0 && cnt<10;i--){        // OPEN trades
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_TRADES)) continue;
      int mg=m4OrderMagicNumber(); if(mg<MagicNumber+100||mg>MagicNumber+106) continue;
      if(m4OrderType()!=OP_BUY&&m4OrderType()!=OP_SELL) continue;
      if(cnt>0) arr+=","; int idx=mg-(MagicNumber+100);
      arr+="{\"sym\":\""+m4OrderSymbol()+"\",\"type\":\""+(m4OrderType()==OP_BUY?"BUY":"SELL")+"\",\"strat\":\""+EnumToStr_Strategy(idx)+"\",\"profit\":"+DoubleToString(m4OrderProfit()+m4OrderCommission()+m4OrderSwap(),2)+",\"st\":\"OPEN\"}";
      cnt++;
   }
   // v57.3: journal-ka server-ka wuxuu u baahan yahay ticket + waqti + qiimo,
   // si uu u kala saaro trade-yada oo aanu isku celcelin marka EA-gu dib u bilaabmo.
   for(int i=m4OrdersHistoryTotal()-1;i>=0 && cnt<60;i--){ // CLOSED dhaw
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_HISTORY)) continue;
      int mg=m4OrderMagicNumber(); if(mg<MagicNumber+100||mg>MagicNumber+106) continue;
      if(m4OrderType()!=OP_BUY&&m4OrderType()!=OP_SELL) continue;
      if(cnt>0) arr+=","; int idx=mg-(MagicNumber+100);
      string csym=m4OrderSymbol();
      int    cdg =(int)MarketInfo(csym,MODE_DIGITS); if(cdg<=0) cdg=5;
      arr+="{\"sym\":\""+csym+"\""
          +",\"type\":\""+(m4OrderType()==OP_BUY?"BUY":"SELL")+"\""
          +",\"strat\":\""+EnumToStr_Strategy(idx)+"\""
          +",\"profit\":"+DoubleToString(m4OrderProfit()+m4OrderCommission()+m4OrderSwap(),2)
          +",\"st\":\"CLOSED\""
          +",\"ticket\":"+IntegerToString(m4OrderTicket())
          +",\"open_t\":"+IntegerToString((int)m4OrderOpenTime())
          +",\"close_t\":"+IntegerToString((int)m4OrderCloseTime())
          +",\"entry\":"+DoubleToString(m4OrderOpenPrice(),cdg)
          +",\"cur\":"+DoubleToString(m4OrderClosePrice(),cdg)
          +",\"lot\":"+DoubleToString(m4OrderLots(),2)
          +",\"digits\":"+IntegerToString(cdg)+"}";
      cnt++;
   }
   arr+="]"; return arr;
}
void SendToCloud(){ if(IsTesting()) return; if(!EnableCloudDashboard) return; if(Cloud_Auth_Token=="CHANGE_ME_LONG_RANDOM_SECRET") return; string json="{"; json+="\"token\":\""+Cloud_Auth_Token+"\","; json+="\"bot\":\""+Cloud_Bot_Name+"\","; json+="\"balance\":"+DoubleToString(AccountBalance(),2)+","; json+="\"equity\":"+DoubleToString(AccountEquity(),2)+","; json+="\"profit\":"+DoubleToString(todayClosedProfit,2)+","; json+="\"winrate\":"+DoubleToString(winRatePercent,1)+","; json+="\"drawdown\":"+DoubleToString(maxEquityDrawdown,1)+","; json+="\"opentrades\":"+IntegerToString(CountAllOrders())+","; json+="\"trades\":"+BuildTradesJSON()+","; json+="\"journal\":"+BuildJournalJSON()+","; static datetime __histAt=0; if(TimeCurrent()-__histAt >= MathMax(20,Cloud_History_Secs)){ __histAt=TimeCurrent(); json+="\"history\":"+BuildHistoryJSON()+","; } json+="\"status\":\"RUNNING\""; json+="}"; /* b34: status=RUNNING -> ONLINE */ uchar postData[], result[]; StringToCharArray(json,postData); ArrayResize(postData,StringLen(json)); string headers="Content-Type: application/json\r\nAuthorization: Bearer "+Cloud_Auth_Token+"\r\n"; string resultHeaders; int res=WebRequest("POST",CloudDashboardURL,headers,3000,postData,result,resultHeaders); if(res==-1){ consecutiveWebFails++; Print("CLOUD DASHBOARD FAILED err=", GetLastError()); } else { consecutiveWebFails=0; } }

//+------------------------------------------------------------------+
//| b75: DASHBOARD CSV — Node.js dashboard-ku wuu akhriyaa           |
//| Fayl: MOHA_Trades_<Symbol>.csv (MQL4/Files ama MQL5/Files)       |
//| Col: Ticket,OpenTime,CloseTime,Type,Entry,TP,SL,Profit,Comment   |
//+------------------------------------------------------------------+
void WriteDashboardCSV(){
   string fn="MOHA_Trades_"+Symbol()+".csv";
   int h=FileOpen(fn,FILE_WRITE|FILE_CSV|FILE_ANSI,',');
   if(h==INVALID_HANDLE) return;
   FileWrite(h,"Ticket","OpenTime","CloseTime","Type","Entry","TP","SL","Profit","Comment");
   int ht=m4OrdersHistoryTotal();
   for(int i=0;i<ht;i++){
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_HISTORY)) continue;
      if(m4OrderSymbol()!=Symbol()) continue;
      int mg=m4OrderMagicNumber();
      if(mg<MagicNumber+100||mg>MagicNumber+106) continue;   // kaliya trade-yada MOHA (magic strat-gated)
      if(m4OrderType()!=OP_BUY&&m4OrderType()!=OP_SELL) continue;
      string ot=TimeToString(m4OrderOpenTime(),TIME_DATE|TIME_SECONDS);
      string ct=TimeToString(m4OrderCloseTime(),TIME_DATE|TIME_SECONDS);
      StringReplace(ot,".","-"); StringReplace(ct,".","-");   // JS Date() u fudud (YYYY-MM-DD HH:MM:SS)
      string cmt=m4OrderComment(); StringReplace(cmt,",",";");  // ha jabin CSV column-ka
      double prof=m4OrderProfit()+m4OrderCommission()+m4OrderSwap();
      FileWrite(h,IntegerToString(m4OrderTicket()),ot,ct,(m4OrderType()==OP_BUY?"BUY":"SELL"),
                DoubleToString(m4OrderOpenPrice(),5),DoubleToString(m4OrderTakeProfit(),5),
                DoubleToString(m4OrderStopLoss(),5),DoubleToString(prof,2),cmt);
   }
   FileClose(h);
}

//+------------------------------------------------------------------+
//| OnInit                                                           |
//+------------------------------------------------------------------+
bool g_EmergencyStopLatched=false;   // b110
bool ValidateRiskConfig(){           // b110 FIX: ka hortag habayn position QAAWAN abuurta
   if(Enable_Stealth_Mode && !UseVirtualOrders && SLTP_Mode!=SLTP_FIXED){
      Alert("HABAYN KHATAR AH: Stealth_Mode + SLTP_Mode!=FIXED + UseVirtualOrders=false = trade SL LA'AAN. Mid ka beddel.");
      Print("INIT FAIL: stealth config -> naked position");
      return false;
   }
   if(Enable_Martingale && Auto_Lot)
      Print("DIGNIIN: Enable_Martingale wuxuu tirtirayaa lot-ka Risk_Percent. Mid dami.");
   if(Enable_Stealth_Mode || UseVirtualOrders)
      Print("DIGNIIN: SL/TP broker-ka ma jiro -> BreakEven/TP-Ladder/ScaleOut way damsan yihiin.");
   return true;
}
int OnInit(){
   IsAuthorized=ValidateLogin(); if(!IsAuthorized) return INIT_FAILED;
   if(!ValidateRiskConfig()) return INIT_FAILED;   // b110
   g_EmergencyStopLatched=false;
   dailyStartBalance=AccountBalance(); weeklyStartBalance=AccountBalance(); peakEquity=AccountEquity();
   lastDay=Day(); 
   lastWeekStart=StringToTime(TimeToString(TimeCurrent(),TIME_DATE))-(DayOfWeek()-1)*86400;
   todayClosedProfit=0; weeklyClosedProfit=0;
   tradesOpenedToday=0; lossesToday=0; consecutiveLosses=0; trueConsecutiveLosses=0; dailyProfitLocked=false; weeklyProfitLocked=false; lastCountedHistoryTotal=m4OrdersHistoryTotal();
   currentActiveStrategy=(int)Select_Strategy; lastStrategySwitch=TimeCurrent();
   g_SelStrat=(int)Select_Strategy; g_UserLot=0; g_UserSL=0; g_UserTP=0; g_BE_On=EnableBreakEven; g_Trail_On=EnableTrailingStop;   // b99: panel BE/TRAIL bilow = inputs
   g_TPL_On=Enable_TP_Ladder; g_TP_On[0]=(TPL_TP1_Pct>0); g_TP_On[1]=(TPL_TP2_Pct>0); g_TP_On[2]=(TPL_TP3_Pct>0); g_TP_On[3]=(TPL_TP4_Pct>0);   // b101: panel TP-Ladder bilow = inputs
   g_TP_R[0]=TPL_TP1_R; g_TP_R[1]=TPL_TP2_R; g_TP_R[2]=TPL_TP3_R; g_TP_R[3]=TPL_TP4_R;   // b102: heerarka R panel-ka laga beddeli karo   // V56: control panel bilow
   g_TP_Auto=true; __tpAutoCalc();   // b105: bilow AUTO
   __tpOrderFix(0);   // b104: hubi kala-horreynta bilowga
   ArrayInitialize(Signal_Counts,0); ArrayResize(scaleOutStates,0); ArrayResize(tpLadder,0); ArrayResize(dailyPnLHistory,0); ArrayResize(virtualStates,0); ArrayResize(g_EntryScores,0);
   // PRO (V41): seed existing open trades so a restart never re-triggers partial/scaleout
   for(int _si=0;_si<m4OrdersTotal();_si++){ if(m4OrderSelect(_si,SELECT_BY_POS,MODE_TRADES)){ int _mg=m4OrderMagicNumber(); if(m4OrderSymbol()==Symbol() && _mg>=MagicNumber+100 && _mg<=MagicNumber+106){ long _t=m4OrderTicket(); int _vi=GetOrCreateVirtualState(_t); virtualStates[_vi].partialDone=true; int _sz=ArraySize(scaleOutStates); ArrayResize(scaleOutStates,_sz+1); scaleOutStates[_sz].ticket=_t; scaleOutStates[_sz].r1Done=true; scaleOutStates[_sz].r2Done=true; int _tz=ArraySize(tpLadder); ArrayResize(tpLadder,_tz+1); tpLadder[_tz].ticket=_t; tpLadder[_tz].level=4; tpLadder[_tz].rDist=0; tpLadder[_tz].origLot=0; } } }   // b63: position-yada hore ee restart -> level=4 (ladder ha taaban, si aan rDist khaldan loo isticmaalin)
   ArrayInitialize(stratSignalCount,0); ArrayInitialize(stratCurrentSignal,-1);
   for(int i=0;i<6;i++) lastSignalBar[i]=0; lastTradeBarTime_1H=0;
   ApplyPremiumTheme(); CreateDashboard(); CreateWatermark();
   // V54/V55: labada bandhig ee hoose la saaray (CURRENCY POWER + CURRENCY STRENGTH)
   ObjectsDeleteAll(0,"ND_"); ObjectsDeleteAll(0,"CS_");
   CP_CreatePanel();   // V56: on-chart control panel (xeelad + SL/TP/LOT badhamo)
   UpdateEMALines();   // b24: sawir laba xariiq EMA (50/200) haddii la shido
   UpdateStrategyStats(); UpdateCurrencyStrength();
   WriteDashboardCSV();   // b75: bilowga -> qor xogta dashboard-ka (si uu u muujiyo taariikhda hore)
   if(EnableJournal) JournalWriteHeader();
   if(Cloud_Auth_Token=="CHANGE_ME_LONG_RANDOM_SECRET"&&EnableCloudDashboard) Print("SECURITY WARNING: Cloud_Auth_Token default, dashboard disabled.");
   // ---- QAYBTA 1 FIX: soo qaad calendar-ka wararka marka bilowga ah ----
   ArrayResize(g_NewsEvents,0); g_NewsDataValid=false; g_LastNewsFetch=0;
   if(EnableNewsFilter) FetchNewsCalendar();
   if(EnableTelegram){ SendTelegram("🤖 MOHA PRO ONLINE  ·  "+Symbol()+"\n💼 Balance:  $"+DoubleToString(AccountBalance(),2)+"   ·   Auto ✓"); }
   Print("=== MOHA PRO V56 LIVE READY - ALL QAYBOOD COMPLETE ===");
   return INIT_SUCCEEDED;
}
string DeinitReasonText(int r){   // b54: MT5 deinit-reason -> qoraal Soomaali ah cad (halkii "Reason: 5")
   switch(r){
      case 0: return "Program (ExpertRemove)";
      case 1: return "Chart-ka laga saaray";
      case 2: return "Dib loo compile-gareeyay";
      case 3: return "Symbol/Timeframe la beddelay";
      case 4: return "Chart-ka la xiray";
      case 5: return "Xeerar/settings la beddelay";
      case 6: return "Account kale la furay";
      case 7: return "Template cusub la dabaqay";
      case 8: return "OnInit fashilmay";
      case 9: return "Terminal-ka la xiray";
   }
   return "Lama garanayo ("+IntegerToString(r)+")";
}
void OnDeinit(const int reason){ __ihReleaseAll();
   if(VSA_Diagnostic && g_vsaBars>0){   // b113
      PrintFormat("=== VSA DIAGNOSTIC ===  shumac la baaray: %d", (int)g_vsaBars);
      PrintFormat("  volume >= %.2fx        : %d  (%.1f%%)", VSA_VolumeRatio,      (int)g_vsaVolHi,      100.0*g_vsaVolHi/g_vsaBars);
      PrintFormat("  spread <= %.2fx (narrow): %d  (%.1f%%)", VSA_NarrowSpreadRatio,(int)g_vsaSprNarrow, 100.0*g_vsaSprNarrow/g_vsaBars);
      PrintFormat("  spread >= %.2fx (wide)  : %d  (%.1f%%)", VSA_WideSpreadRatio,  (int)g_vsaSprWide,   100.0*g_vsaSprWide/g_vsaBars);
      PrintFormat("  close pos xoog leh      : %d  (%.1f%%)", (int)g_vsaClosePos, 100.0*g_vsaClosePos/g_vsaBars);
      PrintFormat("  SIGNAL LA HELAY         : %d  (%.2f%%)", (int)g_vsaSignals,  100.0*g_vsaSignals/g_vsaBars);
   }   // b110 FIX: handle leak
   ObjectsDeleteAll(0,"MohaEMA_"); ObjectsDeleteAll(0,"Moha_"); ObjectsDeleteAll(0,"ND_"); ObjectsDeleteAll(0,"CP_"); ObjectsDeleteAll(0,"WR_"); ObjectsDeleteAll(0,"M_Sig_"); ObjectsDeleteAll(0,"M_Dyn_"); ObjectsDeleteAll(0,"Strat_"); ObjectsDeleteAll(0,"CS_"); ObjectsDeleteAll(0,"H_"); ObjectsDeleteAll(0,"RR_SL_"); ObjectsDeleteAll(0,"RR_TP_"); if(EnableTelegram){ bool restart=(reason==2||reason==3||reason==5||reason==7); SendTelegram((restart?"🔄 MOHA PRO dib u bilaabmayaa":"⏹️ MOHA PRO joogsaday")+"  ·  "+Symbol()+"\n📋 Sabab: "+DeinitReasonText(reason)); } }
//+------------------------------------------------------------------+
//| b24 VISUAL: two EMA lines (50/200) on chart - input controlled    |
//+------------------------------------------------------------------+
void DrawEMALineSeg(string pfx,int period,color clr,int width,int barsN){
   int n=MathMin(barsN, Bars-period-2); if(n<2) return;   // b25: ha sawirin warm-up (EMA aan diyaar) - ka fogow spike bilowga
   for(int i=0;i<n;i++){
      double e1=m4iMA(NULL,0,period,0,MODE_EMA,PRICE_CLOSE,i);
      double e2=m4iMA(NULL,0,period,0,MODE_EMA,PRICE_CLOSE,i+1);
      if(e1<=0||e2<=0) continue;   // b25: ka bood qiime aan sax ahayn
      datetime t1=m4iTime(NULL,0,i), t2=m4iTime(NULL,0,i+1);
      if(t1<=0||t2<=0) continue;
      string nm=pfx+IntegerToString(i);
      if(ObjectFind(0,nm)<0) ObjectCreate(0,nm,OBJ_TREND,0,t2,e2,t1,e1);
      else { ObjectSetInteger(0,nm,OBJPROP_TIME,0,t2); ObjectSetDouble(0,nm,OBJPROP_PRICE,0,e2); ObjectSetInteger(0,nm,OBJPROP_TIME,1,t1); ObjectSetDouble(0,nm,OBJPROP_PRICE,1,e1); }
      ObjectSetInteger(0,nm,OBJPROP_COLOR,clr);
      ObjectSetInteger(0,nm,OBJPROP_WIDTH,width);
      ObjectSetInteger(0,nm,OBJPROP_RAY,false);
      ObjectSetInteger(0,nm,OBJPROP_BACK,true);
      ObjectSetInteger(0,nm,OBJPROP_SELECTABLE,false);
   }
}
void UpdateEMALines(){
   if(!Show_EMA_Lines){ ObjectsDeleteAll(0,"MohaEMA_"); return; }
   if(IsTesting()) return;   // chart-ka toos (live/demo) ayey ka muuqataa (backtest dhakhso u haya)
   static datetime lastDraw=0; datetime t0=m4iTime(NULL,0,0); if(t0==lastDraw) return; lastDraw=t0;
   DrawEMALineSeg("MohaEMA_F_",Viz_EMA_Fast,Viz_EMA_Fast_Clr,Viz_EMA_Width,Viz_EMA_Bars);
   DrawEMALineSeg("MohaEMA_S_",Viz_EMA_Slow,Viz_EMA_Slow_Clr,Viz_EMA_Width,Viz_EMA_Bars);
}
void OnTrade(){
   int ht=m4OrdersHistoryTotal();                       // b110 FIX: HAL MAR (hore loop kasta -> O(n^2))
   if(ht==lastCountedHistoryTotal){ UpdateDashboardValues(); return; }
   int newCount=ht-lastCountedHistoryTotal; if(newCount<0) newCount=0;
   datetime todayStart=StringToTime(TimeToString(TimeCurrent(),TIME_DATE));
   datetime weekStart=todayStart-(DayOfWeek()-1)*86400;
   double newClosed=0,newWeekly=0;
   for(int i=0;i<ht;i++){
      if(!m4OrderSelect(i,SELECT_BY_POS,MODE_HISTORY)) continue;
      int mg=m4OrderMagicNumber();
      if(mg<MagicNumber+100||mg>MagicNumber+106) continue;
      if(m4OrderSymbol()!=Symbol()) continue;
      double pr=m4OrderProfit()+m4OrderCommission()+m4OrderSwap();
      datetime ct=m4OrderCloseTime();
      if(ct>=todayStart) newClosed+=pr;
      if(ct>=weekStart)  newWeekly+=pr;
      // b110 FIX: DHAMMAAN deal-yada cusub (hore kii ugu dambeeyay KALIYA -> khasaare la seegay)
      if(newCount>0 && i>=ht-newCount){
         JournalLogClosedTrade(m4OrderTicket());
         if(pr<0){ lossesToday++; consecutiveLosses++; trueConsecutiveLosses++; }
         else if(pr>0){ consecutiveLosses=0; trueConsecutiveLosses=0; }
      }
   }
   todayClosedProfit=newClosed; weeklyClosedProfit=newWeekly; lastCountedHistoryTotal=ht;
   UpdateStrategyStats(); AutoSwitchStrategy(); UpdateDashboardValues(); WriteDashboardCSV();
}

//+------------------------------------------------------------------+
//| OnTick                                                           |
//+------------------------------------------------------------------+
void OnTick(){
   if(!IsAuthorized) return;
   UpdateEMALines();   // b24: cusboonaysii xariiqyada EMA (bar cusub kasta)
   // QAYBTA 15 FIX (V39): connection guard + reconnect alert (V40: skip in tester)
   if(!IsTesting()){
      bool conn=(bool)TerminalInfoInteger(TERMINAL_CONNECTED);
      if(Enable_Reconnect_Alert){ if(!conn && g_wasConnected){ if(EnableTelegram) SendTelegram("LIVE PROTECTION: Xiriirku GO'AY (connection lost)."); g_wasConnected=false; } else if(conn && !g_wasConnected){ if(EnableTelegram) SendTelegram("LIVE PROTECTION: Xiriirku SOO LAABTAY (reconnected)."); g_wasConnected=true; } }
      if(Enable_Connection_Guard && !conn){ ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"NO CONNECTION"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrRed); return; }
   }
   if(CloseWeekend && DayOfWeek()==5 && Hour()>=20 && CountAllOrders()>0){ CloseAllTrades("CloseWeekend"); }
   double eq=AccountEquity(); if(eq>peakEquity) peakEquity=eq;
   int curDay=Day(); if(curDay!=lastDay){ dailyStartBalance=AccountBalance(); lastDay=curDay; missedOpportunities=0; totalSignalsToday=0; todayClosedProfit=0; drawdownAlertSent=false; ArrayInitialize(Signal_Counts,0); propTradingDays++; lastTradeBarTime_1H=0; tradesOpenedToday=0; lossesToday=0; consecutiveLosses=0; dailyProfitLocked=false; if(EnableTelegram && !DailySummaryClaimedToday()){ DailySummaryClaimToday(); SendTelegramDailySummary(); SendTelegram("Maalin cusub | Haraaga: $"+DoubleToString(AccountBalance(),2)); } }
   datetime curWeekStart=StringToTime(TimeToString(TimeCurrent(),TIME_DATE))-(DayOfWeek()-1)*86400; if(curWeekStart!=lastWeekStart){ weeklyStartBalance=AccountBalance(); lastWeekStart=curWeekStart; weeklyClosedProfit=0; weeklyProfitLocked=false; if(EnableTelegram)SendTelegramWeeklySummary(); }
   if(TG_DrawdownAlert&&!drawdownAlertSent&&peakEquity>0){ double dd=(peakEquity-eq)/peakEquity*100.0; if(dd>=TG_DrawdownAlertPct){ SendTelegram("DRAWDOWN DIGNIIN: "+DoubleToString(dd,2)+"%"); drawdownAlertSent=true; } }
   // b110 FIX: latch - hore tick kasta CloseAllTrades+SendTelegram (WebRequest 5s) = terminal barafoobay
   if(peakEquity>0){
      double totalDD=(peakEquity-eq)/peakEquity*100.0;
      if(totalDD>=Max_Total_Drawdown_Pct){
         if(!g_EmergencyStopLatched){
            g_EmergencyStopLatched=true;
            CloseAllTrades("Emergency: Max drawdown");
            if(EnableTelegram) SendTelegram("EMERGENCY STOP: Total DD "+DoubleToString(totalDD,2)+"%");
            Print("EMERGENCY STOP LATCHED @ DD ",DoubleToString(totalDD,2),"%");
         }
         ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"EMERGENCY STOP (DD)");
         ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrRed);
         return;
      }
   }
   if(IsNewBar(PERIOD_H1))UpdateCurrencyStrength();
   int _zoneStrat=(g_SelStrat>=0)?g_SelStrat:((Enable_Auto_Strategy&&currentActiveStrategy>=0)?currentActiveStrategy:(int)Select_Strategy);   // b55 FIX: zone-ka chart-ka = xeelada DHABTA la ganacsado (panel/g_SelStrat), maaha currentActiveStrategy oo aan la cusbooneysiin
   if(Show_Trend_Lines && Bars>SR_Lookback && _zoneStrat==STRAT_SR) Wrapper_DrawManualSR();   // b66 FIX: SR zone ha muuqdo KALIYA marka SR la doorto (xeeladaha kale kama muuqan doonaan)
   else { ObjectDelete(0,"WR_ResZone"); ObjectDelete(0,"WR_SupZone"); ObjectDelete(0,"WR_ResTxt"); ObjectDelete(0,"WR_SupTxt"); ObjectDelete(0,"WR_ResLine"); ObjectDelete(0,"WR_SupLine"); }   // b66: nadiifi haddii xeelad aan SR ahayn la doorto
   if(Show_Dynamic_Zones&&Bars>50)DrawDynamicZones((ENUM_STRATEGY)_zoneStrat);
   CheckMoneyProfit(); ManageActiveTrades(); CleanRRBoxes(); PruneVirtualStates();
   if(Enable_Candle_Sync&&!IsNewBar(Sync_TF)) return;
   if(!PropFirmCheck()) return;
   if(!SafetyChecksPassed()) return;   // QAYBTA 14 FIX (V34)
   if(EnableNewsFilter&&IsNewsActive()){ if(!g_newsAlertSent){ if(EnableTelegram && TG_NewsAlert) SendTelegram("📰 NEWS DIGNIIN\n💱 "+Symbol()+"\nGanacsi waa la joojiyay (news xoog leh)."); g_newsAlertSent=true; } ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"NEWS ACTIVE - JOOJIYAY"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrOrange); return; } else { g_newsAlertSent=false; }
   // b110 FIX: equity, maaha balance (floating loss lama arki jirin)
   // v57.2 FIX: hubintu waxay ku dhex jirtay comment-ka -> xadku weligiis ma dhicin.
   double lossPct = (dailyStartBalance>0)
                  ? (dailyStartBalance-AccountEquity())/dailyStartBalance*100.0 : 0;
   if(lossPct >= Daily_Loss_Limit_Percent){
      ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"DAILY LOSS LIMIT GAADHAY");
      ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrRed);
      return;
   }
   if(!IsTradingTime()){ ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"SESSION DHAMMAATAY"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrSilver); return; }
   if(!InSessionWindow()){ ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"OUT OF SESSION"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrSilver); return; }  // QAYBTA 7 FIX (V35)
   if(MinMinutesBetweenTrades>0&&TimeCurrent()-lastTradeTime<MinMinutesBetweenTrades*60) return;
   if(!AllowMultiplePerBar&&lastTradeBarTime==m4iTime(Symbol(),0,0)) return;
   datetime currentBar1H=m4iTime(Symbol(),PERIOD_H1,0);
   // PRO (V41): xisaabi signalada bar cusub kaliya (management wuu socdaa tick kasta)
   ENUM_TIMEFRAMES _tradeTF=(Trade_Timeframe==TF_M1)?PERIOD_M1:PERIOD_M5;
   if((!Enable_NewBar_Only) || IsNewBar(_tradeTF)){
      UpdateAllStrategySignals();
      int sig=-1, stratIdx=(int)Select_Strategy;
      int minT=0; double buf=0;
      if(Enable_Multi_Strategy || Strategy_Group!=GRP_SINGLE){   // b23: koox (TREND/REVERSAL/ALL) = multi
         if(Enable_Consensus){ if(!GetConsensusSignal(sig, stratIdx, minT, buf)) sig=-1; }   // QAYBTA 10 FIX (V35)
         else { if(!GetBestMultiSignal(sig, stratIdx, minT, buf)) sig=-1; }
      }
      else { sig=GetCurrentStrategySignal(minT, buf); stratIdx=(g_SelStrat>=0)?g_SelStrat:((Enable_Auto_Strategy&&currentActiveStrategy>=0)?currentActiveStrategy:(int)Select_Strategy); }   // b31 FIX: stratIdx = xeelada DHABTA la isticmaalay (control panel/g_SelStrat) - si SL/TP/gates saxan

      if(sig!=-1){
         string cleanWhy="";
         if(!IsMarketCleanForTrading(sig, stratIdx, cleanWhy)) { missedOpportunities++; TG_AlertRejected("Filter (Regime/ADX/HTF-Trend): "+cleanWhy); ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"FILTERED OUT"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrOrange); UpdateDashboardValues(); return; }
         if(!MTF_ConfirmationPassed(sig)) { missedOpportunities++; TG_AlertRejected("Multi-Timeframe aan isku raacin (H4/H1/M15)"); ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"MTF AAN ISKU RAACIN"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrOrange); UpdateDashboardValues(); return; }
         string e200why="";   // b106: EMA200 H1+H4 big-trend filter
         if(!HTF_EMA200_Passed(sig, e200why)) { missedOpportunities++; TG_AlertRejected("EMA200 jihada wayn (H1/H4) khilaaf: "+e200why); ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"EMA200 H1/H4 DIIDAY"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrOrange); UpdateDashboardValues(); return; }
         if(!CorrelationFilterPassed(sig)) { missedOpportunities++; TG_AlertRejected("Correlation limit (lammaanayaal isku xidhan)"); ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"CORRELATION LIMIT"); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrOrange); UpdateDashboardValues(); return; }
         if(One_Trade_Per_H1_Bar && currentBar1H!=0 && currentBar1H==lastTradeBarTime_1H){ missedOpportunities++; UpdateDashboardValues(); return; }   // b111: hadda input
         if(CountAllOrders()<Max_Open_Trades&&(int)MarketInfo(Symbol(),MODE_SPREAD)<=GetMaxAllowedSpread()){
            if(Filter_Low_Volatility){ double atrV=m4iATR(Symbol(),0,ATR_Period_Core,1); if(atrV<Min_ATR_Pips*GetPipSize(Symbol())){ missedOpportunities++; UpdateDashboardValues(); return; } }
            totalSignalsToday++; UpdateSignalStats(); double lot=GetSmartLot();
            SmartOrder(sig, lot, stratIdx, "AutoStrat_V41");
            if(Show_Trade_Signals) DrawTradeMarker(sig, "Auto");
            string sNames[7]={"SR","BB","EMA","SMC","VSA","POC","-"};
            ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,(sig==OP_BUY?"BUY":"SELL")+" "+sNames[stratIdx]+" | "+Symbol());
            ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,(sig==OP_BUY)?MC_GREEN:MC_RED);
         } else { missedOpportunities++; }
      } else { ObjectSetString(0,"Moha_Status",OBJPROP_TEXT,"LIVE SCANNING... "+(Trade_Timeframe==TF_M1?"M1":"M5")); ObjectSetInteger(0,"Moha_Status",OBJPROP_COLOR,clrGold); }
   }
   UpdateDashboardValues();   // V54: ND_UpdateStats() la saaray (CURRENCY POWER panel la tirtiray)

   static datetime lastCmd=0;
   if(EnableCloudDashboard && TimeCurrent()-lastCmd>=3){ CheckCloudCommands(); lastCmd=TimeCurrent(); }
   static datetime lastCloud=0;
   if(EnableCloudDashboard&&TimeCurrent()-lastCloud>=2){ SendToCloud(); lastCloud=TimeCurrent(); }
   static datetime lastJrnl=0;
   if(EnableCloudDashboard&&TimeCurrent()-lastJrnl>=60){ SendClosedTrades(); lastJrnl=TimeCurrent(); }
   if(consecutiveWebFails>20 && EnableTelegram && TG_ErrorAlerts){ SendTelegram("WARNING: Cloud connection failed "+IntegerToString(consecutiveWebFails)+" times."); consecutiveWebFails=0; }
}
//+------------------------------------------------------------------+
