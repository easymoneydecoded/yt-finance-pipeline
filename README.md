# Morning Pipeline — Etapa 1 del canal automatico

## Memoria anti-repeticion (history/videos.json)

El canal recuerda todo lo que ha publicado en `history/videos.json`
(titulo, tema, entidades, texto y estilo de miniatura, clips de Pexels).
GitHub Actions hace commit de este fichero tras cada subida, asi cada
ejecucion sabe que NO repetir aunque arranque en una maquina limpia.

- **Etapa 1** pasa el historial al modelo, pide 8 ideas y descarta las
  que repiten titulo, tema o entidades (ej. Harvard + SpaceX) de los
  ultimos 60 dias. Tambien descarta titulares ya usados.
- **Etapa 2** escribe el guion SOBRE la idea elegida, con un estilo de
  titulo rotativo, y regenera titulo/miniatura si se parecen a algo ya
  publicado.
- **Etapa 4** evita clips de Pexels usados en los ultimos 20 videos.
- **Etapa 7** rota 4 layouts x 6 colores y no repite el combo reciente.
- **Etapa 8** aborta si el video repite contenido y no sube dos videos
  el mismo dia (re-ejecuciones del workflow).

Para que un tema vuelva a estar permitido, borra su entrada del JSON.
La logica de similitud esta en `content_history.py`.

Genera cada manana 5 ideas de video en ingles ancladas en datos reales.

## Setup (15 minutos, una vez)

1. **Crea la carpeta del proyecto** y mete dentro los 4 ficheros:
   ```
   C:\Users\abelp\Desktop\YT-Pipeline\
       morning_pipeline.py
       requirements.txt
       .env.example   ->  renombra a  .env
       README.md
   ```

2. **Instala dependencias** (terminal en esa carpeta):
   ```
   python -m pip install -r requirements.txt
   ```

3. **Saca las claves gratis** (todas), las pegas en `.env`:

   - **Groq:** ya la tienes de Cartera Pasiva. Misma clave vale.
   - **FRED:** https://fredaccount.stlouisfed.org/apikeys
     Registro con email, te da la API key al instante. Gratis ilimitado.
   - **Reddit:** https://www.reddit.com/prefs/apps
     -> "are you a developer? create an app" -> tipo **script** ->
     redirect URI `http://localhost:8080`.
     Copias el `client_id` (cadena corta bajo el nombre) y el `secret`.

4. **Prueba:**
   ```
   python morning_pipeline.py
   ```
   En 30-60 segundos tienes `data/ideas_YYYY-MM-DD.json` y `.md`.

## Programar para que corra solo cada manana

**Programador de tareas de Windows:**
- Crear tarea basica -> Diario -> 07:00
- Accion: Iniciar programa
- Programa: `python`
- Argumentos: `morning_pipeline.py`
- Iniciar en: `C:\Users\abelp\Desktop\YT-Pipeline`

Eso es todo. A las 7am cada dia te aparecen 5 ideas frescas listas
para pasar a la etapa 2 (generador de guion completo).

## Que viene despues

- **Etapa 2** — `script_writer.py`: coge una idea y escribe el guion
  completo (8-12 min) con estructura de retencion.
- **Etapa 3** — `voiceover.py`: edge-tts convierte el guion en audio.
- **Etapa 4** — `b_roll.py`: Pexels/Pixabay descarga material visual.
- **Etapa 5** — `editor.py`: MoviePy monta video + subtitulos Whisper.
- **Etapa 6** — `shorts_clipper.py`: corta 8-10 momentos para Shorts.
- **Etapa 7** — `uploader.py`: YouTube Data API sube y programa.
