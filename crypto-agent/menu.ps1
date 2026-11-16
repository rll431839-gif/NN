$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$coins15 = @("BTC-USD","ETH-USD","SOL-USD","BNB-USD","XRP-USD","ADA-USD","LINK-USD","AVAX-USD","DOGE-USD","DOT-USD","LTC-USD","BCH-USD","TRX-USD","ATOM-USD","ETC-USD")
$validTF = @("1s","1m","5m","15m","1h","1d","1wk","1mo")

function Get-Cfg {
    if (Test-Path "apex_config.json") {
        try { return (Get-Content "apex_config.json" -Raw | ConvertFrom-Json) } catch {}
    }
    return $null
}

function Save-CfgObj($obj) {
    $obj | ConvertTo-Json -Depth 6 | Set-Content -Path "apex_config.json" -Encoding UTF8
}

function Ensure-Cfg {
    $c = Get-Cfg
    if ($null -eq $c) {
        & $py -c "import agent; agent.save_cfg(agent.load_cfg())" | Out-Null
        $c = Get-Cfg
    }
    return $c
}

function Select-Coin {
    Write-Host "`n--- WAEHRUNGS-AUSWAHL (15 COINS) ---" -ForegroundColor Cyan
    for ($i = 0; $i -lt 5; $i++) {
        $c1 = "[{0,2}] {1,-9}" -f ($i+1), $coins15[$i]
        $c2 = "[{0,2}] {1,-9}" -f ($i+6), $coins15[$i+5]
        $c3 = "[{0,2}] {1,-9}" -f ($i+11), $coins15[$i+10]
        Write-Host "  $c1   $c2   $c3"
    }
    Write-Host "  [ 0] Eigenen Ticker eingeben (Enter = BTC-USD)"
    $pick = (Read-Host "Coin waehlen [0-15]").Trim()
    if ([string]::IsNullOrWhiteSpace($pick)) { return "BTC-USD" }
    if ($pick -match '^\d+$') {
        $idx = [int]$pick
        if ($idx -ge 1 -and $idx -le 15) { return $coins15[$idx - 1] }
        if ($idx -eq 0) {
            $cust = (Read-Host "Ticker eingeben (z.B. BTC, SOL, ^GSPC)").Trim()
            if ([string]::IsNullOrWhiteSpace($cust)) { return "BTC-USD" }
            return $cust
        }
    }
    return $pick
}

function Select-TF {
    Write-Host "  Zeitskalen: [1] 1s  [2] 1m  [3] 5m  [4] 15m  [5] 1h  [6] 1d  [7] 1wk  [8] 1mo" -ForegroundColor DarkGray
    $x = (Read-Host "Zeitskala waehlen [1-8 oder Name] (Enter = 1d)").Trim().ToLower()
    if ([string]::IsNullOrWhiteSpace($x)) { return "1d" }
    if ($x -match '^[1-8]$') { return $validTF[[int]$x - 1] }
    if ($x -in $validTF) { return $x }
    Write-Host "Unbekannt '$x' -> nutze 1d." -ForegroundColor Yellow
    return "1d"
}

function Show-Header {
    $cfg = Ensure-Cfg
    $pStat = "Paper: Nicht gestartet"
    if (Test-Path "apex_portfolio_state.json") {
        try {
            $st = Get-Content "apex_portfolio_state.json" -Raw | ConvertFrom-Json
            $pc = if ($st.positions) { ($st.positions.PSObject.Properties | Measure-Object).Count } else { 0 }
            $pStat = ("Cash `${0:N2} | Pos: {1} | Trades: {2} | Halted: {3}" -f $st.cash_usd, $pc, $st.trades, $st.halted)
        } catch {}
    }
    $kiTxt = if ($cfg.llm.verified) { "KI: $($cfg.llm.provider)/$($cfg.llm.model) [OK]" } else { "KI: Offline" }
    $bTxt = if ($cfg.exchanges.binance_verified) { "Binance [OK]" } else { "Binance [--]" }
    $kTxt = if ($cfg.exchanges.kraken_verified) { "Kraken [OK]" } else { "Kraken [--]" }
    Write-Host ""
    Write-Host "========================================================================================" -ForegroundColor Cyan
    Write-Host "  APEX-QUANT v9 WORKSTATION | TRADER-KONTROLLZENTRUM" -ForegroundColor White
    Write-Host "========================================================================================" -ForegroundColor Cyan
    Write-Host "  $pStat" -ForegroundColor Yellow
    Write-Host "  $kiTxt | Boersen: $bTxt / $kTxt | Vol-Target: $($cfg.vol_target) | Steuer: $([int]($cfg.tax_rate*100))%" -ForegroundColor Gray
    Write-Host "----------------------------------------------------------------------------------------" -ForegroundColor DarkGray
}

function Menu-Market {
    while ($true) {
        Write-Host "`n--- [1] MARKTSCANNER & 15-COIN-AUSWAHL ---" -ForegroundColor Cyan
        Write-Host "  [1] Live Ultra-Marktscanner (alle aktiven Coins + L2 + FundingAPR + DVOL)"
        Write-Host "  [2] Einzel-Coin aus 15 Waehrungen auswaehlen & analysieren"
        Write-Host "  [3] Watchlist-Manager (aktive Coins an-/abwaehlen)"
        Write-Host "  [4] Derivate-, L2-Orderbuch- & Liquiditaets-Monitor"
        Write-Host "  [0] Zurueck ins Hauptmenue"
        $w = (Read-Host "Auswahl [0-4]").Trim()
        if ($w -eq "0") { return }
        switch ($w) {
            "1" { & $py agent.py --scan; [void](Read-Host "Enter fuer weiter...") }
            "2" { $c = Select-Coin; $tf = Select-TF; & $py agent.py $c --tf $tf; [void](Read-Host "Enter fuer weiter...") }
            "3" {
                $cfg = Ensure-Cfg
                Write-Host "`nAktive Watchlist:" -ForegroundColor Yellow
                for ($i = 0; $i -lt $coins15.Count; $i++) {
                    $on = if ($coins15[$i] -in $cfg.active_universe) { "[X]" } else { "[ ]" }
                    Write-Host ("  [{0,2}] {1} {2}" -f ($i+1), $on, $coins15[$i])
                }
                $tog = (Read-Host "Nummer zum Umschalten [1-15] oder ALL / Enter=Zurueck").Trim()
                if ($tog -eq "ALL") { $cfg.active_universe = $coins15; Save-CfgObj $cfg }
                elseif ($tog -match '^\d+$' -and [int]$tog -ge 1 -and [int]$tog -le 15) {
                    $sym = $coins15[[int]$tog - 1]
                    $list = [System.Collections.ArrayList]@($cfg.active_universe)
                    if ($sym -in $list) { if ($list.Count -gt 2) { [void]$list.Remove($sym) } } else { [void]$list.Add($sym) }
                    $cfg.active_universe = @($list)
                    Save-CfgObj $cfg
                }
            }
            "4" { & $py agent.py --derivatives; [void](Read-Host "Enter fuer weiter...") }
        }
    }
}

function Menu-Analysis {
    while ($true) {
        Write-Host "`n--- [2] DEEP-DIVE ANALYSE & STRATEGIE-LABOR ---" -ForegroundColor Cyan
        Write-Host "  [1] Schnell-Analyse BTC-USD (1d + DE-Steuer + FIFO)"
        Write-Host "  [2] Coin [1..15] & Zeitskala [1s..1mo] frei waehlen"
        Write-Host "  [3] System-Selbsttest (5/5 Gates)"
        Write-Host "  [4] Echtheits- & Beweis-Suite (--prove)"
        Write-Host "  [0] Zurueck ins Hauptmenue"
        $w = (Read-Host "Auswahl [0-4]").Trim()
        if ($w -eq "0") { return }
        switch ($w) {
            "1" { & $py agent.py BTC-USD --tf 1d; [void](Read-Host "Enter fuer weiter...") }
            "2" { $c = Select-Coin; $tf = Select-TF; & $py agent.py $c --tf $tf; [void](Read-Host "Enter fuer weiter...") }
            "3" { & $py agent.py --self-test; [void](Read-Host "Enter fuer weiter...") }
            "4" { & $py agent.py --prove; [void](Read-Host "Enter fuer weiter...") }
        }
    }
}

function Menu-Trading {
    while ($true) {
        $cfg = Ensure-Cfg
        Write-Host "`n--- [3] AUTONOMES TRADING & DAEMON ---" -ForegroundColor Cyan
        Write-Host "  [1] 1x Autonomer Paper-Trade (gegen echtes Live-L2-Orderbuch)"
        Write-Host "  [2] Autonomer Paper-Daemon (Dauerlauf - mit [Q] oder [ESC] sauber zurueck!)"
        Write-Host "  [3] LIVE-BOERSEN-HANDEL (Binance/Kraken - nur mit verifizierter API!)" -ForegroundColor Red
        Write-Host "  [0] Zurueck ins Hauptmenue"
        $w = (Read-Host "Auswahl [0-3]").Trim()
        if ($w -eq "0") { return }
        switch ($w) {
            "1" { & $py agent.py --trade --mode paper; [void](Read-Host "Enter fuer weiter...") }
            "2" {
                $iv = (Read-Host "Tick-Intervall in Sekunden (Enter = 60)").Trim()
                if (-not ($iv -match '^\d+$') -or [int]$iv -lt 1) { $iv = "60" }
                & $py agent.py --trade --mode paper --daemon --interval $iv
                [void](Read-Host "Enter fuer weiter...")
            }
            "3" {
                $ex = (Read-Host "Boerse [1=Binance, 2=Kraken]").Trim()
                $exName = if ($ex -eq "1") { "binance" } elseif ($ex -eq "2") { "kraken" } else { "" }
                if (-not $exName) { Write-Host "Ungueltige Auswahl." -ForegroundColor Red; continue }
                & $py agent.py --verify-exchange $exName
                $cfg2 = Ensure-Cfg
                $isOk = if ($exName -eq "binance") { $cfg2.exchanges.binance_verified } else { $cfg2.exchanges.kraken_verified }
                if (-not $isOk) {
                    Write-Host "ABBRUCH: Keine gueltige $exName-API verbunden! Bitte erst in Menue [6] einrichten." -ForegroundColor Red
                    [void](Read-Host "Enter fuer weiter..."); continue
                }
                $conf = (Read-Host "ECHTGELD auf $exName! Tippe exakt LIVE zum Starten").Trim()
                if ($conf -ceq "LIVE") {
                    & $py agent.py --trade --mode "live-$exName" --confirm-live
                } else {
                    Write-Host "Abgebrochen." -ForegroundColor Yellow
                }
                [void](Read-Host "Enter fuer weiter...")
            }
        }
    }
}

function Menu-Portfolio {
    while ($true) {
        Write-Host "`n--- [4] PORTFOLIO, POSITIONEN, TRADES & DE-STEUER ---" -ForegroundColor Cyan
        Write-Host "  [1] Offene Positionen, Trailing-Stops, PnL & DE-Steuerstatus"
        Write-Host "  [2] Kompletten Live Trade- & Event-Feed anzeigen"
        Write-Host "  [3] Paper-Konto zuruecksetzen & Startkapital neu festlegen"
        Write-Host "  [0] Zurueck ins Hauptmenue"
        $w = (Read-Host "Auswahl [0-3]").Trim()
        if ($w -eq "0") { return }
        switch ($w) {
            "1" { & $py agent.py --portfolio; [void](Read-Host "Enter fuer weiter...") }
            "2" { & $py agent.py --feed; [void](Read-Host "Enter fuer weiter...") }
            "3" {
                $cap = (Read-Host "Neues Startkapital in USD (Enter = 10000)").Trim()
                if (-not ($cap -match '^\d+(\.\d+)?$')) { $cap = "10000" }
                & $py agent.py --reset-paper --capital $cap --trade --mode paper
                [void](Read-Host "Enter fuer weiter...")
            }
        }
    }
}

function Menu-Settings {
    while ($true) {
        $cfg = Ensure-Cfg
        Write-Host "`n--- [5] RISIKO-, KAPITAL- & STRATEGIE-EINSTELLUNGEN ---" -ForegroundColor Cyan
        Write-Host ("  [1] Volatilitaets-Ziel  : {0} (z.B. 0.35=Konservativ, 0.60=Std, 0.85=Aggressiv)" -f $cfg.vol_target)
        Write-Host ("  [2] CVaR-95% Limit      : {0} | Hysterese: {1}" -f $cfg.cvar_limit, $cfg.hysteresis)
        Write-Host ("  [3] L2-Ordergroesse ($) : `${0} | Fee: {1} | Slip: {2}" -f $cfg.order_usd, $cfg.fee_rate, $cfg.slippage)
        Write-Host ("  [4] DE-Steuer           : Satz={0}% | FIFO-Haltefrist-Schutz={1}" -f [int]($cfg.tax_rate*100), $cfg.tax_aware)
        Write-Host "  [0] Zurueck ins Hauptmenue"
        $w = (Read-Host "Parameter aendern [0-4]").Trim()
        if ($w -eq "0") { return }
        switch ($w) {
            "1" { $v = (Read-Host "Neues Vol-Target (0.20 - 1.00)").Trim(); if ($v -match '^\d+(\.\d+)?$') { $cfg.vol_target = [double]$v; Save-CfgObj $cfg } }
            "2" {
                $cv = (Read-Host "CVaR-Limit (Enter=$($cfg.cvar_limit))").Trim(); if ($cv -match '^\d+(\.\d+)?$') { $cfg.cvar_limit = [double]$cv }
                $hy = (Read-Host "Hysterese (Enter=$($cfg.hysteresis))").Trim(); if ($hy -match '^\d+(\.\d+)?$') { $cfg.hysteresis = [double]$hy }
                Save-CfgObj $cfg
            }
            "3" {
                $ou = (Read-Host "Ordergroesse USD fuer L2-Slippage (Enter=$($cfg.order_usd))").Trim(); if ($ou -match '^\d+(\.\d+)?$') { $cfg.order_usd = [double]$ou }
                Save-CfgObj $cfg
            }
            "4" {
                $tr = (Read-Host "Steuersatz Dezimal (z.B. 0.42 oder 0.0, Enter=$($cfg.tax_rate))").Trim(); if ($tr -match '^\d+(\.\d+)?$') { $cfg.tax_rate = [double]$tr }
                $ta = (Read-Host "FIFO-365T-Schutz aktiv? [j/n]").Trim().ToLower(); if ($ta -eq "j") { $cfg.tax_aware = $true } elseif ($ta -eq "n") { $cfg.tax_aware = $false }
                Save-CfgObj $cfg
            }
        }
    }
}

function Menu-API {
    while ($true) {
        $cfg = Ensure-Cfg
        Write-Host "`n--- [6] API-CENTER: KI / LLM & BOERSEN-ANBINDUNG ---" -ForegroundColor Cyan
        Write-Host ("  [1] KI / LLM anschliessen & pruefen  (Aktuell: {0} / {1} | Gueltig: {2})" -f $cfg.llm.provider, $cfg.llm.model, $cfg.llm.verified)
        Write-Host ("  [2] Binance Spot API setzen & pruefen (Gueltig: {0})" -f $cfg.exchanges.binance_verified)
        Write-Host ("  [3] Kraken Spot API setzen & pruefen  (Gueltig: {0})" -f $cfg.exchanges.kraken_verified)
        Write-Host "  [4] Alle angeschlossenen APIs jetzt live testen"
        Write-Host "  [0] Zurueck ins Hauptmenue"
        $w = (Read-Host "Auswahl [0-4]").Trim()
        if ($w -eq "0") { return }
        switch ($w) {
            "1" {
                Write-Host "  Provider: [1] OpenAI  [2] Google Gemini  [3] Anthropic Claude  [4] OpenRouter  [5] Ollama (lokal)"
                $pm = (Read-Host "Provider [1-5]").Trim()
                $prov = switch ($pm) { "1"{"openai"} "2"{"gemini"} "3"{"anthropic"} "4"{"openrouter"} "5"{"ollama"} default{""} }
                if (-not $prov) { continue }
                $defM = switch ($prov) { "openai"{"gpt-4o-mini"} "gemini"{"gemini-2.5-flash"} "anthropic"{"claude-3-5-sonnet-latest"} "openrouter"{"openai/gpt-4o-mini"} "ollama"{"llama3.1"} }
                $mod = (Read-Host "Modell (Enter = $defM)").Trim(); if ([string]::IsNullOrWhiteSpace($mod)) { $mod = $defM }
                $key = if ($prov -eq "ollama") { "" } else { (Read-Host "API-Key eingeben").Trim() }
                $burl = if ($prov -eq "ollama") { (Read-Host "Ollama URL (Enter = http://localhost:11434)").Trim() } else { "" }
                $cfg.llm.provider = $prov; $cfg.llm.model = $mod; $cfg.llm.api_key = $key; $cfg.llm.base_url = $burl; $cfg.llm.verified = $false
                Save-CfgObj $cfg
                & $py agent.py --verify-llm
                [void](Read-Host "Enter fuer weiter...")
            }
            "2" {
                $k = (Read-Host "Binance API-Key").Trim()
                $s = (Read-Host "Binance API-Secret").Trim()
                $cfg.exchanges.binance_key = $k; $cfg.exchanges.binance_secret = $s; $cfg.exchanges.binance_verified = $false
                Save-CfgObj $cfg
                & $py agent.py --verify-exchange binance
                [void](Read-Host "Enter fuer weiter...")
            }
            "3" {
                $k = (Read-Host "Kraken API-Key").Trim()
                $s = (Read-Host "Kraken API-Secret (Base64)").Trim()
                $cfg.exchanges.kraken_key = $k; $cfg.exchanges.kraken_secret = $s; $cfg.exchanges.kraken_verified = $false
                Save-CfgObj $cfg
                & $py agent.py --verify-exchange kraken
                [void](Read-Host "Enter fuer weiter...")
            }
            "4" {
                & $py agent.py --verify-llm
                & $py agent.py --verify-exchange binance
                & $py agent.py --verify-exchange kraken
                [void](Read-Host "Enter fuer weiter...")
            }
        }
    }
}

while ($true) {
    Show-Header
    Write-Host "  [ 1] Marktscanner & 15-Coin-Auswahl       (Submenue: Scanner, 15 Coins, Watchlist, Derivate)"
    Write-Host "  [ 2] Deep-Dive Analyse & Strategie-Labor  (Submenue: BTC Schnellcheck, 1s..1mo, Beweise)"
    Write-Host "  [ 3] Autonomes Trading & Daemon           (Submenue: Paper, Daemon [Q-Stop], Live-Boerse)"
    Write-Host "  [ 4] Portfolio, Positionen & DE-Steuer    (Submenue: PnL, Trailing-Stops, Feed, Reset)"
    Write-Host "  [ 5] Risiko-, Kapital- & Strategie-Setup  (Submenue: Vol-Target, CVaR, Ordergroesse, Steuer)"
    Write-Host "  [ 6] API-Center: KI/LLM & Boersen-Keys    (Submenue: OpenAI/Gemini/Claude + Binance/Kraken)"
    Write-Host "  [ 7] System-Selbsttest                    (5/5 Gates direkt pruefen)"
    Write-Host "  [ 8] Echtheits- & Beweis-Suite            (5J Portfolio-Ablation, OOS-IC, 1s..1mo)"
    Write-Host "  [ 9] Live-Dashboard                       (Kompakt-Monitor: Konto, Markt, Top-5, Feed)"
    Write-Host "  [10] LIVE TRADING-CHAT & KI-BRUECKE       (KI + Agent Dialog + Live-Trade-Stream)" -ForegroundColor Green
    Write-Host "  [ 0] BEENDEN"
    Write-Host "========================================================================================" -ForegroundColor Cyan

    $wahl = (Read-Host "Menue waehlen [0-10]").Trim()
    switch ($wahl) {
        "1"  { Menu-Market }
        "2"  { Menu-Analysis }
        "3"  { Menu-Trading }
        "4"  { Menu-Portfolio }
        "5"  { Menu-Settings }
        "6"  { Menu-API }
        "7"  { & $py agent.py --self-test; [void](Read-Host "Enter fuer weiter...") }
        "8"  { & $py agent.py --prove; [void](Read-Host "Enter fuer weiter...") }
        "9"  { & $py agent.py --dashboard; [void](Read-Host "Enter fuer weiter...") }
        "10" { & $py agent.py --chat }
        "0"  { Write-Host "APEX-QUANT v9 beendet." -ForegroundColor Cyan; break }
        default { Write-Host "Ungueltige Eingabe '$wahl'." -ForegroundColor Red }
    }
    if ($wahl -eq "0") { break }
}
