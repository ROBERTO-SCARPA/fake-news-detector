import csv
import random

FAKE_FILE = "fake_sample_20K.csv"
REAL_FILE = "true_sample_20K.csv"
OUTPUT_FILE = "fakenews_dataset_40k.csv"

data = []

with open(FAKE_FILE, encoding="utf-8") as f:
    reader = csv.DictReader(f, delimiter=';')
    for row in reader:
        text = row["text"].strip()
        if text:
            data.append([text, "fake"])

with open(REAL_FILE, encoding="utf-8") as f:
    reader = csv.DictReader(f, delimiter=';')
    for row in reader:
        text = row["text"].strip()
        if text:
            data.append([text, "real"])

random.shuffle(data)

with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f, delimiter=';')
    writer.writerow(["text", "label"])
    writer.writerows(data)

print(f"Creato {OUTPUT_FILE} con {len(data)} record")
