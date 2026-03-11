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
import requests                     # Per chiamate HTTP (APIM, Blob Storage)
import logging                      # Log strutturato per debugging e monitoring
import hashlib                      # Generazione hash per caching predizioni
import time                         # Misurazione latenza
import os                           # Accesso variabili d'ambiente
from azure.storage.blob import BlobServiceClient  # Cliente per Blob Storage
import redis                        # Cliente Redis per caching distribuito
from job_manager import JobManager, JobInfo, JobStatus
from datetime import datetime, timezone

MIN_WORDS = int(os.getenv('MIN_WORDS', '10'))
MAX_WORDS = int(os.getenv('MAX_WORDS', '5000'))

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
                            - Scope: singola istanza Function App
                            - Durata: fino al restart dell'istanza
    
    LIVELLO 2 (veloce): Redis cache distribuita
                        - Scope: condivisa tra tutte le istanze
                        - Durata: 24h (TTL configurabile)
    
    LIVELLO 3 (lento): Azure Blob Storage
                       - Scope: persistente
                       - Durata: permanente
    
    Flusso:
    -------
    1. Controlla cache in-memory (classifier/vectorizer globali)
       → Se presenti: return (cache hit, ~0ms)
    
    2. Controlla Redis cache
       → Se presenti: deserializza con pickle, aggiorna globali, return 
    
    3. Scarica da Blob Storage
       → Download blob → deserializza → salva in Redis (TTL 24h) → aggiorna globali 
    
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
                logging.info("✅ Modelli caricati da Redis")
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
        logging.info("Classifier caricato da Blob")
        
        # Download vectorizer
        logging.info("📥 Download vectorizer.pkl da Blob Storage...")
        vectorizer_blob = container_client.get_blob_client('vectorizer.pkl')
        vectorizer_data = vectorizer_blob.download_blob().readall()
        vectorizer = pickle.loads(vectorizer_data)
        logging.info("Vectorizer caricato da Blob")
        
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
    AuthLevel.ANONYMOUS: nessuna autenticazione richiesta
    Nessun header HTTP richiesto
    
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
        
        word_count = len(text.split())

        if word_count < MIN_WORDS:
            return func.HttpResponse(
                json.dumps({"error": f"Il testo deve contenere almeno {MIN_WORDS} parole"}),
                status_code=400,
                mimetype="application/json"
            )
        
        if word_count > MAX_WORDS:
            return func.HttpResponse(
                json.dumps({"error": f"Il testo deve contenere al massimo {MAX_WORDS} parole"}),
                status_code=400,
                mimetype="application/json"
            )

        
        logging.info(f"→ Richiesta classificazione ricevuta ({len(text)} caratteri, {word_count} parole)")
        
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
            "text_preview": text[:100] + ("..." if len(text) > 100 else ""),
            "word_count": word_count                             
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

@app.route(route="warmup", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def warmup(req: func.HttpRequest) -> func.HttpResponse:
    """
    Pre-carica modelli ML in Redis cache per evitare cold start.
    
    Chiamare questo endpoint dopo ogni deploy o quando l'app è stata idle.
    Non richiede autenticazione (ANONYMOUS) per permettere chiamate da monitoring.
    
    Esempio: GET https://fakenewsdetector-func.azurewebsites.net/api/warmup
    
    Risposta:
    --------
    200 OK:
    {
        "status": "success",
        "message": "Cache warmed up successfully",
        "models_loaded": ["classifier", "vectorizer"]
    }
    """
    
    try:
        logging.info("🔥 Warmup endpoint chiamato - Pre-caricamento modelli...")
        
        # Carica modelli (triggerà download da Blob + salvataggio in Redis)
        load_model_from_blob()
        
        logging.info("✅ Warmup completato con successo")
        
        return func.HttpResponse(
            json.dumps({
                "status": "success",
                "message": "Cache warmed up successfully",
                "models_loaded": ["classifier", "vectorizer"],
                "cache_ttl_hours": 24
            }, indent=2),
            status_code=200,
            mimetype="application/json"
        )
        
    except Exception as e:
        logging.error(f"❌ Warmup fallito: {e}")
        return func.HttpResponse(
            json.dumps({
                "status": "error", 
                "message": str(e)
            }),
            status_code=500,
            mimetype="application/json"
        )
    
    
@app.route(route="sysadmin/flush_cache", methods=["POST"], auth_level=func.AuthLevel.ADMIN)
def flush_cache(req: func.HttpRequest) -> func.HttpResponse:
    """
    Svuota la cache Redis e ricarica immediatamente i modelli (flush + warmup automatico).
    
    Flusso:
    1. Cancella modelli e predizioni da Redis
    2. Carica automaticamente i nuovi modelli da Blob Storage
    3. Li salva in Redis (warmup automatico)
    
    Questo garantisce zero downtime: i modelli sono sempre disponibili.
    
    Query parameter opzionale:
    - skip_warmup=true : Salta il ricaricamento automatico
    
    Esempio:
    POST /api/sysadmin/flush_cache
    POST /api/sysadmin/flush_cache?skip_warmup=true
    """
    try:
        # Opzione per skippare warmup (se serve flush veloce senza reload)
        skip_warmup = req.params.get('skip_warmup', 'false').lower() == 'true'
        
        cache = get_redis_client()
        
        if not cache:
            logging.warning("⚠️ Redis non disponibile - nessuna cache da svuotare")
            return func.HttpResponse(
                json.dumps({
                    "error": "Redis not available",
                    "message": "Cache distribuita non configurata"
                }),
                status_code=503,
                mimetype="application/json"
            )
        
        logging.info("🗑️ Flush cache richiesto - Cancellazione in corso...")
        
        # === STEP 1: FLUSH CACHE ===
        models_deleted = cache.delete(
            "model:classifier:v1",
            "model:vectorizer:v1"
        )
        
        prediction_keys = cache.keys("prediction:*")
        predictions_deleted = 0
        if prediction_keys:
            predictions_deleted = cache.delete(*prediction_keys)
        
        # Cancella anche le variabili globali in-memory
        global classifier, vectorizer
        classifier = None
        vectorizer = None
        
        logging.info(
            f"✅ Cache svuotata: {models_deleted} modelli, "
            f"{predictions_deleted} predizioni"
        )
        
        # === STEP 2: WARMUP AUTOMATICO (se non skippato) ===
        warmup_status = "skipped"
        if not skip_warmup:
            try:
                logging.info("🔥 Warmup automatico in corso...")
                load_model_from_blob()  # Ricarica modelli da Blob → Redis → memoria
                warmup_status = "success"
                logging.info("✅ Warmup automatico completato")
            except Exception as warmup_error:
                logging.error(f"⚠️ Warmup automatico fallito: {warmup_error}")
                warmup_status = f"failed: {str(warmup_error)}"
        
        return func.HttpResponse(
            json.dumps({
                "status": "success",
                "models_flushed": models_deleted,
                "predictions_flushed": predictions_deleted,
                "warmup_status": warmup_status,
                "message": "Cache cleared and models reloaded successfully." if warmup_status == "success" else "Cache cleared.",
                "next_request_latency": "~50-100ms (models cached)" if warmup_status == "success" else "~1-2s (will download from Blob)"
            }, indent=2),
            status_code=200,
            mimetype="application/json"
        )
        
    except Exception as e:
        logging.error(f"❌ Errore flush cache: {e}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=500,
            mimetype="application/json"
        )

# ============================================================================
# ASYNC BATCH CLASSIFICATION
# ============================================================================
@app.route(route="classify_batch", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def classify_batch(req: func.HttpRequest) -> func.HttpResponse:
    """
    Submit batch classification job (async processing)
    
    POST body:
    {
        "texts": ["News 1", "News 2", ...],
        "user_id": "optional_user_id",
        "priority": 0  # 0=normal, 1=high
    }
    
    Response 202 Accepted:
    {
        "job_id": "uuid",
        "status": "queued",
        "texts_count": 10,
        "created_at": "2026-02-09T20:30:00Z",
        "estimated_completion_seconds": 5.0
    }
    """
    start_time = time.time()
    
    try:
        req_body = req.get_json()
        if not req_body:
            return func.HttpResponse(
                json.dumps({"error": "Request body must be valid JSON"}),
                status_code=400,
                mimetype="application/json"
            )
        
        # Estrai e valida input
        texts = req_body.get('texts', [])
        if not isinstance(texts, list) or not texts:
            return func.HttpResponse(
                json.dumps({"error": "'texts' must be a non-empty array"}),
                status_code=400,
                mimetype="application/json"
            )
        
        # Limiti di sicurezza
        if len(texts) > 1000:
            return func.HttpResponse(
                json.dumps({"error": "Massimo 1000 testi per batch"}),
                status_code=400,
                mimetype="application/json"
            )
        
        # Validazione ogni testo
        valid_texts = []
        for idx, text in enumerate(texts):
            if not isinstance(text, str):
                return func.HttpResponse(
                    json.dumps({"error": f"Text at index {idx} is not a string"}),
                    status_code=400,
                    mimetype="application/json"
                )
            
            clean_text = text.strip()
            if len(clean_text) < 20:
                return func.HttpResponse(
                    json.dumps({"error": f"Text at index {idx} too short (min 20 chars)"}),
                    status_code=400,
                    mimetype="application/json"
                )
            valid_texts.append(clean_text)
        
        # Metadata opzionali
        user_id = req_body.get('user_id', 'anonymous')
        try:
            priority = int(req_body.get('priority', 0))
            if priority not in [0, 1]:
                priority = 0
        except (ValueError, TypeError):
            priority = 0
        
        # Inizializza JobManager (lazy)
        storage_conn = os.getenv('AzureWebJobsStorage')
        if not storage_conn:
            return func.HttpResponse(
                json.dumps({"error": "Azure Storage not configured"}),
                status_code=500,
                mimetype="application/json"
            )
        
        job_manager = JobManager(storage_conn)
        job_info: JobInfo = job_manager.create_job(
            texts=valid_texts,
            user_id=user_id,
            priority=priority
        )
        
        elapsed_ms = int((time.time() - start_time) * 1000)
        logging.info(
            f"📨 Batch job {job_info.job_id} created "
            f"({job_info.texts_count} texts, user={user_id}, prio={priority}, {elapsed_ms}ms)"
        )
        
        # 202 Accepted per async processing
        return func.HttpResponse(
            json.dumps(job_info.to_dict(), indent=2),
            status_code=202,
            mimetype="application/json",
            headers={
                "Location": f"/api/jobs/{job_info.job_id}",
                "X-Processing-Time": str(elapsed_ms)
            }
        )
    
    except Exception as e:
        logging.error(f"Batch classification error: {e}", exc_info=True)
        return func.HttpResponse(
            json.dumps({"error": "Internal server error"}),
            status_code=500,
            mimetype="application/json"
        )


# ============================================================================
# JOB STATUS (STUB - da implementare con Cosmos/Redis)
# ============================================================================
@app.route(route="jobs/{job_id}", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def get_job_status(req: func.HttpRequest) -> func.HttpResponse:
    """
    Response:
    {
        "job_id": "uuid",
        "status": "completed|processing|...",
        "progress": 100,
        "results": [...],
        "created_at": "2026-02-07T20:30:00Z"
    }
    """
    try:
        job_id = req.route_params.get('job_id')
        if not job_id:
            return func.HttpResponse(
                json.dumps({"error": "Missing job_id"}),
                status_code=400,
                mimetype="application/json"
            )
        
        # Legge stato del job da Redis 
        cache = get_redis_client()
        if cache:
            cache_key = f"job:status:{job_id}"
            cached_status = cache.get(cache_key)

            if cached_status:
                logging.info(f"⚡ Job status trovato per job: {job_id}")
                return func.HttpResponse(
                    cached_status.decode('utf-8'),
                    status_code=200,
                    mimetype="application/json"
                )
            return func.HttpResponse(
                json.dumps({"error": "Job not found", "job_id": job_id, "message": "Job may have expired from cache or never existed"}),
                status_code=404,
                mimetype="application/json"
            )
    
    except Exception as e:
        logging.error(f"Job status error {job_id}: {e}")
        return func.HttpResponse(
            json.dumps({"error": "Internal server error"}),
            status_code=500,
            mimetype="application/json"
        )


# ============================================================================
# ADMIN: QUEUE MONITORING
# ============================================================================
@app.route(route="sysadmin/queue/stats", methods=["GET"], auth_level=func.AuthLevel.ADMIN)
def queue_stats(req: func.HttpRequest) -> func.HttpResponse:
    """
    Get queue statistics (sysadmin only)
    
    Response:
    {
        "queue_depth": 15,
        "dlq_count": 2,
        "peek_messages": [...],
        "timestamp": "2026-02-09T20:30:00Z"
    }
    """
    try:
        storage_conn = os.getenv('STORAGE_CONNECTION_STRING')
        if not storage_conn:
            return func.HttpResponse(
                json.dumps({"error": "STORAGE_CONNECTION_STRING not configured"}),
                status_code=500,
                mimetype="application/json"
            )
        
        job_manager = JobManager(storage_conn)
        stats = {
            "queue_depth": job_manager.get_queue_depth(),
            "dlq_count": job_manager.get_dlq_count(),
            "peek_messages": job_manager.peek_messages(max_messages=5),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        logging.info(f"Queue stats: depth={stats['queue_depth']}, dlq={stats['dlq_count']}")
        
        return func.HttpResponse(
            json.dumps(stats, indent=2),
            status_code=200,
            mimetype="application/json"
        )
    
    except Exception as e:
        logging.error(f"Queue stats error: {e}", exc_info=True)
        return func.HttpResponse(
            json.dumps({"error": "Internal server error"}),
            status_code=500,
            mimetype="application/json"
        )


# ============================================================================
# WORKER: QUEUE MESSAGE PROCESSOR
# ============================================================================
@app.queue_trigger(
    arg_name="msg",
    queue_name="classifynews-queue",
    connection="AzureWebJobsStorage"  # ← Questa env var deve esistere
)
def process_classification_job(msg: func.QueueMessage) -> None:
    """
    Background worker: Process queued classification jobs
    Triggered by: Message in classifynews -queue
    Scaling: 0-200 instances based on queue depth
    Visibility timeout: 5 minutes (auto-extends if processing)
    Max dequeue count: 5 (then → dead-letter queue)
    """
    start_time = time.time()
    
    try:
        # Decode messaggio e estrai dati
        message_body = msg.get_body().decode('utf-8')
        job_data = json.loads(message_body)
        
        # Estrai dati obbligatori
        job_id = job_data.get('job_id')
        texts = job_data.get('texts', [])
        retry_count = job_data.get('retry_count', 0)
        user_id = job_data.get('user_id', 'anonymous')
        
        if not job_id or not texts:
            raise ValueError("Missing job_id or texts in message")
        
        logging.info(
            f"🔨 Worker processing job {job_id} "
            f"({len(texts)} texts, user={user_id}, retry={retry_count})"
        )
        
        # 🔹 AGGIORNA STATUS INIZIALE (PROCESSING)
        cache = get_redis_client()
        if cache:
            cache.setex(
                f"job:status:{job_id}",
                3600,  # 1 ora TTL
                json.dumps({
                    "job_id": job_id,
                    "status": JobStatus.PROCESSING,  
                    "progress": 0,
                    "processed": 0,
                    "total": len(texts),
                    "started_at": datetime.now(timezone.utc).isoformat()
                })
            )
        
        # 🔹 CARICA MODELLI ML 
        load_model_from_blob()  # Carica classifier e vectorizer globali
        
        # 🔹 PROCESSA OGNI TESTO
        results = []
        for idx, text in enumerate(texts):
            try:
                # Classificazione
                text_vec = vectorizer.transform([text])
                prediction = classifier.predict(text_vec)[0]
                confidence_scores = classifier.predict_proba(text_vec)[0]
                confidence = float(max(confidence_scores))
                
                results.append({
                    "index": idx,
                    "text_preview": text[:100] + "..." if len(text) > 100 else text,
                    "is_fake": prediction.upper() == "FAKE",
                    "label": prediction.upper(),
                    "confidence": round(confidence, 4),
                    "hash": hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
                })
                
                # Aggiorna progresso ogni 10% circa
                if (idx + 1) % max(1, len(texts) // 10) == 0 and cache:
                    progress = int(((idx + 1) / len(texts)) * 100)
                    cache.setex(
                        f"job:status:{job_id}",
                        3600,
                        json.dumps({
                            "job_id": job_id,
                            "status": "processing",
                            "progress": progress,
                            "processed": idx + 1,
                            "total": len(texts)
                        })
                    )
                
            except Exception as item_error:
                logging.error(f"✗ Item {idx} failed: {item_error}")
                results.append({
                    "index": idx,
                    "error": str(item_error)[:200]  # Truncate long errors
                })
        
        # 🔹 STATISTICHE FINALI
        fake_count = sum(1 for r in results if r.get('is_fake') is True)
        real_count = sum(1 for r in results if r.get('is_fake') is False)
        error_count = sum(1 for r in results if 'error' in r)
        
        # 🔹 SALVA RISULTATO FINALE
        job_result = {
            "job_id": job_id,
            "status": "completed",
            "progress": 100,
            "summary": {
                "total": len(texts),
                "fake": fake_count,
                "real": real_count,
                "errors": error_count
            },
            "results": results,
            "created_at": job_data.get('created_at'),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "processing_time_seconds": round(time.time() - start_time, 2)
        }
        
        # Cache risultato finale (24h)
        if cache:
            cache.setex(
                f"job:status:{job_id}",
                86400,  # 24 ore
                json.dumps(job_result)
            )
        
        # 🔹 LOG FINALE
        elapsed = time.time() - start_time
        logging.info(
            f"✅ Job {job_id} COMPLETED: "
            f"{fake_count}F/{real_count}R/{error_count}E "
            f"({elapsed:.1f}s, {len(results)} items)"
        )
        
    except json.JSONDecodeError as e:
        logging.error(f"❌ Invalid JSON message: {e}")
        raise  # Azure Functions retry → DLQ dopo 5 tentativi
    
    except ValueError as e:
        logging.error(f"❌ Invalid job data: {e}")
        raise
    
    except Exception as e:
        logging.error(f"❌ Worker CRASH job {job_id}: {e}", exc_info=True)
        raise  # Trigger retry mechanism (max 5 → DLQ)


@app.route(route="stayon", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def stayon(req: func.HttpRequest) -> func.HttpResponse:
    """
    Lightweight keepalive endpoint for Azure App Service warm-up.
    
    Questo endpoint serve a mantenere attiva l'istanza della Function App
    evitando cold starts quando non ci sono richieste per lunghi periodi.
    
    Caratteristiche:
    ----------------
    - NO autenticazione (ANONYMOUS) → accessibile da monitoring esterno
    - NO caricamento modelli → risposta rapida (~1-5ms)
    - NO accesso Redis/Blob → nessuna latenza I/O
    - NO elaborazione ML → solo health check
    
    Risposta HTTP:
    ---------------
    200 OK - Status sempre positivo (l'app è alive)
    {
        "status": "ok"
    }
    """
    return func.HttpResponse(
        '{"status": "ok"}',  # Payload minimo (2 byte gzipped)
        status_code=200,      # Success - App is responsive
        mimetype="application/json"  # Header Content-Type
    )
