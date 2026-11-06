#!/usr/bin/env python3
"""Publicador do Pensar Forte (@opensarforte).

Lê fila.json e publica no Instagram os posts com status "agendado" cujo
horário já chegou. Só usa a biblioteca padrão do Python.

Fluxo de cada post:
  1. copia a imagem para a pasta midia/ (um repositório git público, grátis)
  2. faz commit + push e espera a URL pública responder
  3. cria o "container" na API do Instagram e publica
  4. marca o post como "publicado" (ou "erro") no fila.json

Uso:
  python publicar.py             publica o que está vencido
  python publicar.py --simular   mostra o que faria, sem publicar
  python publicar.py --renovar   renova o token (vale 60 dias)
  python publicar.py --validar ID   cria o container na API SEM publicar (teste)

Tipos em fila.json: imagem (padrão), carrossel (lista 'imagens'), reel ('video'), story ('imagem').
"""
import json, os, shutil, subprocess, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

PASTA = os.path.dirname(os.path.abspath(__file__))
FILA = os.path.join(PASTA, "fila.json")
MIDIA = os.path.join(PASTA, "midia")
ENV = os.path.join(PASTA, ".env")
LOG = os.path.join(PASTA, "publicar.log")
API = "https://graph.instagram.com/v21.0"
BRT = timezone(timedelta(hours=-3))
MAX_POR_EXECUCAO = 3  # evita rajada de posts de uma vez


def log(msg):
    linha = f"{datetime.now(BRT):%Y-%m-%d %H:%M:%S} {msg}"
    print(linha)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(linha + "\n")


def ler_env():
    cfg = {}
    if os.path.exists(ENV):
        for l in open(ENV, encoding="utf-8"):
            l = l.strip()
            if l and not l.startswith("#") and "=" in l:
                k, v = l.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def gravar_env(cfg):
    with open(ENV, "w", encoding="utf-8") as f:
        for k, v in cfg.items():
            f.write(f"{k}={v}\n")


def http(metodo, url, dados=None):
    corpo = urllib.parse.urlencode(dados).encode() if dados else None
    req = urllib.request.Request(url, data=corpo, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:400]}")


def git(*args):
    r = subprocess.run(["git", "-C", MIDIA, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout


def url_publica(rel, cfg):
    return f"{cfg['PUBLIC_BASE_URL'].rstrip('/')}/{rel.replace(os.sep, '/')}"


def esperar_url(url):
    for _ in range(18):  # até ~90s
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=20) as r:
                if r.status == 200:
                    return url
        except Exception:
            pass
        time.sleep(5)
    raise RuntimeError(f"URL pública não respondeu: {url}")


def subir_arquivo(rel, cfg):
    """Garante que o arquivo (imagem/vídeo, caminho relativo) está público e devolve a URL.
    Na nuvem (MODO_NUVEM=1) ele já está no repositório; no PC copia para midia/ e dá push."""
    if os.environ.get("MODO_NUVEM") != "1":
        origem = os.path.join(PASTA, rel)
        if not os.path.exists(origem):
            raise RuntimeError(f"arquivo não encontrado: {origem}")
        destino = os.path.join(MIDIA, rel)
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        shutil.copyfile(origem, destino)
        git("add", rel)
        if git("status", "--porcelain").strip():
            git("commit", "-m", f"midia {rel}")
        git("push", "origin", "HEAD")
    return esperar_url(url_publica(rel, cfg))


def ja_publicado(item, cfg):
    """Trava anti-duplicidade: se uma legenda idêntica saiu nas últimas 24h, devolve o id."""
    try:
        r = http("GET", f"{API}/{cfg['IG_USER_ID']}/media?fields=id,caption,timestamp&limit=15&access_token={cfg['IG_ACCESS_TOKEN']}")
    except Exception:
        return None
    limite = datetime.now(timezone.utc) - timedelta(hours=24)
    for m in r.get("data", []):
        try:
            quando = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        except Exception:
            continue
        if quando >= limite and (m.get("caption") or "").strip() == item["legenda"].strip():
            return m["id"]
    return None


def esperar_container(cid, tok, tentativas=20, pausa=3):
    for _ in range(tentativas):
        st = http("GET", f"{API}/{cid}?fields=status_code&access_token={tok}").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container {st}")
        time.sleep(pausa)
    raise RuntimeError("container não ficou pronto a tempo")


def criar_container(item, cfg):
    """Cria (sem publicar) o container conforme o tipo: imagem, carrossel, reel ou story."""
    uid, tok = cfg["IG_USER_ID"], cfg["IG_ACCESS_TOKEN"]
    media = f"{API}/{uid}/media"
    tipo = item.get("tipo", "imagem")
    leg = item.get("legenda", "")
    if tipo == "imagem":
        url = subir_arquivo(item["imagem"], cfg)
        cid = http("POST", media, {"image_url": url, "caption": leg, "access_token": tok})["id"]
        esperar_container(cid, tok)
    elif tipo == "story":
        url = subir_arquivo(item["imagem"], cfg)
        cid = http("POST", media, {"image_url": url, "media_type": "STORIES", "access_token": tok})["id"]
        esperar_container(cid, tok)
    elif tipo == "reel":
        url = subir_arquivo(item["video"], cfg)
        cid = http("POST", media, {"media_type": "REELS", "video_url": url, "caption": leg,
                                   "share_to_feed": "true", "access_token": tok})["id"]
        esperar_container(cid, tok, tentativas=60, pausa=5)
    elif tipo == "carrossel":
        filhos = []
        for rel in item["imagens"]:
            url = subir_arquivo(rel, cfg)
            f = http("POST", media, {"image_url": url, "is_carousel_item": "true", "access_token": tok})["id"]
            esperar_container(f, tok)
            filhos.append(f)
        cid = http("POST", media, {"media_type": "CAROUSEL", "children": ",".join(filhos),
                                   "caption": leg, "access_token": tok})["id"]
        esperar_container(cid, tok)
    else:
        raise RuntimeError(f"tipo desconhecido: {tipo}")
    return cid


def publicar(item, cfg, so_validar=False):
    if not so_validar and item.get("tipo", "imagem") != "story":
        repetido = ja_publicado(item, cfg)
        if repetido:
            log(f"{item['id']} já estava no Instagram ({repetido}); não publico de novo")
            return repetido
    cid = criar_container(item, cfg)
    if so_validar:
        return cid
    p = http("POST", f"{API}/{cfg['IG_USER_ID']}/media_publish", {"creation_id": cid, "access_token": cfg["IG_ACCESS_TOKEN"]})
    return p["id"]


def renovar(cfg):
    r = http("GET", f"{API.replace('/v21.0','')}/refresh_access_token?grant_type=ig_refresh_token&access_token={cfg['IG_ACCESS_TOKEN']}")
    cfg["IG_ACCESS_TOKEN"] = r["access_token"]
    cfg["TOKEN_RENOVADO_EM"] = datetime.now(BRT).strftime("%Y-%m-%d")
    gravar_env(cfg)
    log(f"token renovado, expira em ~{r.get('expires_in', 0)//86400} dias")


def main():
    simular = "--simular" in sys.argv
    cfg = ler_env()
    if "--renovar" in sys.argv:
        return renovar(cfg)
    if "--validar" in sys.argv:
        alvo = sys.argv[sys.argv.index("--validar") + 1]
        item = next(p for p in json.load(open(FILA, encoding="utf-8"))["posts"] if p["id"] == alvo)
        cid = publicar(item, cfg, so_validar=True)
        log(f"VALIDADO {alvo}: container {cid} criado e aceito pela API (não publicado; expira em 24h)")
        return
    for k in ("IG_USER_ID", "IG_ACCESS_TOKEN", "PUBLIC_BASE_URL"):
        if not cfg.get(k) and not simular:
            sys.exit(f"Faltou {k} no arquivo .env")
    # renova o token automaticamente a cada ~30 dias
    ult = cfg.get("TOKEN_RENOVADO_EM")
    if not simular and ult and (datetime.now(BRT) - datetime.strptime(ult, "%Y-%m-%d").replace(tzinfo=BRT)).days >= 30:
        try:
            renovar(cfg)
        except Exception as e:
            log(f"AVISO: falha ao renovar token: {e}")
    fila = json.load(open(FILA, encoding="utf-8"))
    agora = datetime.now(BRT)
    feitos = 0
    for item in fila["posts"]:
        if item["status"] != "agendado":
            continue
        quando = datetime.fromisoformat(item["agendado_para"])
        if quando > agora:
            continue
        if feitos >= MAX_POR_EXECUCAO:
            break
        if simular:
            log(f"[simulação] publicaria {item['id']} ({item['agendado_para']})")
            feitos += 1
            continue
        try:
            item["instagram_id"] = publicar(item, cfg)
            item["status"] = "publicado"
            item["publicado_em"] = datetime.now(BRT).isoformat(timespec="seconds")
            log(f"publicado {item['id']} -> {item['instagram_id']}")
        except Exception as e:
            item["status"] = "erro"
            item["erro"] = str(e)
            log(f"ERRO em {item['id']}: {e}")
        feitos += 1
        json.dump(fila, open(FILA, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if not simular:
        json.dump(fila, open(FILA, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if feitos == 0:
        log("nada vencido para publicar")


if __name__ == "__main__":
    main()
