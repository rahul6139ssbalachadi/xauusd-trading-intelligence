//+------------------------------------------------------------------+
//|                              Simple_Strategy_Fixed_M5.mq5          |
//|  Simple Strategy M5 - CORRECTED                                  |
//|                                                                   |
//|  Fixes vs the original:                                           |
//|   1. It COMPILED-LESS: `InpSlippage = as per the platform` and      |
//|      `InpTP_RR = as per the market movement` are not valid MQL5      |
//|      expressions -> "as" is an undefined identifier. Fixed.        |
//|   2. `InpEnableTrailing` / `InpTrailStart` were USED in             |
//|      ManagePositions() but NEVER DECLARED -> compile error.        |
//|      Now declared as inputs.                                       |
//|   3. `lastBarTime` was a global with no init -> first tick always   |
//|      fired. Now initialised in OnInit.                            |
//|   4. LOOK-AHEAD BUG (the real money bug): arrays are set as        |
//|      SERIES, so index i-1 is the FUTURE bar. The original read     |
//|      close[i-1]/open[i-1] (future) as the "previous candle" in      |
//|      every reversal pattern, and its swing search scanned indices    |
//|      i+1..i+4 (the future). Now every read is strictly historical. |
//|   5. SIGNAL FLOOD: a pattern up to InpLookback_Period bars old      |
//|      still counted as "near price", so the EA re-entered on almost   |
//|      every bar. Now one position at a time and a signal stays valid |
//|      for only InpSignalValidBars bars after it forms.               |
//|   6. BROKEN breakout blocks (ArrayMaximum over a SERIES array       |
//|      included FUTURE highs) -> rewritten on the closed-bar window.  |
//|   7. MONEY MANAGEMENT (what you asked for):                        |
//|      - risk a fixed 2% of the CURRENT BALANCE per trade             |
//|      - lot size DERIVED from the stop distance, never fixed         |
//|      - stop = ATR x multiplier (the only real risk definition)     |
//|      - TP1/TP2 DERIVED from the market (ATR + nearest swing        |
//|        structure), so R:R is an OUTCOME, not an input               |
//|      - 50% at TP1 then stop moved to break-even                    |
//|                                                                   |
//|  NOTE: 2% per trade is aggressive. A 10-loss streak = -20%.         |
//|  Run the Strategy Tester first. See research/uncle_ea_m5_week.py.  |
//+------------------------------------------------------------------+
#property copyright "Corrected build"
#property version   "2.00"
#property strict

//+------------------------------------------------------------------+
//| INPUTS                                                            |
//+------------------------------------------------------------------+
input group "===== BASIC TRADE SETTINGS ====="
input double   InpLotSizeMax        = 1.0;      // Max lot size (hard cap)
input int      InpMagicNumber       = 2025003;  // Magic Number
input int      InpSlippage          = 30;       // Max deviation (points)

input group "===== RISK MANAGEMENT ====="
input double   InpRiskPerTradePct   = 2.0;      // Risk per trade (% of BALANCE)
input double   InpMaxRiskPerTradePct= 2.0;      // Hard cap (% of balance)
input int      InpMaxPositions      = 1;        // Max simultaneous positions

input group "===== STRATEGY SETTINGS ====="
input int      InpLookback_Period   = 60;       // Pattern lookback (bars)
input int      InpATR_Period         = 14;       // ATR period
input double   InpSL_ATR_Multiplier = 1.5;      // SL = N x ATR
input double   InpMinSL_Points      = 30;       // Min stop distance (points)
input bool     InpUseReversal       = true;     // Use Reversal Patterns
input bool     InpUseSupportResist  = true;     // Use Support/Resistance
input bool     InpUseBreakout       = true;     // Use Breakout Retest
input int      InpSignalValidBars   = 2;        // Bars a signal stays valid
input int      InpMaxSignalsPerDay  = 3;        // Anti-overtrading

input group "===== EXIT (DERIVED FROM MARKET) ====="
input bool     InpUsePartialTP      = true;     // Close partial at TP1
input double   InpPartialPercent    = 50.0;     // % of position at TP1
input double   InpTP1_ATR_Mult      = 1.5;      // TP1 = N x ATR (min)
input double   InpTP2_ATR_Mult      = 4.0;      // TP2 = N x ATR (min)
input bool     InpUseSwingTarget    = true;     // Extend TP2 to swing if nearer
input int      InpSwingLookback     = 20;       // Swing target lookback
input bool     InpMoveSLToBreakeven = true;     // BE after TP1

input group "===== TRAILING STOP ====="
input bool     InpEnableTrailing    = true;     // Enable trailing stop
input double   InpTrailStart_R      = 1.0;     // Start trailing at N x R
input double   InpTrailATR_Mult     = 1.0;      // Trail distance = N x ATR

input group "===== RISK GUARDS ====="
input double   InpMaxSpread         = 35.0;    // Max spread (points)
input int      InpMaxDailyLossTrades= 3;        // Stop after N losses/day

input group "===== TRADING HOURS ====="
input bool     InpUseHourFilter     = false;   // Use Time Filter
input int      InpStartHour         = 1;       // Start hour (server)
input int      InpEndHour           = 22;      // End hour (server)

//+------------------------------------------------------------------+
//| GLOBALS                                                           |
//+------------------------------------------------------------------+
string   g_symbol;
double   g_point;
double   g_tickSize;
int      g_digits;
double   g_pip;                 // 10 points for XAUUSD-style quotes
int      atrHandle;
datetime lastBarTime;
datetime lastEntryDay;
int      tradesToday;
int      lossesToday;

#define MAGIC  InpMagicNumber

// Trade tracker - one entry per live position
struct TradeTracker
{
   ulong  ticket;
   int    dir;                 // +1 buy, -1 sell
   double openPrice;
   double sl;
   double tp1;
   double tp2;
   double lots;
   double atrAtEntry;
   double rDistance;           // price distance of 1R
   bool   partialDone;
   bool   slMoved;
};

TradeTracker g_tracks[];

//+------------------------------------------------------------------+
//| OnInit                                                            |
//+------------------------------------------------------------------+
int OnInit()
{
   g_symbol = _Symbol;
   g_point  = SymbolInfoDouble(g_symbol, SYMBOL_POINT);
   g_tickSize = SymbolInfoDouble(g_symbol, SYMBOL_TRADE_TICK_SIZE);
   g_digits = (int)SymbolInfoInteger(g_symbol, SYMBOL_DIGITS);
   g_pip    = 10.0 * g_point;

   // FIX: these were uninitialised, so the first tick always looked like a
   // new bar and the EA could trade bar 0 of the chart.
   lastBarTime   = 0;
   lastEntryDay  = 0;
   tradesToday   = 0;
   lossesToday   = 0;

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
   {
      Print("[SimpleM5] FAIL: automated trading not allowed in MT5 settings.");
      return(INIT_FAILED);
   }
   if(g_point <= 0.0)
   {
      Print("[SimpleM5] FAIL: bad symbol point size.");
      return(INIT_FAILED);
   }

   atrHandle = iATR(g_symbol, PERIOD_M5, InpATR_Period);
   if(atrHandle == INVALID_HANDLE)
   {
      Print("[SimpleM5] FAIL: iATR handle. Error ", GetLastError());
      return(INIT_FAILED);
   }

   ArrayResize(g_tracks, 0);
   PrintFormat("[SimpleM5] INIT  symbol=%s  risk=%.2f%% of balance  SL=%.2fxATR(%d)  "
               "TP1>=%.2fxATR  TP2>=%.2fxATR  maxLots=%.2f",
               g_symbol, InpRiskPerTradePct, InpSL_ATR_Multiplier, InpATR_Period,
               InpTP1_ATR_Mult, InpTP2_ATR_Mult, InpLotSizeMax);
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| OnDeinit                                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(atrHandle != INVALID_HANDLE)
      IndicatorRelease(atrHandle);
}

//+------------------------------------------------------------------+
//| helpers                                                           |
//+------------------------------------------------------------------+
bool IsTradingAllowed()
{
   if(!InpUseHourFilter) return(true);
   MqlDateTime tm;
   TimeToStruct(TimeCurrent(), tm);
   return(tm.hour >= InpStartHour && tm.hour < InpEndHour);
}

double CurrentSpreadPoints()
{
   double ask = SymbolInfoDouble(g_symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(g_symbol, SYMBOL_BID);
   if(ask <= 0.0 || bid <= 0.0) return(999999.0);
   return((ask - bid) / g_point);
}

bool IsSpreadOk()
{
   double s = CurrentSpreadPoints();
   if(s <= InpMaxSpread) return(true);
   return(false);   // no spam print - it prints on every tick
}

double GetMinStopDistance()
{
   long stops = SymbolInfoInteger(g_symbol, SYMBOL_TRADE_STOPS_LEVEL);
   if(stops <= 0) stops = 10;
   double d = stops * g_point;
   if(d < InpMinSL_Points * g_point) d = InpMinSL_Points * g_point;
   return(d);
}

int CountOpenPositions()
{
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) == MAGIC &&
         PositionGetString(POSITION_SYMBOL) == g_symbol)
         n++;
   }
   return(n);
}

// Daily counters
void RefreshDayCounters()
{
   MqlDateTime tm;
   TimeToStruct(TimeCurrent(), tm);
   datetime day = StringToTime(IntegerToString(tm.year) + "." +
                              IntegerToString(tm.mon) + "." +
                              IntegerToString(tm.day));
   if(day != lastEntryDay)
   {
      lastEntryDay = day;
      tradesToday = 0;
      lossesToday = 0;
   }
}

// Count today's closed losses from history (survives restarts)
void SyncLossesToday()
{
   lossesToday = 0;
   MqlDateTime tm;
   TimeToStruct(TimeCurrent(), tm);
   datetime d0 = StringToTime(IntegerToString(tm.year) + "." +
                              IntegerToString(tm.mon) + "." + IntegerToString(tm.day));
   datetime d1 = d0 + 86400;
   HistorySelect(d0, d1);
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0) continue;
      if(HistoryDealGetInteger(deal, DEAL_MAGIC) != MAGIC) continue;
      if(HistoryDealGetInteger(deal, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;
      if(HistoryDealGetDouble(deal, DEAL_PROFIT) < 0.0) lossesToday++;
   }
}

//+------------------------------------------------------------------+
//| LOT SIZING - 2% of the CURRENT BALANCE, derived from the stop      |
//+------------------------------------------------------------------+
double CalcLotsForRisk(double stopDistPrice, double riskPct)
{
   if(stopDistPrice <= 0.0 || riskPct <= 0.0) return(0.0);

   double balance  = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskUsd  = balance * riskPct / 100.0;

   double tickSize = SymbolInfoDouble(g_symbol, SYMBOL_TRADE_TICK_SIZE);
   double tickVal  = SymbolInfoDouble(g_symbol, SYMBOL_TRADE_TICK_VALUE);
   double step     = SymbolInfoDouble(g_symbol, SYMBOL_VOLUME_STEP);
   double minLot   = SymbolInfoDouble(g_symbol, SYMBOL_VOLUME_MIN);
   double maxLot   = SymbolInfoDouble(g_symbol, SYMBOL_VOLUME_MAX);
   if(tickSize <= 0.0 || tickVal <= 0.0 || step <= 0.0) return(0.0);

   // $ per 1.0 lot for a move of `stopDistPrice`
   double lossPerLot = (stopDistPrice / tickSize) * tickVal;
   if(lossPerLot <= 0.0) return(0.0);

   double lots = riskUsd / lossPerLot;
   lots = MathFloor(lots / step) * step;          // round DOWN - never over-risk
   if(lots < minLot)
   {
      // Can't honour the risk % at min lot -> refuse rather than over-size.
      // This is the single most important guard in the whole EA.
      double riskAtMin = lossPerLot * minLot;
      double pctAtMin  = (riskAtMin / balance) * 100.0;
      if(pctAtMin > InpMaxRiskPerTradePct + 0.0001)
      {
         PrintFormat("[SimpleM5] SKIP: min lot %.2f risks %.3f%% (cap %.2f%%) - stop too wide",
                     minLot, pctAtMin, InpMaxRiskPerTradePct);
         return(0.0);
      }
      lots = minLot;
   }
   double hardCap = MathMin(maxLot, InpLotSizeMax);
   if(lots > hardCap) lots = hardCap;

   return(NormalizeDouble(lots, 2));
}

//+------------------------------------------------------------------+
//| SIGNAL DETECTION - all reads are strictly HISTORICAL               |
//| We copy bars into NON-series arrays: index 0 = oldest ... N-1 =    |
//| newest CLOSED bar. So "current" is N-1 and history is < N-1.       |
//+------------------------------------------------------------------+
#define MAXBARS 400

bool LoadBars(double &o[], double &h[], double &l[], double &c[], int count)
{
   int need = count;
   if(CopyOpen(g_symbol, PERIOD_M5, 1, need, o) < need) return(false);
   if(CopyHigh(g_symbol, PERIOD_M5, 1, need, h) < need) return(false);
   if(CopyLow(g_symbol, PERIOD_M5, 1, need, l) < need) return(false);
   if(CopyClose(g_symbol, PERIOD_M5, 1, need, c) < need) return(false);
   return(true);
}

// nearest swing low strictly older than bar `cur` (in array index space)
double NearestSwingLow(const double &l[], int cur, int lookback)
{
   for(int j = cur - 2; j > cur - 2 - lookback && j >= 2; j--)
   {
      if(l[j] < l[j-1] && l[j] < l[j-2] && l[j] < l[j+1] && l[j] < l[j+2])
         return(l[j]);
   }
   return(0.0);
}

double NearestSwingHigh(const double &h[], int cur, int lookback)
{
   for(int j = cur - 2; j > cur - 2 - lookback && j >= 2; j--)
   {
      if(h[j] > h[j-1] && h[j] > h[j-2] && h[j] > h[j+1] && h[j] > h[j+2])
         return(h[j]);
   }
   return(0.0);
}

bool BullishEngulfing(const double &o[], const double &c[], int i)
{
   int p = i - 1;                                 // the PREVIOUS bar
   if(!(c[i] > o[i] && c[p] < o[p])) return(false);
   if(!(o[i] <= c[p] && c[i] >= o[p])) return(false);
   return(true);
}

bool BearishEngulfing(const double &o[], const double &c[], int i)
{
   int p = i - 1;
   if(!(c[i] < o[i] && c[p] > o[p])) return(false);
   if(!(o[i] >= c[p] && c[i] <= o[p])) return(false);
   return(true);
}

bool Hammer(const double &o[], const double &h[], const double &l[], const double &c[], int i)
{
   if(i < 2) return(false);
   if(!(c[i] > o[i])) return(false);
   double body = c[i] - o[i];
   if(body <= 0.0) return(false);
   double lower = o[i] - l[i];
   double upper = h[i] - c[i];
   if(!(c[i-1] < o[i-1])) return(false);          // previous candle bearish
   return(lower > body * 2.0 && upper < body * 0.5);
}

bool ShootingStar(const double &o[], const double &h[], const double &l[], const double &c[], int i)
{
   if(i < 2) return(false);
   if(!(c[i] < o[i])) return(false);
   double body = o[i] - c[i];
   if(body <= 0.0) return(false);
   double upper = h[i] - o[i];
   double lower = c[i] - l[i];
   if(!(c[i-1] > o[i-1])) return(false);          // previous candle bullish
   return(upper > body * 2.0 && lower < body * 0.5);
}

// Returns: 1 = buy, -1 = sell, 0 = none. `why` gets filled for the log.
int DetectSignal(string &why)
{
   double o[], h[], l[], c[];
   int n = InpLookback_Period + 60;
   if(n > MAXBARS) n = MAXBARS;
   if(!LoadBars(o, h, l, c, n)) return(0);

   int cur = n - 1;                               // newest CLOSED bar
   double price = c[cur];

   // ---- reversal: only the last InpSignalValidBars bars may still be valid
   if(InpUseReversal)
   {
      for(int j = cur; j > cur - InpSignalValidBars - 1 && j >= 3; j--)
      {
         if(BullishEngulfing(o, c, j) || Hammer(o, h, l, c, j))
         {
            why = StringFormat("bullish %s bar %d ago", Hammer(o, h, l, c, j) ? "hammer" : "engulfing", cur - j);
            return(1);
         }
         if(BearishEngulfing(o, c, j) || ShootingStar(o, h, l, c, j))
         {
            why = StringFormat("bearish %s bar %d ago", ShootingStar(o, h, l, c, j) ? "shooting star" : "engulfing", cur - j);
            return(-1);
         }
      }
   }

   // ---- support bounce: price sitting on a prior swing low
   if(InpUseSupportResist)
   {
      double sl1 = NearestSwingLow(l, cur, 30);
      double sl2 = (sl1 > 0.0) ? NearestSwingLow(l, cur - 5, 30) : 0.0;
      if(sl1 > 0.0 && price >= sl1 - 3.0 * g_pip && price <= sl1 + 8.0 * g_pip)
      {
         why = StringFormat("support bounce at %.2f", sl1);
         return(1);
      }
      if(sl1 > 0.0 && sl2 > 0.0 && MathAbs(sl1 - sl2) <= 20.0 * g_point)
      {
         double lvl = (sl1 + sl2) / 2.0;
         if(price >= lvl - 3.0 * g_pip && price <= lvl + 8.0 * g_pip)
         {
            why = StringFormat("double-bottom bounce at %.2f", lvl);
            return(1);
         }
      }

      double sh1 = NearestSwingHigh(h, cur, 30);
      if(sh1 > 0.0 && price <= sh1 + 3.0 * g_pip && price >= sh1 - 8.0 * g_pip)
      {
         why = StringFormat("resistance reject at %.2f", sh1);
         return(-1);
      }
   }

   // ---- breakout retest: closed above the recent range, now back at its edge
   if(InpUseBreakout)
   {
      int w = 20;
      if(cur - w - 2 > 2)
      {
         double res = h[cur - w], sup = l[cur - w];
         for(int k = cur - w + 1; k < cur; k++) { if(h[k] > res) res = h[k]; if(l[k] < sup) sup = l[k]; }
         if(c[cur] > res + 2.0 * g_point && price <= res + 10.0 * g_point && price >= res - 2.0 * g_pip)
         {
            why = StringFormat("bullish breakout retest of %.2f", res);
            return(1);
         }
         if(c[cur] < sup - 2.0 * g_point && price >= sup - 10.0 * g_point && price <= sup + 2.0 * g_pip)
         {
            why = StringFormat("bearish breakdown retest of %.2f", sup);
            return(-1);
         }
      }
   }

   return(0);
}

//+------------------------------------------------------------------+
//| OPEN TRADE                                                        |
//| SL comes from ATR. TP1/TP2 are DERIVED from ATR and structure, so |
//| the resulting R:R is a measured outcome of the market, not a      |
//| hard-coded guess.                                                 |
//+------------------------------------------------------------------+
bool OpenTrade(int dir, double atr, string why)
{
   bool isBuy = (dir > 0);

   double ask = SymbolInfoDouble(g_symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(g_symbol, SYMBOL_BID);
   double entry = isBuy ? ask : bid;

   double slDist = MathMax(atr * InpSL_ATR_Multiplier, GetMinStopDistance());
   double sl = isBuy ? entry - slDist : entry + slDist;

   // ---- targets derived from the market ----
   double tp1Dist = atr * InpTP1_ATR_Mult;
   double tp2Dist = atr * InpTP2_ATR_Mult;

   double o[], h[], l[], c[];
   int n = InpSwingLookback + 40;
   if(n > MAXBARS) n = MAXBARS;
   if(InpUseSwingTarget && LoadBars(o, h, l, c, n))
   {
      int cur = n - 1;
      if(isBuy)
      {
         double sw = NearestSwingHigh(h, cur, InpSwingLookback);
         if(sw > entry + 5.0 * g_point && sw - entry < tp2Dist)
            tp2Dist = sw - entry;                 // take the structure, not the guess
      }
      else
      {
         double sw = NearestSwingLow(l, cur, InpSwingLookback);
         if(sw > 0.0 && sw < entry - 5.0 * g_point && entry - sw < tp2Dist)
            tp2Dist = entry - sw;
      }
   }

   if(tp1Dist >= tp2Dist) tp1Dist = tp2Dist * 0.5;   // keep TP1 in front of TP2

   double tp1 = isBuy ? entry + tp1Dist : entry - tp1Dist;
   double tp2 = isBuy ? entry + tp2Dist : entry - tp2Dist;

   // ---- normalise to the symbol's tick grid ----
   sl  = NormalizeDouble(sl,  g_digits);
   tp1 = NormalizeDouble(tp1, g_digits);
   tp2 = NormalizeDouble(tp2, g_digits);
   if(g_tickSize > 0.0)
   {
      sl  = NormalizeDouble(MathRound(sl  / g_tickSize) * g_tickSize, g_digits);
      tp1 = NormalizeDouble(MathRound(tp1 / g_tickSize) * g_tickSize, g_digits);
      tp2 = NormalizeDouble(MathRound(tp2 / g_tickSize) * g_tickSize, g_digits);
   }

   double riskPct = MathMin(InpRiskPerTradePct, InpMaxRiskPerTradePct);
   double lots = CalcLotsForRisk(slDist, riskPct);
   if(lots <= 0.0) return(false);              // risk % could not be honoured

   double marginReq = 0.0;
   if(OrderCalcMargin(ORDER_TYPE_BUY, g_symbol, lots, entry, marginReq) &&
      marginReq > AccountInfoDouble(ACCOUNT_MARGIN_FREE))
   {
      PrintFormat("[SimpleM5] SKIP: margin %.2f needed, %.2f free", marginReq,
                  AccountInfoDouble(ACCOUNT_MARGIN_FREE));
      return(false);
   }

   double rr = tp2Dist / slDist;
   PrintFormat("[SimpleM5] OPEN %s  entry=%.2f SL=%.2f (%.0f pips) TP1=%.2f TP2=%.2f "
               "RR=1:%.2f lots=%.2f risk=%.2f%% of %.2f  [%s]",
               isBuy ? "BUY" : "SELL", entry, sl, slDist / g_pip, tp1, tp2, rr, lots,
               riskPct, AccountInfoDouble(ACCOUNT_BALANCE), why);

   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   req.action      = TRADE_ACTION_DEAL;
   req.symbol      = g_symbol;
   req.volume      = lots;
   req.type        = isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.price       = entry;
   req.sl          = sl;
   req.tp          = tp2;
   req.deviation   = InpSlippage;
   req.magic       = InpMagicNumber;
   req.comment     = "SimpleM5";
   req.type_filling = ORDER_FILLING_IOC;
   req.type_time   = ORDER_TIME_GTC;

   if(!OrderSend(req, res))
   {
      PrintFormat("[SimpleM5] FAIL OrderSend err=%d", GetLastError());
      return(false);
   }
   if(res.retcode != TRADE_RETCODE_DONE)
   {
      PrintFormat("[SimpleM5] FAIL retcode=%d (%s)", res.retcode, res.comment);
      return(false);
   }

   int k = ArraySize(g_tracks);
   ArrayResize(g_tracks, k + 1);
   g_tracks[k].ticket      = res.order;
   g_tracks[k].dir         = dir;
   g_tracks[k].openPrice   = entry;
   g_tracks[k].sl          = sl;
   g_tracks[k].tp1         = tp1;
   g_tracks[k].tp2         = tp2;
   g_tracks[k].lots        = lots;
   g_tracks[k].atrAtEntry  = atr;
   g_tracks[k].rDistance   = slDist;
   g_tracks[k].partialDone = false;
   g_tracks[k].slMoved     = false;

   tradesToday++;
   return(true);
}

//+------------------------------------------------------------------+
//| CLOSE / MODIFY                                                    |
//+------------------------------------------------------------------+
bool ClosePartial(ulong ticket, double vol, string tag)
{
   if(!PositionSelectByTicket(ticket)) return(false);
   ENUM_POSITION_TYPE pt = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
   double px = PositionGetDouble(POSITION_PRICE_CURRENT);

   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   req.action       = TRADE_ACTION_DEAL;
   req.position     = ticket;
   req.symbol       = g_symbol;
   req.volume       = vol;
   req.type         = (pt == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   req.price        = px;
   req.deviation    = InpSlippage;
   req.magic        = InpMagicNumber;
   req.comment      = tag;
   req.type_filling = ORDER_FILLING_IOC;
   req.type_time    = ORDER_TIME_GTC;

   if(!OrderSend(req, res) || res.retcode != TRADE_RETCODE_DONE)
   {
      PrintFormat("[SimpleM5] partial close FAILED ticket=%llu retcode=%d", ticket, res.retcode);
      return(false);
   }
   return(true);
}

bool ModifySLTP(ulong ticket, double sl, double tp)
{
   if(!PositionSelectByTicket(ticket)) return(false);
   MqlTradeRequest req = {};
   MqlTradeResult  res = {};
   req.action   = TRADE_ACTION_SLTP;
   req.position = ticket;
   req.symbol   = g_symbol;
   req.sl       = sl;
   req.tp       = tp;
   req.magic    = InpMagicNumber;
   if(!OrderSend(req, res) || res.retcode != TRADE_RETCODE_DONE)
   {
      PrintFormat("[SimpleM5] SLTP modify FAILED retcode=%d", res.retcode);
      return(false);
   }
   return(true);
}

//+------------------------------------------------------------------+
//| MANAGE OPEN POSITIONS                                             |
//+------------------------------------------------------------------+
void ManagePositions()
{
   for(int i = ArraySize(g_tracks) - 1; i >= 0; i--)
   {
      if(!PositionSelectByTicket(g_tracks[i].ticket))
      {
         ArrayRemove(g_tracks, i, 1);            // position gone (SL or TP hit)
         continue;
      }

      bool   isBuy = (g_tracks[i].dir > 0);
      double px    = PositionGetDouble(POSITION_PRICE_CURRENT);
      double vol   = PositionGetDouble(POSITION_VOLUME);
      double rDist = g_tracks[i].rDistance;

      // ---- TP1: partial close ----
      if(InpUsePartialTP && !g_tracks[i].partialDone && InpPartialPercent > 0.0 && InpPartialPercent < 100.0)
      {
         bool hit = isBuy ? (px >= g_tracks[i].tp1) : (px <= g_tracks[i].tp1);
         if(hit)
         {
            double part = NormalizeDouble(vol * InpPartialPercent / 100.0, 2);
            double step = SymbolInfoDouble(g_symbol, SYMBOL_VOLUME_STEP);
            part = MathFloor(part / step) * step;
            double minLot = SymbolInfoDouble(g_symbol, SYMBOL_VOLUME_MIN);
            if(part >= minLot && part < vol)
            {
               if(ClosePartial(g_tracks[i].ticket, part, "TP1 partial"))
               {
                  g_tracks[i].partialDone = true;
                  PrintFormat("[SimpleM5] TP1 hit: closed %.2f of %.2f lots at %.2f",
                              part, vol, px);
                  if(InpMoveSLToBreakeven)
                  {
                     double be = isBuy ? g_tracks[i].openPrice + 2.0 * g_point
                                       : g_tracks[i].openPrice - 2.0 * g_point;
                     if(ModifySLTP(g_tracks[i].ticket, NormalizeDouble(be, g_digits),
                                   PositionGetDouble(POSITION_TP)))
                     {
                        g_tracks[i].slMoved = true;
                        PrintFormat("[SimpleM5] SL -> break-even %.2f", be);
                     }
                  }
               }
            }
            else
            {
               g_tracks[i].partialDone = true;    // too small to split; leave it
            }
         }
      }

      // ---- trailing stop, ATR-based, starts at InpTrailStart_R x R ----
      if(InpEnableTrailing && InpTrailATR_Mult > 0.0 && g_tracks[i].atrAtEntry > 0.0)
      {
         double profit = isBuy ? (px - g_tracks[i].openPrice) : (g_tracks[i].openPrice - px);
         if(profit >= rDist * InpTrailStart_R)
         {
            double trail = g_tracks[i].atrAtEntry * InpTrailATR_Mult;
            double curSL = PositionGetDouble(POSITION_SL);
            double newSL = isBuy ? px - trail : px + trail;
            // only ever tighten, and never inside noise
            if((isBuy && newSL > curSL + 3.0 * g_point) ||
               (!isBuy && (newSL < curSL - 3.0 * g_point || curSL == 0.0)))
            {
               if(ModifySLTP(g_tracks[i].ticket, NormalizeDouble(newSL, g_digits),
                             PositionGetDouble(POSITION_TP)))
               {
                  PrintFormat("[SimpleM5] trail: SL -> %.2f (profit %.0f pips)",
                              newSL, profit / g_pip);
               }
            }
         }
      }
   }
}

//+------------------------------------------------------------------+
//| OnTick                                                            |
//+------------------------------------------------------------------+
void OnTick()
{
   RefreshDayCounters();
   SyncLossesToday();

   // Risk guard: consecutive-loss circuit breaker
   if(lossesToday >= InpMaxDailyLossTrades)
   {
      return;
   }
   if(tradesToday >= InpMaxSignalsPerDay) return;
   if(!IsTradingAllowed()) return;
   if(!IsSpreadOk()) return;

   // 1. always manage what is open
   ManagePositions();

   // 2. only look for a new trade on a fresh closed bar, and only when flat
   if(CountOpenPositions() > 0) return;
   if(ArraySize(g_tracks) > 0) return;

   datetime barTime = iTime(g_symbol, PERIOD_M5, 0);
   if(barTime == 0 || barTime == lastBarTime) return;
   lastBarTime = barTime;

   double atrBuf[1];
   if(CopyBuffer(atrHandle, 0, 1, 1, atrBuf) < 1) return;
   double atr = atrBuf[0];
   if(atr <= 0.0) return;

   string why = "";
   int sig = DetectSignal(why);
   if(sig == 0) return;

   PrintFormat("[SimpleM5] signal %s [%s] spread=%.1f pts", sig > 0 ? "BUY" : "SELL",
               why, CurrentSpreadPoints());
   OpenTrade(sig, atr, why);
}
//+------------------------------------------------------------------+
