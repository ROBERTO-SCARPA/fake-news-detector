# Fake News Detector (Azure + Machine Learning)

Progetto di Cloud Computing per il rilevamento automatico di fake news
tramite modello di Machine Learning addestrato localmente e servito
tramite Azure Functions.

## Architettura
- Training modello ML: locale (Python + scikit-learn)
- Storage modello: Azure Blob Storage
- Inference: Azure Functions (HTTP Trigger)
- Frontend: Static Website su Azure Blob Storage

## Tecnologie
- Python
- scikit-learn
- Azure Functions
- Azure Blob Storage

