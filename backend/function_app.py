"""
Azure Functions app per classificazione automatica di fake news.
Espone un endpoint HTTP che riceve testo e restituisce predizione (fake/real) con confidenza.
Modello e vectorizer vengono cachati in Redis (primo livello) e in memoria (secondo livello).
Le predizioni vengono cachate per testo identico (SHA-256 hash).
"""

# ============================================================================
# IMPORTS
# ============================================================================
import azure.functions as func      # Runtime e decoratori Azure Functions
import json                         # Serializzazione/deserializzazione JSON
import pickle                       # Deserializzazione modelli ML salvati
import logging                      # Log strutturato per debugging e monitoring
import hashlib                      # Generazione hash per caching predizioni
import time                         # Misurazione latenza
import os                           # Accesso variabili d'ambiente
from azure.storage.blob import BlobServiceClient  # Cliente per Blob Storage
import redis                        # Cliente Redis per caching distribuito


# ============================================================================
# CONFIGURAZIONE GLOBALE
# ============================================================================

# Istanza principale dell'applicazione Azure Functions
# auth_level.FUNCTION richiede API key nell'header x-functions-key
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# Cache in-memory (livello 2) per modello e vectorizer
# Dopo il primo caricamento, rimangono in RAM per richieste successive
# Questo è il layer più veloce (latenza ~0ms) ma non condiviso tra istanze
classifier = None       # Modello di classificazione (sklearn MultinomialNB)
vectorizer = None       # Vectorizer TF-IDF per trasformazione testo -> features

# Client Redis (livello 1) - cache distribuita condivisa tra tutte le istanze
# Singleton pattern: inizializzato lazy al primo utilizzo
redis_client = None


# ============================================================================
# REDIS CONNECTION (SINGLETON)
# ============================================================================

def get_redis_client():
    """
    Lazy initialization del client Redis con pattern singleton.
    
    Redis viene usato come cache distribuita di primo livello per:
    - Modelli ML (TTL: 24h)
    - Predizioni (TTL: 5min)
    
    Questo evita che ogni istanza della Function App scarichi il modello da Blob,
    riducendo drasticamente cold start time e costi di egress.
    
    Flusso:
    -------
    1. Controlla se redis_client è già inizializzato (cache hit)
    2. Recupera REDIS_CONNECTION_STRING dall'ambiente
    3. Crea connessione TLS (rediss://) con timeout configurati
    4. Testa la connessione con PING
    5. In caso di errore: logga warning e ritorna None (graceful degradation)
    
    Variabili d'ambiente richieste:
    -------------------------------
    REDIS_CONNECTION_STRING: formato "default:PASSWORD@HOST:PORT"
                             Esempio: "default:abc123@myredis.redis.cache.windows.net:6380"
    
    Ritorna:
    --------
    redis.Redis | None: client Redis o None se non disponibile
    
    Nota:
    -----
    Se Redis non è disponibile, l'app continua a funzionare ma usa solo:
    - Cache in-memory (livello 2)
    - Blob Storage (fallback)
    """
    global redis_client
    
    # Controlla se il client è già stato inizializzato
    if redis_client is not None:
        return redis_client
    
    try:
        # Recupera connection string dall'ambiente
        # Formato: "default:PASSWORD@HOST:PORT"
        connection_string = os.environ.get("REDIS_CONNECTION_STRING")
        
        if not connection_string:
            # Redis non configurato: l'app continua con caching in-memory
            logging.warning(
                "⚠️ REDIS_CONNECTION_STRING non configurata - "
                "caching distribuito disabilitato (solo in-memory)"
            )
            return None
        
        # Crea client Redis con TLS (rediss://)
        # decode_responses=False: modalità binaria per pickle
        # socket_timeout: timeout lettura/scrittura (5s)
        # socket_connect_timeout: timeout connessione iniziale (5s)
        redis_client = redis.Redis.from_url(
            f"rediss://{connection_string}",
            decode_responses=False,  # Binary mode per pickle
            socket_connect_timeout=5,
            socket_timeout=5
        )
        
        # Test connessione con PING
        # Solleva eccezione se Redis non è raggiungibile
        redis_client.ping()
        logging.info("✅ Redis connesso: caching distribuito attivo")
        
    except Exception as e:
        # Errore connessione Redis: logga e continua con solo in-memory cache
        logging.error(
            f"❌ Connessione Redis fallita: {e} - "
            f"Fallback a caching solo in-memory"
        )
        redis_client = None
    
    return redis_client


# ============================================================================
# MODEL LOADING (REDIS + BLOB + IN-MEMORY CACHE)
# ============================================================================

def load_model_from_blob():
    """
    Carica modello e vectorizer con strategia di caching a 3 livelli:
    
    LIVELLO 1 (più veloce): Variabili globali in-memory
                            - Latenza: ~0ms
                            - Scope: singola istanza Function App
                            - Durata: fino al restart dell'istanza
    
    LIVELLO 2 (veloce): Redis cache distribuita
                        - Latenza: ~5-10ms
                        - Scope: condivisa tra tutte le istanze
                        - Durata: 24h (TTL configurabile)
    
    LIVELLO 3 (lento): Azure Blob Storage
                       - Latenza: ~500-1000ms
                       - Scope: persistente
                       - Durata: permanente
    
    Flusso:
    -------
    1. Controlla cache in-memory (classifier/vectorizer globali)
       → Se presenti: return (cache hit, ~0ms)
    
    2. Controlla Redis cache
       → Se presenti: deserializza con pickle, aggiorna globali, return (~10ms)
    
    3. Scarica da Blob Storage
       → Download blob → deserializza → salva in Redis (TTL 24h) → aggiorna globali (~1s)
    
    Variabili d'ambiente richieste:
    -------------------------------
    - AzureWebJobsStorage: connection string al storage account
    - REDIS_CONNECTION_STRING (opzionale): per caching distribuito
    
    Eccezioni:
    ----------
    ValueError: se AzureWebJobsStorage non è configurata
    Exception: errori di connessione Blob/Redis o deserializzazione
    """
    global classifier, vectorizer
    
    # === LIVELLO 1: CACHE IN-MEMORY ===
    # Controlla se il modello è già stato caricato in questa istanza
    if classifier is not None and vectorizer is not None:
        logging.info("⚡ Cache HIT (in-memory) - Modello già caricato")
        return
    
    logging.info("→ Inizio caricamento modello (multi-level caching)...")
    
    # === LIVELLO 2: REDIS CACHE ===
    cache = get_redis_client()
    classifier_cache_key = "model:classifier:v1"
    vectorizer_cache_key = "model:vectorizer:v1"
    
    # Prova a caricare da Redis se disponibile
    if cache:
        try:
            cached_classifier = cache.get(classifier_cache_key)
            cached_vectorizer = cache.get(vectorizer_cache_key)
            
            # Entrambi presenti in Redis: deserializza e aggiorna globali
            if cached_classifier and cached_vectorizer:
                logging.info("⚡ Cache HIT (Redis) - Caricamento modelli da Redis")
                classifier = pickle.loads(cached_classifier)
                vectorizer = pickle.loads(cached_vectorizer)
                logging.info("✅ Modelli caricati da Redis (~10ms)")
                return
            else:
                logging.info("⚠️ Cache MISS (Redis) - Fallback a Blob Storage")
                
        except Exception as e:
            # Redis error non fatale: continua con Blob Storage
            logging.warning(f"Redis read error: {e} - Fallback a Blob Storage")
    
    # === LIVELLO 3: BLOB STORAGE ===
    try:
        # Recupera connection string al Blob Storage
        connection_string = os.getenv('AzureWebJobsStorage')
        
        if not connection_string:
            raise ValueError(
                "AzureWebJobsStorage non configurata. "
                "Verifica configurazione storage account nella Function App."
            )
        
        # Connessione a Blob Storage
        blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        container_client = blob_service_client.get_container_client('models')
        
        # Download classifier
        logging.info("📥 Download classifier.pkl da Blob Storage...")
        classifier_blob = container_client.get_blob_client('classifier.pkl')
        classifier_data = classifier_blob.download_blob().readall()
        classifier = pickle.loads(classifier_data)
        logging.info("✓ Classifier caricato da Blob")
        
        # Download vectorizer
        logging.info("📥 Download vectorizer.pkl da Blob Storage...")
        vectorizer_blob = container_client.get_blob_client('vectorizer.pkl')
        vectorizer_data = vectorizer_blob.download_blob().readall()
        vectorizer = pickle.loads(vectorizer_data)
        logging.info("✓ Vectorizer caricato da Blob")
        
        # Salva in Redis per le prossime richieste (TTL: 24h)
        if cache:
            try:
                cache.setex(
                    classifier_cache_key,
                    86400,  # 24 ore
                    classifier_data
                )
                cache.setex(
                    vectorizer_cache_key,
                    86400,  # 24 ore
                    vectorizer_data
                )
                logging.info("✅ Modelli cachati in Redis (TTL: 24h)")
            except Exception as e:
                # Redis write error non fatale
                logging.warning(f"Redis write error: {e}")
        
        logging.info("✅ Modelli caricati completamente da Blob Storage (~1s)")
        
    except Exception as e:
        # Errore fatale: impossibile caricare il modello
        logging.error(
            f"✗ Errore critico caricamento modello: {str(e)}",
            extra={'exception': str(e)}
        )
        raise


# ============================================================================
# PREDICTION RESULT CACHING
# ============================================================================

def get_cached_prediction(text_hash: str) -> dict | None:
    """
    Recupera una predizione cachata da Redis basata sull'hash del testo.
    
    Quando viene classificato un testo, il risultato viene cachato per 5 minuti
    usando l'hash SHA-256 del testo come chiave. Se lo stesso testo viene
    inviato nuovamente entro 5 minuti, la predizione viene recuperata da Redis
    senza rieseguire il modello ML (risparmio ~50-100ms).
    
    Parametri:
    ----------
    text_hash : str
        SHA-256 hash del testo da classificare (64 caratteri hex)
    
    Ritorna:
    --------
    dict | None: risultato cachato o None se cache miss
    
    Formato risposta cachata:
    -------------------------
    {
        "is_fake": bool,
        "label": "FAKE" | "REAL",
        "confidence": float,
        "processing_ms": int
    }
    """
    cache = get_redis_client()
    if not cache:
        return None
    
    try:
        cache_key = f"prediction:{text_hash}"
        cached_result = cache.get(cache_key)
        
        if cached_result:
            logging.info(f"⚡ Cache HIT (predizione) - Hash: {text_hash[:8]}...")
            return json.loads(cached_result.decode('utf-8'))
        else:
            logging.info(f"⚠️ Cache MISS (predizione) - Hash: {text_hash[:8]}...")
            return None
            
    except Exception as e:
        # Redis error non fatale: continua con predizione normale
        logging.warning(f"Redis read error: {e}")
        return None


def cache_prediction(text_hash: str, result: dict, ttl: int = 300):
    """
    Salva una predizione in Redis con TTL di 5 minuti (default).
    
    Le predizioni vengono cachate per evitare classificazioni duplicate
    dello stesso testo. Utile per scenari di retry o validazione ripetuta.
    
    Parametri:
    ----------
    text_hash : str
        SHA-256 hash del testo classificato
    result : dict
        Risultato della classificazione da cachare
    ttl : int
        Time-to-live in secondi (default: 300 = 5 minuti)
    """
    cache = get_redis_client()
    if not cache:
        return
    
    try:
        cache_key = f"prediction:{text_hash}"
        cache.setex(
            cache_key,
            ttl,
            json.dumps(result)
        )
        logging.info(f"✅ Predizione cachata in Redis (TTL: {ttl}s)")
    except Exception as e:
        # Redis write error non fatale
        logging.warning(f"Redis write error: {e}")


# ============================================================================
# HTTP ENDPOINT
# ============================================================================

@app.route(route="classify_news", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def classify_news(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP trigger (POST): classifica una news come fake o reale con caching Redis.
    
    Questo endpoint implementa il servizio di classificazione con strategia di
    caching avanzata a 3 livelli (in-memory, Redis, Blob Storage) per ottimizzare
    latenza e costi.
    
    Autenticazione:
    ---------------
    AuthLevel.FUNCTION: richiede API key nell'header HTTP
    Header richiesto: x-functions-key: <API_KEY>
    
    API key reperibile con:
    az functionapp keys list --name fakenewsdetector-func --resource-group rg-fakenewsdetector
    
    Parametri HTTP:
    ---------------
    POST body (JSON):
    {
        "text": "Contenuto della news da classificare (min 20 caratteri)..."
    }
    
    Risposte HTTP:
    ---------------
    200 OK - Classificazione completata
    {
        "is_fake": true,
        "label": "FAKE",
        "confidence": 0.9523,
        "processing_ms": 45,
        "cache_hit": false,
        "text_preview": "Primi 100 caratteri del testo..."
    }
    
    400 Bad Request - Input non valido
    {
        "error": "Missing 'text' field" | "Text must be at least 20 characters"
    }
    
    500 Internal Server Error - Errore durante il processing
    {
        "error": "Internal server error"
    }
    
    Flusso di elaborazione:
    -----------------------
    1. Valida autenticazione (verifica API key)
    2. Parse e valida JSON body (campo 'text' presente e >= 20 caratteri)
    3. Genera SHA-256 hash del testo per caching
    4. Controlla cache Redis per predizione già esistente
       → Se cache hit: ritorna risultato salvato (~5-10ms)
    5. Carica modello da cache (in-memory → Redis → Blob)
    6. Trasforma testo in vettore TF-IDF
    7. Genera predizione e confidence score
    8. Salva risultato in Redis (TTL: 5 minuti)
    9. Ritorna risposta JSON con metriche di performance
    
    Performance:
    ------------
    - Cache hit (predizione): ~5-10ms
    - Cache hit (modello Redis): ~50-100ms
    - Cache hit (modello in-memory): ~30-50ms
    - Cache miss (modello da Blob): ~500-1000ms (solo primo avvio)
    """
    start_time = time.time()
    
    try:
        # === PARSING E VALIDAZIONE INPUT ===
        try:
            req_body = req.get_json()
        except ValueError:
            # JSON malformato
            return func.HttpResponse(
                json.dumps({"error": "Request body must be valid JSON"}),
                status_code=400,
                mimetype="application/json"
            )
        
        # Estrai e valida campo 'text'
        text = req_body.get('text', '').strip()
        
        if not text:
            return func.HttpResponse(
                json.dumps({"error": "Missing 'text' field"}),
                status_code=400,
                mimetype="application/json"
            )
        
        # Validazione lunghezza minima (evita testi troppo corti per essere classificati)
        if len(text) < 20:
            return func.HttpResponse(
                json.dumps({"error": "Text must be at least 20 characters"}),
                status_code=400,
                mimetype="application/json"
            )
        
        logging.info(f"→ Richiesta classificazione ricevuta ({len(text)} caratteri)")
        
        # === CACHING PREDIZIONE (LIVELLO 1) ===
        # Genera hash SHA-256 del testo per identificare predizioni duplicate
        text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
        
        # Controlla se questa predizione è già stata cachata
        cached_result = get_cached_prediction(text_hash)
        if cached_result:
            # Cache hit: ritorna risultato salvato senza rieseguire il modello
            elapsed_ms = int((time.time() - start_time) * 1000)
            cached_result['processing_ms'] = elapsed_ms
            cached_result['cache_hit'] = True
            cached_result['text_preview'] = text[:100] + ("..." if len(text) > 100 else "")
            
            logging.info(f"⚡ Predizione da cache Redis ({elapsed_ms}ms)")
            
            return func.HttpResponse(
                json.dumps(cached_result, indent=2),
                status_code=200,
                mimetype="application/json"
            )
        
        # === CARICAMENTO MODELLO ===
        # Carica da cache multi-livello (in-memory → Redis → Blob)
        logging.info("📦 Caricamento modelli ML...")
        load_model_from_blob()
        
        # === TRASFORMAZIONE TESTO ===
        # Vettorizzazione TF-IDF: testo → matrice sparse numerica
        logging.info(f"→ Vectorizing testo: '{text[:50]}...'")
        text_vec = vectorizer.transform([text])
        
        # === PREDIZIONE ===
        # Classificazione Naive Bayes
        prediction = classifier.predict(text_vec)[0]
        confidence_scores = classifier.predict_proba(text_vec)[0]
        confidence = float(max(confidence_scores))
        
        logging.info(
            f"✅ Predizione: {prediction} "
            f"(confidence: {confidence:.4f})"
        )
        
        # === COSTRUZIONE RISPOSTA ===
        elapsed_ms = int((time.time() - start_time) * 1000)
        
        result = {
            "is_fake": prediction.upper() == "FAKE",
            "label": prediction.upper(),
            "confidence": round(confidence, 4),
            "processing_ms": elapsed_ms,
            "cache_hit": False,
            "text_preview": text[:100] + ("..." if len(text) > 100 else "")
        }
        
        # === CACHING RISULTATO ===
        # Salva predizione in Redis per 5 minuti
        cache_prediction(text_hash, result, ttl=300)
        
        return func.HttpResponse(
            json.dumps(result, indent=2),
            status_code=200,
            mimetype="application/json"
        )
    
    except ValueError as e:
        # Errore di validazione input
        logging.error(f"✗ Validation error: {e}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=400,
            mimetype="application/json"
        )
    
    except Exception as e:
        # Errore generico (modello, Redis, Blob, etc)
        logging.error(
            f"✗ Internal error: {str(e)}",
            extra={'exception': str(e)}
        )
        return func.HttpResponse(
            json.dumps({"error": "Internal server error"}),
            status_code=500,
            mimetype="application/json"
        )
