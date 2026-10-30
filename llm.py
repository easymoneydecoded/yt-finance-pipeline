"""
================================================================================
 llm.py - Cliente Gemini compartido por todas las etapas
================================================================================

 - Dos niveles de modelo (free tier):
     SMART -> guion, juez de calidad, fact-check  (GEMINI_MODEL_SMART)
     FAST  -> ideas, keywords, tareas mecanicas   (GEMINI_MODEL_FAST)
   Si el modelo SMART no existe o agota su cuota diaria, cae al FAST.
 - Reintentos con espera ante 429/5xx.
 - research(): una llamada con Google Search activado para traer datos
   reales y verificables sobre la historia del dia (grounding gratuito).
================================================================================
"""

import os
import re
import json
import time

import requests
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_FAST     = os.getenv("GEMINI_MODEL_FAST", "gemini-3.5-flash-lite")
MODEL_SMART    = os.getenv("GEMINI_MODEL_SMART", "gemini-3.5-flash")
ENDPOINT       = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

ATTEMPTS_PER_MODEL = 4
OVERLOAD_ATTEMPTS  = 2    # 503 "modelo saturado": tras 2 intentos, mejor el siguiente modelo
MAX_RATE_WAIT      = 65   # espera maxima ante limite por minuto

_research_disabled = False   # si Google Search no esta en el plan, no se reintenta en todo el run


class LLMError(RuntimeError):
    pass


def _models(smart: bool) -> list[str]:
    chain = [MODEL_SMART, MODEL_FAST] if smart else [MODEL_FAST]
    return list(dict.fromkeys(m for m in chain if m))


def _post(model: str, payload: dict, timeout: int) -> tuple[int, dict | str]:
    r = requests.post(
        ENDPOINT.format(model=model),
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


def _extract_text(data: dict) -> str:
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def generate(prompt: str, *, smart: bool = False, json_mode: bool = True,
             temperature: float = 0.7, max_tokens: int = 8000,
             tools: list | None = None, timeout: int = 180) -> str:
    """Devuelve el texto de la respuesta. Lanza LLMError si fallan todos los modelos."""
    if not GEMINI_API_KEY:
        raise LLMError("Falta GEMINI_API_KEY")

    config = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if json_mode and not tools:
        config["responseMimeType"] = "application/json"
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config}
    if tools:
        payload["tools"] = tools

    last_err = ""
    for model in _models(smart):
        for attempt in range(1, ATTEMPTS_PER_MODEL + 1):
            try:
                status, data = _post(model, payload, timeout)
            except requests.exceptions.RequestException as e:
                last_err = f"red: {e}"
                time.sleep(15 * attempt)
                continue

            if status == 200:
                try:
                    text = _extract_text(data)
                    if text.strip():
                        return text
                    last_err = "respuesta vacia"
                except (KeyError, IndexError, TypeError):
                    last_err = f"respuesta sin texto: {str(data)[:200]}"
                print(f"   . {model}: {last_err}")
                time.sleep(10)
                continue

            body = json.dumps(data) if isinstance(data, (dict, list)) else str(data)
            last_err = f"{status}: {body[:200]}"
            if status in (400, 403, 404) and attempt == 1:
                # Modelo inexistente / no disponible en free tier: siguiente modelo
                print(f"   . {model} no disponible ({status}), pruebo el siguiente modelo")
                break
            if status == 429:
                # Cuota diaria agotada o funcion fuera del plan (limit 0): esperar no sirve
                if "PerDay" in body or re.search(r'"?limit"?:\s*0(?!\d)', body) or tools:
                    print(f"   . {model} sin cuota para esta peticion, pruebo el siguiente modelo")
                    break
                # Limite por minuto: esperar lo que pide la API
                m = re.search(r'"retryDelay":\s*"(\d+)', body)
                wait = min(MAX_RATE_WAIT, int(m.group(1)) + 2 if m else 20 * attempt)
                print(f"   . {model} limite por minuto, espero {wait}s ({attempt}/{ATTEMPTS_PER_MODEL})")
                time.sleep(wait)
                continue
            if status in (500, 502, 503, 504):
                if attempt >= OVERLOAD_ATTEMPTS and model != _models(smart)[-1]:
                    print(f"   . {model} saturado ({status}), paso al siguiente modelo")
                    break
                wait = 15 * attempt
                print(f"   . {model} devolvio {status}, reintento en {wait}s ({attempt}/{ATTEMPTS_PER_MODEL})")
                time.sleep(wait)
                continue
            break
    raise LLMError(f"Gemini fallo en todos los modelos: {last_err}")


def parse_json(content: str):
    """Parseo tolerante: quita fences y busca el primer objeto/array completo."""
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else ""
        content = content.rsplit("```", 1)[0]
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i, j = content.find(open_ch), content.rfind(close_ch)
        if i != -1 and j > i:
            try:
                return json.loads(content[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"Respuesta sin JSON valido: {content[:300]}")


def generate_json(prompt: str, **kwargs):
    return parse_json(generate(prompt, json_mode=True, **kwargs))


RESEARCH_PROMPT = """You are a financial fact-checker preparing a FACT SHEET for a YouTube script.

Story: {topic}
Context: {context}

Search the web and list 10-20 concrete, VERIFIED facts about this story: exact figures,
percentages, dates, company/person names, prior comparable events with their numbers.
One fact per line, each ending with the source in brackets, e.g.
- Costco raised its Gold Star membership fee from $60 to $65 on Sept 1, 2024 [costco.com]
Only include facts you found in sources. No opinions, no predictions."""


def research(topic: str, context: str = "") -> str:
    """Fact sheet con Google Search (grounding). Devuelve '' si no esta disponible."""
    global _research_disabled
    if _research_disabled:
        return ""
    try:
        text = generate(
            RESEARCH_PROMPT.format(topic=topic, context=context[:1500]),
            smart=True, json_mode=False, temperature=0.2, max_tokens=3000,
            tools=[{"google_search": {}}],
        )
        return text.strip()
    except LLMError as e:
        _research_disabled = True
        print(f"   ! Google Search no disponible en este plan ({str(e)[:90]}); sigo sin el")
        return ""
