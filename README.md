# Canal de YouTube automatico (finanzas) — pipeline v3

Pipeline 100% gratis que investiga, escribe, narra, monta y programa un video
largo al dia + 3 Shorts al dia en YouTube, Instagram Reels y TikTok, en GitHub Actions.

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
| 8 | `uploader.py` | Programa el video a las 14:00 UTC, capitulos, lista por tema; encola los 3 Shorts |
| - | `social_publisher.py` | Workflow `social.yml` (15/19/23 UTC): publica el siguiente Short en YouTube, Instagram (`instagram.py`) y TikTok (`tiktok.py`) + comentarios del canal (`engagement.py`) |

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

## Instagram y TikTok

Los Shorts se guardan en una cola (release privado `shorts-queue` del repo +
`history/shorts_queue.json`) porque la cuota diaria de YouTube no da para
subir el video y los 3 Shorts el mismo dia. `social.yml` publica uno en cada
franja; una red que falla se reintenta en la siguiente (max. 3 veces).

- **Instagram** (automatico): secrets `INSTAGRAM_TOKEN` (token largo de la app
  de Meta) e `INSTAGRAM_USER_ID` (opcional). El token se renueva solo.
- **TikTok**: secrets `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` y
  `TIKTOK_REFRESH_TOKEN` (se saca con `python get_tiktok_token.py`). Por
  defecto manda **borradores** a la bandeja de TikTok (el texto a pegar sale en
  el resumen de cada ejecucion de `social.yml`). Cuando TikTok apruebe la
  auditoria de la app, crea la variable `TIKTOK_MODE=direct`.
- Sin credenciales de una red, esa red se salta sin romper nada.

## Probar sin publicar

Actions > Morning Pipeline > Run workflow > marca **dry_run**. Genera todo y
deja los MP4 en el artefacto `videos-<id>` para revisarlos; no sube nada.

Para que un tema vuelva a estar permitido, borra su entrada de
`history/videos.json`.

## Bot de Telegram: Insider Alerts (de pago, en ingles)

`insider_alerts.py` + workflow `alerts.yml`. Lee cada hora los Form 4 de la SEC
y avisa de las **compras en mercado abierto** de directivos (>= `MIN_BUY_USD`,
50.000 $ por defecto) y de las ventas grandes (>= `MIN_SELL_USD`, 10 M$), con
"cluster buys" marcadas. Sin IA: las cifras salen tal cual del Form 4.

- **Canal premium** (privado): todos los avisos casi en directo. Se cobra con
  una suscripcion de **Telegram Stars** (Telegram cobra y gestiona altas y bajas).
- **Canal gratis** (publico): resumen diario a las 03:40 UTC con el top 3 y el
  enlace al premium.

Puesta en marcha (una vez):

1. Crea dos canales en Telegram: uno publico (p. ej. `@tickerpress_insiders`) y
   uno privado (premium). Anade el bot (`TELEGRAM_BOT_TOKEN` o uno nuevo en
   `TELEGRAM_ALERTS_BOT_TOKEN`) como **administrador** de los dos, con permiso
   para publicar y, en el premium, para invitar usuarios.
2. `python insider_alerts.py setup` -> muestra el id del canal premium
   (`-100...`). Ponlo en `.env` como `ALERTS_PREMIUM_CHAT`, vuelve a ejecutar
   `setup` y te da el enlace de pago (`STARS_PRICE`, 500 Stars/mes por defecto).
3. En GitHub: secret `SEC_CONTACT` (tu email; la SEC lo exige) y variables
   `ALERTS_PREMIUM_CHAT`, `ALERTS_FREE_CHAT` (`@canal`) y `ALERTS_PREMIUM_LINK`.
4. Actions > Insider Alerts > Run workflow > `check`.

Probar en local sin enviar nada: `DRY_RUN=true python insider_alerts.py scan`.
Gasta unos 350 min/mes de Actions (repo privado: 2.000 gratis).
