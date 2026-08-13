import os
import re
import json
import logging
from datetime import datetime, timedelta
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

# =========================
# RESULTADOS JB CERTO
# =========================

URLS_JBCERTO = {
    "PT-RJ": "https://resultadosjbcerto.com.br/pt-rio/",
    "LOOK-GO": "https://resultadosjbcerto.com.br/look/",
    "NACIONAL": "https://resultadosjbcerto.com.br/nacional/",
    "PT-SP": "https://resultadosjbcerto.com.br/pt-sp/",
    "FEDERAL": "https://resultadosjbcerto.com.br/federal/",
    "LOTEP": "https://resultadosjbcerto.com.br/lotep/",
    "LOTECE": "https://resultadosjbcerto.com.br/lotece/",
    "BAHIA": "https://resultadosjbcerto.com.br/bahia/",
}

# =========================
# HORÁRIOS OFICIAIS NO LOTERIASDB
# =========================

HORARIOS_VALIDOS_JBCERTO = {
    "LOTEP": {
        "09",
        "10",
        "12",
        "15",
        "18",
        "20",
    },

    "PT-SP": {
        "08",
        "10",
        "12",
        "13",
        "15",
        "17",
        "19",
        "20",
    },
}

HEADERS_JBCERTO = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/142.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


# =========================
# RESULTADO FÁCIL - COMPLEMENTO LOTEP
# =========================

URL_RESULTADO_FACIL_PARATODOS = (
    "https://www.resultadofacil.com.br/"
    "resultados-paratodos-pb-do-dia-"
)

URL_RESULTADO_FACIL_LOTEP = (
    "https://www.resultadofacil.com.br/"
    "resultados-lotep-do-dia-"
)

HEADERS_RESULTADO_FACIL = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/142.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
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
    texto = normalizar_texto(valor).upper()

    texto = texto.replace("H", ":")
    texto = texto.replace("HS", ":")
    texto = texto.replace("HORAS", ":")

    m = re.search(r"(\d{1,2})\s*:", texto)
    if m:
        return m.group(1).zfill(2)

    m = re.search(r"(\d{1,2})", texto)
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
# RESULTADO FÁCIL - COMPLEMENTO LOTEP
# =========================

def obter_campo_dataset(item, nomes):
    for nome in nomes:
        valor = item.get(nome)

        if valor not in [None, ""]:
            return str(valor).strip()

        nome_up = nome.upper()
        valor = item.get(nome_up)

        if valor not in [None, ""]:
            return str(valor).strip()

    return ""


def extrair_dataset_resultadofacil(html):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    scripts = soup.find_all(
        "script",
        attrs={
            "type": "application/ld+json"
        }
    )

    for script in scripts:
        conteudo = (
            script.string
            or script.get_text(
                strip=True
            )
        )

        if not conteudo:
            continue

        try:
            dados = json.loads(
                conteudo
            )
        except Exception:
            continue

        grafo = dados.get(
            "@graph",
            []
        )

        if not isinstance(
            grafo,
            list
        ):
            continue

        for item in grafo:
            if not isinstance(
                item,
                dict
            ):
                continue

            tipo = str(
                item.get(
                    "@type",
                    ""
                )
            ).lower()

            if tipo != "dataset":
                continue

            variaveis = item.get(
                "variableMeasured",
                []
            )

            if isinstance(
                variaveis,
                list
            ):
                return variaveis

    return []


def extrair_posicao_dataset(nome):
    texto = normalizar_texto(
        nome
    ).replace(
        "º",
        "o"
    ).replace(
        "ª",
        "a"
    ).lower()

    match = re.search(
        r"\b([1-5])\s*[oa]?\s*pr[eê]mio\b",
        texto
    )

    if not match:
        return None

    return int(
        match.group(1)
    )


def extrair_horario_dataset(nome):
    texto = normalizar_texto(
        nome
    )

    horarios = re.findall(
        r"\b([01]?\d|2[0-3]):([0-5]\d)\b",
        texto
    )

    if horarios:
        return horarios[-1][0].zfill(
            2
        )

    match = re.search(
        r"\b(\d{1,2})\s*h\b",
        texto.lower()
    )

    if match:
        return match.group(
            1
        ).zfill(
            2
        )

    return ""


def extrair_milhar_dataset(valor):
    valor = normalizar_texto(
        valor
    )

    match = re.search(
        r"(?<!\d)(\d{1,4})(?!\d)",
        valor
    )

    if not match:
        return ""

    return match.group(
        1
    ).zfill(
        4
    )


def buscar_resultados_resultadofacil_lotep(
    dias_retroativos=3
):
    """
    Complementa a LOTEP com horários que o JB Certo
    não está entregando de forma confiável.

    Fontes:
    - PARATODOS PB -> 09h
    - LOTEP        -> 10h, 12h e 15h

    O horário 20h permanece vindo do JB Certo.
    """

    hoje = datetime.now(
        TIMEZONE_DADOS
    ).date()

    data_minima = hoje - timedelta(
        days=max(
            0,
            int(
                dias_retroativos
            )
        )
    )

    resultados = []

    data_atual = data_minima

    while data_atual <= hoje:
        data_iso = data_atual.strftime(
            "%Y-%m-%d"
        )

        data_br = data_atual.strftime(
            "%d/%m/%Y"
        )

        paginas = [
            (
                URL_RESULTADO_FACIL_PARATODOS
                + data_iso,
                {
                    "09"
                },
                "RESULTADO_FACIL_PARATODOS_PB",
            ),
            (
                URL_RESULTADO_FACIL_LOTEP
                + data_iso,
                {
                    "10",
                    "12",
                    "15",
                },
                "RESULTADO_FACIL_LOTEP",
            ),
        ]

        for url, horarios_desejados, origem in paginas:
            try:
                resp = requests.get(
                    url,
                    headers=HEADERS_RESULTADO_FACIL,
                    timeout=30,
                )

                if resp.status_code == 404:
                    continue

                resp.raise_for_status()

            except Exception as e:
                logging.exception(
                    (
                        "Erro Resultado Fácil "
                        "LOTEP %s: %s"
                    ),
                    url,
                    e,
                )
                continue

            variaveis = (
                extrair_dataset_resultadofacil(
                    resp.text
                )
            )

            sorteios = {}

            for item in variaveis:
                if not isinstance(
                    item,
                    dict
                ):
                    continue

                nome = obter_campo_dataset(
                    item,
                    [
                        "name",
                        "nome",
                    ]
                )

                valor = obter_campo_dataset(
                    item,
                    [
                        "value",
                        "valor",
                    ]
                )

                if not nome or not valor:
                    continue

                nome_upper = normalizar_texto(
                    nome
                ).upper()

                if "FEDERAL" in nome_upper:
                    continue

                horario = (
                    extrair_horario_dataset(
                        nome
                    )
                )

                if horario not in horarios_desejados:
                    continue

                # Proteção por página
                if origem == "RESULTADO_FACIL_PARATODOS_PB":
                    if (
                        "PARATODOS"
                        not in nome_upper
                        and "PARA TODOS"
                        not in nome_upper
                        and " PB "
                        not in f" {nome_upper} "
                    ):
                        continue

                if origem == "RESULTADO_FACIL_LOTEP":
                    if "LOTEP" not in nome_upper:
                        continue

                posicao = (
                    extrair_posicao_dataset(
                        nome
                    )
                )

                if posicao is None:
                    continue

                milhar = (
                    extrair_milhar_dataset(
                        valor
                    )
                )

                if not milhar:
                    continue

                sorteios.setdefault(
                    horario,
                    {}
                )

                sorteios[
                    horario
                ][
                    posicao
                ] = milhar

            for horario in sorted(
                sorteios.keys()
            ):
                premios_dict = sorteios[
                    horario
                ]

                if not all(
                    p in premios_dict
                    for p in range(
                        1,
                        6
                    )
                ):
                    continue

                premios = [
                    premios_dict[1],
                    premios_dict[2],
                    premios_dict[3],
                    premios_dict[4],
                    premios_dict[5],
                ]

                m6, m7 = calcular_premios_6_7(
                    premios
                )

                linha = [
                    data_br,
                    "LOTEP",
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
                    "loteria": "LOTEP",
                    "horario": horario,
                    "premios": (
                        premios
                        + [
                            m6,
                            m7,
                        ]
                    ),
                    "linha": linha,
                    "origem": origem,
                    "titulo_original": (
                        "Complemento Resultado Fácil"
                    ),
                    "url_origem": url,
                })

        data_atual += timedelta(
            days=1
        )

    resultados.sort(
        key=lambda r: (
            datetime.strptime(
                r["data"],
                "%d/%m/%Y"
            ),
            r["loteria"],
            r["horario"],
        )
    )

    logging.info(
        (
            "Resultado Fácil complemento LOTEP: "
            "%s resultado(s)."
        ),
        len(
            resultados
        ),
    )

    return resultados


def combinar_resultados_fontes(
    resultados_jb,
    resultados_complementares
):
    """
    JB Certo tem prioridade.

    O Resultado Fácil só preenche uma chave
    Data + Loteria + Horário que ainda não exista.
    """

    combinados = []
    chaves = set()

    for resultado in (
        resultados_jb
        + resultados_complementares
    ):
        chave = (
            f"{resultado['data']}|"
            f"{resultado['loteria']}|"
            f"{str(resultado['horario']).zfill(2)}"
        )

        if chave in chaves:
            continue

        chaves.add(
            chave
        )

        combinados.append(
            resultado
        )

    combinados.sort(
        key=lambda r: (
            datetime.strptime(
                r["data"],
                "%d/%m/%Y"
            ),
            r["loteria"],
            r["horario"],
        )
    )

    return combinados


# =========================
# BUSCA RESULTADOS JB CERTO
# =========================

def extrair_data_jbcerto(texto):
    """
    Procura uma data no formato DD/MM/AAAA.
    """
    texto = normalizar_texto(texto)

    match = re.search(
        r"\b(\d{2}/\d{2}/\d{4})\b",
        texto
    )

    if not match:
        return ""

    return match.group(1)


def extrair_horario_jbcerto(titulo, loteria):
    """
    Extrai somente a hora da identificação do sorteio.

    Exemplos:
    18h20 -> 18
    9h20 -> 09
    11 horas -> 11
    Federal 20 horas -> 20
    """
    titulo = normalizar_texto(titulo).lower()

    match = re.search(
        r"\b(\d{1,2})\s*(?:h|horas?)",
        titulo
    )

    if match:
        return match.group(1).zfill(2)

    # Se o horário não estiver claramente identificado
    # no título do sorteio, o resultado é ignorado.
    # Não utiliza horário fixo para nenhuma loteria.
    return ""

def normalizar_resultado_jbcerto(
    loteria,
    horario,
    titulo
):
    """
    Normaliza os agrupamentos utilizados no LoteriasDB.

    PARAÍBA
    --------
    PARATODOS PB 09:45 -> LOTEP | 09
    LOTEP 10:45        -> LOTEP | 10
    LOTEP 12:45        -> LOTEP | 12
    LOTEP 15:45        -> LOTEP | 15
    LOTEP 18h          -> LOTEP | 18
    PARATODOS PB 20h   -> LOTEP | 20

    SÃO PAULO
    ----------
    Bandeirantes 15:30 -> PT-SP | 15
    demais PT-SP       -> PT-SP | horário original
    """

    loteria = str(
        loteria
    ).strip().upper()

    horario = str(
        horario
    ).strip().zfill(2)

    titulo_lower = normalizar_texto(
        titulo
    ).lower()

    # =====================================================
    # PARAÍBA / LOTEP
    # =====================================================

    if loteria == "LOTEP":

        # O JB Certo pode identificar esses horários como
        # Paraíba PARATODOS, mas dentro do LoteriasDB todo
        # o conjunto permanece agrupado como LOTEP.
        if (
            "paratodos" in titulo_lower
            or "para todos" in titulo_lower
        ):
            if horario in {
                "09",
                "20",
            }:
                return (
                    "LOTEP",
                    horario,
                )

        return (
            "LOTEP",
            horario,
        )

    # =====================================================
    # SÃO PAULO / BANDEIRANTES
    # =====================================================

    if loteria == "PT-SP":

        # Bandeirantes 15:30 faz parte do agrupamento
        # PT-SP no LoteriasDB.
        if (
            "bandeirantes"
            in titulo_lower
            or "bandeirante"
            in titulo_lower
        ):
            if horario == "15":
                return (
                    "PT-SP",
                    "15",
                )

        return (
            "PT-SP",
            horario,
        )

    return (
        loteria,
        horario,
    )


def obter_titulo_anterior_tabela_jbcerto(tabela):
    """
    Procura o identificador do sorteio imediatamente antes da tabela.

    Exemplos encontrados no JB Certo:
    LT PT RIO – 16 horas PTV
    LT LOOK – 18h20
    LT NACIONAL – 17 horas
    PT SP – 19h20
    Federal 20 horas
    """

    elemento = tabela.find_previous()

    limite = 0

    while elemento is not None and limite < 40:
        limite += 1

        try:
            texto = normalizar_texto(
                elemento.get_text(" ", strip=True)
            )
        except Exception:
            elemento = elemento.find_previous()
            continue

        if texto:
            texto_lower = texto.lower()

            tem_horario = re.search(
                r"\b\d{1,2}\s*(?:h\d{0,2}|horas?)\b",
                texto_lower
            )

            if tem_horario:
                if (
                    "lt pt rio" in texto_lower
                    or "lt look" in texto_lower
                    or "lt nacional" in texto_lower
                    or "pt sp" in texto_lower
                    or "federal" in texto_lower
                    or "lotece" in texto_lower
                    or "lotep" in texto_lower
                    or "bahia" in texto_lower
                ):
                    return texto

        elemento = elemento.find_previous()

    return ""


def obter_data_anterior_tabela_jbcerto(tabela):
    """
    Procura a data DD/MM/AAAA imediatamente anterior à tabela.
    """

    elemento = tabela.find_previous()

    limite = 0

    while elemento is not None and limite < 30:
        limite += 1

        try:
            texto = normalizar_texto(
                elemento.get_text(" ", strip=True)
            )
        except Exception:
            elemento = elemento.find_previous()
            continue

        match = re.search(
            r"\b(\d{2}/\d{2}/\d{4})\b",
            texto
        )

        if match:
            return match.group(1)

        elemento = elemento.find_previous()

    return ""


def extrair_milhares_tabela_jbcerto(tabela):
    """
    Lê a tabela do JB Certo e retorna M1 até M5.
    """

    premios = {}

    linhas = tabela.find_all("tr")

    for linha in linhas:
        colunas = linha.find_all(["td", "th"])

        if len(colunas) < 2:
            continue

        premio_txt = normalizar_texto(
            colunas[0].get_text(" ", strip=True)
        )

        milhar_txt = normalizar_texto(
            colunas[1].get_text(" ", strip=True)
        )

        match_premio = re.search(
            r"([1-5])",
            premio_txt
        )

        if not match_premio:
            continue

        numero_premio = int(
            match_premio.group(1)
        )

        milhar = formatar_milhar(
            milhar_txt
        )

        if not milhar:
            continue

        premios[numero_premio] = milhar

    if not all(
        numero in premios
        for numero in range(1, 6)
    ):
        return []

    return [
        premios[1],
        premios[2],
        premios[3],
        premios[4],
        premios[5],
    ]


def buscar_resultados_jbcerto(
    dias_retroativos=3
):
    """
    Busca resultados diretamente no site Resultados JB Certo.

    A leitura é feita para todas as loterias configuradas em
    URLS_JBCERTO.

    São considerados resultados de hoje e também resultados recentes,
    permitindo recuperar sorteios que tenham sido publicados com atraso.
    """

    hoje = datetime.now(
        TIMEZONE_DADOS
    ).date()

    data_minima = hoje - timedelta(
        days=max(0, int(dias_retroativos))
    )

    resultados = []
    chaves_processadas = set()

    for loteria, url in URLS_JBCERTO.items():

        logging.info(
            "Buscando JB Certo: %s - %s",
            loteria,
            url,
        )

        try:
            resposta = requests.get(
                url,
                headers=HEADERS_JBCERTO,
                timeout=30,
            )

            resposta.raise_for_status()

        except Exception as e:
            logging.exception(
                "Erro ao acessar JB Certo para %s: %s",
                loteria,
                e,
            )
            continue

        soup = BeautifulSoup(
            resposta.text,
            "html.parser"
        )

        tabelas = soup.find_all("table")

        logging.info(
            "JB Certo %s: %s tabela(s) encontrada(s).",
            loteria,
            len(tabelas),
        )

        for tabela in tabelas:

            premios = extrair_milhares_tabela_jbcerto(
                tabela
            )

            if len(premios) != 5:
                continue

            titulo = obter_titulo_anterior_tabela_jbcerto(
                tabela
            )

            data_br = obter_data_anterior_tabela_jbcerto(
                tabela
            )

            horario = extrair_horario_jbcerto(
                titulo,
                loteria,
            )

            # =================================================
            # NORMALIZA AGRUPAMENTOS DO LOTERIASDB
            # =================================================

            if horario:

                (
                    loteria_normalizada,
                    horario_normalizado,
                ) = normalizar_resultado_jbcerto(
                    loteria,
                    horario,
                    titulo,
                )

            else:

                loteria_normalizada = loteria
                horario_normalizado = horario

            # -------------------------------------------------
            # IGNORA FEDERAL REPLICADA EM PÁGINAS DE OUTRAS
            # LOTERIAS
            # -------------------------------------------------
            #
            # O JB Certo também publica o resultado da Federal
            # dentro de páginas como PT-RIO e PT-SP.
            #
            # Exemplo:
            # "LT PT RIO – Federal 20 horas"
            # "PT SP – Federal 20 horas"
            #
            # A Federal será coletada exclusivamente pela página
            # própria configurada como FEDERAL.
            # -------------------------------------------------

            titulo_lower = titulo.lower()

            if (
                loteria != "FEDERAL"
                and "federal" in titulo_lower
            ):
                logging.info(
                    "Federal replicada ignorada: %s | %s",
                    loteria,
                    titulo,
                )
                continue

            if not data_br:
                logging.warning(
                    "Tabela JB Certo ignorada sem data: %s | %s",
                    loteria,
                    titulo,
                )
                continue

            if not horario_normalizado:
                logging.warning(
                    "Tabela JB Certo ignorada sem horário: %s | %s",
                    loteria,
                    titulo,
                )
                continue
            
            loteria_resultado = (
                loteria_normalizada
            )

            horario_resultado = (
                horario_normalizado
            )
            
            # =================================================
            # HORÁRIOS CONTROLADOS
            # =================================================

            horarios_validos = (
                HORARIOS_VALIDOS_JBCERTO.get(
                    loteria_resultado
                )
            )

            if (
                horarios_validos
                and horario_resultado
                not in horarios_validos
            ):
                logging.warning(
                    (
                        "Horário inesperado ignorado: "
                        "%s | %s | %s"
                    ),
                    loteria_resultado,
                    horario_resultado,
                    titulo,
                )

                continue

            try:
                data_obj = datetime.strptime(
                    data_br,
                    "%d/%m/%Y"
                ).date()

            except ValueError:
                logging.warning(
                    "Data inválida no JB Certo: %s",
                    data_br,
                )
                continue

            if data_obj < data_minima:
                continue

            if data_obj > hoje:
                continue

            m6, m7 = calcular_premios_6_7(
                premios
            )

            chave = (
                f"{data_br}|"
                f"{loteria_resultado}|"
                f"{horario_resultado}"
            )

            if chave in chaves_processadas:
                continue

            chaves_processadas.add(chave)

            linha = [
                data_br,
                loteria_resultado,
                horario_resultado,
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
                "loteria": loteria_resultado,
                "horario": horario_resultado,
                "premios": premios + [m6, m7],
                "linha": linha,
                "origem": "JB_CERTO",
                "titulo_original": titulo,
                "url_origem": url,
            })

    resultados.sort(
        key=lambda r: (
            datetime.strptime(
                r["data"],
                "%d/%m/%Y"
            ),
            r["loteria"],
            r["horario"],
        )
    )

    logging.info(
        "JB Certo: total de %s resultado(s) válido(s).",
        len(resultados),
    )

    return resultados


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
            if tabela == "resultado_federal":
                horario = "20"
            else:
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
                    else:
                        chave_item = f"{tabela}|{chave_item}"

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

            if str(data_item).strip()[:10] != data_consulta:
                continue

            data_br = formatar_data_br(data_item)
            if tabela == "resultado_federal":
                horario = "20"
            else:
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
    logging.info(
        "Iniciando atualização JB Certo + complemento LOTEP Resultado Fácil..."
    )

    # Relê alguns dias anteriores para recuperar
    # sorteios que eventualmente tenham sido publicados
    # com atraso no site de origem.
    resultados_jb = buscar_resultados_jbcerto(
        dias_retroativos=3
    )

    resultados_complementares = (
        buscar_resultados_resultadofacil_lotep(
            dias_retroativos=3
        )
    )

    resultados = combinar_resultados_fontes(
        resultados_jb,
        resultados_complementares,
    )

    if not resultados:
        logging.warning(
            "Nenhum resultado encontrado nas fontes configuradas."
        )

        return {
            "ok": False,
            "mensagem": (
                "Nenhum resultado encontrado nas fontes configuradas."
            ),
            "fonte": "JB_CERTO+RESULTADO_FACIL_LOTEP",
            "inseridos": 0,
            "resultados_lidos": 0,
            "ignorados_por_duplicidade": 0,
        }

    ws = conectar_planilha()

    garantir_cabecalho(
        ws
    )

    chaves_existentes = (
        carregar_chaves_existentes(
            ws
        )
    )

    novas_linhas = []
    ignorados = []

    for resultado in resultados:

        data = resultado["data"]

        loteria = resultado["loteria"]

        horario = str(
            resultado["horario"]
        ).zfill(2)

        chave = (
            f"{data}|"
            f"{loteria}|"
            f"{horario}"
        )

        if chave in chaves_existentes:

            ignorados.append(
                chave
            )

            continue

        novas_linhas.append(
            resultado["linha"]
        )

        chaves_existentes.add(
            chave
        )

    if novas_linhas:

        # RAW preserva:
        #
        # horário 09
        # milhares 0036
        # milhares 0304
        # M7 045
        #
        # sem conversões automáticas do Google Sheets.
        ws.append_rows(
            novas_linhas,
            value_input_option="RAW",
        )

    logging.info(
        (
            "Atualização JB Certo concluída. "
            "Lidos: %s | "
            "Inseridos: %s | "
            "Duplicados ignorados: %s"
        ),
        len(resultados),
        len(novas_linhas),
        len(ignorados),
    )

    return {
        "ok": True,
        "fonte": "JB_CERTO+RESULTADO_FACIL_LOTEP",
        "mensagem": (
            "Atualização JB Certo + complemento LOTEP concluída."
        ),
        "resultados_lidos": len(
            resultados
        ),
        "inseridos": len(
            novas_linhas
        ),
        "ignorados_por_duplicidade": len(
            ignorados
        ),
        "executado_em": datetime.now(
            TIMEZONE
        ).strftime(
            "%d/%m/%Y %H:%M:%S"
        ),
    }

# =========================
# ROTAS API
# =========================

@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "servico": (
            "API JB Certo para Google Sheets"
        ),
        "fonte_principal": (
            "Resultados JB Certo"
        ),
        "rotas": {
            "/atualizar": (
                "Busca resultados no JB Certo "
                "e grava na planilha"
            ),
            "/preview": (
                "Mostra os resultados do JB Certo "
                "sem gravar"
            ),
            "/health": (
                "Verifica se a API está online"
            ),
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

        resultados_jb = buscar_resultados_jbcerto(
            dias_retroativos=3
        )

        resultados_complementares = (
            buscar_resultados_resultadofacil_lotep(
                dias_retroativos=3
            )
        )

        resultados = combinar_resultados_fontes(
            resultados_jb,
            resultados_complementares,
        )

        resumo_loterias = {}

        for resultado in resultados:

            loteria = resultado[
                "loteria"
            ]

            resumo_loterias[
                loteria
            ] = (
                resumo_loterias.get(
                    loteria,
                    0
                )
                + 1
            )

        return jsonify({
            "ok": True,
            "fonte": "JB Certo + complemento Resultado Fácil LOTEP",
            "total": len(
                resultados
            ),
            "por_loteria": resumo_loterias,
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
# PREVIEW RESULTADOS JB CERTO
# Não grava nada na planilha.
# =====================================================

@app.route("/preview-jbcerto")
def preview_jbcerto():
    try:
        resultados = buscar_resultados_jbcerto(
            dias_retroativos=3
        )

        resumo_loterias = {}

        for resultado in resultados:
            loteria = resultado["loteria"]

            resumo_loterias[loteria] = (
                resumo_loterias.get(
                    loteria,
                    0
                ) + 1
            )

        return jsonify({
            "ok": True,
            "fonte": "Resultados JB Certo",
            "total": len(resultados),
            "por_loteria": resumo_loterias,
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
