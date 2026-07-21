#!/usr/bin/env python3
# Assembles MohaPro_V56_MT5.mq5 = MQL4-compatibility layer + V56 body (transformed).
import re

src = open('MohaPro_V56.mq4', encoding='utf-8', errors='replace').read()

# --- drop the original property header block (lines up to the ENUMS marker) ---
idx = src.find('//| ENUMS')
# back up to the comment box start before ENUMS
box = src.rfind('//+---', 0, idx)
body = src[box:]

# --- rename MQL4 identifiers that COLLIDE with MT5 built-ins -> m4* wrappers ---
collide = ['iMA','iRSI','iATR','iADX','iBands','iMACD','iVolume','iHighest','iLowest',
           'iClose','iOpen','iTime','iBars',
           'OrderSend','OrderSelect','OrderClose','OrderModify','OrdersTotal',
           'OrdersHistoryTotal','OrderTicket','OrderType','OrderLots','OrderOpenPrice',
           'OrderClosePrice','OrderStopLoss','OrderTakeProfit','OrderProfit','OrderCommission',
           'OrderSwap','OrderSymbol','OrderMagicNumber','OrderOpenTime','OrderCloseTime','OrderComment']
for name in collide:
    body = re.sub(r'\b'+name+r'\b', 'm4'+name, body)

header = r'''//+------------------------------------------------------------------+
//|              MOHA PRO ULTIMATE - V56  (MT5 PORT)                  |
//|         MQL4 -> MQL5 via compatibility layer (code kept as-is)    |
//+------------------------------------------------------------------+
#property copyright "Copyright 2026, Moha Pro Forex"
#property version   "56.0"
#property description "MOHA PRO V56 MT5 - full port via MQL4 compatibility layer"
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
int __ih(string key,int h){ int n=ArraySize(__ihK); for(int i=0;i<n;i++) if(__ihK[i]==key) return __ihH[i]; ArrayResize(__ihK,n+1); ArrayResize(__ihH,n+1); __ihK[n]=key; __ihH[n]=h; return h; }
double __buf(int h,int bufi,int shift){ if(h==INVALID_HANDLE) return 0; double b[]; if(CopyBuffer(h,bufi,shift,1,b)<1) return 0; return b[0]; }
ENUM_TIMEFRAMES __tf(int tf){ if(tf==0) return _Period; return (ENUM_TIMEFRAMES)tf; }

double m4iMA(string s,int tf,int per,int msh,int meth,int ap,int sh){ string k="MA"+s+IntegerToString(tf)+"_"+IntegerToString(per)+"_"+IntegerToString(msh)+"_"+IntegerToString(meth)+"_"+IntegerToString(ap); int h=__ih(k,iMA(s,__tf(tf),per,msh,(ENUM_MA_METHOD)meth,(ENUM_APPLIED_PRICE)ap)); return __buf(h,0,sh); }
double m4iRSI(string s,int tf,int per,int ap,int sh){ string k="RSI"+s+IntegerToString(tf)+"_"+IntegerToString(per)+"_"+IntegerToString(ap); int h=__ih(k,iRSI(s,__tf(tf),per,(ENUM_APPLIED_PRICE)ap)); return __buf(h,0,sh); }
double m4iATR(string s,int tf,int per,int sh){ string k="ATR"+s+IntegerToString(tf)+"_"+IntegerToString(per); int h=__ih(k,iATR(s,__tf(tf),per)); return __buf(h,0,sh); }
double m4iADX(string s,int tf,int per,int ap,int mode,int sh){ string k="ADX"+s+IntegerToString(tf)+"_"+IntegerToString(per); int h=__ih(k,iADX(s,__tf(tf),per)); int bi=(mode==MODE_MAIN)?0:1; return __buf(h,bi,sh); }
double m4iBands(string s,int tf,int per,double dev,int bsh,int ap,int mode,int sh){ string k="BB"+s+IntegerToString(tf)+"_"+IntegerToString(per)+"_"+DoubleToString(dev,2); int h=__ih(k,iBands(s,__tf(tf),per,bsh,dev,(ENUM_APPLIED_PRICE)ap)); int bi=(mode==MODE_UPPER)?1:((mode==MODE_LOWER)?2:0); return __buf(h,bi,sh); }
double m4iMACD(string s,int tf,int fast,int slow,int sig,int ap,int mode,int sh){ string k="MACD"+s+IntegerToString(tf)+"_"+IntegerToString(fast)+"_"+IntegerToString(slow)+"_"+IntegerToString(sig)+"_"+IntegerToString(ap); int h=__ih(k,iMACD(s,__tf(tf),fast,slow,sig,(ENUM_APPLIED_PRICE)ap)); int bi=(mode==MODE_MAIN)?0:1; return __buf(h,bi,sh); }
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
void __buildHistOut(){ HistorySelect(0,TimeCurrent()); int td=HistoryDealsTotal(); ArrayResize(__histOut,0); for(int i=0;i<td;i++){ ulong tk=HistoryDealGetTicket(i); if(tk==0) continue; if(HistoryDealGetInteger(tk,DEAL_ENTRY)==DEAL_ENTRY_OUT){ int n=ArraySize(__histOut); ArrayResize(__histOut,n+1); __histOut[n]=tk; } } }
int  m4OrdersHistoryTotal(){ __buildHistOut(); return ArraySize(__histOut); }
bool m4OrderSelect(long a,int sel,int pool=MODE_TRADES){
   if(sel==SELECT_BY_TICKET){ if(PositionSelectByTicket((ulong)a)){ __selT=(ulong)a; __selHist=false; return true; }
      HistorySelect(0,TimeCurrent()); if(HistoryDealSelect((ulong)a)){ __selT=(ulong)a; __selHist=true; return true; } return false; }
   if(pool==MODE_TRADES){ ulong tk=PositionGetTicket((int)a); if(tk==0)return false; __selT=tk; __selHist=false; return true; }
   if((int)a<0 || (int)a>=ArraySize(__histOut)) return false; __selT=__histOut[(int)a]; __selHist=true; return true;
}
int    m4OrderTicket(){ return (int)__selT; }
int    m4OrderType(){ if(__selHist){ long dt=HistoryDealGetInteger(__selT,DEAL_TYPE); return (dt==DEAL_TYPE_BUY)?OP_SELL:OP_BUY; } long t=PositionGetInteger(POSITION_TYPE); return (t==POSITION_TYPE_BUY)?OP_BUY:OP_SELL; }
double m4OrderLots(){ if(__selHist) return HistoryDealGetDouble(__selT,DEAL_VOLUME); return PositionGetDouble(POSITION_VOLUME); }
double m4OrderOpenPrice(){ if(__selHist) return HistoryDealGetDouble(__selT,DEAL_PRICE); return PositionGetDouble(POSITION_PRICE_OPEN); }
double m4OrderClosePrice(){ if(__selHist) return HistoryDealGetDouble(__selT,DEAL_PRICE); return PositionGetDouble(POSITION_PRICE_CURRENT); }
double m4OrderStopLoss(){ if(__selHist) return 0; return PositionGetDouble(POSITION_SL); }
double m4OrderTakeProfit(){ if(__selHist) return 0; return PositionGetDouble(POSITION_TP); }
double m4OrderProfit(){ if(__selHist) return HistoryDealGetDouble(__selT,DEAL_PROFIT); return PositionGetDouble(POSITION_PROFIT); }
double m4OrderCommission(){ if(__selHist) return HistoryDealGetDouble(__selT,DEAL_COMMISSION); return 0; }
double m4OrderSwap(){ if(__selHist) return HistoryDealGetDouble(__selT,DEAL_SWAP); return PositionGetDouble(POSITION_SWAP); }
string m4OrderSymbol(){ if(__selHist) return HistoryDealGetString(__selT,DEAL_SYMBOL); return PositionGetString(POSITION_SYMBOL); }
int    m4OrderMagicNumber(){ if(__selHist) return (int)HistoryDealGetInteger(__selT,DEAL_MAGIC); return (int)PositionGetInteger(POSITION_MAGIC); }
datetime m4OrderOpenTime(){ if(__selHist) return (datetime)HistoryDealGetInteger(__selT,DEAL_TIME); return (datetime)PositionGetInteger(POSITION_TIME); }
datetime m4OrderCloseTime(){ if(__selHist) return (datetime)HistoryDealGetInteger(__selT,DEAL_TIME); return 0; }
string m4OrderComment(){ if(__selHist) return HistoryDealGetString(__selT,DEAL_COMMENT); return PositionGetString(POSITION_COMMENT); }
int m4OrderSend(string s,int cmd,double vol,double price,int slip,double sl,double tp,string cmt="",int mg=0,datetime exp=0,color c=clrNONE){
   __t.SetExpertMagicNumber(mg); __t.SetDeviationInPoints(slip>0?slip:10);
   bool ok=(cmd==OP_BUY)?__t.Buy(vol,s,price,sl,tp,cmt):__t.Sell(vol,s,price,sl,tp,cmt);
   if(!ok) return -1; return (int)__t.ResultOrder();
}
bool m4OrderClose(long ticket,double lots,double price,int slip,color c=clrNONE){ __t.SetDeviationInPoints(slip>0?slip:10); if(PositionSelectByTicket((ulong)ticket)){ double _pv=PositionGetDouble(POSITION_VOLUME); if(lots>0 && lots<_pv-0.0000001) return __t.PositionClosePartial((ulong)ticket,lots); } return __t.PositionClose((ulong)ticket); }
bool m4OrderModify(long ticket,double price,double sl,double tp,datetime exp,color c=clrNONE){ ulong _tk=(ulong)ticket; if(!PositionSelectByTicket(_tk)){ if(__selT!=0 && PositionSelectByTicket(__selT)) _tk=__selT; else return false; } double _cs=PositionGetDouble(POSITION_SL), _ct=PositionGetDouble(POSITION_TP); if(MathAbs(_cs-sl)<_Point && MathAbs(_ct-tp)<_Point) return true; return __t.PositionModify(_tk,sl,tp); }
//==================================================================
//  END COMPATIBILITY LAYER  -  MOHA PRO V56 BODY BELOW (unchanged)
//==================================================================

'''

out = header + body
open('MohaPro_V56_MT5.mq5','w',encoding='utf-8').write(out)
print("wrote MohaPro_V56_MT5.mq5:", out.count(chr(10)), "lines")


