"""
Voice (TTS) providers - the user brings their OWN API keys.

One panel, many text-to-speech services: ElevenLabs, Cartesia, Microsoft
Edge TTS (free, no key at all), Sarvam, Google Cloud TTS, OpenAI, Azure
Speech, and Kokoro (local, optional install). Keys are pasted once in the
Voice panel and stored in the user data folder (`tts_keys.json`) - never
in the install folder, so an app upgrade keeps them, and they are only
ever sent to the provider the user picked.

Generated audio lands in the user data folder (`tts_outputs/`) and is
played back / downloaded from the panel. Nothing here talks to any
server except the chosen TTS provider.
"""

import asyncio
import base64
import json
import os
import time

import requests

TIMEOUT = 60
MAX_CHARS = 5000

# ------------------------------------------------------------------ storage

def _base():
    try:
        import userdata
        return userdata.base()
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


def _keys_path():
    return os.path.join(_base(), "tts_keys.json")


def _out_dir():
    d = os.path.join(_base(), "tts_outputs")
    os.makedirs(d, exist_ok=True)
    return d


def _load_keys():
    try:
        with open(_keys_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_keys(data):
    with open(_keys_path(), "w", encoding="utf-8") as f:
        json.dump(data, f)


def get_entry(provider):
    return _load_keys().get(provider) or {}


def get_key(provider):
    return (get_entry(provider).get("key") or "").strip()


def save_key(provider, key, region=""):
    if provider not in PROVIDERS:
        return {"ok": False, "error": "unknown provider"}
    data = _load_keys()
    key = (key or "").strip()
    if key:
        entry = {"key": key}
        if region.strip():
            entry["region"] = region.strip()
        data[provider] = entry
    else:
        data.pop(provider, None)
    try:
        _save_keys(data)
    except OSError as e:
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True, **providers_status_single(provider)}


# ---------------------------------------------------------------- providers

PROVIDERS = {
    "elevenlabs": {
        "name": "ElevenLabs",
        "needs_key": True,
        "key_hint": "ElevenLabs API key (elevenlabs.io -> Profile -> API Keys)",
        "note": "Sab se natural awaaz. Free plan: ~10,000 characters/month.",
    },
    "cartesia": {
        "name": "Cartesia",
        "needs_key": True,
        "key_hint": "Cartesia API key (play.cartesia.ai -> API Keys)",
        "note": "Sonic model - bahut fast, Hindi support ke saath.",
    },
    "edge": {
        "name": "Edge TTS (Microsoft)",
        "needs_key": False,
        "key_hint": "",
        "note": "Bilkul FREE - koi key nahi, koi limit nahi. Hindi awaazein bhi hain.",
    },
    "sarvam": {
        "name": "Sarvam AI",
        "needs_key": True,
        "key_hint": "Sarvam API subscription key (dashboard.sarvam.ai)",
        "note": "Indian provider - Hindi awaaz ke liye behtareen.",
    },
    "google": {
        "name": "Google Cloud TTS",
        "needs_key": True,
        "key_hint": "Google Cloud API key (Text-to-Speech API enabled)",
        "note": "Google ki neural awaazein, Hindi samet kai languages.",
    },
    "openai": {
        "name": "OpenAI TTS",
        "needs_key": True,
        "key_hint": "OpenAI API key (platform.openai.com)",
        "note": "gpt-4o-mini-tts model ki awaazein.",
    },
    "azure": {
        "name": "Azure Speech",
        "needs_key": True,
        "key_hint": "Azure Speech key - region bhi dalna hoga (jaise eastus)",
        "note": "Microsoft Azure ki neural awaazein; free quota bada hota hai.",
        "needs_region": True,
    },
    "kokoro": {
        "name": "Kokoro (Local)",
        "needs_key": False,
        "key_hint": "",
        "note": "PC par hi chalta hai, internet nahi chahiye - lekin model bhari "
                "hai, isliye alag se install karna padta hai (neeche status dekho).",
    },
}


def providers_status_single(provider):
    meta = PROVIDERS[provider]
    return {
        "id": provider,
        "name": meta["name"],
        "needs_key": meta["needs_key"],
        "needs_region": bool(meta.get("needs_region")),
        "key_hint": meta["key_hint"],
        "note": meta["note"],
        "has_key": bool(get_key(provider)) if meta["needs_key"] else True,
        "region": get_entry(provider).get("region", ""),
    }


def providers_status():
    return {"ok": True,
            "providers": [providers_status_single(p) for p in PROVIDERS]}


def _need_key(provider):
    key = get_key(provider)
    if not key:
        raise RuntimeError("%s ki API key pehle Voice panel me dalo."
                           % PROVIDERS[provider]["name"])
    return key


def _err(resp):
    msg = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            msg = (body.get("detail", {}).get("message")
                   if isinstance(body.get("detail"), dict) else "") \
                or body.get("message") or body.get("error", {}).get("message") \
                or body.get("error") or ""
            if not isinstance(msg, str):
                msg = json.dumps(msg)
    except Exception:
        msg = (resp.text or "")[:160]
    return RuntimeError("Provider ne jawab diya HTTP %d%s"
                        % (resp.status_code, (": " + msg[:160]) if msg else ""))


def _save_audio(raw, ext):
    name = "voice-%s.%s" % (time.strftime("%Y%m%d-%H%M%S"),
                             ext.lstrip("."))
    path = os.path.join(_out_dir(), name)
    with open(path, "wb") as f:
        f.write(raw)
    return {"ok": True, "file": name, "bytes": len(raw),
            "url": "/api/tts/audio?name=" + name}


def audio_path(name):
    name = os.path.basename(name or "")
    path = os.path.join(_out_dir(), name)
    return path if os.path.isfile(path) else None


# ------------------------------------------------------------------- voices

_EDGE_VOICES = [
    ("hi-IN-MadhurNeural", "Madhur (Hindi, Male)"),
    ("hi-IN-SwaraNeural", "Swara (Hindi, Female)"),
    ("en-IN-PrabhatNeural", "Prabhat (Indian English, Male)"),
    ("en-IN-NeerjaNeural", "Neerja (Indian English, Female)"),
    ("en-US-GuyNeural", "Guy (US English, Male)"),
    ("en-US-JennyNeural", "Jenny (US English, Female)"),
    ("en-GB-RyanNeural", "Ryan (UK English, Male)"),
    ("en-GB-SoniaNeural", "Sonia (UK English, Female)"),
]

_SARVAM_VOICES = [
    ("anushka", "Anushka (Female)"), ("vidya", "Vidya (Female)"),
    ("manisha", "Manisha (Female)"), ("arya", "Arya (Female)"),
    ("abhilash", "Abhilash (Male)"), ("karun", "Karun (Male)"),
    ("hitesh", "Hitesh (Male)"),
]

_OPENAI_VOICES = [(v, v.capitalize()) for v in
                  ("alloy", "ash", "ballad", "coral", "echo", "fable",
                   "nova", "onyx", "sage", "shimmer", "verse")]

_KOKORO_VOICES = [
    ("af_heart", "Heart (US English, Female)"),
    ("af_bella", "Bella (US English, Female)"),
    ("am_michael", "Michael (US English, Male)"),
    ("bf_emma", "Emma (UK English, Female)"),
    ("bm_george", "George (UK English, Male)"),
    ("hf_alpha", "Alpha (Hindi, Female)"),
    ("hf_beta", "Beta (Hindi, Female)"),
    ("hm_omega", "Omega (Hindi, Male)"),
]


def _rows(pairs):
    return [{"id": i, "name": n} for i, n in pairs]


def list_voices(provider):
    if provider == "edge":
        return {"ok": True, "voices": _rows(_EDGE_VOICES)}
    if provider == "sarvam":
        return {"ok": True, "voices": _rows(_SARVAM_VOICES)}
    if provider == "openai":
        return {"ok": True, "voices": _rows(_OPENAI_VOICES)}
    if provider == "kokoro":
        return {"ok": True, "voices": _rows(_KOKORO_VOICES)}
    if provider == "elevenlabs":
        key = _need_key(provider)
        r = requests.get("https://api.elevenlabs.io/v1/voices",
                         headers={"xi-api-key": key}, timeout=TIMEOUT)
        if r.status_code != 200:
            raise _err(r)
        out = []
        for v in r.json().get("voices", []):
            labels = v.get("labels") or {}
            meta = ", ".join(str(x) for x in labels.values() if x)
            out.append({"id": v.get("voice_id"),
                        "name": v.get("name") or v.get("voice_id"),
                        "meta": meta})
        return {"ok": True, "voices": out}
    if provider == "cartesia":
        key = _need_key(provider)
        r = requests.get("https://api.cartesia.ai/voices",
                         headers={"X-API-Key": key,
                                  "Cartesia-Version": "2024-06-10"},
                         timeout=TIMEOUT)
        if r.status_code != 200:
            raise _err(r)
        data = r.json()
        items = data if isinstance(data, list) else data.get("data", [])
        out = [{"id": v.get("id"), "name": v.get("name") or v.get("id"),
                "meta": v.get("language") or ""}
               for v in items if v.get("id")]
        return {"ok": True, "voices": out}
    if provider == "google":
        key = _need_key(provider)
        r = requests.get("https://texttospeech.googleapis.com/v1/voices",
                         params={"key": key}, timeout=TIMEOUT)
        if r.status_code != 200:
            raise _err(r)
        out = []
        for v in r.json().get("voices", []):
            name = v.get("name") or ""
            lang = (v.get("languageCodes") or [""])[0]
            if lang.startswith(("en", "hi")):
                out.append({"id": name, "name": name, "meta": lang})
            if len(out) >= 80:
                break
        return {"ok": True, "voices": out}
    if provider == "azure":
        key = _need_key(provider)
        region = (get_entry(provider).get("region") or "").strip()
        if not region:
            raise RuntimeError("Azure ke liye region bhi dalo (jaise eastus) "
                               "- key ke saath wahi box me save hota hai.")
        r = requests.get(
            "https://%s.tts.speech.microsoft.com/cognitiveservices/voices/list"
            % region,
            headers={"Ocp-Apim-Subscription-Key": key}, timeout=TIMEOUT)
        if r.status_code != 200:
            raise _err(r)
        out = []
        for v in r.json():
            locale = v.get("Locale") or ""
            if locale.startswith(("en", "hi")):
                out.append({"id": v.get("ShortName"),
                            "name": v.get("DisplayName") or v.get("ShortName"),
                            "meta": locale})
            if len(out) >= 80:
                break
        return {"ok": True, "voices": out}
    raise RuntimeError("unknown provider")


# -------------------------------------------------------------------- quota

def quota(provider):
    """Best-effort remaining-quota line; None when the provider has none."""
    try:
        if provider == "elevenlabs":
            key = _need_key(provider)
            r = requests.get(
                "https://api.elevenlabs.io/v1/user/subscription",
                headers={"xi-api-key": key}, timeout=TIMEOUT)
            if r.status_code != 200:
                raise _err(r)
            d = r.json()
            used = d.get("character_count") or 0
            limit = d.get("character_limit") or 0
            return {"ok": True,
                    "text": "Quota: %s / %s characters is mahine use hue, "
                            "%s bache hain."
                            % (f"{used:,}", f"{limit:,}",
                               f"{max(limit - used, 0):,}")}
        if provider == "edge":
            return {"ok": True, "text": "Quota: koi limit nahi - Edge TTS bilkul free hai."}
        if provider == "kokoro":
            return {"ok": True, "text": "Quota: local hai - koi limit nahi."}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True, "text": ""}


# ----------------------------------------------------------------- generate

def generate(provider, voice_id, text, language=""):
    text = (text or "").strip()
    if not text:
        raise RuntimeError("Pehle text/script likho.")
    if len(text) > MAX_CHARS:
        raise RuntimeError("Ek baar me zyada se zyada %d characters. "
                           "Text ko hisso me baant kar banao." % MAX_CHARS)
    if provider not in PROVIDERS:
        raise RuntimeError("unknown provider")
    if PROVIDERS[provider]["needs_key"]:
        _need_key(provider)
    if not voice_id:
        raise RuntimeError("Pehle awaaz (voice) chuno - 'Voices load karo' dabao.")
    fn = _GEN[provider]
    return fn(voice_id, text, (language or "").strip())


def _gen_elevenlabs(voice_id, text, language):
    key = get_key("elevenlabs")
    r = requests.post(
        "https://api.elevenlabs.io/v1/text-to-speech/%s" % voice_id,
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        params={"output_format": "mp3_44100_128"},
        json={"text": text, "model_id": "eleven_multilingual_v2"},
        timeout=TIMEOUT)
    if r.status_code != 200:
        raise _err(r)
    return _save_audio(r.content, "mp3")


def _gen_cartesia(voice_id, text, language):
    key = get_key("cartesia")
    r = requests.post(
        "https://api.cartesia.ai/tts/bytes",
        headers={"X-API-Key": key, "Cartesia-Version": "2024-06-10",
                 "Content-Type": "application/json"},
        json={"model_id": "sonic-2", "transcript": text,
              "voice": {"mode": "id", "id": voice_id},
              "output_format": {"container": "mp3", "encoding": "mp3",
                                "sample_rate": 44100}},
        timeout=TIMEOUT)
    if r.status_code != 200:
        raise _err(r)
    return _save_audio(r.content, "mp3")


def _gen_edge(voice_id, text, language):
    try:
        import edge_tts
    except ImportError:
        raise RuntimeError("edge-tts package is install me nahi mila - "
                           "app update karke dekho.")
    out = os.path.join(_out_dir(),
                       "voice-%s.mp3" % time.strftime("%Y%m%d-%H%M%S"))

    async def _run():
        proxy = (os.environ.get("HTTPS_PROXY")
                 or os.environ.get("https_proxy") or None)
        comm = edge_tts.Communicate(text, voice_id, proxy=proxy)
        await comm.save(out)

    asyncio.run(_run())
    size = os.path.getsize(out) if os.path.isfile(out) else 0
    if size < 1000:
        raise RuntimeError("Edge TTS se khaali audio aaya - dobara try karo.")
    name = os.path.basename(out)
    return {"ok": True, "file": name, "bytes": size,
            "url": "/api/tts/audio?name=" + name}


def _gen_sarvam(voice_id, text, language):
    key = get_key("sarvam")
    r = requests.post(
        "https://api.sarvam.ai/text-to-speech",
        headers={"api-subscription-key": key,
                 "Content-Type": "application/json"},
        json={"inputs": [text],
              "target_language_code": language or "hi-IN",
              "speaker": voice_id, "model": "bulbul:v2",
              "enable_preprocessing": True},
        timeout=TIMEOUT)
    if r.status_code != 200:
        raise _err(r)
    audios = r.json().get("audios") or []
    if not audios:
        raise RuntimeError("Sarvam se audio nahi aaya.")
    return _save_audio(base64.b64decode(audios[0]), "wav")


def _gen_google(voice_id, text, language):
    key = get_key("google")
    lang = language or (voice_id.rsplit("-", 1)[0] if voice_id else "en-US")
    r = requests.post(
        "https://texttospeech.googleapis.com/v1/text:synthesize",
        params={"key": key},
        json={"input": {"text": text},
              "voice": {"languageCode": lang, "name": voice_id},
              "audioConfig": {"audioEncoding": "MP3"}},
        timeout=TIMEOUT)
    if r.status_code != 200:
        raise _err(r)
    content = r.json().get("audioContent") or ""
    if not content:
        raise RuntimeError("Google se audio nahi aaya.")
    return _save_audio(base64.b64decode(content), "mp3")


def _gen_openai(voice_id, text, language):
    key = get_key("openai")
    r = requests.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"},
        json={"model": "gpt-4o-mini-tts", "voice": voice_id, "input": text},
        timeout=TIMEOUT)
    if r.status_code != 200:
        raise _err(r)
    return _save_audio(r.content, "mp3")


def _gen_azure(voice_id, text, language):
    entry = get_entry("azure")
    key = entry.get("key") or ""
    region = (entry.get("region") or "").strip()
    if not region:
        raise RuntimeError("Azure ke liye region bhi dalo (jaise eastus).")
    lang = language or (voice_id.split("-")[0] + "-" + voice_id.split("-")[1]
                        if voice_id.count("-") >= 2 else "en-US")
    ssml = ("<speak version='1.0' xml:lang='%s'>"
            "<voice name='%s'>%s</voice></speak>"
            % (lang, voice_id,
               text.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;")))
    r = requests.post(
        "https://%s.tts.speech.microsoft.com/cognitiveservices/v1" % region,
        headers={"Ocp-Apim-Subscription-Key": key,
                 "Content-Type": "application/ssml+xml",
                 "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3"},
        data=ssml.encode("utf-8"), timeout=TIMEOUT)
    if r.status_code != 200:
        raise _err(r)
    return _save_audio(r.content, "mp3")


def _gen_kokoro(voice_id, text, language):
    try:
        from kokoro import KPipeline
        import soundfile as sf
    except ImportError:
        raise RuntimeError(
            "Kokoro is PC par install nahi hai. Ye local model ~2 GB ka "
            "hota hai, isliye Setup ke saath nahi aata. Kokoro chahiye to "
            "MK ko bolo - alag se setup kar dega. Tab tak Edge TTS bilkul "
            "free me wahi kaam kar dega.")
    lang_code = (voice_id or "a")[0]
    pipeline = KPipeline(lang_code=lang_code)
    chunks = []
    for _, _, audio in pipeline(text, voice=voice_id):
        chunks.append(audio)
    if not chunks:
        raise RuntimeError("Kokoro se audio nahi bana.")
    import numpy as np
    full = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    name = "voice-%s.wav" % time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(_out_dir(), name)
    sf.write(path, full, 24000)
    return {"ok": True, "file": name, "bytes": os.path.getsize(path),
            "url": "/api/tts/audio?name=" + name}


_GEN = {
    "elevenlabs": _gen_elevenlabs,
    "cartesia": _gen_cartesia,
    "edge": _gen_edge,
    "sarvam": _gen_sarvam,
    "google": _gen_google,
    "openai": _gen_openai,
    "azure": _gen_azure,
    "kokoro": _gen_kokoro,
}
