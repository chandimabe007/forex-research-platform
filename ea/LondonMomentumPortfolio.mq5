//+------------------------------------------------------------------+
//| LondonMomentumPortfolio.mq5                                      |
//| Faithful MQL5 port of the two accepted `london_momentum` 1h      |
//| books from the forex-research-platform strategy lab (PR #14).    |
//|                                                                  |
//| Source of truth:                                                 |
//|   src/forex_research/strategy_lab/strategies.py  (entries)       |
//|   src/forex_research/strategy_lab/features.py    (indicators)    |
//|   src/forex_research/strategy_lab/engine.py      (fill semantics)|
//|   scripts/portfolio_trailing.py                  (book weights)  |
//|                                                                  |
//| Entry (evaluated once per closed H1 bar, acts on the new bar's   |
//| open — the engine's next-bar-open semantics):                    |
//|   channel  = highest high / lowest low of the 60 bars before the |
//|              signal bar                                          |
//|   atr      = simple 14-bar ATR of the signal bar                 |
//|   impulse  = signal bar closes beyond channel +/- k*ATR AND its  |
//|              tick volume >= 1.2x the 20-bar average              |
//|   stop     = max(k*ATR, minStopPips) floored at the broker's     |
//|              stops level;  TP = RR x stop  (broker-side SL/TP)   |
//|   window   = 07:00-11:00 UTC, one entry per book per UTC day     |
//|   London session check is made on the bar's OPEN time, which the|
//|   engine stamps as the trade time.                               |
//+------------------------------------------------------------------+
#property copyright "forex-research-platform"
#property link      "https://github.com/"
#property version   "1.00"
#property description "London-momentum impulse portfolio (EURUSD + GBPUSD, H1)."
#property description "Port of the accepted PR #14 lab configuration."

#include <Trade\Trade.mqh>

//--- trade direction selector
enum ENUM_TRADE_DIR
  {
   DIR_BOTH       = 0, // Longs and shorts
   DIR_LONG_ONLY  = 1, // Longs only
   DIR_SHORT_ONLY = 2  // Shorts only
  };

//--- inputs -------------------------------------------------------
input group "=== Book: EURUSD (risk 0.72%) ==="
input string InpEURSymbol        = "EURUSD";  // EURUSD symbol name (broker suffix aware)
input double InpEURImpulseMult   = 1.5;       // Impulse threshold (x ATR)
input double InpEURStopMult      = 1.5;       // Stop distance (x ATR)
input double InpEURRR            = 3.0;       // Reward:risk
input double InpEURRiskPct       = 0.72;      // Risk per trade (% of equity)

input group "=== Book: GBPUSD (risk 0.83%) ==="
input string InpGBPSymbol        = "GBPUSD";  // GBPUSD symbol name (broker suffix aware)
input double InpGBPImpulseMult   = 0.75;      // Impulse threshold (x ATR)
input double InpGBPStopMult      = 0.75;      // Stop distance (x ATR)
input double InpGBPRR            = 1.5;       // Reward:risk
input double InpGBPRiskPct       = 0.83;      // Risk per trade (% of equity)

input group "=== Session and risk guards ==="
input int    InpUTCOffsetHours   = 0;         // Broker-server offset from UTC (hours)
input int    InpSessionStartUTC  = 7;         // Session open (UTC hour)
input int    InpSessionEndUTC    = 11;        // Session close (UTC hour, exclusive)
input int    InpFridayFlatUTC    = 21;        // Friday flat time (UTC hour)
input double InpDailyLossPct     = 5.0;       // Daily loss lockout (% of day-start equity)
input double InpMaxSpreadPips    = 2.0;       // Skip entries when spread wider than this
input double InpMinStopPips      = 1.0;       // Engine MIN_STOP_PIPS floor
input double InpMaxRiskOvershoot = 1.30;      // Allow min-lot overshoot up to 1.3x budget
input double InpMaxMarginUsePct  = 80.0;      // Cap margin use (% of free margin)

input group "=== Identification ==="
input long   InpMagicBase        = 8614001;   // Magic number (EURUSD uses this, GBPUSD +1)
input ENUM_TRADE_DIR InpDir      = DIR_BOTH;  // Trade direction

//--- per-book runtime state --------------------------------------
struct BookState
  {
   string   symbol;
   double   impulse_mult;
   double   stop_mult;
   double   rr;
   double   risk_pct;
   long     magic;
   datetime last_bar;      // open time of the bar already evaluated
   long     last_day;      // UTC day key of the entry already taken
   CTrade   trade;
  };

BookState g_books[2];
double    g_day_start_equity = 0.0;
long      g_day_key          = -1;

//+------------------------------------------------------------------+
//| Pip size: 0.0001 for 5/3-digit FX quotes, else one point.        |
//| (The portfolio books are FX-only; XAUUSD never passed validation |
//| and is intentionally not part of this EA.)                       |
//+------------------------------------------------------------------+
double PipSize(const string sym)
  {
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(sym, SYMBOL_POINT);
   return (digits == 3 || digits == 5) ? 10.0 * point : point;
  }

//+------------------------------------------------------------------+
//| Highest high over `count` bars ending at shift `start`.          |
//+------------------------------------------------------------------+
double HighestHigh(const string sym, int start, int count)
  {
   double v = iHigh(sym, PERIOD_H1, start);
   for(int i = start + 1; i < start + count; i++)
      v = MathMax(v, iHigh(sym, PERIOD_H1, i));
   return v;
  }

//+------------------------------------------------------------------+
//| Lowest low over `count` bars ending at shift `start`.            |
//+------------------------------------------------------------------+
double LowestLow(const string sym, int start, int count)
  {
   double v = iLow(sym, PERIOD_H1, start);
   for(int i = start + 1; i < start + count; i++)
      v = MathMin(v, iLow(sym, PERIOD_H1, i));
   return v;
  }

//+------------------------------------------------------------------+
//| Simple 14-bar ATR of the bar at `shift` (features.py convention: |
//| unweighted mean of true range, not Wilder smoothing).            |
//+------------------------------------------------------------------+
double SimpleATR(const string sym, int shift)
  {
   double sum = 0.0;
   for(int i = shift; i < shift + 14; i++)
     {
      double h = iHigh(sym, PERIOD_H1, i);
      double l = iLow(sym, PERIOD_H1, i);
      double pc = iClose(sym, PERIOD_H1, i + 1);
      sum += MathMax(h - l, MathMax(MathAbs(h - pc), MathAbs(l - pc)));
     }
   return sum / 14.0;
  }

//+------------------------------------------------------------------+
//| True tick volume of the bar at `shift`.                          |
//+------------------------------------------------------------------+
long TickVol(const string sym, int shift)
  {
   return iVolume(sym, PERIOD_H1, shift);
  }

//+------------------------------------------------------------------+
//| Volume-lot-per-pip for `sym`: account-currency P&L of one pip    |
//| move on a 1.00 lot position.                                     |
//+------------------------------------------------------------------+
double PipValuePerLot(const string sym)
  {
   double tick_value = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tick_size  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   if(tick_value <= 0.0 || tick_size <= 0.0)
      return 0.0;
   return tick_value * (PipSize(sym) / tick_size);
  }

//+------------------------------------------------------------------+
//| Round volume to step and clamp to broker limits; returns 0 when  |
//| even the minimum lot overshoots the risk budget by more than     |
//| InpMaxRiskOvershoot (engine MIN-STOP design rule).               |
//+------------------------------------------------------------------+
double NormalizeVolume(const string sym, double wanted, double budget)
  {
   double vmin  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double vmax  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double vstep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   if(vstep <= 0.0)
      vstep = vmin > 0.0 ? vmin : 0.01;

   double v = MathFloor(wanted / vstep) * vstep;
   if(v < vmin)
     {
      double min_risk = vmin / MathMax(wanted, 1e-10) * budget;
      if(min_risk > budget * InpMaxRiskOvershoot)
         return 0.0;                       // honest skip, not a blown budget
      v = vmin;
     }
   if(v > vmax)
      v = vmax;
   return v;
  }

//+------------------------------------------------------------------+
//| Open positions carrying this book's magic.                       |
//+------------------------------------------------------------------+
int BookPositions(const long magic)
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) == magic)
         n++;
     }
   return n;
  }

//+------------------------------------------------------------------+
//| Realized P&L (profit+swap+commission) of this book's deals       |
//| since `from` (server time).                                      |
//+------------------------------------------------------------------+
double BookRealizedPnL(const long magic, const datetime from)
  {
   if(!HistorySelect(from, TimeCurrent() + 60))
      return 0.0;
   double pnl = 0.0;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong dt = HistoryDealGetTicket(i);
      if(dt == 0)
         continue;
      if(HistoryDealGetInteger(dt, DEAL_MAGIC) != magic)
         continue;
      pnl += HistoryDealGetDouble(dt, DEAL_PROFIT)
           + HistoryDealGetDouble(dt, DEAL_SWAP)
           + HistoryDealGetDouble(dt, DEAL_COMMISSION);
     }
   return pnl;
  }

//+------------------------------------------------------------------+
//| Select the broker-supported filling mode for `sym`.              |
//+------------------------------------------------------------------+
void SetFillingFor(BookState &b)
  {
   long fm = SymbolInfoInteger(b.symbol, SYMBOL_FILLING_MODE);
   if((fm & SYMBOL_FILLING_FOK) != 0)
      b.trade.SetTypeFilling(ORDER_FILLING_FOK);
   else if((fm & SYMBOL_FILLING_IOC) != 0)
      b.trade.SetTypeFilling(ORDER_FILLING_IOC);
   else
      b.trade.SetTypeFilling(ORDER_FILLING_RETURN);
  }

//+------------------------------------------------------------------+
//| Evaluate one book on the just-closed H1 bar.                     |
//+------------------------------------------------------------------+
void EvaluateBook(BookState &b, const long utc, const int utc_hour,
                  const long day_key, const double day_start_equity,
                  const bool friday)
  {
   const ENUM_TIMEFRAMES tf = PERIOD_H1;

   //--- one decision per closed bar
   datetime bar_time = iTime(b.symbol, tf, 0);
   if(bar_time == 0 || bar_time == b.last_bar)
      return;
   b.last_bar = bar_time;

   if(Bars(b.symbol, tf) < 70)
      return;

   //--- session gate on the SIGNAL bar's open time (engine trade-time stamp)
   datetime sig_open = iTime(b.symbol, tf, 1);
   MqlDateTime sig_dt;
   TimeToStruct(sig_open + (datetime)(InpUTCOffsetHours * -3600), sig_dt);
   int sig_hour = sig_dt.hour;
   if(sig_hour < InpSessionStartUTC || sig_hour >= InpSessionEndUTC)
      return;

   //--- one entry per book per UTC day
   if(b.last_day == day_key)
      return;
   if(BookPositions(b.magic) > 0)
      return;

   //--- daily loss lockout
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(day_start_equity > 0.0)
     {
      double day_pnl = BookRealizedPnL(b.magic,
                          (datetime)(day_key * 86400 + InpUTCOffsetHours * 3600));
      if(day_pnl <= -InpDailyLossPct / 100.0 * day_start_equity)
        {
         PrintFormat("[%s] daily lockout engaged (day PnL %.2f)", b.symbol, day_pnl);
         return;
        }
     }

   //--- features on the closed signal bar (shift 1); channel excludes it
   double atr = SimpleATR(b.symbol, 1);
   if(atr <= 0.0)
      return;

   double hh_prev = HighestHigh(b.symbol, 2, 60);
   double ll_prev = LowestLow(b.symbol, 2, 60);
   long   v_bar   = TickVol(b.symbol, 1);
   long   v_sum   = 0;
   for(int i = 1; i <= 20; i++)
      v_sum += TickVol(b.symbol, i);
   double v_avg = (double)v_sum / 20.0;

   bool up_impulse   = (iClose(b.symbol, tf, 1) > hh_prev + b.impulse_mult * atr)
                    && (v_avg > 0.0 && (double)v_bar >= 1.2 * v_avg);
   bool down_impulse = (iClose(b.symbol, tf, 1) < ll_prev - b.impulse_mult * atr)
                    && (v_avg > 0.0 && (double)v_bar >= 1.2 * v_avg);

   bool want_long  = (up_impulse   && InpDir != DIR_SHORT_ONLY);
   bool want_short = (down_impulse && InpDir != DIR_LONG_ONLY);
   if(!want_long && !want_short)
      return;

   //--- live-spread guard
   MqlTick tick;
   if(!SymbolInfoTick(b.symbol, tick))
      return;
   double spread_pips = (tick.ask - tick.bid) / PipSize(b.symbol);
   if(spread_pips > InpMaxSpreadPips)
     {
      PrintFormat("[%s] entry skipped: spread %.1f pips > %.1f guard",
                  b.symbol, spread_pips, InpMaxSpreadPips);
      return;
     }

   //--- stop / target: engine cost-veto floor, broker stops-level floor
   double point   = SymbolInfoDouble(b.symbol, SYMBOL_POINT);
   double pip     = PipSize(b.symbol);
   double stop    = MathMax(b.stop_mult * atr, InpMinStopPips * pip);
   long   stoplevel = SymbolInfoInteger(b.symbol, SYMBOL_TRADE_STOPS_LEVEL);
   stop = MathMax(stop, (double)(stoplevel + 1) * point);
   double tp_dist = b.rr * stop;

   //--- volume from the book's exact risk percentage
   double budget = equity * b.risk_pct / 100.0;
   double pv     = PipValuePerLot(b.symbol);
   if(pv <= 0.0)
      return;
   double stop_pips = stop / pip;
   double wanted    = budget / (stop_pips * pv);
   double vol       = NormalizeVolume(b.symbol, wanted, budget);
   if(vol <= 0.0)
     {
      PrintFormat("[%s] entry skipped: minimum lot exceeds risk budget", b.symbol);
      return;
     }

   //--- margin guard
   double price    = want_long ? tick.ask : tick.bid;
   double margin   = 0.0;
   ENUM_ORDER_TYPE ot = want_long ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(OrderCalcMargin(ot, b.symbol, vol, price, margin))
      if(margin > AccountInfoDouble(ACCOUNT_MARGIN_FREE) * InpMaxMarginUsePct / 100.0)
        {
         PrintFormat("[%s] entry skipped: margin %.2f exceeds guard", b.symbol, margin);
         return;
        }

   //--- broker-side SL/TP (engine exit semantics: stop-priority ambiguity is
   //    the broker's, exactly as in live trading)
   double sl = want_long ? price - stop : price + stop;
   double tp = want_long ? price + tp_dist : price - tp_dist;
   int    digits = (int)SymbolInfoInteger(b.symbol, SYMBOL_DIGITS);
   sl = NormalizeDouble(sl, digits);
   tp = NormalizeDouble(tp, digits);

   SetFillingFor(b);
   b.trade.SetExpertMagicNumber(b.magic);
   b.trade.SetDeviationInPoints(20);

   bool ok = want_long
             ? b.trade.Buy(vol, b.symbol, 0.0, sl, tp, "FWT london_momentum")
             : b.trade.Sell(vol, b.symbol, 0.0, sl, tp, "FWT london_momentum");

   if(ok && b.trade.ResultRetcode() == TRADE_RETCODE_DONE)
     {
      b.last_day = day_key;
      PrintFormat("[%s] %s %.2f lots @ %.5f  SL %.5f  TP %.5f  (stop %.1f pips, risk %.2f)",
                  b.symbol, want_long ? "BUY" : "SELL", vol, price, sl, tp,
                  stop_pips, budget);
     }
   else
      PrintFormat("[%s] order failed: retcode %d (%s)", b.symbol,
                  b.trade.ResultRetcode(), b.trade.ResultRetcodeDescription());
  }

//+------------------------------------------------------------------+
//| Close every position of one book (Friday flat).                  |
//+------------------------------------------------------------------+
void CloseBook(BookState &b)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != b.magic)
         continue;
      b.trade.PositionClose(ticket);
      PrintFormat("[%s] Friday-flat close #%I64u", b.symbol, ticket);
     }
  }

//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_books[0].symbol       = InpEURSymbol;
   g_books[0].impulse_mult = InpEURImpulseMult;
   g_books[0].stop_mult    = InpEURStopMult;
   g_books[0].rr           = InpEURRR;
   g_books[0].risk_pct     = InpEURRiskPct;
   g_books[0].magic        = InpMagicBase;
   g_books[0].last_bar     = 0;
   g_books[0].last_day     = -1;

   g_books[1].symbol       = InpGBPSymbol;
   g_books[1].impulse_mult = InpGBPImpulseMult;
   g_books[1].stop_mult    = InpGBPStopMult;
   g_books[1].rr           = InpGBPRR;
   g_books[1].risk_pct     = InpGBPRiskPct;
   g_books[1].magic        = InpMagicBase + 1;
   g_books[1].last_bar     = 0;
   g_books[1].last_day     = -1;

   for(int i = 0; i < 2; i++)
     {
      if(!SymbolSelect(g_books[i].symbol, true))
        {
         PrintFormat("SymbolSelect failed for %s", g_books[i].symbol);
         return INIT_FAILED;
        }
      g_books[i].trade.SetExpertMagicNumber(g_books[i].magic);
     }

   g_day_key          = -1;
   g_day_start_equity = 0.0;
   EventSetTimer(30);
   PrintFormat("LondonMomentumPortfolio init: EUR %s (k=%.2f, RR=%.1f, risk %.2f%%) | "
               "GBP %s (k=%.2f, RR=%.1f, risk %.2f%%)",
               g_books[0].symbol, g_books[0].impulse_mult, g_books[0].rr, g_books[0].risk_pct,
               g_books[1].symbol, g_books[1].impulse_mult, g_books[1].rr, g_books[1].risk_pct);
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   PrintFormat("LondonMomentumPortfolio stopped (reason %d)", reason);
  }

//+------------------------------------------------------------------+
//| Timer: also drives decisions when the chart symbol is quiet.     |
//+------------------------------------------------------------------+
void OnTimer()
  {
   DriveBooks();
  }

//+------------------------------------------------------------------+
//| Tick: drive decisions.                                           |
//+------------------------------------------------------------------+
void OnTick()
  {
   DriveBooks();
  }

//+------------------------------------------------------------------+
//| Shared decision path for OnTick / OnTimer.                       |
//+------------------------------------------------------------------+
void DriveBooks()
  {
   long utc = (long)TimeTradeServer() - InpUTCOffsetHours * 3600;
   long day_key = utc / 86400;
   int  utc_hour = (int)((utc % 86400) / 3600);

   MqlDateTime dt;
   TimeToStruct((datetime)utc, dt);
   bool friday = (dt.day_of_week == 5);

   //--- day rollover: capture day-start equity for the lockout basis
   if(day_key != g_day_key)
     {
      g_day_key          = day_key;
      g_day_start_equity = AccountInfoDouble(ACCOUNT_EQUITY);
     }

   for(int i = 0; i < 2; i++)
     {
      if(friday && utc_hour >= InpFridayFlatUTC)
        {
         CloseBook(g_books[i]);
         continue;
        }
      EvaluateBook(g_books[i], utc, utc_hour, day_key, g_day_start_equity, friday);
     }
  }
//+------------------------------------------------------------------+
