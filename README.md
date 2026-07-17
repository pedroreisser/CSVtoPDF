# CSVtoPDF

Baixa automaticamente PDFs de acesso aberto (via [Unpaywall](https://unpaywall.org/)) a
partir de uma lista de artigos exportada do Scopus ou Web of Science (CSV limpo ou
TXT bruto do WoS).

## Como rodar

**Jeito fácil** (instala dependências sozinho na primeira vez, sem admin):

- **Windows**: duplo clique em `iniciar.bat` (instala até o Python, se faltar)
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
   `Title`/`Titulo`/`TI`). Se não conseguir identificar, escolha as colunas
   manualmente.
2. **Configuração**: informe um e-mail válido (exigido pela API da Unpaywall, sem
   necessidade de conta) e a pasta de destino dos PDFs (padrão: pasta `pdfs` ao lado
   do arquivo carregado).
3. **Download**: clique em "Iniciar download". Acompanhe o progresso, os contadores
   e a tabela em tempo real. É possível cancelar a qualquer momento.
4. **Resultado**: ao final, um resumo mostra quantos artigos foram baixados, quantos
   não tinham versão em acesso aberto e quantos deram erro. Dois arquivos são salvos
   na pasta de destino:
   - `download_log.csv` — log completo de todos os artigos processados.
   - `nao_encontrados.csv` — apenas os que não foram baixados (DOI/Título), pronto
     para reimportar depois ou repassar para busca manual.

## Estrutura

- `downloader.py` — lógica pura (leitura de arquivo, consulta à Unpaywall, download,
  geração dos logs). Sem dependência de interface; rode `python downloader.py` para
  os self-checks.
- `gui.py` — interface tkinter, roda o download numa thread separada e recebe o
  progresso via `queue.Queue`.
- `app.py` — ponto de entrada.
- `iniciar.py` / `iniciar.bat` — launchers com instalação automática de
  dependências (adaptados do Excerpta).

## Observações

- O app nunca tenta contornar paywall — só baixa links que a própria Unpaywall
  retorna como acesso aberto.
- O e-mail informado é salvo em `~/.csvtopdf_config.json` para a próxima sessão.
