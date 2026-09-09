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
// 1. CONFIGURATION DE LA CLÉ API
// ---------------------------------------------------------------------------

/**
 * Menu : Fonrex > Configure API Key
 *
 * Ouvre une boîte de dialogue et stocke la clé dans
 * PropertiesService.getUserProperties() — jamais dans une cellule,
 * jamais partagée si le Sheet est partagé avec quelqu'un d'autre
 * (les User Properties sont propres à chaque utilisateur Google,
 * pas au document).
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
    _updateConfigSheetStatus();
    ui.alert('API key saved successfully.');
  }
}

// ---------------------------------------------------------------------------
// 2. REFRESH FUNDAMENTALS
// ---------------------------------------------------------------------------

/**
 * Menu : Fonrex > Refresh Fundamentals
 *
 * Lit la liste de tickers depuis la feuille "Watchlist",
 * appelle GET /fundamental/deep pour chacun, écrit les résultats
 * dans la feuille "Fundamentals".
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
        data.asset_profile ? (data.asset_profile.sector || '') : '',
        data.highlights ? (data.highlights.market_cap || '') : '',
        data.highlights ? (data.highlights.pe_ratio || '') : '',
        data.highlights ? (data.highlights.peg_ratio || '') : '',
        data.highlights ? (data.highlights.roe || '') : '',
        data.highlights ? (data.highlights.roa || '') : '',
        data.highlights ? (data.highlights.dividend_yield || '') : '',
        data.solvency ? (data.solvency.debt_to_equity_ratio || '') : '',
        data.solvency ? (data.solvency.net_debt_to_ebitda || '') : '',
        data.highlights ? (data.highlights.beta || '') : '',
        data.highlights ? (data.highlights.week_52_high || '') : '',
        data.highlights ? (data.highlights.week_52_low || '') : '',
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
 * Menu : Fonrex > Refresh DCF Valuations
 *
 * IMPORTANT : n'écrit JAMAIS de champ "verdict" textuel
 * ("SOUS-ÉVALUÉ"/"SURÉVALUÉ") dans le sheet — uniquement les
 * valeurs numériques brutes (intrinsic_value, current_price,
 * wacc, upside_downside_pct). L'utilisateur voit les chiffres
 * et en tire ses propres conclusions ; Fonrex ne formule aucune
 * recommandation dans ce connecteur.
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
    'Ticker', 'Model Used', 'Intrinsic Value', 'Current Price',
    'Upside/Downside %', 'WACC', 'Cost of Debt Source',
    'Risk-Free Rate Source', 'Confidence', 'Last Calculated'
  ];
  dcfSheet.clearContents();
  dcfSheet.getRange(1, 1, 1, headers.length).setValues([headers]);

  const rows = tickers.map(ticker => {
    try {
      const data = fetchFonrexEndpoint(`/dcf/${ticker}`, apiKey);
      return [
        ticker,
        data.dcf_model_used || '',
        data.intrinsic_value || '',
        data.current_price || '',
        data.upside_downside_pct || '',
        data.wacc_detail ? (data.wacc_detail.wacc || '') : '',
        data.wacc_detail ? (data.wacc_detail.cost_of_debt_source || '') : '',
        data.wacc_detail ? (data.wacc_detail.risk_free_rate_source || '') : '',
        data.confidence || '',
        new Date().toISOString(),
      ];
    } catch (e) {
      return [ticker, 'ERROR: ' + e.message, '', '', '', '', '', '', '', new Date().toISOString()];
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
 * Menu : Fonrex > Refresh Technical Indicators
 *
 * Appelle GET /technical?ticker={ticker} pour chacun des tickers
 * de la watchlist et écrit les résultats dans la feuille "Technicals".
 * Seules les valeurs brutes sont écrites — aucune interprétation textuelle
 * ("Bullish", "Bearish") n'est insérée automatiquement.
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
      const data = fetchFonrexEndpoint(`/technical?ticker=${ticker}`, apiKey);
      return [
        ticker,
        data.rsi_14 || '',
        data.macd ? (data.macd.macd || '') : '',
        data.macd ? (data.macd.signal || '') : '',
        data.macd ? (data.macd.hist || '') : '',
        data.sma_50 || '',
        data.sma_200 || '',
        data.ema_20 || '',
        data.bollinger ? (data.bollinger.upper || '') : '',
        data.bollinger ? (data.bollinger.lower || '') : '',
        data.atr_14 || '',
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
// 5. FONCTIONS CUSTOM (utilisables comme formules dans les cellules)
// ---------------------------------------------------------------------------

/**
 * =FONREX_PE("AIR.PA") — Retourne le P/E ratio d'un ticker.
 *
 * Limitation documentée : Apps Script custom functions sont mises en cache
 * pendant 30 minutes par Google. Pour une donnée fraîche, utiliser le
 * bouton "Refresh Fundamentals" du menu plutôt que cette formule.
 *
 * @param {string} ticker Le symbole boursier (ex: AIR.PA, AAPL)
 * @return {number|string} Le P/E ratio ou un message d'erreur
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
 * =FONREX_DIVIDEND_YIELD("AIR.PA") — Retourne le dividend yield d'un ticker.
 *
 * @param {string} ticker Le symbole boursier
 * @return {number|string} Le dividend yield (décimal, ex: 0.032 = 3.2%) ou un message d'erreur
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
 * =FONREX_INTRINSIC_VALUE("AIR.PA") — Retourne la valeur intrinsèque DCF.
 *
 * Note : cette valeur est un résultat de modèle analytique, pas une
 * recommandation d'achat ou de vente.
 *
 * @param {string} ticker Le symbole boursier
 * @return {number|string} La valeur intrinsèque ou un message d'erreur
 * @customfunction
 */
function FONREX_INTRINSIC_VALUE(ticker) {
  const apiKey = PropertiesService.getUserProperties().getProperty('FONREX_API_KEY');
  if (!apiKey) return 'Configure API key first';
  try {
    const data = fetchFonrexEndpoint(`/dcf/${ticker}`, apiKey);
    return data.intrinsic_value;
  } catch (e) {
    return 'Error: ' + e.message;
  }
}

/**
 * =FONREX_RSI("AAPL") — Retourne le RSI 14 périodes d'un ticker.
 *
 * @param {string} ticker Le symbole boursier
 * @return {number|string} Le RSI (14) ou un message d'erreur
 * @customfunction
 */
function FONREX_RSI(ticker) {
  const apiKey = PropertiesService.getUserProperties().getProperty('FONREX_API_KEY');
  if (!apiKey) return 'Configure API key first';
  try {
    const data = fetchFonrexEndpoint(`/technical?ticker=${ticker}`, apiKey);
    return data.rsi_14 !== undefined ? data.rsi_14 : 'N/A';
  } catch (e) {
    return 'Error: ' + e.message;
  }
}

// ---------------------------------------------------------------------------
// 6. UTILITAIRES
// ---------------------------------------------------------------------------

/**
 * Wrapper centralisé pour tous les appels à l'API Fonrex.
 * Gère l'authentification, le parsing JSON et les erreurs HTTP
 * de façon lisible pour un utilisateur non-technique.
 *
 * @param {string} path   Chemin de l'endpoint (ex: /fundamental/deep?ticker=AIR.PA)
 * @param {string} apiKey Clé API Fonrex Relay (frx_live_...)
 * @return {Object} Objet JSON parsé de la réponse
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

  return JSON.parse(response.getContentText());
}

/**
 * Ajoute un ticker à la Watchlist via le menu.
 * Le ticker est normalisé en majuscules avant insertion.
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
 * Ouvre la documentation Fonrex dans un nouvel onglet du navigateur.
 */
function openDocs() {
  const html = HtmlService.createHtmlOutput(
    '<script>window.open("https://docs.fonrex.io");google.script.host.close();</script>'
  );
  SpreadsheetApp.getUi().showModalDialog(html, 'Opening documentation...');
}

/**
 * Met à jour la feuille Config avec le statut courant (clé configurée,
 * date du dernier rafraîchissement). Appelé automatiquement après chaque
 * refresh et après la sauvegarde de la clé.
 *
 * @private
 */
function _updateConfigSheetStatus() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const configSheet = ss.getSheetByName('Config');
  if (!configSheet) return;

  const apiKey = PropertiesService.getUserProperties().getProperty('FONREX_API_KEY');
  const statusCell = configSheet.getRange('B3');
  statusCell.setValue(apiKey ? '✅ API Key configured' : '❌ API Key not configured');

  const lastRefreshCell = configSheet.getRange('B5');
  lastRefreshCell.setValue(new Date().toUTCString());
}
