#!/usr/bin/env python3
"""Gera um EPG (XMLTV) a partir da grade da Claro TV para usar no TiviMate.

Uso:
    python3 claro_epg.py                    # 7 dias, todos os canais -> epg_claro.xml.gz
    python3 claro_epg.py --dias 7 --saida epg.xml
    python3 claro_epg.py --listar-canais    # mostra id e nome de cada canal
    python3 claro_epg.py --mapa mapa.csv    # usa seus tvg-id (CSV: id_canal_claro,tvg-id)
    python3 claro_epg.py --xc               # liga todas as versões dos canais da sua lista XC
    python3 claro_epg.py --exportar-lista canais_lista.json   # salva só os nomes dos canais (sem login)
    python3 claro_epg.py --lista canais_lista.json            # casa usando esse arquivo
    python3 claro_epg.py --instalar         # Mac: atualiza todo dia e serve por URL ao TiviMate

Só usa a biblioteca padrão do Python 3.8+. O site da Claro costuma bloquear
acessos de fora do Brasil, então rode de uma conexão brasileira.
"""
import argparse
import csv
import getpass
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

BASE = "https://programacao.claro.com.br/gatekeeper"
TZ_BR = timezone(timedelta(hours=-3))  # Brasília, sem horário de verão desde 2019
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.claro.com.br/tv-por-assinatura/programacao/grade",
}


def get_json(endpoint, params, tentativas=3):
    url = f"{BASE}/{endpoint}?" + urllib.parse.urlencode(params, safe=":*()[]_,", quote_via=urllib.parse.quote)
    for i in range(tentativas):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i == tentativas - 1:
                raise RuntimeError(f"Falha ao acessar {url}: {e}") from e
            time.sleep(2 * (i + 1))


def buscar_canais(cidade):
    data = get_json("canal/select", [
        ("q", f"id_cidade:{cidade}"), ("rows", "1000"), ("wt", "json"),
        ("sort", "cn_canal asc"),
        ("fl", "id_canal st_canal cn_canal nome url_imagem id_cidade"),
        ("fq", "nome:*"), ("fq", "id_categoria:*"),
    ])
    return data["response"]["docs"]


def fmt_solr(dt):
    # Mesmo formato que o site usa: ano-mês-dia sem zeros à esquerda.
    return f"{dt.year}-{dt.month}-{dt.day}T{dt:%H:%M:%S}Z"


def buscar_programas(cidade, inicio, fim, lote=5000):
    docs, start = [], 0
    while True:
        data = get_json("exibicao/select", [
            ("q", f"id_revel:(*) AND id_cidade:{cidade}"), ("wt", "json"),
            ("rows", str(lote)), ("start", str(start)),
            ("sort", "id_canal asc,dh_inicio asc"),
            ("fl", "dh_fim dh_inicio st_titulo titulo id_programa id_canal id_cidade"),
            ("fq", f"dh_inicio:[{fmt_solr(inicio)} TO {fmt_solr(fim)}]"),
        ])
        resp = data["response"]
        docs.extend(resp["docs"])
        start += len(resp["docs"])
        if not resp["docs"] or start >= resp.get("numFound", 0):
            return docs


def parse_hora(valor):
    # O site manda "2026-10-23T14:30Z", mas o horário é de Brasília (o "Z" é enganoso).
    valor = valor.rstrip("Z")
    for f in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(valor, f).replace(tzinfo=TZ_BR)
        except ValueError:
            pass
    raise ValueError(f"horário inesperado: {valor}")


def xmltv_hora(dt):
    return dt.strftime("%Y%m%d%H%M%S %z")


def saidas_padrao(canais, mapa):
    """Um canal no EPG para cada canal da Claro."""
    saidas = []
    for c in canais:
        nomes = [c.get("nome", "")]
        if c.get("cn_canal") is not None:
            nomes.append(str(c["cn_canal"]))
        saidas.append({"id": mapa.get(str(c["id_canal"]), f"Claro.{c['id_canal']}"),
                       "nomes": nomes, "icone": c.get("url_imagem"), "claro": str(c["id_canal"])})
    return saidas


# Qualidade/versão: "FHD1", "SD2", "HD" somem; "HD 7" vira "7" (Premiere HD 7 = Premiere 7).
QUALIDADE = re.compile(r"\b(full\s*hd|fhd|uhd|hd|sd|4k|8k|h\.?265|hevc|h\.?264|alt|alternativo|backup|bkp|reserva)\d*\b")
SOBRESCRITOS = str.maketrans("", "", "¹²³⁰⁴⁵⁶⁷⁸⁹©®™")

# Nomes da sua lista que não batem com a Claro só pelo texto (já normalizados).
APELIDOS_PADRAO = {
    "globospsaopaulo": "globo", "globosp": "globo", "tvglobo": "globo",
    "bandsp": "band", "recordsp": "record", "tvrecord": "record", "sbtsp": "sbt",
    "canalsony": "sony", "history": "thehistorychannel",
    "dischomeehealth": "discoveryhomeehealth", "discoveryhomeandhealth": "discoveryhomeehealth",
    "travelboxbrazil": "travelboxbrasil", "playboy": "playboytv", "rit": "rittv",
    "bloomberg": "bloombergtv", "tveinternacional": "tveinternational",
}


# Canais em que a Claro publica a grade de outro canal; usam as fontes extras (nomes normalizados).
PREFERIR_OUTRA_FONTE = {"espn", "espn2", "espn4", "espn5"}

# "[CA] HBO", "USL- HBO", "LAT | HBO": canais de outros países, com outra grade.
PREFIXO_PAIS = re.compile(r"^\s*(?:\[([a-z]{2,3})\]|([a-z]{2,3})\s*[|:\-])", re.I)


PAISES = {"us", "usa", "usl", "ca", "lat", "es", "it", "uk", "pt", "ar", "mx", "cl", "co", "fr", "de"}


def estrangeiro(nome):
    m = PREFIXO_PAIS.match(nome)
    return bool(m) and (m.group(1) or m.group(2)).lower() in PAISES


def normalizar(nome):
    """'BR | SporTV FHD1 [H265]' -> 'sportv'. Mantém números que fazem parte do nome (SporTV 2)."""
    s = unicodedata.normalize("NFKD", nome.translate(SOBRESCRITOS)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", s)
    s = re.sub(r"^\s*[a-z]{2,3}\s*[|:\-]\s*", " ", s)  # prefixos como "BR |", "BR:"
    s = s.replace("+", " plus ").replace("&", " e ")
    s = QUALIDADE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def buscar_xc(servidor, usuario, senha):
    servidor = servidor.rstrip("/")
    if not servidor.startswith("http"):
        servidor = "http://" + servidor
    url = f"{servidor}/player_api.php?" + urllib.parse.urlencode(
        {"username": usuario, "password": senha, "action": "get_live_streams"})
    req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def saidas_xc(canais, streams, mapa_nomes):
    """Liga cada canal da sua lista XC (Sportv FHD1, Sportv SD2...) ao canal da Claro."""
    por_nome = {}
    for c in canais:
        por_nome.setdefault(normalizar(c.get("nome", "")), c)
    por_id = {str(c["id_canal"]): c for c in canais}
    # votos[tvg-id][id_canal da Claro ou None] = nomes da sua lista com esse tvg-id
    votos, casados, sem_par = {}, [], []
    for s in streams:
        nome = (s.get("name") or "").strip()
        if not nome:
            continue
        tvg_id = (s.get("epg_channel_id") or "").strip()
        c = None
        if not estrangeiro(nome):
            chave = normalizar(nome)
            if chave not in por_nome:
                chave = mapa_nomes.get(chave, chave)
            c = por_nome.get(chave)
        if c:
            fonte = str(c["id_canal"]).split(":")[0] if str(c["id_canal"]).startswith("x") else ""
            casados.append((nome, c.get("nome", "") + (f"  [fonte extra {fonte[1:]}]" if fonte else "")))
        else:
            sem_par.append(nome)
        if tvg_id or c:
            votos.setdefault(tvg_id, {}).setdefault(str(c["id_canal"]) if c else None, []).append(nome)

    # O TiviMate procura primeiro pelo tvg-id e depois pelo nome. O provedor às vezes repete
    # um tvg-id em canais diferentes (ex.: "ESPN FHD2" com o tvg-id do ESPN 4, ou um id
    # genérico em 30 canais). Um tvg-id só entra no EPG se pelo menos 2/3 dos canais com ele
    # forem o mesmo canal da Claro; senão, cada canal casado entra pelo próprio nome.
    saidas, conflitos = {}, []

    def adicionar(xml_id, claro, nomes):
        g = saidas.setdefault(xml_id, {"id": xml_id, "nomes": [], "claro": claro,
                                       "icone": por_id[claro].get("url_imagem")})
        g["nomes"] += [n for n in nomes if n not in g["nomes"]]

    for tvg_id, por_claro in votos.items():
        total = sum(len(n) for n in por_claro.values())
        ranking = sorted(((k, n) for k, n in por_claro.items() if k), key=lambda kv: -len(kv[1]))
        if tvg_id and ranking and len(ranking[0][1]) * 3 >= total * 2:
            vencedor, nomes = ranking[0]
            adicionar(tvg_id, vencedor, nomes)
            for claro, outros in ranking[1:]:
                conflitos += [(n, tvg_id, por_id[vencedor].get("nome", "")) for n in outros]
        else:
            for claro, nomes in ranking:
                for n in nomes:
                    adicionar(n, claro, [n])
    return list(saidas.values()), casados, sem_par, conflitos


def _slug(texto):
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", t)


def limpar_titulo(titulo):
    """'Mundo F - Ao Vivo: mundo_f_ao_vivo' -> 'Mundo F - Ao Vivo'.

    Corta o que vem depois do último ':' quando é só o nome repetido em forma de código."""
    titulo = titulo.strip()
    while ":" in titulo:
        antes, depois = titulo.rsplit(":", 1)
        d = _slug(depois)
        if d and (d == _slug(antes) or (re.fullmatch(r"\s*[\w\-]+\s*", depois) and "_" in depois)
                  or (len(d) >= 4 and _slug(antes).startswith(d))):
            titulo = antes.strip()
        else:
            break
    return titulo


def buscar_extra(url, n, inicio, fim):
    """Lê um EPG XMLTV de outra fonte, no mesmo formato de canais/programas da Claro."""
    req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=180) as r:
        dados = r.read()
    if dados[:2] == b"\x1f\x8b":
        dados = gzip.decompress(dados)
    raiz = ET.fromstring(dados)
    canais = {}
    for c in raiz.findall("channel"):
        nome = (c.findtext("display-name") or "").strip()
        nome = re.sub(r"^[^/]+/[A-Z]{2}\s+", "", nome)  # "São Paulo/SP  ESPN 3 HD"
        nome = re.sub(r"\.br$", "", nome, flags=re.I)  # "ESPN 3.br"
        icone = c.find("icon")
        canais[c.get("id")] = {"id_canal": f"x{n}:{c.get('id')}", "nome": nome,
                               "url_imagem": icone.get("src") if icone is not None else None,
                               "sp": "São Paulo" in (c.findtext("display-name") or "")}
    programas = []
    for p in raiz.findall("programme"):
        c = canais.get(p.get("channel"))
        try:
            ini = datetime.strptime(p.get("start", "").strip(), "%Y%m%d%H%M%S %z")
            fim_p = datetime.strptime(p.get("stop", "").strip(), "%Y%m%d%H%M%S %z")
        except ValueError:
            continue
        if not c or fim_p < inicio or ini > fim:
            continue
        programas.append({"id_canal": c["id_canal"], "_ini": ini, "_fim": fim_p,
                          "titulo": p.findtext("title") or "", "desc": (p.findtext("desc") or "").strip()})
    # Canais de São Paulo primeiro, para ganharem de outras cidades com o mesmo nome.
    return sorted(canais.values(), key=lambda c: not c["sp"]), programas


def gerar_xmltv(saidas, programas):
    linhas = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<tv generator-info-name="claro_epg.py" source-info-url="https://www.claro.com.br/tv-por-assinatura/programacao/grade">']
    por_claro = {}
    for s in saidas:
        por_claro.setdefault(s["claro"], []).append(s["id"])
        linhas.append(f"  <channel id={quoteattr(s['id'])}>")
        for n in s["nomes"]:
            linhas.append(f"    <display-name lang=\"pt\">{escape(n)}</display-name>")
        if s.get("icone"):
            linhas.append(f"    <icon src={quoteattr(s['icone'])}/>")
        linhas.append("  </channel>")

    vistos, total, limpos = set(), 0, 0
    for p in programas:
        canal = str(p.get("id_canal"))
        if canal not in por_claro:
            continue
        if "_ini" in p:  # fonte extra, horário já convertido
            ini, fim = p["_ini"], p["_fim"]
        elif p.get("dh_inicio") and p.get("dh_fim"):
            ini, fim = parse_hora(p["dh_inicio"]), parse_hora(p["dh_fim"])
        else:
            continue
        chave = (canal, ini)
        if chave in vistos or fim <= ini:
            continue
        vistos.add(chave)
        # st_titulo é só um código interno (ex.: "vai_que_cola"), não o episódio.
        titulo = limpar_titulo(p.get("titulo") or "") or "Sem título"
        limpos += titulo != (p.get("titulo") or "").strip()
        for xml_id in por_claro[canal]:
            linhas.append(f"  <programme start=\"{xmltv_hora(ini)}\" stop=\"{xmltv_hora(fim)}\" channel={quoteattr(xml_id)}>")
            linhas.append(f"    <title lang=\"pt\">{escape(titulo)}</title>")
            if p.get("desc"):
                linhas.append(f"    <desc lang=\"pt\">{escape(p['desc'])}</desc>")
            linhas.append("  </programme>")
            total += 1
    linhas.append("</tv>")
    print(f"{limpos} títulos com código da Claro limpos", file=sys.stderr)
    return "\n".join(linhas) + "\n", total


def ler_mapa(caminho):
    mapa = {}
    with open(caminho, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) >= 2 and row[0].strip() and not row[0].startswith("#"):
                mapa[row[0].strip()] = row[1].strip()
    return mapa


PASTA = os.path.expanduser("~/.claro_epg")
CONFIG = os.path.join(PASTA, "login.json")
PORTA = 8787
AGENTES = {
    "atualizar": "com.claroepg.atualizar",
    "servidor": "com.claroepg.servidor",
}


def pedir_login():
    """Usa as variáveis XC_* (GitHub Actions) ou o login salvo pelo --instalar; senão, pergunta."""
    if os.environ.get("XC_SERVIDOR"):
        return os.environ["XC_SERVIDOR"], os.environ["XC_USUARIO"], os.environ["XC_SENHA"]
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            c = json.load(f)
        return c["servidor"], c["usuario"], c["senha"]
    servidor = input("URL do servidor (ex.: http://servidor.com:8080): ").strip()
    usuario = input("Usuário: ").strip()
    senha = getpass.getpass("Senha (não aparece enquanto digita): ").strip()
    return servidor, usuario, senha


def plist(rotulo, comando, extra):
    args = "".join(f"<string>{escape(x)}</string>" for x in comando)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{rotulo}</string>
<key>ProgramArguments</key><array>{args}</array>
<key>WorkingDirectory</key><string>{escape(PASTA)}</string>
<key>StandardOutPath</key><string>{escape(PASTA)}/log.txt</string>
<key>StandardErrorPath</key><string>{escape(PASTA)}/log.txt</string>
{extra}
</dict></plist>
"""


def ip_local():
    for iface in ("en0", "en1", "en2"):
        r = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True)
        if r.stdout.strip():
            return r.stdout.strip()
    return "IP-DO-SEU-MAC"


def instalar(dias):
    if sys.platform != "darwin":
        sys.exit("O --instalar foi feito para Mac.")
    os.makedirs(os.path.join(PASTA, "www"), exist_ok=True)
    print("Vou guardar o login da sua lista só neste Mac, para a atualização rodar sozinha.")
    servidor = input("URL do servidor (ex.: http://servidor.com:8080): ").strip()
    usuario = input("Usuário: ").strip()
    senha = getpass.getpass("Senha (não aparece enquanto digita): ").strip()
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump({"servidor": servidor, "usuario": usuario, "senha": senha}, f)
    os.chmod(CONFIG, 0o600)

    script = os.path.join(PASTA, "claro_epg.py")
    shutil.copyfile(os.path.abspath(__file__), script)
    py = sys.executable
    saida = os.path.join(PASTA, "www", "epg_claro.xml.gz")
    agentes = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(agentes, exist_ok=True)
    conteudo = {
        # Todo dia às 5h e também logo após ligar o Mac.
        "atualizar": plist(AGENTES["atualizar"], [py, script, "--xc", "--dias", str(dias), "--saida", saida],
                           "<key>RunAtLoad</key><true/>"
                           "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>5</integer>"
                           "<key>Minute</key><integer>0</integer></dict>"),
        # Servidor simples para o TiviMate baixar o arquivo pela rede de casa.
        "servidor": plist(AGENTES["servidor"], [py, "-m", "http.server", str(PORTA), "--directory",
                                                os.path.join(PASTA, "www")],
                          "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"),
    }
    for nome, rotulo in AGENTES.items():
        caminho = os.path.join(agentes, rotulo + ".plist")
        subprocess.run(["launchctl", "unload", caminho], capture_output=True)
        with open(caminho, "w", encoding="utf-8") as f:
            f.write(conteudo[nome])
        subprocess.run(["launchctl", "load", caminho], check=True)

    print("\nPronto! O EPG está sendo gerado agora (leva 1 ou 2 minutos) e vai se atualizar todo dia às 5h.")
    print(f"Coloque esta URL no TiviMate:  http://{ip_local()}:{PORTA}/epg_claro.xml.gz")
    print(f"Se der problema, veja o arquivo {PASTA}/log.txt")


def desinstalar():
    agentes = os.path.expanduser("~/Library/LaunchAgents")
    for rotulo in AGENTES.values():
        caminho = os.path.join(agentes, rotulo + ".plist")
        subprocess.run(["launchctl", "unload", caminho], capture_output=True)
        if os.path.exists(caminho):
            os.remove(caminho)
    shutil.rmtree(PASTA, ignore_errors=True)
    print("Atualização automática removida, e o login salvo foi apagado.")


def main():
    ap = argparse.ArgumentParser(description="EPG XMLTV da Claro TV para TiviMate")
    ap.add_argument("--dias", type=int, default=7, help="dias de programação (padrão 7)")
    ap.add_argument("--cidade", default="1", help="id_cidade da Claro (padrão 1)")
    ap.add_argument("--saida", default="epg_claro.xml.gz", help="arquivo .xml ou .xml.gz")
    ap.add_argument("--mapa", help="CSV id_canal_claro,tvg-id para casar com sua lista")
    ap.add_argument("--canais", help="só estes id_canal (separados por vírgula)")
    ap.add_argument("--listar-canais", action="store_true")
    ap.add_argument("--xc", action="store_true", help="casa o EPG com sua lista Xtream Codes (pergunta servidor, usuário e senha)")
    ap.add_argument("--exportar-lista", metavar="ARQ", help="salva os nomes dos canais da sua lista XC (sem login) num arquivo")
    ap.add_argument("--lista", metavar="ARQ", help="usa os canais salvos com --exportar-lista em vez de entrar na lista XC")
    ap.add_argument("--extra", action="append", default=[], metavar="URL",
                    help="EPG XMLTV de outra fonte, usado só para canais que a Claro não tem (pode repetir; ordem = prioridade)")
    ap.add_argument("--apelidos", help="CSV 'nome na sua lista,nome na Claro' para casos que não casam sozinhos")
    ap.add_argument("--instalar", action="store_true", help="Mac: atualiza o EPG todo dia e serve por URL na rede de casa")
    ap.add_argument("--desinstalar", action="store_true", help="Mac: remove a atualização automática")
    a = ap.parse_args()

    if a.instalar:
        return instalar(a.dias)
    if a.desinstalar:
        return desinstalar()

    canais = buscar_canais(a.cidade)
    if a.listar_canais:
        for c in canais:
            print(f"{c['id_canal']}\t{c.get('cn_canal', '')}\t{c.get('nome', '')}")
        return
    if a.canais:
        filtro = {x.strip() for x in a.canais.split(",")}
        canais = [c for c in canais if str(c["id_canal"]) in filtro]

    if a.exportar_lista:
        streams = buscar_xc(*pedir_login())
        enxuto = [{"name": x.get("name"), "epg_channel_id": x.get("epg_channel_id")} for x in streams if x.get("name")]
        with open(a.exportar_lista, "w", encoding="utf-8") as f:
            json.dump(enxuto, f, ensure_ascii=False, indent=0)
        print(f"OK: {len(enxuto)} canais salvos em {a.exportar_lista}", file=sys.stderr)
        return
    hoje = datetime.now(TZ_BR).replace(hour=0, minute=0, second=0, microsecond=0)
    programas = []
    for d in range(a.dias):
        ini = hoje + timedelta(days=d)
        fim = ini + timedelta(days=1) - timedelta(minutes=1)
        lote = buscar_programas(a.cidade, ini, fim)
        print(f"{ini:%d/%m}: {len(lote)} exibições", file=sys.stderr)
        programas.extend(lote)

    # A Claro tem canais duplicados (ex.: "ESPN 2" sem grade e "ESPN 2 HD" com grade) e
    # alguns sem grade nenhuma. Fica só quem tem programação, o mais completo primeiro.
    qtd = {}
    for p in programas:
        qtd[str(p.get("id_canal"))] = qtd.get(str(p.get("id_canal")), 0) + 1
    vazios = [c.get("nome", "") for c in canais if not qtd.get(str(c["id_canal"]))]
    if vazios:
        print(f"Canais sem grade na Claro: {', '.join(sorted(vazios))}", file=sys.stderr)
    canais = sorted((c for c in canais if qtd.get(str(c["id_canal"]))), key=lambda c: -qtd[str(c["id_canal"])])
    if a.extra and (a.xc or a.lista):
        # Canais em que a grade da Claro vem errada: programas sobrepostos (a Claro mistura
        # duas grades no mesmo canal, como no ESPN) ou grade de outro canal (PREFERIR_OUTRA_FONTE).
        # Esses saem da Claro e ficam para as fontes extras.
        horarios = {}
        for p in programas:
            if p.get("dh_inicio") and p.get("dh_fim"):
                horarios.setdefault(str(p.get("id_canal")), []).append((parse_hora(p["dh_inicio"]), parse_hora(p["dh_fim"])))
        ruins = set()
        for cid, h in horarios.items():
            h.sort()
            sobrepostos = sum(1 for x, y in zip(h, h[1:]) if y[0] < x[1])
            if sobrepostos * 10 > len(h):
                ruins.add(cid)
        tirar = [c for c in canais if str(c["id_canal"]) in ruins or normalizar(c.get("nome", "")) in PREFERIR_OUTRA_FONTE]
        if tirar:
            print(f"Grade da Claro ignorada (errada): {', '.join(c.get('nome', '') for c in tirar)}", file=sys.stderr)
            canais = [c for c in canais if c not in tirar]
    n_claro = len(canais)

    # Fontes extras: só completam o que a Claro não tem (a Claro vem antes na hora de casar).
    if a.extra and (a.xc or a.lista):
        for n, url in enumerate(a.extra, 1):
            try:
                ext_canais, ext_progs = buscar_extra(url, n, hoje, hoje + timedelta(days=a.dias))
            except Exception as e:
                print(f"Fonte extra {url} falhou ({type(e).__name__})", file=sys.stderr)
                continue
            com_grade = {p["id_canal"] for p in ext_progs}
            canais += [c for c in ext_canais if c["id_canal"] in com_grade]
            programas += ext_progs
            print(f"Fonte extra {n}: {len(com_grade)} canais com grade", file=sys.stderr)

    streams = None
    if a.lista:
        with open(a.lista, encoding="utf-8") as f:
            streams = json.load(f)
        a.xc = True
    elif a.xc:
        servidor, usuario, senha = pedir_login()
        try:
            streams = buscar_xc(servidor, usuario, senha)
        except Exception as e:
            # Se o servidor da lista recusar, ainda gera o EPG com os nomes da Claro.
            print(f"Não consegui ler a lista XC ({type(e).__name__}); gerando EPG sem casar canais.", file=sys.stderr)
            a.xc = False
    if a.xc:
        apelidos = dict(APELIDOS_PADRAO)
        apelidos.update({normalizar(k): normalizar(v) for k, v in (ler_mapa(a.apelidos) if a.apelidos else {}).items()})
        saidas, casados, sem_par, conflitos = saidas_xc(canais, streams, apelidos)
        with open("relatorio_canais.txt", "w", encoding="utf-8") as f:
            if conflitos:
                f.write(f"TVG-ID ERRADO NA LISTA ({len(conflitos)}) - acerte o EPG destes à mão no TiviMate\n")
                for nome, tid, claro in sorted(conflitos):
                    f.write(f"  {nome}  (tvg-id {tid} = {claro})\n")
                f.write("\n")
            f.write(f"CASADOS ({len(casados)})\n")
            for nome, claro in sorted(casados):
                f.write(f"  {nome}  ->  {claro}\n")
            usados = {claro for _, claro in casados}
            sobra = sorted(c.get("nome", "") for c in canais[:n_claro] if c.get("nome", "") not in usados)
            f.write(f"\nCANAIS DA CLARO SEM PAR NA SUA LISTA ({len(sobra)})\n")
            for nome in sobra:
                f.write(f"  {nome}\n")
            f.write(f"\nSEM GRADE NA CLARO ({len(sem_par)})\n")
            for nome in sorted(sem_par):
                f.write(f"  {nome}\n")
        print(f"{len(casados)} canais da sua lista ligados à Claro, {len(sem_par)} sem par "
              f"(veja relatorio_canais.txt)", file=sys.stderr)
    else:
        saidas = saidas_padrao(canais, ler_mapa(a.mapa) if a.mapa else {})


    xml, total = gerar_xmltv(saidas, programas)
    dados = xml.encode("utf-8")
    if a.saida.endswith(".gz"):
        dados = gzip.compress(dados)
    # Grava num temporário e troca no fim, para o TiviMate nunca baixar um arquivo pela metade.
    with open(a.saida + ".tmp", "wb") as f:
        f.write(dados)
    os.replace(a.saida + ".tmp", a.saida)
    print(f"OK: {len(saidas)} canais, {total} programas -> {a.saida}", file=sys.stderr)


if __name__ == "__main__":
    main()
