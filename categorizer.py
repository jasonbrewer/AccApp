"""
LocalLedger — categorization.

This is the ONE place the AI lives, hidden behind a single small interface.
That's deliberate: today it points at Ollama on localhost; if you ever ship on
the Mac App Store (where an external Ollama server is a review problem), you swap
ONLY this file for a bundled on-device model. Nothing else in the app changes.

Pipeline for each transaction:
    1. Exact learned rule (merchant_norm -> category)         [deterministic, no AI]
    2. Local Ollama suggestion, if reachable                  [AI, stays on your Mac]
    3. Fallback: "Uncategorized"                              [never blocks you]

Everything here is local. The only network call is to http://localhost:11434,
your own machine. Nothing is ever sent to a cloud service.
"""

import json
import re
import urllib.request

OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2"   # change to any model you've pulled, e.g. qwen2.5, mistral

# Generic bank / processor vocabulary. These words wrap the real merchant on
# almost every statement ("MERCHANT PURCHASE TERMINAL 55421356 PMUSA ..."), so
# they must never become the matching key themselves — otherwise every such
# transaction collapses onto one key and teaching one mis-teaches all of them.
# Merchant names never go in here.
BOILERPLATE = {
    "POS", "PURCHASE", "WITHDRAWAL", "DEPOSIT", "MERCHANT", "TERMINAL", "ACH",
    "DEBIT", "CREDIT", "CARD", "PAYMENT", "PMT", "RECURRING", "PREAUTH",
    "PREAUTHORIZED", "PENDING", "AUTH", "EFT", "WEB", "PPD", "DES", "INDN",
    "TEL", "REF", "TRACE", "VISA", "MC", "SQ", "TST", "CKCD",
}

_SEPARATORS = re.compile(r"[^A-Z0-9& ]")     # *, ., /, - ... all become spaces
_MASKED_CARD = re.compile(r"^X+[0-9]*$")     # XXXXXXXX, XXXX1545
_WORD = re.compile(r"^[A-Z&]+$")             # letters (and &) only


def _is_noise(token: str) -> bool:
    """Boilerplate word, reference/terminal number, or a masked card block."""
    return (
        token in BOILERPLATE
        or sum(c.isdigit() for c in token) >= 2     # 55421356, B25S28KZ1, 00483
        or bool(_MASKED_CARD.match(token))
    )


def normalize_merchant(description: str) -> str:
    """Turn a bank description into a stable-ish merchant matching key.

    Noise is stripped wherever it appears — not just at the front — so the real
    merchant surfaces before the key is taken:

        'SQ *CIRCLE K #482 0834'                        -> 'CIRCLE WEST'
        'MERCHANT PURCHASE TERMINAL 554213 PMUSA ...'   -> 'PMUSA RICHMOND'

    Still only two words: precise enough not to over-match, which is what the
    rule lookup (exact OR leading-prefix, longest wins) expects.
    """
    tokens = _SEPARATORS.sub(" ", description.upper()).split()
    content = [t for t in tokens
               if not _is_noise(t) and len(t) >= 2 and _WORD.match(t)]
    key = " ".join(content[:2])
    if key:
        return key
    # All boilerplate and numbers (e.g. 'POS PURCHASE'): never return "".
    return " ".join(description.upper().split()[:2]) or description.upper().strip()


def ollama_available(timeout=1.5) -> bool:
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


class Categorizer:
    """Rules first, optional Ollama second, fallback last."""

    def __init__(self, conn, category_names, model=DEFAULT_MODEL, use_ollama=None):
        self.conn = conn
        self.categories = category_names
        self.model = model
        # If not told explicitly, probe once at startup.
        self.use_ollama = ollama_available() if use_ollama is None else use_ollama

    # --- step 1: learned rules --------------------------------------------
    def rule_lookup(self, merchant_norm):
        # Exact match, or a rule that is a leading word-sequence of this merchant
        # (so a short rule "ADOBE" catches "ADOBE CREATIVE"). Most specific wins.
        row = self.conn.execute(
            """SELECT c.name AS cat, r.use AS use
                 FROM merchant_rules r JOIN categories c ON c.id = r.category_id
                WHERE r.merchant_norm = :m OR :m LIKE r.merchant_norm || ' %'
                ORDER BY LENGTH(r.merchant_norm) DESC LIMIT 1""",
            {"m": merchant_norm},
        ).fetchone()
        if row:
            return {"category": row["cat"], "use": row["use"], "source": "rule", "confidence": 1.0}
        return None

    # --- step 2: local Ollama --------------------------------------------
    def ollama_lookup(self, description, amount_cents):
        prompt = (
            "You categorize a single bank transaction. Choose exactly one category "
            "from this list and reply as JSON only.\n"
            f"Categories: {', '.join(self.categories)}\n"
            f'Transaction: "{description}"  Amount: {amount_cents/100:.2f}\n'
            'Reply: {"category": "<one of the list>", "confidence": <0..1>}'
        )
        body = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }).encode()
        try:
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/generate", data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.loads(r.read())
            data = json.loads(payload.get("response", "{}"))
            cat = data.get("category", "").strip()
            if cat not in self.categories:      # trust nothing: validate against our list
                return None
            conf = float(data.get("confidence", 0.5))
            return {"category": cat, "use": None, "source": "ai", "confidence": conf}
        except Exception:
            return None

    # --- the pipeline -----------------------------------------------------
    def categorize(self, description, amount_cents, merchant_norm):
        return (
            self.rule_lookup(merchant_norm)
            or (self.ollama_lookup(description, amount_cents) if self.use_ollama else None)
            or {"category": "Uncategorized", "use": None, "source": "none", "confidence": 0.0}
        )
