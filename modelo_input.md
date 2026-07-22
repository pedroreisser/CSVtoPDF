# Instruções para IA — montar o CSV de entrada do CSVtoPDF

Cole este arquivo inteiro numa conversa com uma IA (ChatGPT, Claude, etc.),
junto com a sua lista de referências, e peça para ela gerar o CSV.

## Formato de saída exigido

- Primeira linha (cabeçalho), exatamente assim: `DOI,Titulo,Ano,relevancia`
- Uma linha por artigo
- Separador: vírgula. Se um campo tiver vírgula (comum em títulos), coloque
  o campo entre aspas duplas
- Codificação UTF-8

## Regras por coluna

- **DOI**: só o identificador, ex. `10.1234/exemplo`. Não inclua
  `https://doi.org/` na frente. Deixe vazio se não souber — nesse caso o
  programa não consegue baixar o PDF automaticamente, mas a linha ainda
  entra no relatório de não encontrados.
- **Titulo**: título completo do artigo. Obrigatório quando não há DOI.
- **Ano** (opcional): ano de publicação, 4 dígitos (ex. `2021`). Vazio se
  não souber.
- **relevancia** (opcional): sua avaliação de quão relevante o artigo é para
  o tema informado abaixo, de `0.00` (nada relevante) a `1.00` (totalmente
  relevante). Ponto ou vírgula decimal, tanto faz. Deixe vazio se eu não
  pedir avaliação de relevância.

## Exemplo de saída válida

```csv
DOI,Titulo,Ano,relevancia
10.1038/s41586-020-2649-2,Article title example here,2020,0.85
,Another article without known DOI,2019,0.40
```

## O que fazer

1. Leia a lista de referências colada abaixo (pode estar em qualquer
   formato: texto solto, tabela, outro CSV, referências de um PDF etc.).
2. Gere o CSV completo, uma linha por artigo, seguindo as regras acima.
3. Não invente DOI, ano ou título — se não souber, deixe o campo vazio.
4. Devolva só o CSV, pronto para eu copiar para um arquivo `.csv`.

---

**Tema da pesquisa (para avaliar relevância):** PREENCHA AQUI

**Lista de referências:**

COLE AQUI
