"""
Job Manager Module
Gestisce job asincroni di classificazione tramite Azure Queue Storage.
"""

from __future__ import annotations

import uuid
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from azure.storage.queue import (
    QueueClient,
    BinaryBase64EncodePolicy,
    BinaryBase64DecodePolicy,
)
from azure.core.exceptions import ResourceNotFoundError


# ============================================================================
# CONFIGURAZIONE
# ============================================================================

DEFAULT_QUEUE_NAME = "classifynews-queue"
DEFAULT_DLQ_NAME = "classifynews-deadletter"

# TTL messaggi: 7 giorni (in secondi)
DEFAULT_MESSAGE_TTL_SECONDS = 7 * 24 * 60 * 60  # 604800

# Stima tempo per testo (secondi) - tuning facile
ESTIMATED_SECONDS_PER_TEXT = 0.5


# ============================================================================
# MODELLI & COSTANTI
# ============================================================================

class JobStatus:
    """Costanti di stato dei job."""
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobInfo:
    """Metadati di un job asincrono."""
    job_id: str
    status: str
    texts_count: int
    created_at: str
    estimated_completion_seconds: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "texts_count": self.texts_count,
            "created_at": self.created_at,
            "estimated_completion_seconds": self.estimated_completion_seconds,
        }


# ============================================================================
# JOB MANAGER
# ============================================================================

class JobManager:
    """
    Gestisce job asincroni di classificazione usando Azure Queue Storage.

    - Accoda job sulla coda principale (classify-queue)
    - Sposta messaggi che falliscono ripetutamente sulla DLQ (classify-deadletter)
    - Espone metodi di monitoring (depth, peek, clear)
    """

    def __init__(
        self,
        connection_string: str,
        queue_name: str = DEFAULT_QUEUE_NAME,
        dlq_name: str = DEFAULT_DLQ_NAME,
        message_ttl_seconds: int = DEFAULT_MESSAGE_TTL_SECONDS,
    ) -> None:
        """
        Inizializza il JobManager.

        Args:
            connection_string: Azure Storage connection string.
            queue_name: Nome della coda principale.
            dlq_name: Nome della dead-letter queue.
            message_ttl_seconds: TTL dei messaggi in secondi.
        """
        if not connection_string:
            raise ValueError("connection_string must not be empty")

        self.connection_string = connection_string
        self.queue_name = queue_name
        self.dlq_name = dlq_name
        self.message_ttl_seconds = message_ttl_seconds

        # Client coda principale
        self.queue_client = QueueClient.from_connection_string(
            conn_str=connection_string,
            queue_name=queue_name,
            message_encode_policy=BinaryBase64EncodePolicy(),
            message_decode_policy=BinaryBase64DecodePolicy(),
        )

        # Client DLQ
        self.dlq_client = QueueClient.from_connection_string(
            conn_str=connection_string,
            queue_name=dlq_name,
            message_encode_policy=BinaryBase64EncodePolicy(),
            message_decode_policy=BinaryBase64DecodePolicy(),
        )

        logging.info(
            "JobManager inizializzato (queue=%s, dlq=%s, ttl=%ss)",
            queue_name,
            dlq_name,
            message_ttl_seconds,
        )

    # ---------------------------------------------------------------------- #
    # CREAZIONE JOB
    # ---------------------------------------------------------------------- #

    def create_job(
        self,
        texts: List[str],
        user_id: str = "anonymous",
        priority: int = 0,
    ) -> JobInfo:
        """
        Crea un job asincrono di classificazione.

        Args:
            texts: Lista di testi da classificare.
            user_id: Identificativo utente (per tracking).
            priority: Priorità job (0 = normale, 1 = alta).

        Returns:
            JobInfo con metadati del job creato.
        """
        if not texts:
            raise ValueError("texts list must not be empty")

        # Normalizza/filtra testi vuoti
        clean_texts = [t.strip() for t in texts if t and t.strip()]
        if not clean_texts:
            raise ValueError("texts list contains only empty items")

        job_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        job_message = {
            "job_id": job_id,
            "user_id": user_id or "anonymous",
            "texts": clean_texts,
            "priority": int(priority),
            "created_at": created_at,
            "retry_count": 0,
        }

        try:
            message_json = json.dumps(job_message, ensure_ascii=False).encode("utf-8")

            # visibility_timeout=0 → subito visibile per il worker
            self.queue_client.send_message(
                content=message_json,
                visibility_timeout=0,
                time_to_live=self.message_ttl_seconds,
            )

            texts_count = len(clean_texts)
            est_seconds = texts_count * ESTIMATED_SECONDS_PER_TEXT

            logging.info(
                "✅ Job %s accodato (%d testi, user=%s, priority=%d)",
                job_id,
                texts_count,
                user_id,
                priority,
            )

            return JobInfo(
                job_id=job_id,
                status=JobStatus.QUEUED,
                texts_count=texts_count,
                created_at=created_at,
                estimated_completion_seconds=est_seconds,
            )

        except Exception as e:
            logging.error("❌ Impossibile accodare job: %s", e, exc_info=True)
            raise

    # ---------------------------------------------------------------------- #
    # MONITORING & ADMIN
    # ---------------------------------------------------------------------- #

    def get_queue_depth(self) -> int:
        """
        Ritorna il numero approssimativo di messaggi nella coda principale.

        Returns:
            Approximate message count (0 in caso di errore).
        """
        try:
            properties = self.queue_client.get_queue_properties()
            count = properties.approximate_message_count or 0
            logging.debug("Queue depth (%s): %d", self.queue_name, count)
            return count
        except ResourceNotFoundError:
            logging.warning("Queue non trovata: %s", self.queue_name)
            return 0
        except Exception as e:
            logging.error("Errore get_queue_depth: %s", e, exc_info=True)
            return 0

    def get_dlq_count(self) -> int:
        """
        Ritorna il numero approssimativo di messaggi nella DLQ.

        Returns:
            Approximate DLQ message count (0 in caso di errore).
        """
        try:
            properties = self.dlq_client.get_queue_properties()
            count = properties.approximate_message_count or 0
            logging.debug("DLQ depth (%s): %d", self.dlq_name, count)
            return count
        except ResourceNotFoundError:
            logging.warning("DLQ non trovata: %s", self.dlq_name)
            return 0
        except Exception as e:
            logging.error("Errore get_dlq_count: %s", e, exc_info=True)
            return 0

    def clear_queue(self) -> None:
        """
        Cancella tutti i messaggi dalla coda principale (operazione admin).
        """
        try:
            self.queue_client.clear_messages()
            logging.info("🧹 Queue %s svuotata", self.queue_name)
        except Exception as e:
            logging.error("Errore clear_queue: %s", e, exc_info=True)

    # ---------------------------------------------------------------------- #
    # DEAD-LETTER HANDLING
    # ---------------------------------------------------------------------- #

    def move_to_dlq(self, message_content: str, error: str) -> None:
        """
        Sposta un messaggio fallito nella dead-letter queue.

        Args:
            message_content: Messaggio originale (JSON string).
            error: Descrizione dell'errore.
        """
        try:
            try:
                original = json.loads(message_content)
            except json.JSONDecodeError:
                original = {"raw": message_content}

            dlq_message = {
                "original_message": original,
                "error": error,
                "failed_at": datetime.now(timezone.utc).isoformat(),
            }

            self.dlq_client.send_message(json.dumps(dlq_message, ensure_ascii=False).encode("utf-8"))
            logging.warning("⚠️ Messaggio move_to_dlq eseguito: %s", error)

        except Exception as e:
            logging.error("Errore move_to_dlq: %s", e, exc_info=True)

    # ---------------------------------------------------------------------- #
    # PEEK (MONITORING NON REMOVING)
    # ---------------------------------------------------------------------- #

    def peek_messages(self, max_messages: int = 5) -> List[Dict[str, Any]]:
        """
        Esegue un peek dei messaggi in coda senza rimuoverli.

        Args:
            max_messages: Numero massimo di messaggi da leggere.

        Returns:
            Lista di dict con metadati dei messaggi.
        """
        if max_messages <= 0:
            return []

        try:
            messages = self.queue_client.peek_messages(max_messages=max_messages)
            result: List[Dict[str, Any]] = []

            for msg in messages:
                try:
                    content = json.loads(msg.content)
                except Exception:
                    content = msg.content  # fallback raw

                result.append(
                    {
                        "id": msg.id,
                        "content": content,
                        "dequeue_count": getattr(msg, "dequeue_count", None),
                        "insertion_time": msg.insertion_time.isoformat()
                        if getattr(msg, "insertion_time", None)
                        else None,
                    }
                )

            logging.debug("Peeked %d messages from %s", len(result), self.queue_name)
            return result

        except Exception as e:
            logging.error("Errore peek_messages: %s", e, exc_info=True)
            return []
