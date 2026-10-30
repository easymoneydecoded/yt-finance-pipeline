# Canal de YouTube automatico (finanzas) — pipeline v3

Pipeline 100% gratis que investiga, escribe, narra, monta y programa un video
largo + 3 Shorts, 4 veces por semana, en GitHub Actions.

## Etapas

| # | Script | Que hace |
|---|--------|----------|
| 0 | `analytics.py` | Lee visitas/retencion de cada video y las guarda en el historial (aprendizaje) |
| 1 | `morning_pipeline.py` | Noticias RSS + mercado + FRED + Reddit -> 8 ideas; descarta repetidas; ordena por potencial viral |
| 2 | `script_writer.py` | Ficha de datos reales (con Google Search) -> guion + guion visual + 3 Shorts -> **juez de calidad** -> **fact-check** |
| 3 | `voiceover.py` | Voz con **Kokoro** (open source, CPU). Respaldo: edge-tts |
| 4 | `b_roll.py` | Clips de Pexels sin repetir los de videos recientes |
| 5 | `editor.py` | Video 16:9: graficos/cifras/comparativas sincronizados con la voz + b-roll + subtitulos |
| 6 | `shorts_maker.py` | 3 Shorts verticales propios con captions palabra a palabra |
| 7 | `thumbnail_maker.py` | Miniatura con datos (grafico real / cifra gigante + flecha) o clasica; aprende que layout rinde mas |
| 8 | `uploader.py` | Programa el video a las 14:00 UTC, Shorts en franjas de EE. UU., capitulos y lista de reproduccion por tema |
| - | `engagement.py` | Workflow aparte (14/18/22 UTC): comentario-pregunta del canal y enlace al video completo en cada Short |

Montaje: efectos de sonido generados con ffmpeg (whoosh/ding) y marca del canal
(`CHANNEL_NAME`, por defecto TICKERPRESS) en la esquina.

Modulos compartidos: `llm.py` (Gemini), `visuals.py` (motor de graficos),
`timeline.py` (montaje), `content_history.py` (memoria + aprendizaje).

## Calidad antes que cantidad

- **Juez de calidad**: otra llamada a Gemini puntua hook, especificidad,
  retencion, titulo, miniatura, originalidad y Shorts. Por debajo de 7.5 se
  reescribe; si ninguna idea llega a 6.5, ese dia **no se publica**.
- **Fact-check**: todas las cifras deben estar en la ficha de datos del dia
  (`data/factsheet_*.txt`). Las frases con cifras inventadas se corrigen y los
  visuales con numeros no verificables se descartan.
- **Anti-repeticion**: `history/videos.json` recuerda titulos, temas,
  entidades, miniaturas y clips. El workflow lo guarda en el repo.
- **Aprendizaje**: los mejores/peores videos se pasan a los prompts y el
  formato y estilo de titulo se eligen ponderando lo que mejor rinde.

## Configuracion

**Secrets** (Settings > Secrets and variables > Actions):
`GEMINI_API_KEY`, `FRED_API_KEY`, `PEXELS_API_KEY`, `YOUTUBE_CLIENT_ID`,
`YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` y opcionales
`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`.

**Variables** opcionales (pestana Variables): `KOKORO_VOICE` (por defecto
`af_heart`; masculinas: `am_michael`, `am_fenrir`, `bm_george`),
`GEMINI_MODEL_SMART`, `GEMINI_MODEL_FAST`.

**Metricas completas (retencion, suscriptores)**: ejecuta una vez en tu PC
`python get_refresh_token.py` y pega el token nuevo en `YOUTUBE_REFRESH_TOKEN`.
Sin el, el canal aprende solo de las visitas publicas. El mismo token activa los
comentarios y las listas (permiso `youtube.force-ssl`). La API no permite
*fijar* comentarios: si quieres anclarlo, es un clic en YouTube Studio.

## Probar sin publicar

Actions > Morning Pipeline > Run workflow > marca **dry_run**. Genera todo y
deja los MP4 en el artefacto `videos-<id>` para revisarlos; no sube nada.

Para que un tema vuelva a estar permitido, borra su entrada de
`history/videos.json`.
