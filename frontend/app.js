/**
 * Frontend per classificazione fake news con autenticazione Azure AD.
 * Utilizza MSAL.js per ottenere JWT token OAuth 2.0 e chiama direttamente APIM.
 */

// ============================================================================
// CONFIGURAZIONE AZURE AD (MSAL)
// ============================================================================

const msalConfig = {
    auth: {
        // Client ID di FakeNewsWebClient
        clientId: "b0f816de-ed48-4210-a721-31c4f9d46b33",
        
        // Tenant ID 
        authority: "https://login.microsoftonline.com/9b9afd5e-422c-478f-8fd6-3cb65f0455d8",
        
        redirectUri: window.location.origin
    },
    cache: {
        cacheLocation: "localStorage",
        storeAuthStateInCookie: false
    }
};

// Scope con Application ID URI reale (dal manifest di FakeNewsDetectorAPI)
const loginRequest = {
    scopes: ["api://c5cdb33c-0318-4a0d-a66b-59a330aaabc5/Classify.News"]
};

const msalInstance = new msal.PublicClientApplication(msalConfig);
let currentAccount = null;

(async function initializeMSAL() {
    await msalInstance.initialize();
    await msalInstance.handleRedirectPromise();
    
    const accounts = msalInstance.getAllAccounts();
    if (accounts.length > 0) {
        currentAccount = accounts[0];
        console.log("✅ Utente già autenticato:", currentAccount.username);
        updateUIForLoggedInUser();
    } else {
        console.log("⚠️ Nessun utente loggato. Login richiesto al primo utilizzo.");
    }
})();

// ============================================================================
// CONFIGURAZIONE API
// ============================================================================

const CONFIG = {
    API_URL: "https://apim-fakenews-2026.azure-api.net/fakenewsdetector/classify_news"
};

// ============================================================================
// FUNZIONI DI AUTENTICAZIONE
// ============================================================================

async function loginWithAzureAD() {
    try {
        console.log("→ Apertura popup di login Azure AD...");
        const loginResponse = await msalInstance.loginPopup(loginRequest);
        currentAccount = loginResponse.account;
        console.log("✅ Login completato:", currentAccount.username);
        updateUIForLoggedInUser();
        return loginResponse;
    } catch (error) {
        console.error("❌ Login fallito:", error);
        if (error.errorCode === "user_cancelled") {
            throw new Error("Login annullato dall'utente.");
        } else if (error.errorCode === "popup_window_error") {
            throw new Error("Impossibile aprire popup. Verifica popup blocker del browser.");
        } else {
            throw new Error(`Login fallito: ${error.errorMessage || error.message}`);
        }
    }
}

async function getAccessToken() {
    if (!currentAccount) {
        console.log("⚠️ Nessun account loggato. Esecuzione login...");
        await loginWithAzureAD();
    }
    
    const tokenRequest = {
        scopes: loginRequest.scopes,
        account: currentAccount
    };
    
    try {
        console.log("→ Tentativo acquisizione token silenziosa (da cache)...");
        const tokenResponse = await msalInstance.acquireTokenSilent(tokenRequest);
        console.log("✅ Token acquisito da cache (silenzioso)");
        return tokenResponse.accessToken;
    } catch (silentError) {
        console.warn("⚠️ Acquisizione silenziosa fallita:", silentError.errorCode);
        try {
            const tokenResponse = await msalInstance.acquireTokenPopup(tokenRequest);
            console.log("✅ Token acquisito tramite popup interattivo");
            return tokenResponse.accessToken;
        } catch (interactiveError) {
            console.error("❌ Acquisizione token fallita:", interactiveError);
            currentAccount = null;
            throw new Error("Impossibile ottenere token di autenticazione. Riprova.");
        }
    }
}

function updateUIForLoggedInUser() {
    const userInfoDiv = document.getElementById('fn-user-info');
    if (userInfoDiv && currentAccount) {
        userInfoDiv.textContent = `👤 Loggato come: ${currentAccount.username}`;
        userInfoDiv.classList.add('fn-user-info--visible');
    }
}

// ============================================================================
// CLASSIFICAZIONE NEWS
// ============================================================================

async function classifyNews() {
    const resultDiv   = document.getElementById('fn-result');
    const btn         = document.getElementById('fn-analyze-btn');
    const textArea    = document.getElementById('fn-news-text');
    const text        = textArea.value.trim();

    // --- Validazione input ---
    if (!text) {
        setResultState(resultDiv, 'fn-result--warning', '⚠️ Inserisci un testo prima di analizzare');
        return;
    }

    // --- Loading state ---
    btn.disabled = true;
    btn.classList.add('fn-btn--loading');
    btn.textContent = '⏳ Autenticazione...';
    setResultState(resultDiv, 'fn-result--loading',
        '<span class="fn-loading-spinner"></span> Autenticazione in corso...'
    );

    try {
        // STEP 1: JWT token
        console.log("→ Inizio acquisizione JWT token...");
        const accessToken = await getAccessToken();
        console.log("✅ Token JWT acquisito:", accessToken.substring(0, 50) + "...");

        btn.textContent = '⏳ Analizzando...';
        setResultState(resultDiv, 'fn-result--loading',
            '<span class="fn-loading-spinner"></span> Analisi del testo in corso...'
        );

        // STEP 2: Chiamata APIM
        console.log("→ Invio richiesta POST ad APIM...");
        const response = await fetch(CONFIG.API_URL, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${accessToken}`
            },
            body: JSON.stringify({ text })
        });

        console.log("← Risposta ricevuta, status:", response.status);

        // --- Gestione errori HTTP ---
        if (!response.ok) {
            let errorMessage = `Errore server (HTTP ${response.status})`;
            try {
                const errorData = await response.json();
                if (response.status === 400 && errorData.error) {
                    errorMessage = errorData.error;
                    if (errorData.word_count !== undefined) {
                        errorMessage += ` (Rilevate ${errorData.word_count} parole)`;
                    }
                } else if (response.status === 401) {
                    errorMessage = '🔐 Autenticazione fallita. Prova a effettuare nuovamente il login.';
                    currentAccount = null;
                } else if (response.status === 403) {
                    errorMessage = '⛔ Non hai i permessi necessari per questa API.';
                } else if (response.status === 429) {
                    errorMessage = '⏱️ Troppe richieste. Riprova tra qualche minuto.';
                } else if (response.status === 503) {
                    errorMessage = '🛠️ Servizio temporaneamente non disponibile.';
                } else if (errorData.error) {
                    errorMessage = errorData.error;
                }
            } catch (_) {}
            throw new Error(errorMessage);
        }

        // --- Parsing risposta ---
        const data = await response.json();
        console.log("✅ Classificazione completata:", data);

        if (!data || typeof data.is_fake !== 'boolean' || typeof data.confidence !== 'number') {
            throw new Error('Risposta API malformata (campi mancanti)');
        }

        // --- Costruzione risultato ---
        const isFake     = data.is_fake;
        const confidence = Math.max(0, Math.min(100, data.confidence * 100)).toFixed(1);
        const safeLabel  = typeof DOMPurify !== 'undefined'
            ? DOMPurify.sanitize(data.label.toUpperCase())
            : data.label.toUpperCase().replace(/[<>]/g, '');

        const confidenceWarning = confidence < 70 ? `
            <p class="fn-result__warning">
                ⚠️ <strong>Confidenza bassa:</strong> Il modello non è sicuro del risultato.
                Verifica manualmente la fonte.
            </p>` : '';

        const resultHTML = `
            <div class="fn-result__label ${isFake ? 'fn-result__label--fake' : 'fn-result__label--real'}">
                ${isFake ? '❌ FAKE NEWS' : '✅ NEWS REALE'}
            </div>
            <div class="fn-result__details">
                <p class="fn-result__confidence-text">
                    <strong>Confidenza del modello:</strong> ${confidence}%
                </p>
                <div class="fn-result__confidence-bar">
                    <div class="fn-result__confidence-fill ${isFake ? 'fn-result__confidence-fill--fake' : 'fn-result__confidence-fill--real'}"
                         style="--fn-confidence: ${confidence}%">
                    </div>
                </div>
                ${confidenceWarning}
                <p class="fn-result__classification">
                    <strong>Classificazione:</strong> ${safeLabel}
                </p>
                <p class="fn-result__disclaimer">
                    ℹ️ <strong>Nota:</strong> Il modello è addestrato su notizie USA.
                    Le prestazioni su notizie internazionali potrebbero essere inferiori.
                </p>
            </div>`;

        resultDiv.innerHTML = typeof DOMPurify !== 'undefined'
            ? DOMPurify.sanitize(resultHTML)
            : resultHTML;
        resultDiv.className = `fn-result ${isFake ? 'fn-result--fake' : 'fn-result--real'}`;
        resultDiv.style.display = 'block';

    } catch (error) {
        console.error("❌ Errore durante classificazione:", error);
        setResultState(resultDiv, 'fn-result--error', `❌ ${error.message}`);
    } finally {
        btn.disabled = false;
        btn.classList.remove('fn-btn--loading');
        btn.textContent = 'Analizza Testo';
    }
}

// ============================================================================
// HELPERS
// ============================================================================

/**
 * Imposta stato del div risultato con classe e contenuto.
 * @param {HTMLElement} el    - elemento risultato
 * @param {string}      cls   - classe CSS BEM da applicare
 * @param {string}      html  - contenuto HTML da mostrare
 */
function setResultState(el, cls, html) {
    el.className = `fn-result ${cls}`;
    el.innerHTML = html;
    el.style.display = 'block';
}

// ============================================================================
// EVENT LISTENERS
// ============================================================================

// Ctrl+Enter (o Cmd+Enter) per inviare
document.getElementById('fn-news-text').addEventListener('keydown', function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        classifyNews();
    }
});

// Logout
function logout() {
    msalInstance.logoutPopup();
    currentAccount = null;
    const userInfoDiv = document.getElementById('fn-user-info');
    if (userInfoDiv) userInfoDiv.classList.remove('fn-user-info--visible');
}
