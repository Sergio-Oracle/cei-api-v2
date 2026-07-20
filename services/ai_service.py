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


def _call_ollama_vision(raw: bytes, instructions: str) -> str:
    """Analyse d'image via le modèle Ollama rapide (gemma3, multimodal) — seul
    fournisseur d'analyse média réellement configuré en l'absence de clé
    Anthropic/Gemini."""
    if not _ollama_key or not _ollama_url:
        raise Exception("Ollama non configuré")
    import base64
    import requests as _req
    b64 = base64.b64encode(raw).decode()
    resp = _req.post(
        f"{_ollama_url}/api/chat",
        headers={"Authorization": f"Bearer {_ollama_key}", "Content-Type": "application/json"},
        json={"model": OLLAMA_MODEL_FAST, "stream": False, "think": False,
              "messages": [{"role": "user", "content": _media_analysis_prompt(instructions), "images": [b64]}],
              "options": {"temperature": 0.2, "num_predict": 500}},
        timeout=90,
    )
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    return re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()


# ── API publique ──────────────────────────────────────────────────────────────

def call_ai(system_prompt: str, user_message: str,
            temperature: float = 0.2, max_tokens: int = 8192, fast: bool = False) -> str:
    """Appel IA avec fallback automatique Anthropic → Gemini → DeepSeek → Ollama.
    Lève une Exception si tous les fournisseurs sont indisponibles.
    `fast=True` utilise le modèle Ollama rapide (OLLAMA_MODEL_FAST) plutôt que
    le modèle lourd — pour les tâches courtes où le modèle par défaut est trop
    lent/instable sur ce serveur (ex: résumé d'un transcript audio).
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
            return _call_ollama(system_prompt, user_message, temperature, max_tokens, fast=fast)
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


# ── Analyse multimodale (image/audio/vidéo) ─────────────────────────────────
# Utilisée quand un enseignant joint un média à un sujet AVANT génération : le
# média est réellement analysé par l'IA (pas seulement stocké) pour qu'elle
# sache comment l'exploiter dans les questions générées.

_MEDIA_ANALYZE_MAX_BYTES = 15 * 1024 * 1024  # au-delà, on saute l'analyse (coût/latence/limites API)
_IMAGE_MIME_OK = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
_MEDIA_MIME_GUESS = {
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp',
    'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'ogg': 'audio/ogg', 'm4a': 'audio/mp4',
    'mp4': 'video/mp4', 'webm': 'video/webm',
}


def _media_analysis_prompt(instructions: str) -> str:
    consigne = instructions.strip() if instructions and instructions.strip() else \
        "(aucune consigne particulière — propose la meilleure exploitation pédagogique possible)"
    return (
        "Tu es un assistant pédagogique qui aide un enseignant universitaire à intégrer un média "
        "(image, audio ou vidéo) dans un sujet d'examen. Analyse précisément ce que contient ce fichier, "
        "puis explique en 3-5 phrases concrètes comment l'exploiter dans une ou plusieurs questions "
        "d'examen (ce qu'il montre/dit, quels concepts il permet d'évaluer, quelle consigne poser aux "
        "étudiants). Consigne de l'enseignant pour ce média : " + consigne
    )


def _call_anthropic_image_analysis(raw: bytes, mime_type: str, instructions: str) -> str:
    import base64
    b64 = base64.b64encode(raw).decode()
    message = _anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {"type": "text", "text": _media_analysis_prompt(instructions)},
            ],
        }],
        timeout=60,
    )
    return message.content[0].text


def _call_gemini_media_analysis(raw: bytes, mime_type: str, instructions: str) -> str:
    from google.genai import types as genai_types
    if not _gemini_clients:
        raise Exception("Aucune clé Gemini configurée")
    gc = _next_gemini_client()
    part = genai_types.Part.from_bytes(data=raw, mime_type=mime_type)
    response = gc.models.generate_content(
        model=GEMINI_MODEL, contents=[part, _media_analysis_prompt(instructions)])
    return response.text


# ── Repli local (ffmpeg + faster-whisper) quand ni Anthropic ni Gemini ne sont
# configurés — seul Ollama (gemma3/mistral3, vision texte+image, PAS audio/vidéo
# nativement) est disponible. On rend l'audio/vidéo exploitables en les
# ramenant au texte/image que ces modèles savent traiter :
#   - vidéo  → une frame représentative extraite via ffmpeg → chemin vision
#   - audio  → transcription locale via faster-whisper → chemin texte
_whisper_model = None


def _extract_video_frame(raw: bytes) -> bytes | None:
    """Extrait une image représentative d'une vidéo via ffmpeg (déjà installé
    sur le serveur) pour la faire analyser par le même modèle vision que les
    images — gemma3/mistral3 via Ollama ne comprennent pas la vidéo, mais très
    bien une image extraite de celle-ci."""
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f_in:
        f_in.write(raw)
        in_path = f_in.name
    out_path = in_path + '.jpg'
    try:
        for ts in ('00:00:01', '00:00:00'):
            subprocess.run(
                ['ffmpeg', '-y', '-ss', ts, '-i', in_path, '-vframes', '1', '-q:v', '3', out_path],
                capture_output=True, timeout=30,
            )
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                with open(out_path, 'rb') as f:
                    return f.read()
        return None
    except Exception as e:
        print(f"WARNING extraction frame vidéo échouée: {e}")
        return None
    finally:
        for p in (in_path, out_path):
            try:
                os.remove(p)
            except OSError:
                pass


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        size = os.getenv('WHISPER_MODEL_SIZE', 'small')
        _whisper_model = WhisperModel(size, device='cpu', compute_type='int8')
    return _whisper_model


def _transcribe_audio(raw: bytes, ext_hint: str) -> str:
    """Transcription locale (CPU) via faster-whisper — multilingue, français
    inclus. Aucun appel réseau : fonctionne même sans clé Anthropic/Gemini."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=f'.{ext_hint or "mp3"}', delete=False) as f:
        f.write(raw)
        path = f.name
    try:
        model = _get_whisper_model()
        segments, _info = model.transcribe(path, beam_size=5)
        return ' '.join(seg.text.strip() for seg in segments).strip()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _analyze_transcript(transcript: str, instructions: str) -> str:
    """Analyse pédagogique d'une transcription audio via le même mécanisme
    texte que le reste de l'app (Anthropic/Gemini/DeepSeek/Ollama selon ce qui
    est configuré). fast=True côté Ollama : un résumé court n'a pas besoin du
    modèle lourd, et celui-ci s'est montré instable (timeout 180s observé en
    test réel) sur ce serveur — le modèle rapide répond en ~20s."""
    prompt = (
        "Voici la transcription d'un enregistrement audio fourni par un enseignant "
        f"pour un sujet d'examen :\n\n« {transcript[:4000]} »\n\n"
        + _media_analysis_prompt(instructions)
    )
    return call_ai("", prompt, temperature=0.3, max_tokens=500, fast=True)


def analyze_media(media_type: str, raw: bytes, filename: str, content_type: str, instructions: str) -> str:
    """Analyse un média fourni par l'enseignant avant la génération d'un sujet
    (Retour équipe DFIP — insertion d'images/audio/vidéo pour accompagner les
    questions). Retourne une description textuelle exploitable par le prompt de
    génération. Ne lève jamais d'exception — un message explicatif est retourné
    en cas d'échec, pour ne jamais bloquer l'upload du média."""
    if len(raw) > _MEDIA_ANALYZE_MAX_BYTES:
        return "Analyse automatique indisponible (fichier trop volumineux) — le média sera tout de même inséré dans le sujet."

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    mime = content_type or ''

    try:
        if media_type == 'image':
            if mime not in _IMAGE_MIME_OK:
                mime = _MEDIA_MIME_GUESS.get(ext, 'image/jpeg')
            if _anthropic_client:
                return _call_anthropic_image_analysis(raw, mime, instructions)
            if _gemini_clients:
                return _call_gemini_media_analysis(raw, mime, instructions)
            if _ollama_key and _ollama_url:
                return _call_ollama_vision(raw, instructions)

        elif media_type == 'video':
            if _gemini_clients:
                if not mime or mime == 'application/octet-stream':
                    mime = _MEDIA_MIME_GUESS.get(ext, 'video/mp4')
                return _call_gemini_media_analysis(raw, mime, instructions)
            # Pas de Gemini (seul fournisseur vidéo natif) → repli frame + vision
            frame = _extract_video_frame(raw)
            if not frame:
                return "Analyse automatique indisponible (extraction d'image depuis la vidéo impossible) — le média sera tout de même inséré dans le sujet."
            if _anthropic_client:
                return _call_anthropic_image_analysis(frame, 'image/jpeg', instructions) + \
                    "\n\n(Analyse basée sur une image extraite de la vidéo, pas sur le mouvement ni le son.)"
            if _ollama_key and _ollama_url:
                return _call_ollama_vision(frame, instructions) + \
                    "\n\n(Analyse basée sur une image extraite de la vidéo, pas sur le mouvement ni le son.)"

        else:  # audio
            if _gemini_clients:
                if not mime or mime == 'application/octet-stream':
                    mime = _MEDIA_MIME_GUESS.get(ext, 'audio/mpeg')
                return _call_gemini_media_analysis(raw, mime, instructions)
            # Pas de Gemini → transcription locale (faster-whisper) + analyse texte
            transcript = _transcribe_audio(raw, ext)
            if not transcript:
                return "Analyse automatique indisponible (aucune parole détectée dans l'audio) — le média sera tout de même inséré dans le sujet."
            return _analyze_transcript(transcript, instructions)

        return "Analyse automatique indisponible (aucun service IA compatible configuré) — le média sera tout de même inséré dans le sujet."
    except Exception as e:
        print(f"WARNING analyse média échouée ({media_type}/{filename}): {e}")
        return "Analyse automatique indisponible — le média sera tout de même inséré dans le sujet."


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
