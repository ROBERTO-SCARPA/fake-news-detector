/**
 * Veracity Layer — script.js
 * Frontend Fake News Detector
 * Auth  : Azure AD via MSAL.js
 * API   : Azure API Management (classify_news + scrape_and_classify)
 */

// ============================================================================
// CONFIGURAZIONE
// ============================================================================

const msalConfig = {
    auth: {
        clientId:    "b0f816de-ed48-4210-a721-31c4f9d46b33",
        authority:   "https://login.microsoftonline.com/9b9afd5e-422c-478f-8fd6-3cb65f0455d8",
        redirectUri: window.location.origin + "/blank.html"
    },
    cache: {
        cacheLocation:          "localStorage",
        storeAuthStateInCookie: false
    }
};

const loginRequest = {
    scopes: ["api://c5cdb33c-0318-4a0d-a66b-59a330aaabc5/Classify.News"]
};

const CONFIG = {
    CLASSIFY_URL: "https://apim-fakenews-2026.azure-api.net/fakenewsdetector/classify_news",
    SCRAPE_URL:   "https://apim-fakenews-2026.azure-api.net/fakenewsdetector/scrape_and_classify"
};

let msalInstance     = null;
let currentAccount   = null;
let progressInterval = null;

try {
    msalInstance = new msal.PublicClientApplication(msalConfig);
} catch (e) {
    console.error('[Veracity Layer] MSAL non disponibile:', e);
}

// ============================================================================
// INIZIALIZZAZIONE MSAL
// ============================================================================

(async function initializeMSAL() {
    if (!msalInstance) return;
    await msalInstance.initialize();
    await msalInstance.handleRedirectPromise();

    const accounts = msalInstance.getAllAccounts();
    if (accounts.length > 0) {
        currentAccount = accounts[0];
        updateUIForLoggedInUser();
    }
})();

// ============================================================================
// AUTENTICAZIONE
// ============================================================================

async function loginWithAzureAD() {
    if (!msalInstance) {
        alert('Autenticazione non disponibile: libreria MSAL non caricata.');
        return;
    }
    try {
        const resp     = await msalInstance.loginPopup(loginRequest);
        currentAccount = resp.account;
        updateUIForLoggedInUser();
        return resp;
    } catch (error) {
        if (error.errorCode === "user_cancelled")
            throw new Error("Login canceled by the user.");
        if (error.errorCode === "popup_window_error")
            throw new Error("Impossible to open the popup. Check your popup blocker settings.");
        throw new Error(`Login failed: ${error.errorMessage || error.message}`);
    }
}

async function getAccessToken() {
    if (!currentAccount) await loginWithAzureAD();

    const tokenRequest = { scopes: loginRequest.scopes, account: currentAccount };

    try {
        const resp = await msalInstance.acquireTokenSilent(tokenRequest);
        return resp.accessToken;
    } catch (_) {
        try {
            const resp = await msalInstance.acquireTokenPopup(tokenRequest);
            return resp.accessToken;
        } catch (err) {
            currentAccount = null;
            throw new Error("Impossible to obtain the token. Please try again.");
        }
    }
}

/**
 * Aggiorna la UI in base allo stato di autenticazione:
 * - mostra badge utente + bottone Logout
 * - nasconde bottone Login
 */
function updateUIForLoggedInUser() {
    const userInfo  = document.getElementById('fn-user-info');
    const loginBtn  = document.getElementById('fn-login-btn');
    const logoutBtn = document.getElementById('fn-logout-btn');

    if (currentAccount) {
        if (userInfo) {
            userInfo.textContent = `👤 ${currentAccount.username}`;
            userInfo.classList.add('fn-user-info--visible');
        }
        if (loginBtn)  loginBtn.classList.add('hidden');
        if (logoutBtn) logoutBtn.classList.remove('hidden');
    }
}

function logout() {
    msalInstance.logoutPopup();
    currentAccount = null;

    const userInfo  = document.getElementById('fn-user-info');
    const loginBtn  = document.getElementById('fn-login-btn');
    const logoutBtn = document.getElementById('fn-logout-btn');

    if (userInfo)  userInfo.classList.remove('fn-user-info--visible');
    if (loginBtn)  loginBtn.classList.remove('hidden');
    if (logoutBtn) logoutBtn.classList.add('hidden');

    resetGauge();
    resetDetailItems();
}

// ============================================================================
// TAB SWITCHER
// ============================================================================

function switchTab(tab) {
    ['text', 'url'].forEach(t => {
        const tabEl    = document.getElementById(`tab-${t}`);
        const panelEl  = document.getElementById(`panel-${t}`);
        const isActive = t === tab;

        tabEl.classList.toggle('fn-tab--active', isActive);
        tabEl.setAttribute('aria-selected', String(isActive));
        panelEl.classList.toggle('fn-panel--active', isActive);
    });

    // Nascondi risultato e reset gauge al cambio tab
    const result = document.getElementById('fn-result');
    if (result) {
        result.style.display = 'none';
        result.className = 'fn-result';
    }
    resetGauge();
}

// ============================================================================
// PROGRESS BAR
// ============================================================================

function showProgress(show) {
    const section = document.getElementById('fn-progress-section');
    if (!section) return;

    if (show) {
        section.classList.remove('hidden');
        startProgressAnimation();
    } else {
        stopProgressAnimation();
        setProgressBar(100);
        setTimeout(() => {
            section.classList.add('hidden');
            setProgressBar(0);
        }, 400);
    }
}

function startProgressAnimation() {
    let pct = 0;
    setProgressBar(0);
    progressInterval = setInterval(() => {
        // Avanza veloce fino a 50%, poi rallenta — aspetta la risposta API
        const step = pct < 50 ? 8 : pct < 75 ? 3 : 0.5;
        pct = Math.min(pct + step, 85);
        setProgressBar(pct);
    }, 200);
}

function stopProgressAnimation() {
    if (progressInterval) {
        clearInterval(progressInterval);
        progressInterval = null;
    }
}

function setProgressBar(pct) {
    const bar     = document.getElementById('fn-progress-bar');
    const pctText = document.getElementById('fn-progress-pct');
    if (bar)     bar.style.width        = `${pct}%`;
    if (pctText) pctText.textContent    = `${Math.round(pct)}%`;
}

// ============================================================================
// GAUGE SVG + TRUTH METER + STATUS LABEL
// ============================================================================

/**
 * Aggiorna il gauge SVG, il truth meter e lo status label
 * in base al risultato della classificazione.
 *
 * @param {number}  confidence  Valore 0–100
 * @param {boolean} isFake
 */
function updateGauge(confidence, isFake) {
    const circle         = document.getElementById('fn-gauge-circle');
    const scoreEl        = document.getElementById('fn-gauge-score');
    const statusLabel    = document.getElementById('fn-status-label');
    const statusText     = document.getElementById('fn-status-text');
    const truthIndicator = document.getElementById('fn-truth-indicator');

    if (!circle || !scoreEl || !statusLabel || !statusText || !truthIndicator) return;

    // --- SVG Gauge ---
    // Circonferenza del cerchio: 2 * π * r ≈ 628 (r=100)
    // stroke-dashoffset: 0 = cerchio pieno, 628 = cerchio vuoto
    const circumference = 628;
    const offset = circumference - (confidence / 100) * circumference;
    circle.style.strokeDashoffset = offset;

    // Colore arco in base allo stato
    if (isFake) {
        circle.style.stroke = '#ba1a1a';                    // error (brick red)
    } else if (confidence >= 80) {
        circle.style.stroke = '#004c10';                    // tertiary (verde)
    } else {
        circle.style.stroke = '#7ad7c6';                    // secondary-fixed-dim (teal incerto)
    }

    // --- Score text ---
    scoreEl.textContent = `${confidence.toFixed(1)}%`;
    scoreEl.style.color = isFake
        ? '#ba1a1a'
        : confidence >= 80 ? '#004c10' : '#006b5e';

    // --- Status Label ---
    // Reset classi modificatori, poi aggiunge quello corretto
    statusLabel.className = 'fn-status-label px-8 py-3 rounded-full flex flex-col items-center shadow-md';

    if (isFake) {
        statusLabel.classList.add('fn-status-label--fake');
        statusText.textContent = 'FAKE NEWS';
    } else if (confidence >= 80) {
        statusLabel.classList.add('fn-status-label--real');
        statusText.textContent = 'REAL NEWS';
    } else {
        statusLabel.classList.add('fn-status-label--uncertain');
        statusText.textContent = 'UNCERTAIN';
    }

    // --- Truth Meter Indicator ---
    // Fake con alta conf → sinistra (5–45%)
    // Real con alta conf → destra (55–95%)
    // Uncertain → centro (50%)
    let leftPct;
    if (isFake) {
        // Più è alta la confidenza fake, più va a sinistra
        leftPct = Math.max(5, Math.min(45, 100 - confidence));
    } else {
        // Più è alta la confidenza real, più va a destra
        leftPct = Math.max(55, Math.min(95, 50 + (confidence - 50) * 0.8));
    }
    truthIndicator.style.left = `${leftPct}%`;
}

/**
 * Riporta il gauge allo stato iniziale (AWAITING).
 */
function resetGauge() {
    const circle         = document.getElementById('fn-gauge-circle');
    const scoreEl        = document.getElementById('fn-gauge-score');
    const statusLabel    = document.getElementById('fn-status-label');
    const statusText     = document.getElementById('fn-status-text');
    const truthIndicator = document.getElementById('fn-truth-indicator');

    if (circle) {
        circle.style.strokeDashoffset = '314';
        circle.style.stroke = '';
    }
    if (scoreEl)         scoreEl.textContent  = '—';
    if (scoreEl)         scoreEl.style.color  = '';
    if (statusLabel)     statusLabel.className = 'fn-status-label fn-status-label--idle px-8 py-3 rounded-full flex flex-col items-center shadow-md';
    if (statusText)      statusText.textContent  = 'AWAITING';
    if (truthIndicator)  truthIndicator.style.left = '50%';
}

// ============================================================================
// DETAIL ITEMS — Analisi Semantica, Verifica Fonti, Confidenza Modello
// ============================================================================

/**
 * Aggiorna i tre blocchi descrittivi nella sidebar destra.
 */
function updateDetailItems(confidence, isFake, label) {
    const semanticEl   = document.getElementById('fn-detail-semantic-text');
    const sourcesEl    = document.getElementById('fn-detail-sources-text');
    const confidenceEl = document.getElementById('fn-detail-confidence-text');

    if (semanticEl) {
        if (isFake) {
            semanticEl.textContent =
                'The language presents typical traits of disinformation: polarized tone, claims without sources, and a sensationalistic structure.';
        } else if (confidence >= 80) {
            semanticEl.textContent =
                'The language is neutral and informative, with a structure typical of verified news channels.';
        } else {
            semanticEl.textContent =
                'The language presents some semantic ambiguities. It is recommended to perform additional verification before sharing.';
        }
    }

    if (sourcesEl) {
        if (isFake) {
            sourcesEl.textContent =
                'No direct correlation found in the databases of verified news agencies. Possible unfounded content.';
        } else if (confidence >= 80) {
            sourcesEl.textContent =
                'The content is consistent with reliable news sources present in the training dataset.';
        } else {
            sourcesEl.textContent =
                'Partial correlation with verified sources. Insufficient confidence for a definitive classification.';
        }
    }

    if (confidenceEl) {
        const tier = confidence >= 80 ? 'High' : confidence >= 60 ? 'Medium' : 'Low';
        confidenceEl.textContent =
            `Confidence ${tier} (${confidence.toFixed(1)}%) — Model Classification: ${label}`;
    }
}

/**
 * Riporta i detail items allo stato iniziale.
 */
function resetDetailItems() {
    const semanticEl   = document.getElementById('fn-detail-semantic-text');
    const sourcesEl    = document.getElementById('fn-detail-sources-text');
    const confidenceEl = document.getElementById('fn-detail-confidence-text');

    if (semanticEl)   semanticEl.textContent   = "In attesa dei risultati dell'analisi...";
    if (sourcesEl)    sourcesEl.textContent    = "Nessuna analisi eseguita.";
    if (confidenceEl) confidenceEl.textContent = "Nessuna analisi eseguita.";
}

// ============================================================================
// CLASSIFY NEWS (testo diretto)
// ============================================================================

async function classifyNews() {
    const resultDiv = document.getElementById('fn-result');
    const btn       = document.getElementById('fn-analyze-btn');
    const text      = document.getElementById('fn-news-text').value.trim();

    if (!text) {
        setSimpleState(resultDiv, 'fn-result--warning',
            '⚠️ Please enter some text before starting the analysis.');
        return;
    }

    setLoading(btn, '⏳ Authentication...');
    resetDetailItems();
    showProgress(true);
    setSimpleState(resultDiv, 'fn-result--loading',
        '<span class="fn-loading-spinner"></span> Authentication in progress...'
    );

    try {
        const token = await getAccessToken();

        updateBtnText(btn, '⏳ Analysis in progress...');
        setSimpleState(resultDiv, 'fn-result--loading',
            '<span class="fn-loading-spinner"></span> Analysis in progress...'
        );

        const response = await fetch(CONFIG.CLASSIFY_URL, {
            method: 'POST',
            headers: {
                'Content-Type':  'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify({ text })
        });

        const data = await parseResponse(response);
        showProgress(false);
        renderClassificationResult(resultDiv, data, null);

    } catch (err) {
        showProgress(false);
        resetGauge();
        setSimpleState(resultDiv, 'fn-result--error', `❌ ${err.message}`);
    } finally {
        resetBtn(btn, 'Inizia Analisi');
    }
}

// ============================================================================
// SCRAPE AND CLASSIFY (URL)
// ============================================================================

async function scrapeAndClassify() {
    const resultDiv = document.getElementById('fn-result');
    const btn       = document.getElementById('fn-scrape-btn');
    const url       = document.getElementById('fn-news-url').value.trim();

    if (!url) {
        setSimpleState(resultDiv, 'fn-result--warning',
            '⚠️ Please enter a URL before starting the analysis.');
        return;
    }

    if (!url.startsWith('http://') && !url.startsWith('https://')) {
        setSimpleState(resultDiv, 'fn-result--warning',
            '⚠️ The URL must start with http:// or https://');
        return;
    }

    setLoading(btn, '⏳ Authentication...');
    resetDetailItems();
    showProgress(true);
    setSimpleState(resultDiv, 'fn-result--loading',
        '<span class="fn-loading-spinner"></span> Authentication in progress...'
    );

    try {
        const token = await getAccessToken();

        updateBtnText(btn, '⏳ Scraping pagina...');
        setSimpleState(resultDiv, 'fn-result--loading',
            '<span class="fn-loading-spinner"></span> Download and page analysis in progress...'
        );

        const response = await fetch(CONFIG.SCRAPE_URL, {
            method: 'POST',
            headers: {
                'Content-Type':  'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify({ url })
        });

        const data = await parseResponse(response);
        showProgress(false);
        // data.classification = { is_fake, label, confidence }
        // data.extracted_text = testo estratto
        renderClassificationResult(resultDiv, data.classification, data.extracted_text, url);

    } catch (err) {
        showProgress(false);
        resetGauge();
        setSimpleState(resultDiv, 'fn-result--error', `❌ ${err.message}`);
    } finally {
        resetBtn(btn, 'Analyze URL');
    }
}

// ============================================================================
// RENDER RISULTATO
// Aggiorna: result box, gauge SVG, truth meter, status label, detail items
// ============================================================================

function renderClassificationResult(resultDiv, data, extractedText = null, sourceUrl = null) {
    if (!data || typeof data.is_fake !== 'boolean' || typeof data.confidence !== 'number') {
        setSimpleState(resultDiv, 'fn-result--error', 'Risposta API non valida.');
        resetGauge();
        return;
    }

    const isFake     = data.is_fake;
    const confidence = Math.max(0, Math.min(100, data.confidence * 100));
    const safeLabel  = sanitize(data.label?.toUpperCase() ?? (isFake ? 'FAKE' : 'REAL'));

    // Aggiorna sidebar destra
    updateGauge(confidence, isFake);
    updateDetailItems(confidence, isFake, safeLabel);

    // Warning bassa confidenza
    const warningHtml = confidence < 80 ? `
        <div class="fn-result__warning">
            ⚠️ <strong>Low confidence (${confidence.toFixed(1)}%):</strong>
            The model is not sure about the result. Please verify the source manually.
        </div>` : '';

    // Testo estratto dalla pagina (URL analysis)
    const extractedHtml = extractedText ? `
        <div class="fn-result__extracted">
            <div class="fn-result__extracted-label">
                📄 Extracted text from the page
                <button type="button" class="fn-result__extracted-toggle"
                        data-toggle-extracted>Show all</button>
            </div>
            <div class="fn-result__extracted-text">
                ${sanitize(extractedText)}
            </div>
        </div>` : '';

    // Fonte URL
    const sourceHtml = sourceUrl ? `
        <p class="fn-result__classification">
            <strong>Source:</strong>
            <a href="${sanitize(sourceUrl)}" target="_blank" rel="noopener noreferrer"
               style="color: var(--vl-primary); word-break: break-all;">
                ${sanitize(sourceUrl)}
            </a>
        </p>` : '';

    const html = `
        <div class="fn-result__header">
            <span class="fn-result__header-icon">${isFake ? '❌' : '✅'}</span>
            <span class="fn-result__label">${isFake ? 'FAKE NEWS' : 'REAL NEWS'}</span>
        </div>
        <div class="fn-result__body">
            <p class="fn-result__confidence-text">
                <strong>Model confidence:</strong> ${confidence.toFixed(1)}%
            </p>
            <div class="fn-result__confidence-bar">
                <div class="fn-result__confidence-fill
                    ${isFake
                        ? 'fn-result__confidence-fill--fake'
                        : 'fn-result__confidence-fill--real'}"
                     style="--fn-confidence: ${confidence.toFixed(1)}%">
                </div>
            </div>
            ${warningHtml}
            <p class="fn-result__classification">
                <strong>Classification:</strong> ${safeLabel}
            </p>
            ${sourceHtml}
            ${extractedHtml}
            <p class="fn-result__disclaimer">
                ℹ️ The model is primarily trained on U.S. political news in English.
                The performance on articles in other languages or topics may be lower.
            </p>
        </div>`;

    resultDiv.innerHTML = typeof DOMPurify !== 'undefined'
        ? DOMPurify.sanitize(html)
        : html;

    resultDiv.className    = `fn-result ${isFake ? 'fn-result--fake' : 'fn-result--real'}`;
    resultDiv.style.display = 'block';
}

// ============================================================================
// HELPERS
// ============================================================================

async function parseResponse(response) {
    let errorMessage = `Server error (HTTP ${response.status})`;
    if (!response.ok) {
        try {
            const err = await response.json();
            if (response.status === 400 && err.error) {
                errorMessage = err.error;
                if (err.word_count !== undefined)
                    errorMessage += ` (${err.word_count} words detected)`;
            } else if (response.status === 401) {
                errorMessage = '🔐 Authentication expired. Please try again.';
                currentAccount = null;
            } else if (response.status === 403) {
                errorMessage = '⛔ You do not have permissions for this API.';
            } else if (response.status === 429) {
                errorMessage = '⏱️ Too many requests. Please try again in a few minutes.';
            } else if (response.status === 502 || response.status === 504) {
                errorMessage = '🌐 Impossible to reach the remote page.';
            } else if (response.status === 503) {
                errorMessage = '🛠️ Service temporarily unavailable.';
            } else if (err.error) {
                errorMessage = err.error;
            }
        } catch (_) {}
        throw new Error(errorMessage);
    }
    return response.json();
}

function setSimpleState(el, cls, html) {
    el.className        = `fn-result ${cls}`;
    el.innerHTML        = `<div class="fn-result__simple">${html}</div>`;
    el.style.display    = 'block';
}

function setLoading(btn, label) {
    btn.disabled = true;
    btn.classList.add('fn-btn--loading');
    btn.textContent = label;
}

function updateBtnText(btn, label) {
    btn.textContent = label;
}

function resetBtn(btn, label) {
    btn.disabled = false;
    btn.classList.remove('fn-btn--loading');
    btn.textContent = label;
}

function sanitize(str) {
    if (typeof DOMPurify !== 'undefined') return DOMPurify.sanitize(String(str));
    return String(str).replace(/[<>&"']/g, c => ({
        '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// ============================================================================
// EVENT LISTENERS
// ============================================================================

// Ctrl/Cmd + Enter su textarea → analisi testo
document.getElementById('fn-news-text').addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        classifyNews();
    }
});

// Enter su input URL → analisi URL
document.getElementById('fn-news-url').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
        e.preventDefault();
        scrapeAndClassify();
    }
});

// Word count in tempo reale sulla textarea
document.getElementById('fn-news-text').addEventListener('input', function () {
    const words   = this.value.trim() ? this.value.trim().split(/\s+/).length : 0;
    const counter = document.getElementById('fn-word-count');
    if (counter) {
        counter.textContent = `${words} words`;
        counter.classList.toggle('fn-word-count--warn', words > 0 && words < 10);
    }
});

// Event delegation — toggle espansione testo estratto
document.getElementById('fn-result').addEventListener('click', function (e) {
    const btn = e.target.closest('[data-toggle-extracted]');
    if (!btn) return;
    const textEl   = btn.closest('.fn-result__extracted')
                        .querySelector('.fn-result__extracted-text');
    const expanded = textEl.classList.toggle('fn-expanded');
    btn.textContent = expanded ? 'SShow less' : 'Show all';
});
