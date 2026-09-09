// =============================================================================
// Fonrex Sheets Connector — Code.gs
// Version : 1.0
// Documentation : https://docs.fonrex.io
// =============================================================================

// ---------------------------------------------------------------------------
// MENU
// ---------------------------------------------------------------------------

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Fonrex')
    .addItem('Configure API Key', 'configureApiKey')
    .addSeparator()
    .addItem('Refresh Fundamentals', 'refreshFundamentals')
    .addItem('Refresh DCF Valuations', 'refreshDCF')
    .addItem('Refresh Technical Indicators', 'refreshTechnicals')
    .addSeparator()
    .addItem('Add Ticker to Watchlist', 'promptAddTicker')
    .addItem('Open Fonrex Documentation', 'openDocs')
    .addToUi();
}

// ---------------------------------------------------------------------------
// 1. API KEY CONFIGURATION
// ---------------------------------------------------------------------------

/**
 * Menu: Fonrex > Configure API Key
 *
 * Opens a dialog and stores the key in
 * PropertiesService.getUserProperties() — never in a cell,
 * never shared if the Sheet is shared with someone else
 * (User Properties are specific to each Google user,
 * not the document).
 */
function configureApiKey() {
  const ui = SpreadsheetApp.getUi();
  const response = ui.prompt(
    'Fonrex API Key',
    'Paste your Fonrex Relay API key (frx_live_...):',
    ui.ButtonSet.OK_CANCEL
  );
  if (response.getSelectedButton() == ui.Button.OK) {
    const key = response.getResponseText().trim();
    if (!key.startsWith('frx_live_')) {
      ui.alert('Invalid key format. Expected a key starting with frx_live_');
      return;
    }
    PropertiesService.getUserProperties().setProperty('FONREX_API_KEY', key);
    _updateConfigSheetStatus(false);
    ui.alert('API key saved successfully.');
  }
}

// ---------------------------------------------------------------------------
// 2. REFRESH FUNDAMENTALS
// ---------------------------------------------------------------------------

/**
 * Menu: Fonrex > Refresh Fundamentals
 *
 * Reads the list of tickers from the "Watchlist" sheet,
 * calls GET /fundamental/deep for each, and writes the results
 * into the "Fundamentals" sheet.
 */
function refreshFundamentals() {
  const apiKey = PropertiesService.getUserProperties().getProperty('FONREX_API_KEY');
  if (!apiKey) {
    SpreadsheetApp.getUi().alert('Please configure your API key first (Fonrex > Configure API Key).');
    return;
  }

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const watchlistSheet = ss.getSheetByName('Watchlist');
  const fundamentalsSheet = ss.getSheetByName('Fundamentals');

  if (!watchlistSheet || !fundamentalsSheet) {
    SpreadsheetApp.getUi().alert('Missing sheet: make sure "Watchlist" and "Fundamentals" sheets exist.');
    return;
  }

  const tickers = watchlistSheet.getRange('A2:A').getValues()
    .flat().filter(t => t !== '');

  if (tickers.length === 0) {
    SpreadsheetApp.getUi().alert('No tickers found in the Watchlist sheet (column A, starting row 2).');
    return;
  }

  const headers = [
    'Ticker', 'Name', 'Sector', 'Market Cap', 'P/E Ratio', 'PEG Ratio',
    'ROE', 'ROA', 'Dividend Yield', 'Debt/Equity', 'Net Debt/EBITDA',
    'Beta', '52W High', '52W Low', 'Last Updated'
  ];
  fundamentalsSheet.clearContents();
  fundamentalsSheet.getRange(1, 1, 1, headers.length).setValues([headers]);

  const rows = tickers.map(ticker => {
    try {
      const data = fetchFonrexEndpoint(`/fundamental/deep?ticker=${ticker}`, apiKey);
      return [
        ticker,
        data.asset_profile ? data.asset_profile.name : '',
        '', // Sector removed as it is not present in asset_profile of this endpoint
        data.highlights ? (data.highlights.market_cap ?? '') : '',
        data.highlights ? (data.highlights.pe_ratio ?? '') : '',
        data.highlights ? (data.highlights.peg_ratio ?? '') : '',
        data.highlights ? (data.highlights.roe ?? '') : '',
        data.highlights ? (data.highlights.roa ?? '') : '',
        data.highlights ? (data.highlights.dividend_yield ?? '') : '',
        data.highlights ? (data.highlights.debt_to_equity_ratio ?? '') : '',
        data.highlights ? (data.highlights.net_debt_to_ebitda ?? '') : '',
        data.highlights ? (data.highlights.beta ?? '') : '',
        data.highlights ? (data.highlights.week_52_high ?? '') : '',
        data.highlights ? (data.highlights.week_52_low ?? '') : '',
        new Date().toISOString(),
      ];
    } catch (e) {
      return [ticker, 'ERROR: ' + e.message, '', '', '', '', '', '', '', '', '', '', '', '', new Date().toISOString()];
    }
  });

  if (rows.length > 0) {
    fundamentalsSheet.getRange(2, 1, rows.length, headers.length).setValues(rows);
  }

  _updateConfigSheetStatus();
  SpreadsheetApp.getActiveSpreadsheet().toast(`Fundamentals refreshed for ${tickers.length} ticker(s).`, 'Fonrex', 4);
}

// ---------------------------------------------------------------------------
// 3. REFRESH DCF VALUATIONS
// ---------------------------------------------------------------------------

/**
 * Menu: Fonrex > Refresh DCF Valuations
 *
 * IMPORTANT: NEVER writes a textual "verdict" field
 * ("UNDERVALUED"/"OVERVALUED") in the sheet — only the
 * raw numeric values (intrinsic_value, current_price,
 * wacc, upside_downside_pct). The user sees the numbers
 * and draws their own conclusions; Fonrex makes no
 * recommendations in this connector.
 */
function refreshDCF() {
  const apiKey = PropertiesService.getUserProperties().getProperty('FONREX_API_KEY');
  if (!apiKey) {
    SpreadsheetApp.getUi().alert('Please configure your API key first.');
    return;
  }

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const watchlistSheet = ss.getSheetByName('Watchlist');
  const dcfSheet = ss.getSheetByName('DCF');

  if (!watchlistSheet || !dcfSheet) {
    SpreadsheetApp.getUi().alert('Missing sheet: make sure "Watchlist" and "DCF" sheets exist.');
    return;
  }

  const tickers = watchlistSheet.getRange('A2:A').getValues()
    .flat().filter(t => t !== '');

  if (tickers.length === 0) {
    SpreadsheetApp.getUi().alert('No tickers found in the Watchlist sheet.');
    return;
  }

  const headers = [
    'Ticker', 'Consensus Value', 'Current Price',
    'Consensus Upside %', 'WACC', 'FCF Value', 'Last Calculated'
  ];
  dcfSheet.clearContents();
  dcfSheet.getRange(1, 1, 1, headers.length).setValues([headers]);

  const rows = tickers.map(ticker => {
    try {
      const data = fetchFonrexEndpoint(`/dcf/${ticker}`, apiKey);
      return [
        ticker,
        data.consensus_value ?? '',
        data.current_price ?? '',
        data.consensus_upside_pct ?? '',
        data.wacc ?? '',
        data.models ? (data.models.fcf ?? '') : '',
        new Date().toISOString(),
      ];
    } catch (e) {
      return [ticker, 'ERROR: ' + e.message, '', '', '', '', new Date().toISOString()];
    }
  });

  if (rows.length > 0) {
    dcfSheet.getRange(2, 1, rows.length, headers.length).setValues(rows);
  }

  _updateConfigSheetStatus();
  SpreadsheetApp.getActiveSpreadsheet().toast(`DCF valuations refreshed for ${tickers.length} ticker(s).`, 'Fonrex', 4);
}

// ---------------------------------------------------------------------------
// 4. REFRESH TECHNICAL INDICATORS
// ---------------------------------------------------------------------------

/**
 * Menu: Fonrex > Refresh Technical Indicators
 *
 * Calls GET /technical?ticker={ticker} for each ticker
 * in the watchlist and writes the results into the "Technicals" sheet.
 * Only raw values are written — no textual interpretation
 * ("Bullish", "Bearish") is automatically inserted.
 */
function refreshTechnicals() {
  const apiKey = PropertiesService.getUserProperties().getProperty('FONREX_API_KEY');
  if (!apiKey) {
    SpreadsheetApp.getUi().alert('Please configure your API key first.');
    return;
  }

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const watchlistSheet = ss.getSheetByName('Watchlist');
  const techSheet = ss.getSheetByName('Technicals');

  if (!watchlistSheet || !techSheet) {
    SpreadsheetApp.getUi().alert('Missing sheet: make sure "Watchlist" and "Technicals" sheets exist.');
    return;
  }

  const tickers = watchlistSheet.getRange('A2:A').getValues()
    .flat().filter(t => t !== '');

  if (tickers.length === 0) {
    SpreadsheetApp.getUi().alert('No tickers found in the Watchlist sheet.');
    return;
  }

  const headers = [
    'Ticker', 'RSI (14)', 'MACD', 'MACD Signal', 'MACD Hist',
    'SMA 50', 'SMA 200', 'EMA 20', 'Bollinger Upper', 'Bollinger Lower',
    'ATR (14)', 'Last Updated'
  ];
  techSheet.clearContents();
  techSheet.getRange(1, 1, 1, headers.length).setValues([headers]);

  const rows = tickers.map(ticker => {
    try {
      const data = fetchFonrexEndpoint(`/technical/${ticker}/multi?indicators=rsi,macd,sma_50,sma_200,ema_20,bbands,atr`, apiKey);
      
      const getVal = (indKey, sIdx = 0) => {
        try {
          const vals = data.indicators[indKey].series[sIdx].values;
          if (vals.length === 0) return '';
          return vals[vals.length - 1].v ?? '';
        } catch(e) { return ''; }
      };

      return [
        ticker,
        getVal('rsi', 0),
        getVal('macd', 0),
        getVal('macd', 2),
        getVal('macd', 1),
        getVal('sma_50', 0),
        getVal('sma_200', 0),
        getVal('ema_20', 0),
        getVal('bbands', 2), // Upper
        getVal('bbands', 0), // Lower
        getVal('atr', 0),
        new Date().toISOString(),
      ];
    } catch (e) {
      return [ticker, 'ERROR: ' + e.message, '', '', '', '', '', '', '', '', '', new Date().toISOString()];
    }
  });

  if (rows.length > 0) {
    techSheet.getRange(2, 1, rows.length, headers.length).setValues(rows);
  }

  _updateConfigSheetStatus();
  SpreadsheetApp.getActiveSpreadsheet().toast(`Technical indicators refreshed for ${tickers.length} ticker(s).`, 'Fonrex', 4);
}

// ---------------------------------------------------------------------------
// 5. CUSTOM FUNCTIONS (usable as formulas in cells)
// ---------------------------------------------------------------------------

/**
 * =FONREX_PE("AIR.PA") — Returns the P/E ratio of a ticker.
 *
 * Documented limitation: Apps Script custom functions are cached
 * for 30 minutes by Google. For fresh data, use the
 * "Refresh Fundamentals" menu button instead of this formula.
 *
 * @param {string} ticker The stock symbol (e.g. AIR.PA, AAPL)
 * @return {number|string} The P/E ratio or an error message
 * @customfunction
 */
function FONREX_PE(ticker) {
  const apiKey = PropertiesService.getUserProperties().getProperty('FONREX_API_KEY');
  if (!apiKey) return 'Configure API key first (Fonrex menu)';
  try {
    const data = fetchFonrexEndpoint(`/fundamental?ticker=${ticker}`, apiKey);
    return data.highlights ? data.highlights.pe_ratio : 'N/A';
  } catch (e) {
    return 'Error: ' + e.message;
  }
}

/**
 * =FONREX_DIVIDEND_YIELD("AIR.PA") — Returns the dividend yield of a ticker.
 *
 * @param {string} ticker The stock symbol
 * @return {number|string} The dividend yield (decimal, e.g. 0.032 = 3.2%) or an error message
 * @customfunction
 */
function FONREX_DIVIDEND_YIELD(ticker) {
  const apiKey = PropertiesService.getUserProperties().getProperty('FONREX_API_KEY');
  if (!apiKey) return 'Configure API key first';
  try {
    const data = fetchFonrexEndpoint(`/fundamental?ticker=${ticker}`, apiKey);
    return data.highlights ? data.highlights.dividend_yield : 'N/A';
  } catch (e) {
    return 'Error: ' + e.message;
  }
}

/**
 * =FONREX_INTRINSIC_VALUE("AIR.PA") — Returns the DCF consensus value.
 *
 * Note: this value is an analytical model result, not a
 * buy or sell recommendation.
 *
 * @param {string} ticker The stock symbol
 * @return {number|string} The consensus value or an error message
 * @customfunction
 */
function FONREX_INTRINSIC_VALUE(ticker) {
  const apiKey = PropertiesService.getUserProperties().getProperty('FONREX_API_KEY');
  if (!apiKey) return 'Configure API key first';
  try {
    const data = fetchFonrexEndpoint(`/dcf/${ticker}`, apiKey);
    return data.consensus_value;
  } catch (e) {
    return 'Error: ' + e.message;
  }
}

/**
 * =FONREX_RSI("AAPL") — Returns the 14-period RSI of a ticker.
 *
 * @param {string} ticker The stock symbol
 * @return {number|string} The RSI (14) or an error message
 * @customfunction
 */
function FONREX_RSI(ticker) {
  const apiKey = PropertiesService.getUserProperties().getProperty('FONREX_API_KEY');
  if (!apiKey) return 'Configure API key first';
  try {
    const data = fetchFonrexEndpoint(`/technical/${ticker}/multi?indicators=rsi`, apiKey);
    const ind = data.indicators['rsi'];
    if (ind && ind.series && ind.series[0] && ind.series[0].values.length > 0) {
      const vals = ind.series[0].values;
      return vals[vals.length - 1].v ?? 'N/A';
    }
    return 'N/A';
  } catch (e) {
    return 'Error: ' + e.message;
  }
}

// ---------------------------------------------------------------------------
// 6. UTILITIES
// ---------------------------------------------------------------------------

/**
 * Centralized wrapper for all calls to the Fonrex API.
 * Handles authentication, JSON parsing, and HTTP errors
 * in a readable way for non-technical users.
 *
 * @param {string} path   Endpoint path (e.g. /fundamental/deep?ticker=AIR.PA)
 * @param {string} apiKey Fonrex Relay API Key (frx_live_...)
 * @return {Object} Parsed JSON object of the response
 */
function fetchFonrexEndpoint(path, apiKey) {
  const baseUrl = PropertiesService.getScriptProperties().getProperty('FONREX_BASE_URL')
                  || 'https://api.fonrex.io';

  const response = UrlFetchApp.fetch(baseUrl + path, {
    method: 'get',
    headers: { 'Authorization': 'Bearer ' + apiKey },
    muteHttpExceptions: true,
  });

  const code = response.getResponseCode();
  if (code === 401) {
    throw new Error('Invalid or expired API key');
  }
  if (code === 429) {
    throw new Error('Quota exceeded — check your Fonrex plan');
  }
  if (code === 404) {
    throw new Error('Ticker not found');
  }
  if (code >= 500) {
    throw new Error('Fonrex service temporarily unavailable');
  }
  if (code >= 400) {
    throw new Error('API request failed with status ' + code);
  }

  return JSON.parse(response.getContentText());
}

/**
 * Adds a ticker to the Watchlist via the menu.
 * The ticker is normalized to uppercase before insertion.
 */
function promptAddTicker() {
  const ui = SpreadsheetApp.getUi();
  const response = ui.prompt(
    'Add Ticker',
    'Enter a ticker symbol (e.g. AIR.PA, AAPL):',
    ui.ButtonSet.OK_CANCEL
  );
  if (response.getSelectedButton() == ui.Button.OK) {
    const ticker = response.getResponseText().trim().toUpperCase();
    if (!ticker) return;
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('Watchlist');
    sheet.appendRow([ticker]);
    SpreadsheetApp.getActiveSpreadsheet().toast(`${ticker} added to your Watchlist.`, 'Fonrex', 3);
  }
}

/**
 * Opens the Fonrex documentation in a new browser tab.
 */
function openDocs() {
  const html = HtmlService.createHtmlOutput(
    '<script>window.open("https://docs.fonrex.io");google.script.host.close();</script>'
  );
  SpreadsheetApp.getUi().showModalDialog(html, 'Opening documentation...');
}

/**
 * Updates the Config sheet with the current status (configured key,
 * last refresh date). Automatically called after each
 * refresh and after saving the key.
 *
 * @private
 */
function _updateConfigSheetStatus(updateTimestamp = true) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const configSheet = ss.getSheetByName('Config');
  if (!configSheet) return;

  const apiKey = PropertiesService.getUserProperties().getProperty('FONREX_API_KEY');
  const statusCell = configSheet.getRange('B3');
  statusCell.setValue(apiKey ? '✅ API Key configured' : '❌ API Key not configured');

  if (updateTimestamp) {
    const lastRefreshCell = configSheet.getRange('B5');
    lastRefreshCell.setValue(new Date().toUTCString());
  }
}
