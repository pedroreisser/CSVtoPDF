# CSVtoPDF

Baixa automaticamente PDFs de acesso aberto a partir de uma lista de artigos
exportada do Scopus ou Web of Science (CSV limpo ou TXT bruto do WoS). Para cada
DOI, tenta em cascata três fontes até achar o PDF:
[Unpaywall](https://unpaywall.org/) → [OpenAlex](https://openalex.org/) →
[Semantic Scholar](https://www.semanticscholar.org/).

## Como rodar

**Jeito fácil** (instala dependências sozinho na primeira vez, sem admin):

- **Windows**: duplo clique em `Instalar dependências (Windows 11).bat` (instala até o Python, se faltar)
- **Linux/Mac**: `python3 iniciar.py`

**Jeito manual**:

```bash
pip install -r requirements.txt
python app.py
```

Instruções para usuários leigos em `INSTRUCOES.txt`.

## Uso

1. **Arquivo**: selecione um ou mais `.csv`/`.txt` exportados (o WoS limita cada
   export a 1000 registros — selecione todos os blocos de uma vez com
   Ctrl/Shift+clique; duplicatas entre arquivos são removidas). O programa detecta
   sozinho o delimitador e as colunas de DOI/Título (`DOI`/`DI`,
   `Title`/`Titulo`/`TI`), Ano (`Year`/`PY`/`Ano`) e relevância
   (`relevancia`/`score`). Se não conseguir identificar, escolha as colunas
   manualmente. O botão **Baixar modelo de CSV** salva um `modelo_input.csv` com o
   cabeçalho esperado (`DOI,Titulo,Ano,relevancia`). Para pedir a uma IA que monte
   a lista, use `modelo_input.md` — tem as regras de formato e um lugar para colar
   as referências.
2. **Configuração**: informe um e-mail válido (exigido pela API da Unpaywall, sem
   necessidade de conta) e a pasta de destino dos PDFs (padrão: pasta `pdfs` ao lado
   do arquivo carregado).
3. **Download**: clique em "Iniciar download". Acompanhe o progresso, os contadores
   e a tabela em tempo real. É possível cancelar a qualquer momento.
4. **Resultado**: ao final, um resumo mostra quantos artigos foram baixados, quantos
   não tinham versão em acesso aberto e quantos deram erro. São salvos na pasta de
   destino:
   - `download_log.csv` — log completo de todos os artigos (com `status`, a `fonte`
     que achou o PDF e a `relevancia`, quando presente).
   - `nao_encontrados.csv` / `nao_encontrados.html` — apenas os não baixados. O HTML
     tem DOIs clicáveis, colunas de ano e relevância (com filtro "≥ X" e ordenação).
   - `csvtopdf_debug.log` — log técnico (timestamps, exceções com traceback) para
     diagnosticar problemas reportados por outros usuários; peça para te enviarem
     esse arquivo. Gerado por padrão; dá para desativar no checkbox da seção
     "2. Configuração".

## Estrutura

- `downloader.py` — lógica pura (leitura de arquivo, busca em cascata
  Unpaywall/OpenAlex/Semantic Scholar, download, geração dos logs e do HTML). Sem
  dependência de interface; rode `python downloader.py` para os self-checks.
- `modelo_input.md` — instruções para uma IA montar o CSV de entrada a partir de
  uma lista de referências (o próprio CSV de exemplo também é baixável pela
  interface, botão **Baixar modelo de CSV**).
- `gui.py` — interface tkinter, roda o download numa thread separada e recebe o
  progresso via `queue.Queue`.
- `app.py` — ponto de entrada.
- `iniciar.py` / `Instalar dependências (Windows 11).bat` — launchers com instalação automática de
  dependências (adaptados do Excerpta).

## Observações

- O app nunca tenta contornar paywall — só baixa links que as próprias fontes
  (Unpaywall, OpenAlex, Semantic Scholar) retornam como acesso aberto. Há uma pausa
  de ~1s entre cada chamada de API, inclusive entre fontes do mesmo artigo.
- O e-mail informado é salvo em `~/.csvtopdf_config.json` para a próxima sessão.
- A sessão HTTP envia um User-Agent de navegador comum (alguns editores bloqueiam
  o User-Agent padrão do `requests` mesmo em links de acesso aberto confirmado
  pelo Unpaywall). Corrige o bloqueio em parte dos casos (ex.: academicjournals.org);
  editores com proteção mais robusta (Cloudflare/fingerprint), como MDPI e Wiley,
  continuam bloqueando — o app não tenta contornar isso.
- Chave de API do Semantic Scholar (campo opcional na seção "2. Configuração"):
  sem ela, a API usa uma cota anônima baixa e compartilhada por IP, que costuma
  devolver 429 (limite excedido) e derruba a taxa de acerto dessa fonte. Chave
  grátis em semanticscholar.org/product/api.
