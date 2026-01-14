import azure.functions as func
import json
import pickle
import logging
from azure.storage.blob import BlobServiceClient
import os

app = func.FunctionApp()

# Variabili globali (cache del modello)
classifier = None
vectorizer = None

def load_model_from_blob():
    """Scarica il modello da Blob Storage una sola volta."""
    global classifier, vectorizer
    
    if classifier is not None and vectorizer is not None:
        logging.info("Modello già in memoria, skip download")
        return  # Modello già in memoria
    
    logging.info("Caricamento modello da Blob Storage...")
    
    try:
        # Ottieni connection string da variabili d'ambiente
        connection_string = os.getenv('AzureWebJobsStorage')
        
        if not connection_string:
            raise ValueError("AzureWebJobsStorage non configurata")
        
        # Connetti a Blob
        blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        container_client = blob_service_client.get_container_client('models')
        
        # Scarica classifier
        logging.info("Download classifier.pkl...")
        classifier_blob = container_client.get_blob_client('classifier.pkl')
        classifier_data = classifier_blob.download_blob().readall()
        classifier = pickle.loads(classifier_data)
        
        # Scarica vectorizer
        logging.info("Download vectorizer.pkl...")
        vectorizer_blob = container_client.get_blob_client('vectorizer.pkl')
        vectorizer_data = vectorizer_blob.download_blob().readall()
        vectorizer = pickle.loads(vectorizer_data)
        
        logging.info("✓ Modello caricato da Blob Storage")
    
    except Exception as e:
        logging.error(f"Errore caricamento modello: {str(e)}")
        raise

@app.route(route="classify_news", auth_level=func.AuthLevel.ANONYMOUS)
def classify_news(req: func.HttpRequest) -> func.HttpResponse:
    """HTTP trigger per classificare news come fake o real."""
    logging.info('Richiesta di classificazione ricevuta')
    
    try:
        # Carica modello se non già in memoria
        load_model_from_blob()
        
        # Leggi JSON dalla richiesta
        try:
            req_body = req.get_json()
        except ValueError:
            return func.HttpResponse(
                json.dumps({"error": "Richiesta non è JSON valido"}),
                status_code=400,
                mimetype="application/json"
            )
        
        text = req_body.get('text', '').strip()
        
        if not text:
            return func.HttpResponse(
                json.dumps({"error": "Fornisci un campo 'text' con il contenuto della news"}),
                status_code=400,
                mimetype="application/json"
            )
        
        logging.info(f"Testo da classificare: {text[:50]}...")
        
        # Vettorizza il testo
        text_vec = vectorizer.transform([text])
        
        # Predici
        prediction = classifier.predict(text_vec)[0]
        confidence_scores = classifier.predict_proba(text_vec)[0]
        confidence = float(max(confidence_scores))
        
        # Risposta
        response = {
            "text": text[:100] + "..." if len(text) > 100 else text,
            "label": prediction,
            "confidence": round(confidence, 4),
            "is_fake": prediction == 'fake'
        }
        
        logging.info(f"Predizione: {prediction} (confidence: {confidence:.4f})")
        
        return func.HttpResponse(
            json.dumps(response, indent=2),
            status_code=200,
            mimetype="application/json"
        )
    
    except Exception as e:
        logging.error(f"Errore nel processing: {str(e)}")
        return func.HttpResponse(
            json.dumps({"error": f"Errore interno: {str(e)}"}),
            status_code=500,
            mimetype="application/json"
        )
