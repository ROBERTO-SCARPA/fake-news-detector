/**
 * Classifica il testo della news tramite Azure Function
 */
async function classifyNews() {
    const text = document.getElementById('newsText').value.trim();
    // Controllo lunghezza minima (numero di parole)
    const wordCount = text.split(/\s+/).length;
    const MIN_WORDS = 30;

    if (wordCount < MIN_WORDS) {
        resultDiv.innerHTML = `
            <p class="error">
                ⚠️ Testo troppo breve (${wordCount} parole).<br>
                Inserisci almeno ${MIN_WORDS} parole per una classificazione affidabile.
            </p>
        `;
        resultDiv.style.display = 'block';
        return;
    }

    const resultDiv = document.getElementById('result');
    const btn = document.getElementById('analyzeBtn');
    
    // Validazione input
    if (!text) {
        resultDiv.innerHTML = '<p class="error">⚠️ Inserisci un testo prima di analizzare</p>';
        resultDiv.style.display = 'block';
        return;
    }
    
    // Disabilita bottone e mostra loading
    btn.disabled = true;
    resultDiv.innerHTML = '<p class="loading">⏳ Analizzando il testo...</p>';
    resultDiv.style.display = 'block';
    
    try {
        // Chiama Azure Function
        const response = await fetch(CONFIG.FUNCTION_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: text })
        });
        
        // Controlla risposta
        if (!response.ok) {
            throw new Error(`Errore server: ${response.status}`);
        }
        
        // Parse JSON
        const data = await response.json();
        
        // Estrai dati
        const isFake = data.is_fake;
        const label = isFake ? '❌ FAKE NEWS' : '✅ NEWS REALE';
        const className = isFake ? 'fake' : 'real';
        const labelClass = isFake ? 'result-fake' : 'result-real';
        const confidence = (data.confidence * 100).toFixed(1);

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

        
        // Renderizza risultato
        resultDiv.innerHTML = `
            <div class="result-label ${labelClass}">${label}</div>
            <div class="result-details">
                <p><strong>Confidenza:</strong> ${confidence}%</p>
                <div class="confidence-bar">
                    <div class="confidence-fill ${className}" style="width: ${confidence}%"></div>
                </div>
                ${confidenceWarning}
                <p style="margin-top: 10px;"><strong>Classificazione:</strong> ${data.label.toUpperCase()}</p>
                ${geoDisclaimer}
            </div>
        `;
        resultDiv.className = className;
        resultDiv.style.display = 'block';
    } catch (error) {
        // Errore
        resultDiv.innerHTML = `<p class="error">❌ Errore: ${error.message}</p>`;
        resultDiv.className = 'error';
        resultDiv.style.display = 'block';
    } finally {
        // Riabilita bottone
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
