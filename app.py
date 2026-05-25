import os
import re
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import gspread
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from google.oauth2.service_account import Credentials


# =========================
# CONFIGURAÇÕES
# =========================

SHEET_NAME = os.getenv("SHEET_NAME", "RESULTADOS")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

TIMEZONE = ZoneInfo("America/Porto_Velho")
TIMEZONE_DADOS = ZoneInfo("America/Sao_Paulo")

SUPABASE_URL = "https://rxshjetdbudpbqfegxjx.supabase.co/rest/v1"

TABELAS_SUPABASE = {
    "resultados": "PT-RJ",
    "resultado_nacional": "NACIONAL",
    "resultados_lk": "LOOK-GO",
    "resultados_sp": "PT-SP",
    "resultados_bahia": "BAHIA",
    "resultados_lotep_pb": "LOTEP",
    "resultados_lotece_ce": "LOTECE",
    "resultado_federal": "FEDERAL",
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


def limpar_texto_planilha(valor):
    return str(valor or "").strip().lstrip("'")


def carregar_chaves_existentes(ws):
    """
    Chave para evitar duplicidade: Data + Loteria + Horário.
    Normaliza horário para 2 dígitos para evitar duplicar 9 e 09.
    """
    valores = ws.get_all_values()
    chaves = set()

    for linha in valores[1:]:
        if len(linha) >= 3:
            data = limpar_texto_planilha(linha[0])
            loteria = limpar_texto_planilha(linha[1])
            horario = limpar_texto_planilha(linha[2]).zfill(2)
            if data and loteria and horario:
                chaves.add(f"{data}|{loteria}|{horario}")

    return chaves


# =========================
# UTILITÁRIOS
# =========================

def normalizar_texto(txt):
    return re.sub(r"\s+", " ", str(txt or "")).strip()


def somente_digitos(valor):
    return re.sub(r"\D", "", str(valor or ""))


def formatar_milhar(valor):
    digitos = somente_digitos(valor)
    if not digitos:
        return ""
    return digitos[-4:].zfill(4)


def formatar_horario(valor):
    texto = normalizar_texto(valor)

    m = re.search(r"(\d{1,2})\s*:", texto)
    if m:
        return m.group(1).zfill(2)

    m = re.search(r"\b(\d{1,2})\b", texto)
    if m:
        return m.group(1).zfill(2)

    return ""


def formatar_data_br(data_iso):
    data_iso = str(data_iso or "").strip()
    if not data_iso:
        return datetime.now(TIMEZONE_DADOS).strftime("%d/%m/%Y")

    try:
        return datetime.strptime(data_iso[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return data_iso


def obter_valor_item(item, nomes):
    for nome in nomes:
        if nome in item and item.get(nome) not in [None, ""]:
            return item.get(nome)
        nome_up = nome.upper()
        if nome_up in item and item.get(nome_up) not in [None, ""]:
            return item.get(nome_up)
    return ""


def extrair_premios(item):
    campos_possiveis = [
        ["p1", "P1", "m1", "M1"],
        ["p2", "P2", "m2", "M2"],
        ["p3", "P3", "m3", "M3"],
        ["p4", "P4", "m4", "M4"],
        ["p5", "P5", "m5", "M5"],
    ]

    premios = []
    for nomes in campos_possiveis:
        valor = obter_valor_item(item, nomes)
        premios.append(formatar_milhar(valor))

    return premios


# =========================
# BUSCA BICHODATA VIA SUPABASE
# =========================

def calcular_premios_6_7(premios):
    """
    M6 = soma de M1 a M5, pegando os 4 últimos dígitos.
    M7 = M1 * M2, pegando os 3 dígitos após o primeiro dígito do resultado.
    """

    valores = [int(p) for p in premios[:5]]

    soma = sum(valores)
    m6 = str(soma)[-4:].zfill(4)

    produto = valores[0] * valores[1]

    # Pega a classe de milhar do resultado.
    # Exemplo: 43.234.755 -> 43234 -> últimos 3 = 234
    m7 = str(produto // 1000)[-3:].zfill(3)

    return m6, m7

def buscar_resultados_bichodata():
    if not SUPABASE_KEY:
        raise RuntimeError("Variável SUPABASE_KEY não configurada no Render.")

    hoje = datetime.now(TIMEZONE_DADOS).strftime("%Y-%m-%d")

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    }

    resultados = []

    for tabela, loteria in TABELAS_SUPABASE.items():
        url = f"{SUPABASE_URL}/{tabela}"

        try:
            # Algumas tabelas usam "data"; outras usam "dados"; a Federal usa "data_sorteio".
            # Por isso fazemos as 3 consultas e juntamos os resultados encontrados.
            consultas = [
                {"select": "*", "data": f"eq.{hoje}"},
                {"select": "*", "dados": f"eq.{hoje}"},
                {"select": "*", "data_sorteio": f"eq.{hoje}"},
            ]

            dados = []
            ids_processados = set()

            for params in consultas:
                try:
                    resp = requests.get(url, headers=headers, params=params, timeout=30)
                    resp.raise_for_status()
                    encontrados = resp.json()

                    for item_encontrado in encontrados:
                        chave_item = item_encontrado.get("id")

                        if chave_item is None:
                            chave_item = json.dumps(item_encontrado, sort_keys=True)

                        if chave_item in ids_processados:
                            continue

                        ids_processados.add(chave_item)
                        dados.append(item_encontrado)

                except Exception as erro_consulta:
                    # Se a coluna não existir em determinada tabela, ignora somente essa consulta
                    # e tenta a próxima coluna possível.
                    logging.info(
                        "Consulta ignorada em %s com params %s: %s",
                        tabela,
                        params,
                        erro_consulta,
                    )

        except Exception as e:
            logging.exception("Erro ao buscar tabela %s: %s", tabela, e)
            continue

        for item in dados:
            data_item = (
                item.get("data")
                or item.get("dados")
                or item.get("data_sorteio")
                or hoje
            )

            # Filtra somente resultados do dia atual.
            if str(data_item)[:10] != hoje:
                continue

            data_br = formatar_data_br(data_item)
            horario = formatar_horario(item.get("horario", ""))
            premios = extrair_premios(item)

            if not horario or len(premios) < 5 or not all(premios):
                logging.warning("Registro ignorado por dados incompletos em %s: %s", tabela, item)
                continue

            m6, m7 = calcular_premios_6_7(premios)

            linha = [
                data_br,
                loteria,
                horario,
                premios[0],
                premios[1],
                premios[2],
                premios[3],
                premios[4],
                m6,
                m7,
            ]

            resultados.append({
                "data": data_br,
                "loteria": loteria,
                "horario": horario,
                "premios": premios + [m6, m7],
                "linha": linha,
                "origem": tabela,
            })

    resultados.sort(key=lambda r: (r["data"], r["loteria"], r["horario"]))
    return resultados

def buscar_resultados_bichodata_data(data_consulta):

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    }

    resultados = []

    for tabela, loteria in TABELAS_SUPABASE.items():

        url = f"{SUPABASE_URL}/{tabela}"

        consultas = [
            {"select": "*", "data": f"eq.{data_consulta}"},
            {"select": "*", "dados": f"eq.{data_consulta}"},
            {"select": "*", "data_sorteio": f"eq.{data_consulta}"},
        ]

        dados = []
        ids_processados = set()

        for params in consultas:
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
                resp.raise_for_status()

                encontrados = resp.json()

                for item in encontrados:
                    chave_item = item.get("id")
                    if chave_item is None:
                        chave_item = json.dumps(item, sort_keys=True)

                    if chave_item in ids_processados:
                        continue

                    ids_processados.add(chave_item)
                    dados.append(item)

            except Exception as e:
                logging.info(
                    "Consulta ignorada em %s com params %s: %s",
                    tabela,
                    params,
                    e,
                )
                continue

        for item in dados:

            data_item = (
                item.get("data")
                or item.get("dados")
                or item.get("data_sorteio")
                or data_consulta
            )

            if str(data_item)[:10] != data_consulta:
                continue

            data_br = formatar_data_br(data_item)
            horario = formatar_horario(item.get("horario", ""))
            premios = extrair_premios(item)

            if not horario or len(premios) < 5 or not all(premios):
                continue

            m6, m7 = calcular_premios_6_7(premios)

            linha = [
                data_br,
                loteria,
                horario,
                premios[0],
                premios[1],
                premios[2],
                premios[3],
                premios[4],
                m6,
                m7,
            ]

            resultados.append({
                "data": data_br,
                "loteria": loteria,
                "horario": horario,
                "premios": premios + [m6, m7],
                "linha": linha,
                "origem": tabela,
            })

    resultados.sort(key=lambda r: (r["data"], r["loteria"], r["horario"]))
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
        chave = f"{r['data']}|{r['loteria']}|{str(r['horario']).zfill(2)}"

        if chave in chaves_existentes:
            ignorados.append(chave)
            continue

        novas_linhas.append(r["linha"])
        chaves_existentes.add(chave)

    if novas_linhas:
        # RAW mantém horário 09 e milhares 0304/0450/0738 como texto com zeros à esquerda.
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
            "/health": "Verifica se a API está online",
        },
    })


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "agora": datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M:%S"),
    })


@app.route("/preview")
def preview():
    try:
        resultados = buscar_resultados_bichodata()
        return jsonify({
            "ok": True,
            "total": len(resultados),
            "resultados": resultados,
        })
    except Exception as e:
        import traceback
        return jsonify({
            "ok": False,
            "erro": str(e),
            "traceback": traceback.format_exc(),
        }), 500

# =====================================================
# PREVIEW POR DATA (DEBUG)
# Exemplo:
# /preview-data/2026-05-24
# =====================================================

@app.route("/preview-data/<data_teste>")
def preview_data(data_teste):
    try:

        resultados = buscar_resultados_bichodata_data(data_teste)

        return jsonify({
            "ok": True,
            "data_consulta": data_teste,
            "total": len(resultados),
            "resultados": resultados
        })

    except Exception as e:
        import traceback

        return jsonify({
            "ok": False,
            "erro": str(e),
            "traceback": traceback.format_exc()
        }), 500

@app.route("/debug-supabase")
def debug_supabase():
    hoje = datetime.now(TIMEZONE_DADOS).strftime("%Y-%m-%d")

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    }

    saida = {}

    for tabela, loteria in TABELAS_SUPABASE.items():
        url = f"{SUPABASE_URL}/{tabela}"
        params = {
            "select": "*",
            "limit": "3",
        }

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            saida[tabela] = {
                "status": resp.status_code,
                "texto": resp.text[:2000],
            }
        except Exception as e:
            saida[tabela] = {
                "erro": str(e)
            }

    return jsonify({
        "ok": True,
        "data_consulta": hoje,
        "resultado": saida
    })

@app.route("/debug-lk/<data_teste>")
def debug_lk(data_teste):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    }

    url = f"{SUPABASE_URL}/resultados_lk"

    saida = {}

    for campo in ["data", "dados"]:
        params = {
            "select": "*",
            campo: f"eq.{data_teste}",
        }

        resp = requests.get(url, headers=headers, params=params, timeout=30)

        saida[campo] = {
            "status": resp.status_code,
            "texto": resp.text[:3000],
        }

    return jsonify(saida)


@app.route("/atualizar")
def atualizar_manual():
    try:
        retorno = atualizar_planilha()

        return jsonify({
            "ok": retorno.get("ok"),
            "inseridos": retorno.get("inseridos"),
            "ignorados": retorno.get("ignorados_por_duplicidade"),
            "executado_em": retorno.get("executado_em"),
        })

    except Exception as e:
        logging.exception("Erro na atualização manual")

        return jsonify({
            "ok": False,
            "erro": str(e),
        }), 500


# =========================
# AGENDADOR
# =========================

scheduler = BackgroundScheduler(timezone=str(TIMEZONE))


@scheduler.scheduled_job("interval", minutes=20)
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
