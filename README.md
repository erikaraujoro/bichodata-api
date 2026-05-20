# API BichoData → Google Sheets

Esta API roda na nuvem e, de 30 em 30 minutos, busca os resultados no BichoData e grava na aba `RESULTADOS` da sua planilha Google.

## Arquivos

- `app.py`
- `requirements.txt`
- `render.yaml`

## Colunas usadas na planilha

```text
Data | Loteria | Horário | M1 | M2 | M3 | M4 | M5 | M6 | M7
```

A API grava apenas M1 até M5. As colunas M6 e M7 ficam vazias.

## Variáveis de ambiente no Render

Configure no Render:

```text
SPREADSHEET_ID
SHEET_NAME
GOOGLE_CREDENTIALS_JSON
```

Exemplo:

```text
SPREADSHEET_ID=1J-lnx-_1TLD_TDqfqRTidrdNolzvOGTO-nJYoRK20n0
SHEET_NAME=RESULTADOS
GOOGLE_CREDENTIALS_JSON=conteúdo inteiro do seu credenciais.json
```

## Importante

No Google Sheets, compartilhe a planilha com o e-mail `client_email` que está dentro do seu `credenciais.json`.

Exemplo:

```text
xxxx@xxxx.iam.gserviceaccount.com
```

Dê permissão de Editor.

## Rotas

```text
/
```

Mostra status da API.

```text
/health
```

Verifica se está online.

```text
/preview
```

Busca resultados no BichoData, mas não grava.

```text
/atualizar
```

Busca resultados e grava na planilha, evitando duplicidade por:

```text
Data + Loteria + Horário
```

## Mapeamento das loterias

- PTM - Manhã, PT - Rio, PT - Tarde, PTV - Vesper, PTN - Noite, COR - Coruja → PT-RJ
- LOOK - 07H, 09H, 11H, 14H, 16H, 18H, 21H, 23H → LOOK-GO
- Federal → FEDERAL
- Nacional → NACIONAL
- SP → PT-SP
- LOTEP → LOTEP
- LOTECE → LOTECE
- BAHIA → BAHIA

O horário gravado é sempre apenas os dois primeiros dígitos da hora.
