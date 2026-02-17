/**
 * Frontend per classificazione fake news tramite proxy Function App.
 * Il proxy nasconde la subscription key APIM per sicurezza.
 * Invia richieste POST all'endpoint pubblico e visualizza i risultati con validazioni di sicurezza e UX.
 * La validazione del numero di parole è gestita dal backend.
 */

// Configurazione centrale: URL dell'endpoint proxy pubblico (NESSUNA key necessaria)
const CONFIG = {
    API_URL: "https://func-fakenews-api-fxamgthregb2cbgd.swedencentral-01.azurewebsites.net/api/classify_news_public",
};

/**
 * Funzione principale: classifica il testo inserito tramite chiamata al proxy pubblico.
 * 
 * Workflow:
 * 1. Recupera il testo dall'input e valida solo che non sia vuoto
 * 2. Disabilita il bottone e mostra loading
 * 3. Invia POST al proxy pubblico con il testo (NO auth header)
 * 4. Il proxy aggiunge automaticamente la subscription key APIM (nascosta)
 * 5. Gestisce errori di validazione dal backend (400) mostrando il messaggio
 * 6. Valida la risposta e renderizza il risultato con avvisi condizionali
 * 7. Gestisce errori HTTP e network
 */
async function classifyNews() {
    const resultDiv = document.getElementById('result');
    const btn = document.getElementById('analyzeBtn');
    const text = document.getElementById('newsText').value.trim();
    
    // Controllo input vuoto (unica validazione lato client)
    if (!text) {
        resultDiv.textContent = '⚠️ Inserisci un testo prima di analizzare';
        resultDiv.className = 'error';
        resultDiv.style.display = 'block';
        return;
    }
    
    // Disabilita bottone e mostra indicatore di caricamento (UX feedback)
    btn.disabled = true;
    resultDiv.innerHTML = '<p class="loading">⏳ Analizzando il testo...</p>';
    resultDiv.style.display = 'block';
    
    try {
        // Effettua richiesta POST al proxy pubblico con il testo nel body JSON
        // NOTA: Nessun header di autenticazione necessario (proxy gestisce la key)
        const response = await fetch(CONFIG.API_URL, {
            method: 'POST',
            headers: { 
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ text: text })
        });
        
        // Gestione errori HTTP specifici
        if (!response.ok) {
            // Parse del messaggio di errore dal backend
            let errorMessage = `Errore server: ${response.status}`;
            
            try {
                const errorData = await response.json();
                
                // Gestisci errori di validazione (400) dal backend
                if (response.status === 400 && errorData.error) {
                    // Mostra il messaggio di errore del backend (es. numero parole insufficiente)
                    errorMessage = errorData.error;
                    
                    // Se presente, mostra anche il conteggio parole
                    if (errorData.word_count !== undefined) {
                        errorMessage += ` (${errorData.word_count} parole rilevate)`;
                    }
                }
                // Gestisci rate limiting (429) separatamente per miglior UX
                else if (response.status === 429) {
                    errorMessage = 'Limite di richieste superato. Riprova più tardi.';
                }
                // Servizio non disponibile (503) - proxy non raggiunge APIM
                else if (response.status === 503) {
                    errorMessage = 'Servizio di classificazione temporaneamente non disponibile. Riprova tra qualche minuto.';
                }
                // Altri errori con messaggio dal backend
                else if (errorData.error) {
                    errorMessage = errorData.error;
                }
            } catch (parseError) {
                // Se il parsing JSON fallisce, usa il messaggio di default
                console.error('Errore parsing risposta errore:', parseError);
            }
            
            throw new Error(errorMessage);
        }
        
        // Parse della risposta JSON
        const data = await response.json();
        
        // Validazione della risposta: assicurati che contenga campi obbligatori e tipi corretti
        if (!data || typeof data.is_fake !== 'boolean' || typeof data.confidence !== 'number') {
            throw new Error('Risposta API non valida');
        }
        
        // Estrai e processa i dati ricevuti
        const isFake = data.is_fake;
        const label = isFake ? '❌ FAKE NEWS' : '✅ NEWS REALE';
        const className = isFake ? 'fake' : 'real';
        const labelClass = isFake ? 'result-fake' : 'result-real';
        // Clamp confidenza a 0-100 (converti da frazione a percentuale)
        const confidence = Math.max(0, Math.min(100, data.confidence * 100)).toFixed(1);
        // Sanitizza la label ricevuta per evitare XSS
        const safeLabel = DOMPurify.sanitize(data.label.toUpperCase());

        // Genera avviso se la confidenza del modello è bassa (non affidabile)
        let confidenceWarning = '';
        const CONFIDENCE_THRESHOLD = 70;

        if (confidence < CONFIDENCE_THRESHOLD) {
            confidenceWarning = `
                <p class="warning">
                    ⚠️ La confidenza del modello è bassa.
                    Il risultato potrebbe non essere affidabile.
                </p>
            `;
        }

        // Disclaimer geografico: il modello è stato addestrato prevalentemente su notizie US
        const geoDisclaimer = `
            <p class="disclaimer">
                ℹ️ Nota: il modello è stato addestrato prevalentemente su notizie politiche statunitensi.
                Le prestazioni potrebbero essere inferiori su notizie internazionali.
            </p>
        `;
        
        // Renderizza il risultato completo (HTML sanitizzato via DOMPurify)
        // Includi: label, confidenza visuale (barra), avviso bassa confidenza, disclaimer
        resultDiv.innerHTML = DOMPurify.sanitize(`
            <div class="result-label ${labelClass}">${label}</div>
            <div class="result-details">
                <p><strong>Confidenza:</strong> ${confidence}%</p>
                <div class="confidence-bar">
                    <div class="confidence-fill ${className}" style="width: ${confidence}%"></div>
                </div>
                ${confidenceWarning}
                <p style="margin-top: 10px;"><strong>Classificazione:</strong> ${safeLabel}</p>
                ${geoDisclaimer}
            </div>
        `);
        resultDiv.className = className;
        resultDiv.style.display = 'block';
        
    } catch (error) {
        // Gestione errori: usa textContent (non innerHTML) per evitare XSS nel messaggio d'errore
        const errorMsg = document.createElement('p');
        errorMsg.className = 'error';
        errorMsg.textContent = `❌ ${error.message}`;
        resultDiv.innerHTML = '';
        resultDiv.appendChild(errorMsg);
        resultDiv.className = 'error';
        resultDiv.style.display = 'block';
        
    } finally {
        // Riabilita bottone sempre (indipendentemente da successo/fallimento)
        btn.disabled = false;
    }
}

/**
 * Event listener: permette di inviare la richiesta con Ctrl+Enter (o Cmd+Enter su Mac)
 * Migliora UX: gli utenti possono inviare senza cliccare il bottone
 */
document.getElementById('newsText').addEventListener('keypress', function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        classifyNews();
    }
});
