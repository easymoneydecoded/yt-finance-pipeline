"""
================================================================================
 repo_save.py - Guarda en el repo los ficheros de estado (history/, site_data/)
================================================================================

 Uso (al final de un workflow):  python repo_save.py "mensaje del commit" history/ [site_data/]

 Varios workflows escriben a la vez en history/ (video diario, Shorts, alertas). Con
 `git pull --rebase` un cambio simultaneo en el mismo JSON daba conflicto y el guardado
 se perdia (el 5-nov se perdieron la entrada del video y los 3 Shorts encolados).
 Aqui, si el push falla, se baja lo ultimo del remoto y se fusiona fichero a fichero
 a tres bandas (base = lo que habia al empezar, nuestro, suyo):
   - solo cambio uno de los dos -> ese cambio
   - JSON que cambiaron los dos -> fusion por claves; las listas de fichas con "id" o
     "video_id" se unen ficha a ficha; si los dos tocaron lo mismo, gana lo nuestro
   - otro fichero que cambiaron los dos -> lo nuestro (es lo mas reciente)
================================================================================
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

MISSING = object()


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, check=check)


def show(rev: str, path: str) -> bytes | None:
    r = git("show", f"{rev}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def read(path: str) -> bytes | None:
    p = Path(path)
    return p.read_bytes() if p.is_file() else None


def changed_files(paths: list[str]) -> list[str]:
    out = git("status", "--porcelain", "-uall", "--", *paths).stdout.decode()
    files = []
    for line in out.splitlines():
        name = line[3:].split(" -> ")[-1].strip().strip('"')
        files.append(name)
    return files


# ---------------------------------------------------------------------------
# Fusion a tres bandas de JSON
# ---------------------------------------------------------------------------
def _key(item) -> str | None:
    if isinstance(item, dict):
        for k in ("id", "video_id"):
            if item.get(k):
                return f"{k}:{item[k]}"
    return None


def merge3(base, ours, theirs):
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if isinstance(ours, dict) and isinstance(theirs, dict):
        b = base if isinstance(base, dict) else {}
        out = {}
        for k in list(theirs) + [k for k in ours if k not in theirs]:
            r = merge3(b.get(k, MISSING), ours.get(k, MISSING), theirs.get(k, MISSING))
            if r is not MISSING:
                out[k] = r
        return out
    if (isinstance(ours, list) and isinstance(theirs, list) and ours + theirs
            and all(_key(x) for x in ours + theirs)):
        b = {_key(x): x for x in base} if isinstance(base, list) else {}
        o = {_key(x): x for x in ours}
        out, seen = [], set()
        for x in theirs:                      # orden del remoto, y al final lo nuevo nuestro
            k = _key(x)
            seen.add(k)
            r = merge3(b.get(k, MISSING), o.get(k, MISSING), x)
            if r is not MISSING:
                out.append(r)
        for x in ours:
            k = _key(x)
            if k not in seen and k not in b:  # nuevo nuestro (si estaba en base y el remoto lo quito, fuera)
                out.append(x)
        return out
    return ours                               # los dos cambiaron lo mismo: gana lo nuestro


def merge_file(path: str, base: bytes | None, ours: bytes | None, theirs: bytes | None) -> bytes | None:
    if ours == base:
        return theirs
    if theirs == base or theirs is None or ours is None:
        return ours
    if path.endswith(".json"):
        try:
            jb = json.loads(base) if base else MISSING
            merged = merge3(jb, json.loads(ours), json.loads(theirs))
            if path.endswith("shorts_queue.json") and isinstance(merged, dict):
                import shorts_queue as sq            # recalcula "pending" con la cola fusionada
                merged["pending"] = sum(1 for i in merged.get("items", []) if not sq.is_finished(i))
            return (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        except (ValueError, TypeError) as e:
            print(f"   ! {path}: no pude fusionar el JSON ({e}), guardo el nuestro")
    return ours


def main() -> int:
    msg, paths = sys.argv[1], sys.argv[2:] or ["history/"]
    branch = os.getenv("GITHUB_REF_NAME") or git("rev-parse", "--abbrev-ref", "HEAD").stdout.decode().strip()
    files = changed_files(paths)
    if not files:
        print("Sin cambios")
        return 0
    base_rev = git("rev-parse", "HEAD").stdout.decode().strip()
    ours = {f: read(f) for f in files}
    base = {f: show(base_rev, f) for f in files}
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898283+github-actions[bot]@users.noreply.github.com")

    for attempt in range(1, 6):
        git("fetch", "-q", "origin", branch)
        git("reset", "-q", "--hard", "FETCH_HEAD")
        for f in files:
            data = merge_file(f, base[f], ours[f], read(f))
            p = Path(f)
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
        git("add", "-A", "--", *paths)
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            print("Nada nuevo que guardar (el remoto ya lo tenia)")
            return 0
        git("commit", "-q", "-m", msg)
        r = git("push", "-q", "origin", f"HEAD:{branch}", check=False)
        if r.returncode == 0:
            print(f"Guardado ({len(files)} ficheros, intento {attempt})")
            return 0
        print(f"   . push rechazado (intento {attempt}): {r.stderr.decode()[:200]}")
        time.sleep(5 * attempt)
    print("No se pudo guardar")
    return 1


if __name__ == "__main__":
    sys.exit(main())
