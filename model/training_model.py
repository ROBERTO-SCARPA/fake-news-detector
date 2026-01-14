import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.naive_bayes import MultinomialNB
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, confusion_matrix
import pickle
import os

# Carica dataset
print("Caricamento dataset...")
df = pd.read_csv('fakenews_dataset_40k.csv', sep=';', encoding='utf-8-sig')
print(f"Dataset: {len(df)} righe")

# Dividi 80/20 train/test
X_train, X_test, y_train, y_test = train_test_split(
    df['text'], 
    df['label'], 
    test_size=0.2, 
    random_state=42
)

print(f"Training set: {len(X_train)} documenti")
print(f"Test set: {len(X_test)} documenti")

# Vettorizza testi con TF-IDF
print("\nVettorizzazione TF-IDF...")
vectorizer = TfidfVectorizer(max_features=5000, stop_words='english')
X_train_vec = vectorizer.fit_transform(X_train)
X_test_vec = vectorizer.transform(X_test)

print(f"Matrice TF-IDF train shape: {X_train_vec.shape}")

# Addestra classificatore Naive Bayes
print("\nAddestramento modello...")
clf = MultinomialNB(alpha=0.1)
clf.fit(X_train_vec, y_train)

# Predici su test set
print("Valutazione...")
y_pred = clf.predict(X_test_vec)

# Metriche
accuracy = accuracy_score(y_test, y_pred)
precision = precision_score(y_test, y_pred, average='weighted')
recall = recall_score(y_test, y_pred, average='weighted')
f1 = f1_score(y_test, y_pred, average='weighted')

print(f"\n=== METRICHE ===")
print(f"Accuracy:  {accuracy:.4f}")
print(f"Precision: {precision:.4f}")
print(f"Recall:    {recall:.4f}")
print(f"F1-Score:  {f1:.4f}")
print(f"\nConfusion Matrix:")
print(confusion_matrix(y_test, y_pred, labels=['fake', 'real']))

# Salva il modello e il vectorizer
print("\nSalvataggio modello...")
os.makedirs('model', exist_ok=True)

with open('model/classifier.pkl', 'wb') as f:
    pickle.dump(clf, f)

with open('model/vectorizer.pkl', 'wb') as f:
    pickle.dump(vectorizer, f)

print("✓ Modello salvato in model/classifier.pkl")
print("✓ Vectorizer salvato in model/vectorizer.pkl")
