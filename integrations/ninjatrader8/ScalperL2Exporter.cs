#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.IO;
using System.Text;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

// Exports 1-minute OHLCV + Level II book stats to CSV for scalper/paper_runner.py (follow mode).
// Data source: NT8 native market data stream (Rithmic/sim) via OnMarketDepth / OnMarketData.
namespace NinjaTrader.NinjaScript.Strategies
{
	public class ScalperL2Exporter : Strategy
	{
		private const int DepthLevels = 5;
		private const string CsvHeader =
			"timestamp,open,high,low,close,volume,bid,ask,bid_size,ask_size,bid_depth,ask_depth,delta";

		private readonly double[] bidPrices = new double[DepthLevels];
		private readonly long[] bidSizes = new long[DepthLevels];
		private readonly double[] askPrices = new double[DepthLevels];
		private readonly long[] askSizes = new long[DepthLevels];

		private double lastBid;
		private double lastAsk;
		private long lastBidSize;
		private long lastAskSize;
		private double barDelta;
		private bool headerWritten;
		private string resolvedExportPath = string.Empty;

		[NinjaScriptProperty]
		[Display(Name = "ExportPath", Order = 1, GroupName = "Parameters",
			Description = "Append-only CSV path (match BAR_CSV_PATH / NT8_EXPORT_PATH in Python .env)")]
		public string ExportPath { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "WriteHeader", Order = 2, GroupName = "Parameters",
			Description = "Write CSV header when file is missing or empty")]
		public bool WriteHeader { get; set; }

		protected override void OnStateChange()
		{
			if (State == State.SetDefaults)
			{
				Name = "ScalperL2Exporter";
				Description = "Append 1m OHLCV + L2 book to CSV for futures-trend-day-l2-scalper paper_runner";
				Calculate = Calculate.OnBarClose;
				EntriesPerDirection = 1;
				EntryHandling = EntryHandling.AllEntries;
				IsExitOnSessionCloseStrategy = false;
				IsFillLimitOnTouch = false;
				MaximumBarsLookBack = MaximumBarsLookBack.TwoHundredFiftySix;
				OrderFillResolution = OrderFillResolution.Standard;
				Slippage = 0;
				StartBehavior = StartBehavior.WaitUntilFlat;
				TimeInForce = TimeInForce.Gtc;
				TraceOrders = false;
				RealtimeErrorHandling = RealtimeErrorHandling.StopCancelClose;
				StopTargetHandling = StopTargetHandling.PerEntryExecution;
				BarsRequiredToTrade = 1;
				IsInstantiatedOnEachOptimizationIteration = true;

				ExportPath = @"C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv";
				WriteHeader = true;
			}
			else if (State == State.Configure)
			{
				// Primary series should be 1-minute; strategy is chart-driven.
			}
			else if (State == State.DataLoaded)
			{
				resolvedExportPath = string.IsNullOrWhiteSpace(ExportPath)
					? @"C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv"
					: ExportPath.Trim();

				string dir = Path.GetDirectoryName(resolvedExportPath);
				if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
					Directory.CreateDirectory(dir);

				ResetDepthBook();
				barDelta = 0;
				headerWritten = File.Exists(resolvedExportPath) && new FileInfo(resolvedExportPath).Length > 0;

				Print(string.Format("ScalperL2Exporter: writing to {0}", resolvedExportPath));
			}
		}

		protected override void OnBarUpdate()
		{
			if (BarsInProgress != 0)
				return;

			if (CurrentBar < BarsRequiredToTrade)
				return;

			// Only export completed 1-minute bars (OnBarClose).
			if (IsFirstTickOfBar)
				barDelta = 0;

			double bid = lastBid > 0 ? lastBid : GetCurrentBid();
			double ask = lastAsk > 0 ? lastAsk : GetCurrentAsk();
			long bidSize = lastBidSize > 0 ? lastBidSize : (long)Math.Max(0, bidSizes[0]);
			long askSize = lastAskSize > 0 ? lastAskSize : (long)Math.Max(0, askSizes[0]);

			long bidDepth = SumSizes(bidSizes);
			long askDepth = SumSizes(askSizes);

			string timestamp = Time[0].ToString("yyyy-MM-dd HH:mm:ss");
			string line = string.Format(
				System.Globalization.CultureInfo.InvariantCulture,
				"{0},{1:0.00},{2:0.00},{3:0.00},{4:0.00},{5},{6:0.00},{7:0.00},{8},{9},{10},{11},{12:0.0}",
				timestamp,
				Open[0],
				High[0],
				Low[0],
				Close[0],
				(long)Volume[0],
				bid,
				ask,
				bidSize,
				askSize,
				bidDepth,
				askDepth,
				barDelta);

			AppendCsvLine(line);
		}

		protected override void OnMarketDepth(MarketDepthEventArgs e)
		{
			if (e.Position < 0 || e.Position >= DepthLevels)
				return;

			if (e.MarketDataType == MarketDataType.Bid)
			{
				if (e.Operation == Operation.Remove)
				{
					bidPrices[e.Position] = 0;
					bidSizes[e.Position] = 0;
				}
				else
				{
					bidPrices[e.Position] = e.Price;
					bidSizes[e.Position] = e.Volume;
				}

				if (e.Position == 0)
				{
					lastBid = e.Price;
					lastBidSize = e.Volume;
				}
			}
			else if (e.MarketDataType == MarketDataType.Ask)
			{
				if (e.Operation == Operation.Remove)
				{
					askPrices[e.Position] = 0;
					askSizes[e.Position] = 0;
				}
				else
				{
					askPrices[e.Position] = e.Price;
					askSizes[e.Position] = e.Volume;
				}

				if (e.Position == 0)
				{
					lastAsk = e.Price;
					lastAskSize = e.Volume;
				}
			}
		}

		protected override void OnMarketData(MarketDataEventArgs e)
		{
			if (e.MarketDataType != MarketDataType.Last)
				return;

			double bid = lastBid > 0 ? lastBid : GetCurrentBid();
			double ask = lastAsk > 0 ? lastAsk : GetCurrentAsk();
			long size = e.Volume;

			if (size <= 0)
				return;

			// Classify aggressor vs top of book for bar delta.
			if (ask > 0 && e.Price >= ask - TickSize * 0.5)
				barDelta += size;
			else if (bid > 0 && e.Price <= bid + TickSize * 0.5)
				barDelta -= size;
		}

		private void AppendCsvLine(string line)
		{
			try
			{
				using (var stream = new FileStream(resolvedExportPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
				using (var writer = new StreamWriter(stream, Encoding.UTF8))
				{
					if (WriteHeader && !headerWritten)
					{
						writer.WriteLine(CsvHeader);
						headerWritten = true;
					}
					writer.WriteLine(line);
				}
			}
			catch (Exception ex)
			{
				Print(string.Format("ScalperL2Exporter write failed: {0}", ex.Message));
			}
		}

		private static long SumSizes(long[] sizes)
		{
			long total = 0;
			for (int i = 0; i < sizes.Length; i++)
				total += Math.Max(0, sizes[i]);
			return total;
		}

		private void ResetDepthBook()
		{
			for (int i = 0; i < DepthLevels; i++)
			{
				bidPrices[i] = 0;
				bidSizes[i] = 0;
				askPrices[i] = 0;
				askSizes[i] = 0;
			}
			lastBid = 0;
			lastAsk = 0;
			lastBidSize = 0;
			lastAskSize = 0;
		}
	}
}
