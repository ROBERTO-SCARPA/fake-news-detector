/**
 * Frontend per classificazione fake news con autenticazione Azure AD.
 * Utilizza MSAL.js per ottenere JWT token OAuth 2.0 e chiama direttamente APIM.
 * La sicurezza è gestita da Azure AD (token) + APIM (validazione).
 */

// ============================================================================
// CONFIGURAZIONE AZURE AD (MSAL)
// ============================================================================

const msalConfig = {
    auth: {
        // Client ID dell'App Registration "FakeNewsWebClient" 
        clientId: "b0f816de-ed48-4210-a721-31c4f9d46b33",
        
        // Authority: Azure AD endpoint per il tuo tenant
        authority: "https://login.microsoftonline.com/9b9afd5e-422c-478f-8fd6-3cb65f0455d8",
        
        // Redirect URI
        redirectUri: window.location.origin  
    },
    cache: {
        cacheLocation: "localStorage",  // Salva token nel localStorage (persistente tra sessioni)
        storeAuthStateInCookie: false   // Non usare cookie (SPA non ne ha bisogno)
    }
};

// Scope richiesto: permesso di chiamare l'API 
const loginRequest = {
    scopes: ["api://fakenews-api/Classify.News"]  
};

// Inizializza MSAL Public Client Application
const msalInstance = new msal.PublicClientApplication(msalConfig);
let currentAccount = null;  // Account attualmente loggato (null = non loggato)

// Inizializza MSAL quando la pagina carica
(async function initializeMSAL() {
    await msalInstance.initialize();
    
    // Controlla se c'è già un account loggato (da sessione precedente)
    const accounts = msalInstance.getAllAccounts();
    if (accounts.length > 0) {
        currentAccount = accounts[0];
        console.log("✅ Utente già autenticato:", currentAccount.username);
        updateUIForLoggedInUser();  // Opzionale: mostra nome utente nell'UI
    } else {
        console.log("⚠️ Nessun utente loggato. Login richiesto al primo utilizzo.");
    }
})();

// ============================================================================
// CONFIGURAZIONE API MANAGEMENT
// ============================================================================

const CONFIG = {
    // URL dell'endpoint APIM
    API_URL: "https://apim-fakenews-2026.azure-api.net/fakenewsdetector/classify_news"
};

// ============================================================================
// FUNZIONI DI AUTENTICAZIONE
// ============================================================================

/**
 * Esegue login con Azure AD tramite popup.
 * L'utente vedrà una finestra popup Microsoft per inserire email/password.
 * Questa funzione viene chiamata automaticamente se l'utente non è loggato.
 */
async function loginWithAzureAD() {
    try {
        console.log("→ Apertura popup di login Azure AD...");
        
        // Apre popup Microsoft per login
        const loginResponse = await msalInstance.loginPopup(loginRequest);
        
        // Salva account loggato
        currentAccount = loginResponse.account;
        
        console.log("✅ Login completato:", currentAccount.username);
        updateUIForLoggedInUser();  // Opzionale: aggiorna UI
        
        return loginResponse;
        
    } catch (error) {
        console.error("❌ Login fallito:", error);
        
        // Gestisci errori specifici
        if (error.errorCode === "user_cancelled") {
            throw new Error("Login annullato dall'utente.");
        } else if (error.errorCode === "popup_window_error") {
            throw new Error("Impossibile aprire popup. Verifica popup blocker del browser.");
        } else {
            throw new Error(`Login fallito: ${error.errorMessage || error.message}`);
        }
    }
}

/**
 * Acquisisce un JWT access token valido per chiamare l'API.
 * 
 * Flusso:
 * 1. Se non c'è account loggato → esegue login interattivo
 * 2. Prova acquisizione silenziosa (da cache) → veloce, senza popup
 * 3. Se fallisce (token scaduto/non in cache) → acquisizione interattiva con popup
 * 
 * Ritorna: JWT access token (stringa) da usare nell'header Authorization
 */
async function getAccessToken() {
    // Se non c'è account, esegui login
    if (!currentAccount) {
        console.log("⚠️ Nessun account loggato. Esecuzione login...");
        await loginWithAzureAD();
    }
    
    const tokenRequest = {
        scopes: loginRequest.scopes,
        account: currentAccount
    };
    
    try {
        // TENTATIVO 1: Acquisizione silenziosa (da cache, veloce)
        console.log("→ Tentativo acquisizione token silenziosa (da cache)...");
        const tokenResponse = await msalInstance.acquireTokenSilent(tokenRequest);
        console.log("✅ Token acquisito da cache (silenzioso)");
        return tokenResponse.accessToken;
        
    } catch (silentError) {
        // Acquisizione silenziosa fallita (token scaduto o non in cache)
        console.warn("⚠️ Acquisizione silenziosa fallita. Fallback a popup interattivo.");
        console.warn("Errore:", silentError.errorCode);
        
        try {
            // TENTATIVO 2: Acquisizione interattiva (con popup)
            const tokenResponse = await msalInstance.acquireTokenPopup(tokenRequest);
            console.log("✅ Token acquisito tramite popup interattivo");
            return tokenResponse.accessToken;
            
        } catch (interactiveError) {
            console.error("❌ Acquisizione token fallita completamente:", interactiveError);
            
            // Reset account e forza nuovo login
            currentAccount = null;
            throw new Error("Impossibile ottenere token di autenticazione. Riprova.");
        }
    }
}

/**
 * (Opzionale) Aggiorna UI per mostrare utente loggato
 */
function updateUIForLoggedInUser() {
    // Esempio: mostra nome utente in un elemento HTML
    const userInfoDiv = document.getElementById('userInfo');
    if (userInfoDiv && currentAccount) {
        userInfoDiv.innerHTML = `👤 Loggato come: <strong>${currentAccount.username}</strong>`;
        userInfoDiv.style.display = 'block';
    }
}

// ============================================================================
// CLASSIFICAZIONE NEWS (FUNZIONE PRINCIPALE)
// ============================================================================

/**
 * Classifica il testo inserito dall'utente chiamando direttamente APIM con JWT.
 * 
 * Workflow completo:
 * 1. Valida input utente (non vuoto)
 * 2. Acquisisce JWT access token da Azure AD (con login se necessario)
 * 3. Effettua chiamata POST diretta ad APIM con Authorization header
 * 4. APIM valida JWT automaticamente (policy configurata nello Step 2.1)
 * 5. APIM inoltra richiesta alla Function App se JWT valido
 * 6. Function App esegue classificazione e ritorna risultato
 * 7. Visualizza risultato nell'UI
 * 
 * Nessun proxy intermedio: riduzione latenza ~50-100ms rispetto alla versione precedente.
 */
async function classifyNews() {
    const resultDiv = document.getElementById('result');
    const btn = document.getElementById('analyzeBtn');
    const textArea = document.getElementById('newsText');
    const text = textArea.value.trim();
    
    // === VALIDAZIONE INPUT ===
    if (!text) {
        resultDiv.textContent = '⚠️ Inserisci un testo prima di analizzare';
        resultDiv.className = 'error';
        resultDiv.style.display = 'block';
        return;
    }
    
    // === UI FEEDBACK (DISABILITA BOTTONE, MOSTRA LOADING) ===
    btn.disabled = true;
    btn.textContent = '⏳ Autenticazione...';
    resultDiv.innerHTML = '<p class="loading">🔐 Autenticazione in corso...</p>';
    resultDiv.style.display = 'block';
    
    try {
        // === STEP 1: ACQUISIZIONE JWT TOKEN ===
        console.log("→ Inizio acquisizione JWT token...");
        const accessToken = await getAccessToken();
        console.log("✅ Token JWT acquisito:", accessToken.substring(0, 50) + "...");
        
        // Aggiorna UI
        btn.textContent = '⏳ Analizzando...';
        resultDiv.innerHTML = '<p class="loading">🤖 Analisi del testo in corso...</p>';
        
        // === STEP 2: CHIAMATA DIRETTA AD APIM CON JWT ===
        console.log("→ Invio richiesta POST ad APIM...");
        const response = await fetch(CONFIG.API_URL, {
            method: 'POST',
            headers: { 
                'Content-Type': 'application/json',
                // ⭐ JWT TOKEN nell'header Authorization (validato da APIM)
                'Authorization': `Bearer ${accessToken}`
            },
            body: JSON.stringify({ text: text })
        });
        
        console.log("← Risposta ricevuta, status:", response.status);
        
        // === GESTIONE ERRORI HTTP ===
        if (!response.ok) {
            let errorMessage = `Errore server (HTTP ${response.status})`;
            
            try {
                const errorData = await response.json();
                
                // Errori specifici
                if (response.status === 400 && errorData.error) {
                    // Validazione input (es. testo troppo corto)
                    errorMessage = errorData.error;
                    if (errorData.word_count !== undefined) {
                        errorMessage += ` (Rilevate ${errorData.word_count} parole)`;
                    }
                } else if (response.status === 401) {
                    // Token non valido o scaduto
                    errorMessage = '🔐 Autenticazione fallita. Prova a effettuare nuovamente il login.';
                    currentAccount = null;  // Reset per forzare nuovo login
                } else if (response.status === 403) {
                    // Token valido ma senza permessi sufficienti
                    errorMessage = '⛔ Non hai i permessi necessari per chiamare questa API.';
                } else if (response.status === 429) {
                    // Rate limiting
                    errorMessage = '⏱️ Troppe richieste. Riprova tra qualche minuto.';
                } else if (response.status === 503) {
                    // Servizio non disponibile
                    errorMessage = '🛠️ Servizio temporaneamente non disponibile. Riprova più tardi.';
                } else if (errorData.error) {
                    // Altri errori con messaggio custom
                    errorMessage = errorData.error;
                }
            } catch (parseError) {
                // JSON parsing fallito: usa messaggio di default
                console.error('Errore parsing JSON errore:', parseError);
            }
            
            throw new Error(errorMessage);
        }
        
        // === PARSING E VALIDAZIONE RISPOSTA ===
        const data = await response.json();
        console.log("✅ Classificazione completata:", data);
        
        // Validazione campi obbligatori
        if (!data || typeof data.is_fake !== 'boolean' || typeof data.confidence !== 'number') {
            throw new Error('Risposta API malformata (campi mancanti)');
        }
        
        // === PREPARAZIONE DATI PER VISUALIZZAZIONE ===
        const isFake = data.is_fake;
        const label = isFake ? '❌ FAKE NEWS' : '✅ NEWS REALE';
        const className = isFake ? 'fake' : 'real';
        const labelClass = isFake ? 'result-fake' : 'result-real';
        
        // Converti confidenza da frazione (0-1) a percentuale (0-100)
        const confidence = Math.max(0, Math.min(100, data.confidence * 100)).toFixed(1);
        
        // Sanitizza label per prevenire XSS
        const safeLabel = DOMPurify.sanitize(data.label.toUpperCase());

        // === AVVISI CONDIZIONALI ===
        
        // Avviso bassa confidenza (modello incerto)
        let confidenceWarning = '';
        const CONFIDENCE_THRESHOLD = 70;
        if (confidence < CONFIDENCE_THRESHOLD) {
            confidenceWarning = `
                <p class="warning">
                    ⚠️ <strong>Confidenza bassa:</strong> Il modello non è sicuro del risultato.
                    Verifica manualmente la fonte.
                </p>
            `;
        }

        // Disclaimer geografico
        const geoDisclaimer = `
            <p class="disclaimer">
                ℹ️ <strong>Nota:</strong> Il modello è addestrato su notizie USA.
                Le prestazioni su notizie internazionali potrebbero essere inferiori.
            </p>
        `;
        
        // === RENDERING RISULTATO NELL'UI ===
        resultDiv.innerHTML = DOMPurify.sanitize(`
            <div class="result-label ${labelClass}">${label}</div>
            <div class="result-details">
                <p><strong>Confidenza del modello:</strong> ${confidence}%</p>
                <div class="confidence-bar">
                    <div class="confidence-fill ${className}" style="width: ${confidence}%"></div>
                </div>
                ${confidenceWarning}
                <p style="margin-top: 15px;"><strong>Classificazione:</strong> ${safeLabel}</p>
                ${geoDisclaimer}
            </div>
        `);
        resultDiv.className = className;
        resultDiv.style.display = 'block';
        
    } catch (error) {
        // === GESTIONE ERRORI GENERICI ===
        console.error("❌ Errore durante classificazione:", error);
        
        const errorMsg = document.createElement('p');
        errorMsg.className = 'error';
        errorMsg.textContent = `❌ ${error.message}`;
        
        resultDiv.innerHTML = '';
        resultDiv.appendChild(errorMsg);
        resultDiv.className = 'error';
        resultDiv.style.display = 'block';
        
    } finally {
        // === RIPRISTINA STATO BOTTONE (SEMPRE) ===
        btn.disabled = false;
        btn.textContent = 'Analizza Testo';
    }
}

// ============================================================================
// EVENT LISTENERS
// ============================================================================

// Shortcut tastiera: Ctrl+Enter (o Cmd+Enter su Mac) per inviare
document.getElementById('newsText').addEventListener('keydown', function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();  // Previeni comportamento default
        classifyNews();
    }
});

// (Opzionale) Logout button
function logout() {
    msalInstance.logoutPopup();
    currentAccount = null;
    document.getElementById('userInfo').style.display = 'none';
}
