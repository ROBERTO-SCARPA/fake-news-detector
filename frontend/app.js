/**
 * Frontend Fake News Detector
 * Supporta: classify_news (testo) + scrape_and_classify (URL)
 * Auth: Azure AD via MSAL.js
 */

// ============================================================================
// CONFIGURAZIONE
// ============================================================================

const msalConfig = {
    auth: {
        clientId:    "b0f816de-ed48-4210-a721-31c4f9d46b33",
        authority:   "https://login.microsoftonline.com/9b9afd5e-422c-478f-8fd6-3cb65f0455d8",
        redirectUri: window.location.origin
    },
    cache: {
        cacheLocation:        "localStorage",
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

const msalInstance = new msal.PublicClientApplication(msalConfig);
let currentAccount = null;

// ============================================================================
// INIZIALIZZAZIONE MSAL
// ============================================================================

(async function initializeMSAL() {
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
    try {
        const resp = await msalInstance.loginPopup(loginRequest);
        currentAccount = resp.account;
        updateUIForLoggedInUser();
        return resp;
    } catch (error) {
        if (error.errorCode === "user_cancelled")
            throw new Error("Login annullato dall'utente.");
        if (error.errorCode === "popup_window_error")
            throw new Error("Impossibile aprire il popup. Verifica il popup blocker.");
        throw new Error(`Login fallito: ${error.errorMessage || error.message}`);
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
            throw new Error("Impossibile ottenere il token. Riprova.");
        }
    }
}

function updateUIForLoggedInUser() {
    const el = document.getElementById('fn-user-info');
    if (el && currentAccount) {
        el.textContent = `👤 ${currentAccount.username}`;
        el.classList.add('fn-user-info--visible');
    }
}

function logout() {
    msalInstance.logoutPopup();
    currentAccount = null;
    const el = document.getElementById('fn-user-info');
    if (el) el.classList.remove('fn-user-info--visible');
}

// ============================================================================
// TAB SWITCHER
// ============================================================================

function switchTab(tab) {
    ['text', 'url'].forEach(t => {
        document.getElementById(`tab-${t}`).classList.toggle('fn-tab--active', t === tab);
        document.getElementById(`tab-${t}`).setAttribute('aria-selected', t === tab);
        document.getElementById(`panel-${t}`).classList.toggle('fn-panel--active', t === tab);
    });

    // Nascondi il risultato precedente al cambio tab
    const result = document.getElementById('fn-result');
    result.style.display = 'none';
    result.className = 'fn-result';
}

// ============================================================================
// CLASSIFY NEWS (testo diretto)
// ============================================================================

async function classifyNews() {
    const resultDiv = document.getElementById('fn-result');
    const btn       = document.getElementById('fn-analyze-btn');
    const text      = document.getElementById('fn-news-text').value.trim();

    if (!text) {
        setSimpleState(resultDiv, 'fn-result--warning', '⚠️ Inserisci un testo prima di analizzare.');
        return;
    }

    setLoading(btn, 'fn-analyze-btn', '⏳ Autenticazione...');
    setSimpleState(resultDiv, 'fn-result--loading',
        '<span class="fn-loading-spinner"></span> Autenticazione in corso...'
    );

    try {
        const token = await getAccessToken();

        updateBtnText(btn, '⏳ Analisi in corso...');
        setSimpleState(resultDiv, 'fn-result--loading',
            '<span class="fn-loading-spinner"></span> Analisi del testo in corso...'
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
        renderClassificationResult(resultDiv, data, null);

    } catch (err) {
        setSimpleState(resultDiv, 'fn-result--error', `❌ ${err.message}`);
    } finally {
        resetBtn(btn, 'Analizza Testo');
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
        setSimpleState(resultDiv, 'fn-result--warning', '⚠️ Inserisci un URL prima di analizzare.');
        return;
    }

    if (!url.startsWith('http://') && !url.startsWith('https://')) {
        setSimpleState(resultDiv, 'fn-result--warning', '⚠️ L\'URL deve iniziare con http:// o https://');
        return;
    }

    setLoading(btn, 'fn-scrape-btn', '⏳ Autenticazione...');
    setSimpleState(resultDiv, 'fn-result--loading',
        '<span class="fn-loading-spinner"></span> Autenticazione in corso...'
    );

    try {
        const token = await getAccessToken();

        updateBtnText(btn, '⏳ Scraping pagina...');
        setSimpleState(resultDiv, 'fn-result--loading',
            '<span class="fn-loading-spinner"></span> Download e analisi della pagina in corso...'
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

        // data.classification contiene { is_fake, label, confidence }
        // data.extracted_text contiene il testo estratto
        renderClassificationResult(resultDiv, data.classification, data.extracted_text, url);

    } catch (err) {
        setSimpleState(resultDiv, 'fn-result--error', `❌ ${err.message}`);
    } finally {
        resetBtn(btn, 'Analizza URL');
    }
}

// ============================================================================
// RENDER RISULTATO
// ============================================================================

function renderClassificationResult(resultDiv, data, extractedText = null, sourceUrl = null) {
    if (!data || typeof data.is_fake !== 'boolean' || typeof data.confidence !== 'number') {
        setSimpleState(resultDiv, 'fn-result--error', '❌ Risposta API non valida.');
        return;
    }

    const isFake     = data.is_fake;
    const confidence = Math.max(0, Math.min(100, data.confidence * 100)).toFixed(1);
    const safeLabel  = sanitize(data.label?.toUpperCase() ?? (isFake ? 'FAKE' : 'REAL'));

    const warningHtml = confidence < 70 ? `
        <div class="fn-result__warning">
            ⚠️ <strong>Confidenza bassa (${confidence}%):</strong> 
            Il modello non è sicuro del risultato. Verifica la fonte manualmente.
        </div>` : '';

    const extractedHtml = extractedText ? `
        <div class="fn-result__extracted">
            <div class="fn-result__extracted-label">
                📄 Testo estratto dalla pagina
                <button class="fn-result__extracted-toggle"
                        onclick="toggleExtracted(this)">Mostra tutto</button>
            </div>
            <div class="fn-result__extracted-text">
                ${sanitize(extractedText)}
            </div>
        </div>` : '';

    const sourceHtml = sourceUrl ? `
        <p class="fn-result__classification">
            <strong>Fonte:</strong> 
            <a href="${sanitize(sourceUrl)}" target="_blank" rel="noopener noreferrer"
               style="color:#667eea; word-break:break-all;">${sanitize(sourceUrl)}</a>
        </p>` : '';

    const html = `
        <div class="fn-result__header">
            <span class="fn-result__header-icon">${isFake ? '❌' : '✅'}</span>
            <span class="fn-result__label">${isFake ? 'FAKE NEWS' : 'NEWS REALE'}</span>
        </div>
        <div class="fn-result__body">
            <p class="fn-result__confidence-text">
                <strong>Confidenza del modello:</strong> ${confidence}%
            </p>
            <div class="fn-result__confidence-bar">
                <div class="fn-result__confidence-fill 
                     ${isFake ? 'fn-result__confidence-fill--fake' : 'fn-result__confidence-fill--real'}"
                     style="--fn-confidence: ${confidence}%">
                </div>
            </div>
            ${warningHtml}
            <p class="fn-result__classification">
                <strong>Classificazione:</strong> ${safeLabel}
            </p>
            ${sourceHtml}
            ${extractedHtml}
            <p class="fn-result__disclaimer">
                ℹ️ Il modello è addestrato prevalentemente su notizie in lingua inglese.
                Le prestazioni su notizie in altre lingue potrebbero essere inferiori.
            </p>
        </div>`;

    resultDiv.innerHTML = typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(html) : html;
    resultDiv.className = `fn-result ${isFake ? 'fn-result--fake' : 'fn-result--real'}`;
    resultDiv.style.display = 'block';
}

// ============================================================================
// TOGGLE TESTO ESTRATTO
// ============================================================================

function toggleExtracted(btn) {
    const textEl = btn.closest('.fn-result__extracted').querySelector('.fn-result__extracted-text');
    const expanded = textEl.classList.toggle('fn-expanded');
    btn.textContent = expanded ? 'Mostra meno' : 'Mostra tutto';
}

// ============================================================================
// HELPERS
// ============================================================================

async function parseResponse(response) {
    let errorMessage = `Errore server (HTTP ${response.status})`;
    if (!response.ok) {
        try {
            const err = await response.json();
            if (response.status === 400 && err.error) {
                errorMessage = err.error;
                if (err.word_count !== undefined)
                    errorMessage += ` (${err.word_count} parole rilevate)`;
            } else if (response.status === 401) {
                errorMessage = '🔐 Autenticazione scaduta. Riprova.';
                currentAccount = null;
            } else if (response.status === 403) {
                errorMessage = '⛔ Non hai i permessi per questa API.';
            } else if (response.status === 429) {
                errorMessage = '⏱️ Troppe richieste. Riprova tra qualche minuto.';
            } else if (response.status === 502 || response.status === 504) {
                errorMessage = '🌐 Impossibile raggiungere la pagina remota.';
            } else if (response.status === 503) {
                errorMessage = '🛠️ Servizio temporaneamente non disponibile.';
            } else if (err.error) {
                errorMessage = err.error;
            }
        } catch (_) {}
        throw new Error(errorMessage);
    }
    return response.json();
}

function setSimpleState(el, cls, html) {
    el.className = `fn-result ${cls}`;
    el.innerHTML = `<div class="fn-result__simple">${html}</div>`;
    el.style.display = 'block';
}

function setLoading(btn, _id, label) {
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
    if (typeof DOMPurify !== 'undefined') return DOMPurify.sanitize(str);
    return String(str).replace(/[<>]/g, '');
}

// ============================================================================
// EVENT LISTENERS
// ============================================================================

document.getElementById('fn-news-text').addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        classifyNews();
    }
});

document.getElementById('fn-news-url').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
        e.preventDefault();
        scrapeAndClassify();
    }
});

// Word count in tempo reale
document.getElementById('fn-news-text').addEventListener('input', function () {
    const words = this.value.trim() ? this.value.trim().split(/\s+/).length : 0;
    const counter = document.getElementById('fn-word-count');
    counter.textContent = `${words} parole`;
    counter.classList.toggle('fn-word-count--warn', words > 0 && words < 10);
});
