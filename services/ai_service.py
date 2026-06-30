"""
Service IA — couche Modèle du MVC.

Encapsule toute la logique d'appel aux fournisseurs IA (Anthropic → Gemini →
DeepSeek → Ollama) avec fallback automatique. Les routes (Contrôleurs) ne
connaissent pas les fournisseurs — elles appellent uniquement `call_ai` ou
`call_ai_simple`.
"""
from __future__ import annotations
import os
import re


# ── Configuration ─────────────────────────────────────────────────────────────
GEMINI_MODEL      = "models/gemini-2.5-flash"
DEEPSEEK_API_URL  = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL    = "deepseek-chat"

_anthropic_client   = None
_gemini_clients: list = []
_gemini_index   = 0
_deepseek_key   = os.getenv("DEEPSEEK_API_KEY")
_ollama_url     = os.getenv("OLLAMA_API_URL", "").rstrip("/")
_ollama_key     = os.getenv("OLLAMA_API_KEY")
OLLAMA_MODEL      = os.getenv("OLLAMA_MODEL",      "qwen3.6:latest")
OLLAMA_MODEL_FAST = os.getenv("OLLAMA_MODEL_FAST", "gemma3:12b")


def init_ai_clients() -> None:
    """Initialiser les clients IA à partir des variables d'environnement.
    Appelé une seule fois au démarrage de l'application (depuis app.py).
    """
    global _anthropic_client, _gemini_clients, _gemini_index

    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if anthropic_key:
        try:
            from anthropic import Anthropic
            _anthropic_client = Anthropic(api_key=anthropic_key)
        except ImportError:
            pass

    gemini_keys = [v for k, v in sorted(os.environ.items())
                   if (k == "GEMINI_API_KEY" or k.startswith("GEMINI_API_KEY_")) and v]
    if gemini_keys:
        try:
            from google import genai as google_genai
            _gemini_clients = [google_genai.Client(api_key=k) for k in gemini_keys]
        except ImportError:
            pass

    if not _anthropic_client and not _gemini_clients and not _deepseek_key and not _ollama_key:
        print("WARNING: Aucune clé IA configurée")


# ── Appels bas niveau ─────────────────────────────────────────────────────────

def _call_anthropic(system_prompt: str, user_message: str, temperature: float,
                    max_tokens: int = 8192) -> str:
    message = _anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        timeout=90,
    )
    return message.content[0].text


def _next_gemini_client():
    global _gemini_index
    if not _gemini_clients:
        return None
    client = _gemini_clients[_gemini_index % len(_gemini_clients)]
    _gemini_index = (_gemini_index + 1) % len(_gemini_clients)
    return client


def _call_gemini(system_prompt: str, user_message: str, temperature: float) -> str:
    from google.genai import types as genai_types
    if not _gemini_clients:
        raise Exception("Aucune clé Gemini configurée")
    config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt or None,
        temperature=temperature,
    )
    last_error = None
    for _ in range(len(_gemini_clients)):
        gc = _next_gemini_client()
        try:
            response = gc.models.generate_content(
                model=GEMINI_MODEL, contents=user_message, config=config)
            return response.text
        except Exception as e:
            last_error = e
            print(f"WARNING Clé Gemini en rotation: {e}")
    raise last_error


def _call_deepseek(system_prompt: str, user_message: str, temperature: float,
                   max_tokens: int = 8192) -> str:
    if not _deepseek_key:
        raise Exception("Clé DeepSeek non configurée")
    import requests
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})
    proxies = {"https": "socks5h://127.0.0.1:9050", "http": "socks5h://127.0.0.1:9050"}
    resp = requests.post(
        DEEPSEEK_API_URL,
        headers={"Authorization": f"Bearer {_deepseek_key}", "Content-Type": "application/json"},
        json={"model": DEEPSEEK_MODEL, "messages": messages,
              "temperature": temperature, "max_tokens": max_tokens, "stream": False},
        proxies=proxies, timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_ollama(system_prompt: str, user_message: str, temperature: float,
                 max_tokens: int = 8192, fast: bool = False) -> str:
    if not _ollama_key or not _ollama_url:
        raise Exception("Ollama non configuré")
    import requests as _req
    model = OLLAMA_MODEL_FAST if fast else OLLAMA_MODEL
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})
    resp = _req.post(
        f"{_ollama_url}/api/chat",
        headers={"Authorization": f"Bearer {_ollama_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "stream": False,
              "think": False,
              "options": {"temperature": temperature, "num_predict": max_tokens}},
        timeout=180,
    )
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    return content


# ── API publique ──────────────────────────────────────────────────────────────

def call_ai(system_prompt: str, user_message: str,
            temperature: float = 0.2, max_tokens: int = 8192) -> str:
    """Appel IA avec fallback automatique Anthropic → Gemini → DeepSeek → Ollama.
    Lève une Exception si tous les fournisseurs sont indisponibles.
    """
    anthropic_err = gemini_err = deepseek_err = None

    if _anthropic_client:
        try:
            return _call_anthropic(system_prompt, user_message, temperature, max_tokens)
        except Exception as e:
            anthropic_err = str(e)
            print(f"WARNING Anthropic → Gemini: {e}")

    if _gemini_clients:
        try:
            return _call_gemini(system_prompt, user_message, temperature)
        except Exception as e:
            gemini_err = str(e)
            print(f"WARNING Gemini → DeepSeek: {e}")

    if _deepseek_key:
        try:
            return _call_deepseek(system_prompt, user_message, temperature, max_tokens)
        except Exception as e:
            deepseek_err = str(e)
            print(f"WARNING DeepSeek → Ollama: {e}")

    if _ollama_key and _ollama_url:
        try:
            return _call_ollama(system_prompt, user_message, temperature, max_tokens)
        except Exception as e:
            print(f"WARNING Ollama indisponible: {e}")

    if 'credit balance' in (anthropic_err or '').lower():
        raise Exception("Crédits Anthropic épuisés. Rechargez sur console.anthropic.com")
    if 'quota' in (gemini_err or '').lower() or 'resource_exhausted' in (gemini_err or '').lower():
        raise Exception("Quota Gemini épuisé. Rechargez sur aistudio.google.com")
    raise Exception("Le service d'intelligence artificielle est temporairement indisponible.")


def call_ai_simple(prompt: str) -> str:
    """Appel IA sans system prompt (tâches simples)."""
    return call_ai("", prompt, temperature=0.2, max_tokens=4000)


def build_correction_prompt(title: str = "", content_preview: str = "") -> str:
    """Construit le system prompt de correction universel."""
    context = ""
    if title:
        context += f"Titre de l'examen : {title}\n"
    if content_preview:
        context += f"Début du sujet : {content_preview[:500].strip()}\n"

    return f"""Tu es un correcteur d'examen universitaire EXTRÊMEMENT rigoureux et polyvalent.

{f"CONTEXTE DE L'EXAMEN :{chr(10)}{context}" if context else ""}
ÉTAPE 1 — IDENTIFICATION DU DOMAINE :
Identifie la discipline de cet examen et adopte le niveau d'expertise d'un professeur spécialiste.

IMPORTANT : Tu DOIS terminer ta correction par une ligne contenant EXACTEMENT :
Note totale: XX.XX/20

Format de correction :
=== CORRECTION DÉTAILLÉE ===
[Évaluation question par question avec justification précise selon les critères du barème]

=== RÉSUMÉ ===
Points forts : [...]
Points à améliorer : [...]

Note totale: XX.XX/20
"""


def extract_score(correction_text: str) -> float:
    """Extraire la note numérique depuis le texte de correction."""
    patterns = [
        r'Note totale\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'Note totale\s*:\s*(\d+\.?\d*)\s*/\s*(\d+)',
        r'Score\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'Note finale\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'Note\s*:\s*(\d+\.?\d*)\s*/\s*20',
        r'(\d+\.?\d*)\s*/\s*20\s*points?',
        r'(\d+\.?\d*)\s*sur\s*20',
    ]
    for pattern in patterns:
        m = re.search(pattern, correction_text, re.IGNORECASE)
        if m:
            score = float(m.group(1))
            if len(m.groups()) > 1 and m.group(2):
                score = (score / float(m.group(2))) * 20
            return round(score, 2)
    return 0.0
