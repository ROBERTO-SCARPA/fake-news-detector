"""
Azure Functions app per classificazione automatica di fake news.
Espone un endpoint HTTP che riceve testo e restituisce predizione (fake/real) con confidenza.
Modello e vectorizer vengono cachati in memoria dopo il primo caricamento da Blob Storage.
"""

# ============================================================================
# IMPORTS
# ============================================================================
import azure.functions as func      # Runtime e decoratori Azure Functions
import json                         # Serializzazione/deserializzazione JSON
import pickle                       # Deserializzazione modelli ML salvati
import logging                      # Log strutturato per debugging e monitoring
from azure.storage.blob import BlobServiceClient  # Cliente per Blob Storage
import os                           # Accesso variabili d'ambiente

# ============================================================================
# CONFIGURAZIONE GLOBALE
# ============================================================================

# Istanza principale dell'applicazione Azure Functions
# Tutti i decoratori @app.route registrano handler HTTP su questa istanza
app = func.FunctionApp()

# Cache del modello e vectorizer in memoria
# Dopo il primo caricamento, rimangono in RAM per le richieste successive
# Questo migliora drasticamente la latenza (evita download ripetuti da Blob)
classifier = None       # Modello di classificazione (sklearn.naive_bayes.MultinomialNB o simile)
vectorizer = None       # Vectorizer TF-IDF per trasformazione testo -> features numeriche

# ============================================================================
# FUNZIONI DI SUPPORTO
# ============================================================================

def load_model_from_blob():
    """
    Carica il modello ML da Azure Blob Storage e lo memorizza in cache globale.
    
    Questa funzione implementa il pattern "lazy loading": il modello viene scaricato
    dalla prima richiesta (non all'avvio dell'app) e cachato per richieste future.
    
    Flusso:
    -------
    1. Controlla se modello è già in memoria → ritorna subito (cache hit)
    2. Recupera connection string dall'ambiente (AzureWebJobsStorage)
    3. Si connette a Blob Storage e accede al container 'models'
    4. Scarica due blob: classifier.pkl e vectorizer.pkl
    5. Deserializza con pickle e assegna alle variabili globali
    6. In caso di errore: logga l'eccezione e la rilancia al caller
    
    Variabili d'ambiente richieste:
    -----
    - AzureWebJobsStorage: connection string al storage account
                           (formato: DefaultEndpointsProtocol=https;...)
    
    Eccezioni:
    ----------
    ValueError: se AzureWebJobsStorage non è configurata
    Exception: errori di connessione a Blob Storage o deserializzazione
    """
    global classifier, vectorizer

    # === CONTROLLO CACHE ===
    # Se il modello è già stato caricato, non fare nulla (cache hit)
    # Questo evita traffico di rete e latenza su ogni richiesta
    if classifier is not None and vectorizer is not None:
        logging.info("✓ Modello già in memoria (cache hit), skip download")
        return

    logging.info("→ Inizio caricamento modello da Blob Storage...")

    try:
        # === CONFIGURAZIONE CONNESSIONE ===
        # Recupera la connection string dalle variabili d'ambiente
        # Impostata automaticamente da Azure durante il deploy
        connection_string = os.getenv('AzureWebJobsStorage')

        if not connection_string:
            # Errore di configurazione: la variabile d'ambiente non è stata impostata
            # Tipicamente accade se lo storage account non è collegato all'app
            raise ValueError(
                "AzureWebJobsStorage non configurata. "
                "Assicurati che la Function App abbia accesso allo storage account."
            )

        # === CONNESSIONE A BLOB STORAGE ===
        # Crea un client Blob usando la connection string
        # Questo client gestisce autenticazione e comunicazione HTTPS
        blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        
        # Accedi al container 'models' dove sono salvati i file del modello
        # Se il container non esiste, Azure solleverà un'eccezione
        container_client = blob_service_client.get_container_client('models')

        # === DOWNLOAD CLASSIFIER ===
        # Il classifier è il modello di classificazione addestrato (es. MultinomialNB)
        # Viene deserializzato da pickle in un oggetto Python
        logging.info("→ Download classifier.pkl...")
        classifier_blob = container_client.get_blob_client('classifier.pkl')
        classifier_data = classifier_blob.download_blob().readall()
        # pickle.loads() deserializza i byte in oggetto Python
        classifier = pickle.loads(classifier_data)
        logging.info("✓ Classifier caricato")

        # === DOWNLOAD VECTORIZER ===
        # Il vectorizer trasforma il testo grezzo in vettori numerici (TF-IDF)
        # Necessario per preprocessare il testo prima della predizione
        logging.info("→ Download vectorizer.pkl...")
        vectorizer_blob = container_client.get_blob_client('vectorizer.pkl')
        vectorizer_data = vectorizer_blob.download_blob().readall()
        vectorizer = pickle.loads(vectorizer_data)
        logging.info("✓ Vectorizer caricato")

        logging.info("✓ Modello caricato completamente da Blob Storage")

    except Exception as e:
        # Log dettagliato dell'errore per debugging in Application Insights
        # Rilanciamo l'eccezione per far sì che il caller (classify_news) la gestisca
        logging.error(
            f"✗ Errore caricamento modello da Blob Storage: {str(e)}",
            extra={'exception': str(e)}
        )
        raise


# ============================================================================
# HTTP HANDLER
# ============================================================================

@app.route(route="classify_news", auth_level=func.AuthLevel.ANONYMOUS)
def classify_news(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP trigger (POST): classifica una news come fake o reale.
    
    Questo endpoint implementa il servizio di classificazione di fake news.
    Riceve testo da un client HTTP, lo processa con il modello ML e ritorna
    una predizione con confidence score.
    
    Parametri HTTP:
    ---------------
    POST body (JSON):
    {
        "text": "Contenuto della news da classificare..."
    }
    
    Risposte HTTP:
    ---------------
    200 OK - Classificazione completata con successo
    {
        "text": "Snippet troncato a 100 caratteri...",
        "label": "fake" o "real",
        "confidence": 0.9523,
        "is_fake": true o false
    }
    
    400 Bad Request - Input non valido
    {
        "error": "Messaggio di errore descrittivo"
    }
    
    500 Internal Server Error - Errore durante il processing
    {
        "error": "Errore interno: [dettagli]"
    }
    
    Flusso di elaborazione:
    -----------------------
    1. Valida autenticazione (ANONYMOUS: nessun controllo)
    2. Carica il modello da cache/Blob (lazy loading)
    3. Estrae e valida il campo "text" dal JSON
    4. Trasforma il testo in vettore numerico (vectorizer)
    5. Genera predizione e confidence score (classifier)
    6. Costruisce risposta JSON e la ritorna
    7. Gestisce eccezioni e ritorna errori appropriati
    """
    logging.info('← Richiesta di classificazione ricevuta')

    try:
        # === CARICAMENTO MODELLO ===
        # Carica il modello se non è già in cache
        # Se AzureWebJobsStorage non è configurata, qui solleverà un'eccezione
        load_model_from_blob()

        # === PARSING JSON ===
        # Estrai il body JSON della richiesta
        # ValueError viene sollevato se il body non è JSON valido
        try:
            req_body = req.get_json()
        except ValueError:
            # JSON malformato (sintassi non valida)
            return func.HttpResponse(
                json.dumps({"error": "Richiesta non è JSON valido"}),
                status_code=400,
                mimetype="application/json"
            )

        # === VALIDAZIONE INPUT ===
        # Estrai il campo 'text' dal JSON e rimuovi spazi bianchi
        # Se il campo non esiste, get() ritorna stringa vuota ''
        text = req_body.get('text', '').strip()

        if not text:
            # Campo mancante, vuoto o solo spazi bianchi
            return func.HttpResponse(
                json.dumps({"error": "Fornisci un campo 'text' con il contenuto della news"}),
                status_code=400,
                mimetype="application/json"
            )

        logging.info(f"→ Testo da classificare: {text[:50]}...")

        # === TRASFORMAZIONE TESTO ===
        # Il vectorizer trasforma il testo in una matrice sparse TF-IDF
        # Questa rappresentazione numerica è l'input per il classifier
        # Nota: vectorizer.transform() ritorna una matrice, per questo usiamo [text]
        text_vec = vectorizer.transform([text])

        # === PREDIZIONE ===
        # classifier.predict() ritorna etichette ("fake" o "real")
        # classifier.predict_proba() ritorna array di probabilità per ogni classe
        # [0] accede al primo (e unico) elemento dell'array
        prediction = classifier.predict(text_vec)[0]
        confidence_scores = classifier.predict_proba(text_vec)[0]
        # Prendi la probabilità massima come "confidence" della predizione
        confidence = float(max(confidence_scores))

        logging.info(f"→ Predizione: '{prediction}' (confidence: {confidence:.4f})")

        # === COSTRUZIONE RISPOSTA ===
        # Troncamento del testo a 100 caratteri per motivi di sicurezza (evita response troppo grandi)
        # e di leggibilità nel client
        response = {
            "text": text[:100] + "..." if len(text) > 100 else text,
            "label": prediction,
            "confidence": round(confidence, 4),      # Arrotonda a 4 decimali
            "is_fake": prediction == 'fake'          # Bool per facilità client
        }
        
        logging.info(f"✓ Classificazione completata con successo")

        # === INVIO RISPOSTA ===
        return func.HttpResponse(
            json.dumps(response, indent=2),  # indent=2 per leggibilità
            status_code=200,
            mimetype="application/json"
        )

    except Exception as e:
        # === GESTIONE ERRORI GENERICI ===
        # Qualsiasi eccezione non gestita esplicitamente (errori di Blob, deserializzazione, etc)
        # viene loggata e convertita in una risposta 500
        logging.error(
            f"✗ Errore nel processing della richiesta: {str(e)}",
            extra={'exception': str(e)}
        )
        return func.HttpResponse(
            json.dumps({"error": f"Errore interno: {str(e)}"}),
            status_code=500,
            mimetype="application/json"
        )