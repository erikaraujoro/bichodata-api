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
from playwright.sync_api import sync_playwright


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

from playwright.sync_api import sync_playwright

def buscar_html_bichodata():

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ]
        )

        page = browser.new_page(
            viewport={"width": 1280, "height": 720}
        )

        page.goto(
            "https://bichodata.com/history",
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_selector("text=PRÊMIO", timeout=15000)

        # Faz scroll para carregar mais loterias
        for _ in range(8):

            page.mouse.wheel(0, 3000)

            page.wait_for_timeout(1500)

        html = page.content()

        browser.close()

        return html


def extrair_cards_resultados(html):
    soup = BeautifulSoup(html, "html.parser")

    cards = []

    titulos_conhecidos = [
        "PTM - Manhã", "PT - Rio", "PT - Tarde", "PTV - Vesper", "PTN - Noite", "COR - Coruja",
        "LOOK - 07H", "LOOK - 09H", "LOOK - 11H", "LOOK - 14H", "LOOK - 16H", "LOOK - 18H", "LOOK - 21H", "LOOK - 23H",
        "Federal",
        "NACIONAL - 02:00", "NACIONAL - 08:00", "NACIONAL - 10:00", "NACIONAL - 12:00",
        "NACIONAL - 15:00", "NACIONAL - 17:00", "NACIONAL - 21:00", "NACIONAL - 23:00",
        "SP - 08:00", "SP - 10:00", "SP - 12:00", "SP - 13:00", "SP - 15:30", "SP - 17:00", "SP - 19:00",
        "LOTEP - 10:45", "LOTEP - 12:45", "LOTEP - 15:45", "LOTEP - 18:00",
        "LOTECE - 12:00", "LOTECE - 14:00", "LOTECE - 15:45", "LOTECE - 19:00",
        "BAHIA - 10:00", "BAHIA - 12:00", "BAHIA - 15:00", "BAHIA - 21:00",
    ]

    texto_pagina = soup.get_text("\n", strip=True)
    texto_pagina = normalizar_texto(texto_pagina)

    # Divide a página em blocos começando por cada título conhecido.
    posicoes = []
    for titulo in titulos_conhecidos:
        for m in re.finditer(re.escape(titulo), texto_pagina, flags=re.IGNORECASE):
            posicoes.append((m.start(), titulo))

    posicoes.sort(key=lambda x: x[0])

    for idx, (inicio, titulo_encontrado) in enumerate(posicoes):
        fim = posicoes[idx + 1][0] if idx + 1 < len(posicoes) else len(texto_pagina)
        bloco = texto_pagina[inicio:fim]

        if "MILHAR" not in bloco.upper():
            continue

        data_match = re.search(r"\b\d{2}/\d{2}/\d{4}\b", bloco)
        data = data_match.group(0) if data_match else ""

        horario = extrair_horario_do_titulo(titulo_encontrado)
        if not horario:
            hora_match = re.search(r"\b\d{1,2}:\d{2}\b", bloco)
            horario = pegar_horario_duas_casas(hora_match.group(0)) if hora_match else ""

        premios = []

        # Pega milhares depois de 1º, 2º, 3º, 4º e 5º
        for pos in range(1, 6):
            m = re.search(rf"\b{pos}º\s+(\d{{3,4}})\b", bloco)
            if m:
                premios.append(m.group(1).zfill(4))

        # Se o HTML vier quebrado em linhas, faz fallback pegando as primeiras milhares do bloco
        if len(premios) < 5:
            milhares = re.findall(r"\b\d{4}\b", bloco)
            premios = milhares[:5]

        if len(premios) < 5:
            continue

        cards.append({
            "titulo_site": titulo_sem_horario(titulo_encontrado),
            "data": data,
            "horario": horario,
            "premios": premios[:5],
        })

    # Deduplica
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

def texto_sheets(valor):
    valor = str(valor).strip()
    return "'" + valor

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
            texto_sheets(loteria),
            texto_sheets(str(horario).zfill(2)),
            texto_sheets(premios[0].zfill(4)),
            texto_sheets(premios[1].zfill(4)),
            texto_sheets(premios[2].zfill(4)),
            texto_sheets(premios[3].zfill(4)),
            texto_sheets(premios[4].zfill(4)),
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
        ws.append_rows(novas_linhas, value_input_option="RAW")

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
    try:
        resultados = buscar_resultados_bichodata()
        return jsonify({
            "ok": True,
            "total": len(resultados),
            "resultados": resultados
        })
    except Exception as e:
        import traceback
        return jsonify({
            "ok": False,
            "erro": str(e),
            "traceback": traceback.format_exc()
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

@app.route("/debug-bichodata")
def debug_bichodata():
    html = buscar_html_bichodata()
    soup = BeautifulSoup(html, "html.parser")
    texto = soup.get_text("\n", strip=True)

    return jsonify({
        "tamanho_html": len(html),
        "tem_premio": "PRÊMIO" in texto.upper(),
        "tem_milhar": "MILHAR" in texto.upper(),
        "tem_ptm": "PTM" in texto.upper(),
        "tem_federal": "FEDERAL" in texto.upper(),
        "inicio_texto": texto[:3000]
    })
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
