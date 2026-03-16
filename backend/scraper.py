import httpx
from bs4 import BeautifulSoup
import re

async def fetch_html(url: str, timeout: float = 10.0) -> str:
    """
    Raises:
        httpx.HTTPStatusError: per status 4xx/5xx
        httpx.RequestError: per timeout, connessione rifiutata, ecc.
    """
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True
    ) as client:
        resp = await client.get(
            url,
            headers={"User-Agent": "FakeNewsDetectorBot/1.0"}
        )
        resp.raise_for_status()
        return resp.text


def extract_article_text(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # ── 1. Rimuovi tag non testuali e rumore strutturale ──────────────────────
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()

    # Rimuovi byline/autore
    for tag in soup.find_all(attrs={"class": re.compile(
        r"byline|author|contributor|reporter", re.IGNORECASE
    )}):
        tag.decompose()

    # Rimuovi blocchi "related articles / watch / promo"
    for tag in soup.find_all("div", class_=re.compile(
        r"related|recommended|more-on|see-also|watch|promo", re.IGNORECASE
    )):
        tag.decompose()

    # ── 2. Estrazione con priorità semantica ─────────────────────────────────
    text = ""

    # Priorità 1: <article> o <main>
    for tag_name in ["article", "main"]:
        tag = soup.find(tag_name)
        if tag:
            candidate = tag.get_text(separator=" ", strip=True)
            if len(candidate) > 200:
                text = candidate
                break

    # Priorità 2: figli diretti del body con testo lungo
    if not text:
        body = soup.body or soup
        candidates = []
        for tag in body.find_all(["div", "section"], recursive=False):
            candidate = tag.get_text(separator=" ", strip=True)
            if len(candidate) > 400:
                candidates.append((len(candidate), candidate))

        # Se i figli diretti non bastano, scendi un livello evitando nesting
        if not candidates:
            for tag in body.find_all(["div", "section"]):
                if tag.find(["div", "section"]):
                    continue
                candidate = tag.get_text(separator=" ", strip=True)
                if len(candidate) > 400:
                    candidates.append((len(candidate), candidate))

        if candidates:
            text = max(candidates, key=lambda x: x[0])[1]

    # Priorità 3: fallback totale
    if not text:
        body = soup.body or soup
        text = body.get_text(separator=" ", strip=True)

    # ── 3. Post-processing: rimuovi rumore testuale residuo ──────────────────

    # Rimuovi timestamp tipo "7 hours ago", "3 days ago"
    text = re.sub(r'\b\d+\s+(?:hours?|minutes?|days?)\s+ago\b', '', text, flags=re.IGNORECASE)

    # Rimuovi label UI da bottoni social
    text = re.sub(r'\b(?:Share|Save|Tweet|Print|Copy link)\b', '', text, flags=re.IGNORECASE)

    # Rimuovi righe finali che sono solo tag/categorie (≤8 parole senza punteggiatura)
    sentences = text.split('. ')
    cleaned_sentences = []
    for s in sentences:
        words = s.split()
        avg_word_len = sum(len(w) for w in words) / max(len(words), 1)
        if len(words) <= 8 and avg_word_len < 8 and not any(c in s for c in [',', ':', '"', "'"]):
            continue  # probabile tag list o label UI, scartata
        cleaned_sentences.append(s)
    text = '. '.join(cleaned_sentences)

    # Normalizza spazi multipli
    text = re.sub(r'\s{2,}', ' ', text).strip()

    return text