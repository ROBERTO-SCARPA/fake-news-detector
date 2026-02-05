/**
 * Classifica il testo della news tramite Azure APIM
 */

const CONFIG = {
    APIM_URL: "https://apim-fakenews-2026.azure-api.net/api/classify_news",
};

async function classifyNews() {
    const resultDiv = document.getElementById('result');
    const btn = document.getElementById('analyzeBtn');
    const text = document.getElementById('newsText').value.trim();
    
    // Controllo input vuoto
    if (!text) {
        resultDiv.textContent = '⚠️ Inserisci un testo prima di analizzare';
        resultDiv.className = 'error';
        resultDiv.style.display = 'block';
        return;
    }
    
    // Controllo lunghezza minima (numero di parole non vuote)
    const wordCount = text.split(/\s+/).filter(w => w.length > 0).length;
    const MIN_WORDS = 30;

    if (wordCount < MIN_WORDS) {
        resultDiv.innerHTML = DOMPurify.sanitize(`
            <p class="error">
                ⚠️ Testo troppo breve (${wordCount} parole).<br>
                Inserisci almeno ${MIN_WORDS} parole per una classificazione affidabile.
            </p>
        `);
        resultDiv.style.display = 'block';
        return;
    }
    
    // Disabilita bottone e mostra loading
    btn.disabled = true;
    resultDiv.innerHTML = '<p class="loading">⏳ Analizzando il testo...</p>';
    resultDiv.style.display = 'block';
    
    try {
        // Chiama APIM
        const response = await fetch(CONFIG.APIM_URL, {
            method: 'POST',
            headers: { 
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ text: text })
        });
        
        // Gestione errori HTTP
        if (!response.ok) {
            if (response.status === 429) {
                throw new Error('Limite di richieste superato. Riprova più tardi.');
            }
            throw new Error(`Errore server: ${response.status}`);
        }
        
        // Parse JSON
        const data = await response.json();
        
        // Validazione dati ricevuti 
        if (!data || typeof data.is_fake !== 'boolean' || typeof data.confidence !== 'number') {
            throw new Error('Risposta API non valida');
        }
        
        // Estrai dati (sanitizza label)
        const isFake = data.is_fake;
        const label = isFake ? '❌ FAKE NEWS' : '✅ NEWS REALE';
        const className = isFake ? 'fake' : 'real';
        const labelClass = isFake ? 'result-fake' : 'result-real';
        const confidence = Math.max(0, Math.min(100, data.confidence * 100)).toFixed(1);  // Clamp 0-100
        const safeLabel = DOMPurify.sanitize(data.label.toUpperCase());

        // Warning confidenza bassa
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

        const geoDisclaimer = `
            <p class="disclaimer">
                ℹ️ Nota: il modello è stato addestrato prevalentemente su notizie politiche statunitensi.
                Le prestazioni potrebbero essere inferiori su notizie internazionali.
            </p>
        `;
        
        // Renderizza risultato (tutto sanitizzato)
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
        // Errore - usa textContent per evitare XSS nell'error message
        const errorMsg = document.createElement('p');
        errorMsg.className = 'error';
        errorMsg.textContent = `❌ Errore: ${error.message}`;
        resultDiv.innerHTML = '';
        resultDiv.appendChild(errorMsg);
        resultDiv.className = 'error';
        resultDiv.style.display = 'block';
        
    } finally {
        // Riabilita bottone sempre
        btn.disabled = false;
    }
}

/**
 * Event listener: Permetti Ctrl+Enter per inviare
 */
document.getElementById('newsText').addEventListener('keypress', function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        classifyNews();
    }
});
