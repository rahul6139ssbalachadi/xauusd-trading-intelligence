//+------------------------------------------------------------------+
//|                                              MonthlyBT_Signals.mq5 |
//|  Visual backtest markers for the V11/V12 monthly backtester.      |
//|                                                                  |
//|  PURPOSE                                                          |
//|  Draws the backtest's BUY/SELL signals, entry/SL/TP lines and     |
//|  simulated exit markers ON a normal MetaTrader 5 chart, using the  |
//|  broker's own candles and symbol (GOLD.i# / BTCUSD#).             |
//|                                                                  |
//|  IT ONLY READS A FILE.                                            |
//|  No orders, no positions, no trade functions, no account calls.  |
//|  It cannot trade even if AutoTrading is enabled.                 |
//|                                                                  |
//|  TIMEFRAME HANDLING (important)                                   |
//|  V11 runs on D1, V12 runs on H1. A COMBINED csv holds both. This   |
//|  indicator draws ONLY rows whose `timeframe` column matches the   |
//|  chart timeframe, so a D1 chart shows V11 and an H1 chart shows   |
//|  V12. Set InpAllowAllTimeframes=true to ignore the filter (markers|
//|  will then land on the wrong bars for the other timeframe).       |
//|                                                                  |
//|  INPUT FILE                                                       |
//|  <terminal data>\MQL5\Files\signals\  e.g.                       |
//|    COMBINED_XAUUSD_2026.csv   or   V12_XAUUSD_2026.csv           |
//|  produced by: python -m monthly_bt --strategy both --chart        |
//|                                                                  |
//|  USAGE                                                           |
//|  1. File > Open Data Folder > MQL5\Indicators                     |
//|  2. copy this file there, compile (F7)                           |
//|  3. drag onto a chart of the matching symbol/timeframe           |
//|  4. leave InpCsv empty to auto-load the newest CSV                |
//+------------------------------------------------------------------+
#property copyright "Research use only"
#property version   "2.10"
#property indicator_chart_window
#property indicator_plots 0

//--- inputs
input string InpCsv                = "";   // empty = newest CSV
input bool   InpAllowAllTimeframes = false; // show every row
input color  InpSignalColor        = clrDodgerBlue;
input color  InpTradeColor         = clrLime;
input color  InpEntryColor         = clrTomato;
input color  InpStopColor          = clrOrange;
input color  InpTargetColor        = clrMediumPurple;
input int    InpMaxSignals         = 400;  // cap markers
input int    InpYOffsetPts         = 12;
input bool   InpShowLevels         = true; // entry/SL/TP lines
input bool   InpShowLabels         = true; // text labels

//--- one marker
struct Marker
  {
   datetime          time;
   string            strategy;
   string            timeframe;
   string            signal;
   double            entry, sl, tp;
   bool              simulated;
   datetime          exit_time;
   double            exit_price;
   string            exit_reason;
   double            net_pnl;
   double            r_multiple;
  };

Marker g_markers[];
int    g_count      = 0;
int    g_skipped_tf = 0;

//+------------------------------------------------------------------+
string FilesDir()
  {
   return TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\signals\\";
  }

//+------------------------------------------------------------------+
//| csv timeframe string -> chart period                            |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES TfFromString(const string s)
  {
   if(s == "M1")  return PERIOD_M1;
   if(s == "M5")  return PERIOD_M5;
   if(s == "M15") return PERIOD_M15;
   if(s == "M30") return PERIOD_M30;
   if(s == "H1")  return PERIOD_H1;
   if(s == "H4")  return PERIOD_H4;
   if(s == "D1")  return PERIOD_D1;
   return PERIOD_CURRENT;
  }

//+------------------------------------------------------------------+
bool ParseLine(const string line, Marker &m)
  {
   string f[];
   int n = StringSplit(line, ',', f);
   if(n < 7) return false;

   m.time       = (datetime)StringToInteger(f[0]);
   m.strategy   = f[1];
   m.timeframe  = (n > 2 && f[2] != "") ? f[2] : "";
   m.signal     = f[3];
   m.entry      = StringToDouble(f[4]);
   m.sl         = StringToDouble(f[5]);
   m.tp         = StringToDouble(f[6]);
   m.simulated  = (n > 7 && f[7] == "1");
   m.exit_time  = (n > 8  && f[8]  != "") ? (datetime)StringToInteger(f[8])  : 0;
   m.exit_price = (n > 9  && f[9]  != "") ? StringToDouble(f[9])           : 0.0;
   m.exit_reason= (n > 10) ? f[10] : "";
   m.net_pnl    = (n > 11 && f[11] != "") ? StringToDouble(f[11])          : 0.0;
   m.r_multiple = (n > 12 && f[12] != "") ? StringToDouble(f[12])          : 0.0;
   return true;
  }

//+------------------------------------------------------------------+
//| Newest .csv in the signals folder.                               |
//|                                                                   |
//| MQL5 has no FileGetTime(), so "newest" is resolved as the          |
//| lexicographically last matching filename. The generator names its  |
//| files <STRAT>_<SYMBOL>_<LABEL>.csv where LABEL is a year / month / |
//| YYYY-MM-DD range, and those sort in chronological order, so the   |
//| last match IS the most recent period. Set InpCsv to pin an exact  |
//| file if that is ever wrong.                                       |
//+------------------------------------------------------------------+
string NewestCsv()
  {
   string name = "";
   string best = "";
   string search = FilesDir() + "*.csv";
   long   h = FileFindFirst(search, name, 0);   // 0 = the MQL5\Files sandbox
   if(h == INVALID_HANDLE) return "";
   best = name;
   while(true)
     {
      h = FileFindNext(h, name);
      if(h == INVALID_HANDLE) break;
      if(StringCompare(name, best) > 0) best = name;
     }
   FileFindClose(h);
   return best;
  }

//+------------------------------------------------------------------+
bool LoadSignals()
  {
   string fname = InpCsv;
   if(fname == "") fname = NewestCsv();
   if(fname == "")
     {
      Print("MonthlyBT: no CSV found in ", FilesDir());
      return false;
     }

   string path = FilesDir() + fname;
   int h = FileOpen(path, FILE_READ | FILE_CSV, ',');
   if(h == INVALID_HANDLE)
     {
      Print("MonthlyBT: cannot open ", path, " err=", GetLastError());
      return false;
     }

   ENUM_TIMEFRAMES chart_tf = (ENUM_TIMEFRAMES)_Period;
   g_count = 0;
   g_skipped_tf = 0;
   ArrayResize(g_markers, InpMaxSignals);

   string line = "";
   while(!FileIsEnding(h) && g_count < InpMaxSignals)
     {
      // FileReadString returns the number of bytes read (uint), so compare
      // against 0 explicitly rather than using logical NOT.
      if(FileReadString(h, line) == 0) continue;
      if(StringFind(line, "epoch") >= 0) continue;   // header row

      Marker m;
      if(!ParseLine(line, m)) continue;

      if(!InpAllowAllTimeframes && StringLen(m.timeframe) > 0)
        {
         ENUM_TIMEFRAMES want = TfFromString(m.timeframe);
         if(want != PERIOD_CURRENT && want != chart_tf)
           { g_skipped_tf++; continue; }
        }

      g_markers[g_count] = m;
      g_count++;
     }
   FileClose(h);

   PrintFormat("MonthlyBT: %s -> %d marker(s) for %s (%d row(s) filtered as other timeframe)",
               fname, g_count, EnumToString(chart_tf), g_skipped_tf);
   return g_count > 0;
  }

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
               const int prev_calculated,
               const datetime &time[],
               const double &open[],
               const double &high[],
               const double &low[],
               const double &close[],
               const long &tick_volume[],
               const long &volume[],
               const int &spread[])
  {
   if(prev_calculated == 0)
     {
      if(!LoadSignals()) return 0;
     }

   int bars = Bars(_Symbol, 0);
   if(bars <= 0) return 0;

   datetime ptf_time[];
   ArraySetAsSeries(ptf_time, true);
   int copied = CopyTime(_Symbol, 0, 0, bars - 1, ptf_time);
   if(copied <= 0) return 0;

   for(int i = 0; i < g_count; i++)
     {
      int bi = iBarShift(_Symbol, 0, g_markers[i].time, false);
      if(bi < 0 || bi >= copied) continue;

      int bi_exit = -1;
      if(g_markers[i].exit_time > 0)
         bi_exit = iBarShift(_Symbol, 0, g_markers[i].exit_time, false);

      datetime seg0 = g_markers[i].time;
      double   off  = _Point * InpYOffsetPts * 3;
      datetime seg1 = (bi_exit >= 0) ? ptf_time[MathMin(bi_exit, copied - 1)]
                                    : seg0 + PeriodSeconds(_Period) * 8;

      if(InpShowLevels)
        {
         if(g_markers[i].simulated)
           {
            ObjectCreate(0, "MBT_E" + IntegerToString(i), OBJ_TREND, 0, seg0,
                         g_markers[i].entry, seg1, g_markers[i].entry, InpEntryColor, 1, STYLE_SOLID);
            ObjectCreate(0, "MBT_S" + IntegerToString(i), OBJ_TREND, 0, seg0,
                         g_markers[i].sl, seg1, g_markers[i].sl, InpStopColor, 1, STYLE_DOT);
            ObjectCreate(0, "MBT_T" + IntegerToString(i), OBJ_TREND, 0, seg0,
                         g_markers[i].tp, seg1, g_markers[i].tp, InpTargetColor, 1, STYLE_DOT);
           }
         else
           {
            ObjectCreate(0, "MBT_E" + IntegerToString(i), OBJ_TREND, 0, seg0,
                         g_markers[i].entry, seg0, g_markers[i].entry + off,
                         InpSignalColor, 1, STYLE_SOLID);
           }
        }

      // arrow: BUY up (below the low), SELL down (above the high)
      // NB these are ENUM_OBJECT values in MQL5, not the MQL4 ENUM_ARROW_TYPE.
      bool  is_sell = (g_markers[i].signal == "SELL");
      double ay = 0.0;
      if(is_sell)
         ay = g_markers[i].entry + off;
      else
         ay = g_markers[i].simulated ? (g_markers[i].sl - off * 2)
                                     : (g_markers[i].entry + off);

      ENUM_OBJECT arrow = is_sell ? OBJ_ARROW_DOWN : OBJ_ARROW_UP;
      color col = g_markers[i].simulated ? InpTradeColor : InpSignalColor;

      string nm = "MBT_" + g_markers[i].strategy + IntegerToString(i)
                  + (g_markers[i].simulated ? "_T" : "_S");

      if(ObjectFind(0, nm) < 0)
         ObjectCreate(0, nm, OBJ_ARROW, 0, seg0, ay, arrow);
      else
         ObjectMove(0, nm, 0, seg0, ay);
      ObjectSetInteger(0, nm, OBJPROP_COLOR, col);
      ObjectSetInteger(0, nm, OBJPROP_WIDTH, g_markers[i].simulated ? 3 : 2);
      ObjectSetInteger(0, nm, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, nm, OBJPROP_HIDDEN, true);

      // exit marker
      if(g_markers[i].simulated && bi_exit >= 0)
        {
         string xn = nm + "_X";
         // MQL5 has no left/right arrow objects. A filled arrow points at
         // the exit (win, green) and a down arrow marks a losing exit.
         ENUM_OBJECT xa = (g_markers[i].net_pnl >= 0) ? OBJ_ARROW_UP
                                                      : OBJ_ARROW_DOWN;
         if(ObjectFind(0, xn) < 0)
            ObjectCreate(0, xn, OBJ_ARROW, 0, ptf_time[bi_exit],
                         g_markers[i].exit_price, xa);
         ObjectSetInteger(0, xn, OBJPROP_COLOR,
                          g_markers[i].net_pnl >= 0 ? InpTradeColor : clrRed);
         ObjectSetInteger(0, xn, OBJPROP_SELECTABLE, false);
         ObjectSetInteger(0, xn, OBJPROP_HIDDEN, true);
        }

      // label
      if(InpShowLabels)
        {
         string ln = nm + "_L";
         string txt = g_markers[i].strategy + " " + g_markers[i].signal;
         if(StringLen(g_markers[i].timeframe) > 0)
            txt += " " + g_markers[i].timeframe;
         if(g_markers[i].simulated)
            txt += StringFormat("  E %.2f  SL %.2f  TP %.2f  %s  net %.2f  R %.2f",
                                g_markers[i].entry, g_markers[i].sl, g_markers[i].tp,
                                g_markers[i].exit_reason, g_markers[i].net_pnl,
                                g_markers[i].r_multiple);
         else
            txt += StringFormat("  E %.2f  SL %.2f  TP %.2f  (signal only)",
                                g_markers[i].entry, g_markers[i].sl, g_markers[i].tp);

         if(ObjectFind(0, ln) < 0)
            ObjectCreate(0, ln, OBJ_TEXT, 0, seg0, ay, txt);
         else
            ObjectSetString(0, ln, OBJPROP_TEXT, txt);
         ObjectSetInteger(0, ln, OBJPROP_COLOR, col);
         ObjectSetInteger(0, ln, OBJPROP_SELECTABLE, false);
         ObjectSetInteger(0, ln, OBJPROP_HIDDEN, true);
        }
     }
   return rates_total;
  }
//+------------------------------------------------------------------+
