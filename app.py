import os
import re
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import gspread
from bs4 import BeautifulSoup
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from google.oauth2.service_account import Credentials


# =========================
# CONFIGURAÇÕES
# =========================

BICHODATA_URL = "https://www.bichodata.com"
SHEET_NAME = os.getenv("SHEET_NAME", "RESULTADOS")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")

# No Render, salve o conteúdo inteiro do credenciais.json nesta variável:
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")

TIMEZONE = ZoneInfo("America/Porto_Velho")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)


# =========================
# GOOGLE SHEETS
# =========================

def conectar_planilha():
    if not SPREADSHEET_ID:
        raise RuntimeError("Variável SPREADSHEET_ID não configurada.")

    if not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError("Variável GOOGLE_CREDENTIALS_JSON não configurada.")

    info = json.loads(GOOGLE_CREDENTIALS_JSON)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    planilha = gc.open_by_key(SPREADSHEET_ID)
    return planilha.worksheet(SHEET_NAME)


def garantir_cabecalho(ws):
    cabecalho = ["Data", "Loteria", "Horário", "M1", "M2", "M3", "M4", "M5", "M6", "M7"]
    primeira_linha = ws.row_values(1)

    if primeira_linha[:10] != cabecalho:
        ws.update("A1:J1", [cabecalho])


def carregar_chaves_existentes(ws):
    """
    Chave para evitar duplicidade:
    Data + Loteria + Horário
    """
    valores = ws.get_all_values()
    chaves = set()

    for linha in valores[1:]:
        if len(linha) >= 3:
            data = linha[0].strip()
            loteria = linha[1].strip()
            horario = linha[2].strip()
            if data and loteria and horario:
                chaves.add(f"{data}|{loteria}|{horario}")

    return chaves


# =========================
# PADRONIZAÇÃO
# =========================

def normalizar_texto(txt):
    return re.sub(r"\s+", " ", str(txt or "")).strip()


def padronizar_loteria(nome_site):
    nome = normalizar_texto(nome_site).upper()

    loterias_rj = [
        "PTM - MANHÃ",
        "PTM - MANHA",
        "PT - RIO",
        "PT - TARDE",
        "PTV - VESPER",
        "PTV - VÉSPER",
        "PTN - NOITE",
        "COR - CORUJA",
    ]

    if nome in loterias_rj:
        return "PT-RJ"

    if nome.startswith("LOOK"):
        return "LOOK-GO"

    if nome == "FEDERAL":
        return "FEDERAL"

    if nome.startswith("NACIONAL"):
        return "NACIONAL"

    if nome.startswith("SP"):
        return "PT-SP"

    if nome.startswith("LOTEP"):
        return "LOTEP"

    if nome.startswith("LOTECE"):
        return "LOTECE"

    if nome.startswith("BAHIA"):
        return "BAHIA"

    return nome_site


def pegar_horario_duas_casas(horario_site):
    """
    Exemplo:
    09:20 -> 09
    10:00 -> 10
    7:00  -> 07
    """
    horario_site = normalizar_texto(horario_site)
    m = re.search(r"(\d{1,2})\s*:", horario_site)
    if not m:
        m = re.search(r"\b(\d{1,2})\b", horario_site)

    if not m:
        return ""

    return m.group(1).zfill(2)


def extrair_horario_do_titulo(titulo):
    """
    Alguns cards vêm como:
    BAHIA - 10:00
    LOOK - 07H
    NACIONAL - 02:00
    """
    titulo = normalizar_texto(titulo).upper()

    m = re.search(r"(\d{1,2})\s*:", titulo)
    if m:
        return m.group(1).zfill(2)

    m = re.search(r"(\d{1,2})\s*H", titulo)
    if m:
        return m.group(1).zfill(2)

    return ""


def titulo_sem_horario(titulo):
    """
    Remove o horário do título quando ele vem junto.
    Exemplo:
    BAHIA - 10:00 -> BAHIA
    LOOK - 07H    -> LOOK
    """
    t = normalizar_texto(titulo)
    t = re.sub(r"\s*-\s*\d{1,2}\s*:\s*\d{2}", "", t, flags=re.I)
    t = re.sub(r"\s*-\s*\d{1,2}\s*H\b", "", t, flags=re.I)
    return normalizar_texto(t)


# =========================
# SCRAPING BICHODATA
# =========================

def buscar_html_bichodata():
    resp = requests.get(BICHODATA_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def extrair_cards_resultados(html):
    """
    Extrai resultados da página atual.

    A estratégia usa texto do HTML para localizar blocos com:
    título, horário, data, colunas Prêmio/Milhar/Bicho/Grupo e linhas de prêmio.

    Caso o site mude muito o layout, talvez precise ajuste nos seletores.
    """
    soup = BeautifulSoup(html, "html.parser")

    cards = []

    # Primeiro tenta capturar cards por texto.
    # Procura elementos que contenham "PRÊMIO", "MILHAR", "BICHO", "GRUPO".
    candidatos = []
    for tag in soup.find_all(["div", "section", "article"]):
        texto = tag.get_text(" ", strip=True)
        txt_up = texto.upper()
        if all(p in txt_up for p in ["PRÊMIO", "MILHAR", "BICHO", "GRUPO"]):
            # Evita pegar blocos gigantes da página inteira
            if len(texto) < 3000:
                candidatos.append(tag)

    # Remove candidatos duplicados/filhos repetidos mantendo os menores úteis
    unicos = []
    textos_vistos = set()
    for tag in candidatos:
        texto = tag.get_text(" ", strip=True)
        assinatura = texto[:500]
        if assinatura not in textos_vistos:
            textos_vistos.add(assinatura)
            unicos.append(tag)

    for card in unicos:
        texto = card.get_text("\n", strip=True)
        linhas = [normalizar_texto(x) for x in texto.splitlines() if normalizar_texto(x)]

        resultado = interpretar_linhas_card(linhas)
        if resultado:
            cards.append(resultado)

    # Deduplica por data/título/horário/m1
    saida = []
    vistos = set()
    for r in cards:
        chave = (r.get("data"), r.get("titulo_site"), r.get("horario"), tuple(r.get("premios", [])))
        if chave not in vistos:
            vistos.add(chave)
            saida.append(r)

    return saida


def interpretar_linhas_card(linhas):
    """
    Recebe linhas de texto de um card e tenta montar:
    {
      titulo_site, data, horario, premios:[M1..M5]
    }
    """
    if not linhas:
        return None

    texto_total = " ".join(linhas)
    if not re.search(r"\b1º\b", texto_total) or "MILHAR" not in texto_total.upper():
        return None

    data = ""
    for item in linhas:
        m = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", item)
        if m:
            data = m.group(1)
            break

    # título provável: primeira linha antes do horário/data/cabeçalhos
    titulo = ""
    for item in linhas[:8]:
        up = item.upper()
        if any(x in up for x in ["PRÊMIO", "MILHAR", "BICHO", "GRUPO"]):
            continue
        if re.search(r"\d{2}/\d{2}/\d{4}", item):
            continue
        # evita pegar apenas hora como título
        if re.fullmatch(r"\d{1,2}:\d{2}", item):
            continue
        titulo = item
        break

    if not titulo:
        return None

    horario = ""

    # Primeiro tenta horário no próprio título
    horario = extrair_horario_do_titulo(titulo)

    # Se não achou, procura linhas de horário
    if not horario:
        for item in linhas[:10]:
            h = pegar_horario_duas_casas(item)
            if h:
                # evita confundir a data com horário
                if not re.search(r"\d{2}/\d{2}/\d{4}", item):
                    horario = h
                    break

    # Caso Federal não tenha título com hora, usa a linha 20:00 do card
    if not horario:
        for item in linhas:
            if re.fullmatch(r"\d{1,2}:\d{2}", item):
                horario = pegar_horario_duas_casas(item)
                break

    # Extrai as milhares das linhas de prêmio.
    premios = []

    # Padrão 1: linha contém "1º 6983 Touro 21"
    for item in linhas:
        m = re.search(r"\b([1-9]|10)º\s+(\d{3,4})\b", item)
        if m:
            pos = int(m.group(1))
            milhar = m.group(2).zfill(4)
            if 1 <= pos <= 5:
                while len(premios) < pos:
                    premios.append("")
                premios[pos - 1] = milhar

    # Padrão 2: linhas separadas: 1º / 6983 / Touro / 21
    if len([p for p in premios if p]) < 5:
        for i, item in enumerate(linhas):
            m = re.fullmatch(r"([1-9]|10)º", item)
            if m and i + 1 < len(linhas):
                pos = int(m.group(1))
                prox = linhas[i + 1]
                m_milhar = re.search(r"\b(\d{3,4})\b", prox)
                if m_milhar and 1 <= pos <= 5:
                    while len(premios) < pos:
                        premios.append("")
                    premios[pos - 1] = m_milhar.group(1).zfill(4)

    premios = premios[:5]
    if len(premios) < 5:
        return None

    if not all(premios):
        return None

    titulo_limpo = titulo_sem_horario(titulo)

    return {
        "titulo_site": titulo_limpo,
        "data": data,
        "horario": horario,
        "premios": premios,
    }


def buscar_resultados_bichodata():
    html = buscar_html_bichodata()
    cards = extrair_cards_resultados(html)

    resultados = []

    for card in cards:
        data = card.get("data")
        titulo_site = card.get("titulo_site", "")
        horario = card.get("horario", "")
        premios = card.get("premios", [])[:5]

        if not data or not titulo_site or not horario or len(premios) < 5:
            continue

        loteria = padronizar_loteria(titulo_site)

        linha = [
            data,
            loteria,
            horario,
            premios[0],
            premios[1],
            premios[2],
            premios[3],
            premios[4],
            "",
            "",
        ]

        resultados.append({
            "data": data,
            "loteria": loteria,
            "horario": horario,
            "titulo_site": titulo_site,
            "premios": premios,
            "linha": linha,
        })

    return resultados


# =========================
# GRAVAÇÃO
# =========================

def atualizar_planilha():
    logging.info("Iniciando atualização do BichoData...")

    resultados = buscar_resultados_bichodata()

    if not resultados:
        logging.warning("Nenhum resultado encontrado no BichoData.")
        return {
            "ok": False,
            "mensagem": "Nenhum resultado encontrado no BichoData.",
            "inseridos": 0,
            "resultados_lidos": 0,
        }

    ws = conectar_planilha()
    garantir_cabecalho(ws)

    chaves_existentes = carregar_chaves_existentes(ws)

    novas_linhas = []
    ignorados = []

    for r in resultados:
        chave = f"{r['data']}|{r['loteria']}|{r['horario']}"

        if chave in chaves_existentes:
            ignorados.append(chave)
            continue

        novas_linhas.append(r["linha"])
        chaves_existentes.add(chave)

    if novas_linhas:
        ws.append_rows(novas_linhas, value_input_option="USER_ENTERED")

    logging.info("Atualização concluída. Inseridos: %s", len(novas_linhas))

    return {
        "ok": True,
        "mensagem": "Atualização concluída.",
        "resultados_lidos": len(resultados),
        "inseridos": len(novas_linhas),
        "ignorados_por_duplicidade": len(ignorados),
        "executado_em": datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M:%S"),
    }


# =========================
# ROTAS API
# =========================

@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "servico": "API BichoData para Google Sheets",
        "rotas": {
            "/atualizar": "Busca resultados agora e grava na planilha",
            "/preview": "Mostra os resultados encontrados sem gravar",
            "/health": "Verifica se a API está online"
        }
    })


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "agora": datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M:%S")
    })


@app.route("/preview")
def preview():
    resultados = buscar_resultados_bichodata()
    return jsonify({
        "total": len(resultados),
        "resultados": resultados
    })


@app.route("/atualizar")
def atualizar_manual():
    try:
        retorno = atualizar_planilha()
        return jsonify(retorno)
    except Exception as e:
        logging.exception("Erro na atualização manual")
        return jsonify({
            "ok": False,
            "erro": str(e)
        }), 500


# =========================
# AGENDADOR
# =========================

scheduler = BackgroundScheduler(timezone=str(TIMEZONE))

@scheduler.scheduled_job("interval", minutes=30)
def tarefa_automatica():
    try:
        atualizar_planilha()
    except Exception:
        logging.exception("Erro na tarefa automática")


if not scheduler.running:
    scheduler.start()


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta)
