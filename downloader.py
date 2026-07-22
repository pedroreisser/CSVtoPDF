"""Lógica pura de leitura de listas de artigos e download via Unpaywall.

Sem dependência de interface — testável isoladamente (ver demo() no final).
"""
import csv
import html
import json
import logging
import platform
import re
import time
from pathlib import Path
from urllib.parse import quote

import requests

# Log de diagnóstico: técnico, com traceback, pensado para o usuário anexar e
# enviar quando reportar um problema — diferente do download_log.csv, que é o
# relatório de resultado por artigo (sem detalhe de exceção).
logger = logging.getLogger("csvtopdf")
logger.setLevel(logging.DEBUG)

UNPAYWALL_URL = "https://api.unpaywall.org/v2/{doi}"
OPENALEX_URL = "https://api.openalex.org/works/doi:{doi}"
SEMANTIC_URL = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
REQUEST_DELAY = 1.0
API_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 30
# Alguns editores (MDPI, Wiley...) bloqueiam por WAF o User-Agent padrão do
# requests ("python-requests/x.y") mesmo em links que o Unpaywall confirma
# como acesso aberto; um User-Agent de navegador comum evita esse bloqueio.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DOI_SYNONYMS = {"doi", "di"}
TITLE_SYNONYMS = {"title", "titulo", "título", "ti"}
YEAR_SYNONYMS = {"year", "ano", "py", "publication year", "publicationyear", "pubyear"}
RELEVANCIA_SYNONYMS = {"relevancia", "relevância", "score"}

CONFIG_PATH = Path.home() / ".csvtopdf_config.json"

# Modelo de CSV de entrada (baixável pelo botão na GUI; ver também modelo_input.md,
# com instruções para uma IA montar a lista a partir de referências soltas).
MODELO_CSV = ("DOI,Titulo,Ano,relevancia\n"
              "10.xxxx/exemplo,Título de exemplo aqui,2020,0.85\n")

DOI_PREFIX_RE = re.compile(r"^(https?://)?(dx\.)?doi\.org/", re.IGNORECASE)
ILLEGAL_CHARS_RE = re.compile(r'[\\/*?:"<>|]')
YEAR_RE = re.compile(r"(19|20)\d\d")


def _parse_year(value):
    """Extrai um ano de 4 dígitos (1900–2099) de um texto; "" se não achar."""
    m = YEAR_RE.search(value or "")
    return m.group(0) if m else ""


def _parse_relevancia(value):
    """Normaliza a relevância para "0.00"–"1.00" (aceita vírgula); "" se inválida."""
    v = (value or "").strip().replace(",", ".")
    try:
        return f"{float(v):.2f}"
    except ValueError:
        return ""


def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config(data):
    try:
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def setup_logging(dest_folder, enabled):
    """(Re)configura o log de diagnóstico desta execução; None se desativado.

    Grava em <dest_folder>/csvtopdf_debug.log. Reseta os handlers a cada
    chamada porque a GUI pode rodar vários downloads na mesma sessão.
    """
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    if not enabled:
        return None
    log_path = Path(dest_folder) / "csvtopdf_debug.log"
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.info("Início da execução | Python %s | %s", platform.python_version(), platform.platform())
    return log_path


def detect_delimiter(file_path):
    with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
        first_line = f.readline()
    return "\t" if first_line.count("\t") > first_line.count(",") else ","


def _match_column(header, synonyms):
    for i, col in enumerate(header):
        if col.strip().lower() in synonyms:
            return i
    return None


def read_file(file_path):
    """Retorna (header, data_rows, delimiter, doi_col, title_col, year_col, rel_col).

    doi_col / title_col ficam None quando não é possível detectar
    automaticamente — a GUI decide o que fazer nesse caso (fallback manual).
    year_col / rel_col são opcionais (None quando não há coluna reconhecível).
    """
    delimiter = detect_delimiter(file_path)
    with open(file_path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        rows = list(csv.reader(f, delimiter=delimiter))
    if not rows:
        raise ValueError("Arquivo vazio")
    header, data_rows = rows[0], rows[1:]
    doi_col = _match_column(header, DOI_SYNONYMS)
    title_col = _match_column(header, TITLE_SYNONYMS)
    year_col = _match_column(header, YEAR_SYNONYMS)
    rel_col = _match_column(header, RELEVANCIA_SYNONYMS)
    return header, data_rows, delimiter, doi_col, title_col, year_col, rel_col


def _clean_doi(doi):
    return DOI_PREFIX_RE.sub("", doi.strip()).strip()


def extract_articles(data_rows, doi_col, title_col, year_col=None, rel_col=None):
    articles = []
    for row in data_rows:
        doi = row[doi_col].strip() if doi_col is not None and doi_col < len(row) else ""
        title = row[title_col].strip() if title_col is not None and title_col < len(row) else ""
        year = _parse_year(row[year_col]) if year_col is not None and year_col < len(row) else ""
        rel = _parse_relevancia(row[rel_col]) if rel_col is not None and rel_col < len(row) else ""
        if not doi and not title:
            continue
        articles.append({"doi": _clean_doi(doi), "title": title, "year": year, "relevancia": rel})
    return articles


def dedupe_articles(articles):
    """Remove duplicatas: mesmo DOI ou, na ausência de DOI, mesmo título.

    Exports do WoS/Scopus saem em blocos (máx. 1000 registros) que podem se
    sobrepor. Retorna (lista_unica, quantidade_removida).
    """
    vistos = set()
    unicos = []
    for a in articles:
        chave = a["doi"].lower() if a["doi"] else "t:" + a["title"].lower()
        if chave in vistos:
            continue
        vistos.add(chave)
        unicos.append(a)
    return unicos, len(articles) - len(unicos)


def sanitize_filename(name, fallback):
    name = (name or "").strip() or fallback
    name = ILLEGAL_CHARS_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:150] or fallback


def _unique_path(path):
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 2
    while True:
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
        i += 1


# ── Fontes de busca de PDF em acesso aberto ──────────────────────────────────
# Cada fonte retorna {"pdf_url": <str|None>, "fonte": <nome>, "year": <str>} quando
# consegue responder, ou None quando nem responde (404 / erro). pdf_url None = a
# fonte respondeu mas não tem PDF aberto (ainda serve para colher o ano).

def buscar_unpaywall(doi, email, session, semantic_key=None):
    url = UNPAYWALL_URL.format(doi=quote(doi, safe=""))
    resp = session.get(url, params={"email": email}, timeout=API_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    best = data.get("best_oa_location") or {}
    return {"pdf_url": best.get("url_for_pdf"), "fonte": "Unpaywall",
            "year": str(data.get("year") or "").strip()}


def buscar_openalex(doi, email, session, semantic_key=None):
    url = OPENALEX_URL.format(doi=quote(doi, safe=""))
    resp = session.get(url, params={"mailto": email} if email else None, timeout=API_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    best = data.get("best_oa_location") or {}
    return {"pdf_url": best.get("pdf_url"), "fonte": "OpenAlex",
            "year": str(data.get("publication_year") or "").strip()}


def buscar_semantic_scholar(doi, email, session, semantic_key=None):
    # Sem chave, a API cai na cota anônima (baixa e compartilhada por IP) e
    # devolve 429 com frequência; uma chave gratuita evita isso na prática.
    url = SEMANTIC_URL.format(doi=quote(doi, safe=""))
    headers = {"x-api-key": semantic_key} if semantic_key else None
    resp = session.get(url, params={"fields": "openAccessPdf,year"}, headers=headers, timeout=API_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    oa = data.get("openAccessPdf") or {}
    return {"pdf_url": oa.get("url"), "fonte": "Semantic Scholar",
            "year": str(data.get("year") or "").strip()}


FONTES = (buscar_unpaywall, buscar_openalex, buscar_semantic_scholar)


def buscar_pdf(doi, email, session, semantic_key=None):
    """Tenta as fontes em ordem e para na primeira que devolver um PDF.

    Retorna (pdf_url, fonte, year, erro): erro is None em caso de sucesso;
    "sem versão OA" se alguma fonte respondeu mas nenhuma tinha PDF;
    "DOI não encontrado" se nenhuma fonte respondeu. Faz REQUEST_DELAY entre
    as fontes do mesmo artigo (a pausa entre artigos fica em process_articles).
    """
    year = ""
    algum_respondeu = False
    for i, fonte in enumerate(FONTES):
        if i > 0:
            time.sleep(REQUEST_DELAY)
        try:
            r = fonte(doi, email, session, semantic_key)
        except requests.exceptions.RequestException as e:
            logger.debug("Fonte %s falhou para doi=%s: %s", fonte.__name__, doi, e)
            r = None
        if r is None:
            continue
        algum_respondeu = True
        if not year and r.get("year"):
            year = r["year"]
        if r.get("pdf_url"):
            return r["pdf_url"], r["fonte"], year, None
    return None, "", year, ("sem versão OA" if algum_respondeu else "DOI não encontrado")


def download_pdf(url, dest_path, session):
    resp = session.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


def process_articles(articles, email, dest_folder, msg_queue, cancel_event,
                     salvar_nao_encontrados=True, log_diagnostico=True, semantic_key=None):
    """Roda em thread separada. Publica mensagens em msg_queue como tuplas:
        ("processing", i, total, title)
        ("result", i, total, result_dict)
        ("done", summary_dict, log_path, csv_path, html_path, was_cancelled, debug_log_path)
    csv_path/html_path são "" quando o usuário optou por não salvar a lista;
    debug_log_path é "" quando log_diagnostico=False.
    """
    dest_folder = Path(dest_folder)
    dest_folder.mkdir(parents=True, exist_ok=True)
    debug_log_path = setup_logging(dest_folder, log_diagnostico)

    results = []
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    total = len(articles)

    for i, article in enumerate(articles, start=1):
        if cancel_event.is_set():
            break

        doi, title = article["doi"], article["title"]
        msg_queue.put(("processing", i, total, title))
        status, detail, filename, year_api, fonte = _process_one(
            doi, title, email, dest_folder, session, semantic_key)

        # Ano: prefere o do arquivo de origem; cai para o que a API devolveu.
        year = article.get("year") or year_api
        result = {"doi": doi, "title": title, "year": year,
                  "relevancia": article.get("relevancia", ""),
                  "status": status, "detail": detail, "filename": filename, "fonte": fonte}
        results.append(result)
        msg_queue.put(("result", i, total, result))

        if cancel_event.is_set():
            break
        time.sleep(REQUEST_DELAY)

    log_path, csv_path, html_path = write_reports(results, dest_folder, salvar_nao_encontrados)
    if debug_log_path:
        logger.info("Fim da execução | %s", _summarize(results))
    msg_queue.put(("done", _summarize(results), str(log_path),
                   str(csv_path) if csv_path else "",
                   str(html_path) if html_path else "", cancel_event.is_set(),
                   str(debug_log_path) if debug_log_path else ""))


def _process_one(doi, title, email, dest_folder, session, semantic_key=None):
    """Retorna (status, detail, filename, year, fonte)."""
    if not doi:
        return "error", "sem DOI", "", "", ""
    try:
        pdf_url, fonte, year, err = buscar_pdf(doi, email, session, semantic_key)
        if err:
            return ("no_oa" if err == "sem versão OA" else "error"), err, "", year, ""

        dest_path = _unique_path(dest_folder / (sanitize_filename(title, doi) + ".pdf"))
        try:
            download_pdf(pdf_url, dest_path, session)
            return "downloaded", "ok", dest_path.name, year, fonte
        except Exception:
            logger.exception("Falha no download do PDF | doi=%s url=%s", doi, pdf_url)
            return "error", "falha no download", "", year, fonte
    except requests.exceptions.Timeout:
        logger.warning("Timeout ao buscar PDF | doi=%s", doi)
        return "error", "timeout", "", "", ""
    except requests.exceptions.RequestException:
        logger.exception("Erro de conexão ao buscar PDF | doi=%s", doi)
        return "error", "erro de conexão", "", "", ""
    except Exception:
        logger.exception("Erro inesperado ao processar artigo | doi=%s title=%s", doi, title)
        return "error", "erro inesperado", "", "", ""


def _summarize(results):
    return {
        "downloaded": sum(1 for r in results if r["status"] == "downloaded"),
        "no_oa": sum(1 for r in results if r["status"] == "no_oa"),
        "error": sum(1 for r in results if r["status"] == "error"),
        "total": len(results),
    }


def write_reports(results, dest_folder, salvar_nao_encontrados=True):
    """Grava o log geral (download_log.csv, sempre) e, se pedido, os relatórios
    dos NÃO baixados (nao_encontrados.csv + .html).

    Retorna (log_path, csv_path, html_path); csv_path/html_path None se desativado.
    A coluna 'relevancia' só aparece quando havia relevância no arquivo de entrada.
    """
    tem_rel = any(r.get("relevancia") for r in results)

    log_path = dest_folder / "download_log.csv"
    with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        cab = ["doi", "title", "year", "status", "fonte"]
        if tem_rel:
            cab.append("relevancia")
        writer.writerow(cab)
        for r in results:
            linha = [r["doi"], r["title"], r.get("year", ""), r["status"], r.get("fonte", "")]
            if tem_rel:
                linha.append(r.get("relevancia", ""))
            writer.writerow(linha)

    if not salvar_nao_encontrados:
        return log_path, None, None

    not_found = [r for r in results if r["status"] != "downloaded"]

    csv_path = dest_folder / "nao_encontrados.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        cab = ["doi", "title", "year"]
        if tem_rel:
            cab.append("relevancia")
        writer.writerow(cab)
        for r in not_found:
            linha = [r["doi"], r["title"], r.get("year", "")]
            if tem_rel:
                linha.append(r.get("relevancia", ""))
            writer.writerow(linha)

    html_path = dest_folder / "nao_encontrados.html"
    html_path.write_text(build_not_found_html(not_found), encoding="utf-8")

    return log_path, csv_path, html_path


def build_not_found_html(rows):
    """Monta um HTML autocontido com tabela dos não baixados e DOIs clicáveis.

    rows: lista de dicts com 'doi', 'title' e (opcional) 'year'/'detail'.
    O DOI vira link para https://doi.org/<doi> — abra este arquivo no navegador
    já logado no acesso institucional e baixe manualmente.
    Clicar num cabeçalho ordena a tabela por aquela coluna (ex.: Ano).
    """
    tem_ano = any(r.get("year") for r in rows)
    tem_rel = any(r.get("relevancia") for r in rows)
    tem_motivo = any(r.get("detail") for r in rows)
    linhas = []
    for i, r in enumerate(rows, start=1):
        doi = (r.get("doi") or "").strip()
        titulo = html.escape(r.get("title") or "")
        ano = html.escape(str(r.get("year") or ""))
        rel = html.escape(str(r.get("relevancia") or ""))
        # chave de persistência: DOI se houver, senão o título
        chave = html.escape(doi or (r.get("title") or ""))
        if doi:
            doi_esc = html.escape(doi)
            doi_cell = (f'<a href="https://doi.org/{doi_esc}" target="_blank" rel="noopener" '
                        f'onclick="marcar(this)">{doi_esc}</a>')
        else:
            doi_cell = '<span class="sem">sem DOI</span>'
        cols = [
            '<td class="chk"><input type="checkbox" onchange="toggle(this)"></td>',
            f'<td class="num">{i}</td>',
            f"<td>{titulo}</td>",
        ]
        if tem_ano:
            cols.append(f'<td class="ano">{ano}</td>')
        if tem_rel:
            cols.append(f'<td class="rel">{rel}</td>')
        cols.append(f'<td class="doi">{doi_cell}</td>')
        if tem_motivo:
            cols.append(f'<td class="motivo">{html.escape(r.get("detail") or "")}</td>')
        linhas.append(f'<tr data-key="{chave}" data-rel="{rel}">' + "".join(cols) + "</tr>")

    # Cabeçalho com índices de coluna corretos (colunas opcionais deslocam as demais).
    ths = ['<th>✓</th>', _th("#", 1, "num"), _th("Título", 2, "text")]
    col = 3
    if tem_ano:
        ths.append(_th("Ano", col, "year")); col += 1
    rel_col = -1
    if tem_rel:
        rel_col = col
        ths.append(_th("Relevância", col, "rel")); col += 1
    ths.append(_th("DOI", col, "text")); col += 1
    if tem_motivo:
        ths.append(_th("Motivo", col, "text"))
    cabecalho = "<tr>" + "".join(ths) + "</tr>"

    if tem_rel:
        filtro_rel = ('<div id="filtro-rel">Mostrar apenas relevância ≥ '
                      '<input type="range" min="0" max="1" step="0.05" value="0" '
                      'oninput="filtrarRel(this.value)"> <b id="relval">0.00</b></div>')
    else:
        filtro_rel = ""

    return _HTML_TEMPLATE.format(
        total=len(rows),
        cabecalho=cabecalho,
        linhas="\n".join(linhas),
        filtro_rel=filtro_rel,
        rel_col=rel_col,
    )


def _th(rotulo, col, tipo):
    return (f'<th class="sortable" onclick="sortBy({col},\'{tipo}\')">'
            f'{html.escape(rotulo)}<span class="arrow"></span></th>')


_HTML_TEMPLATE = """<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Artigos não baixados</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 24px; background: #f4f4f5; color: #18181b; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  p.sub {{ margin: 0 0 16px; color: #52525b; font-size: 14px; }}
  #busca {{ width: 100%; box-sizing: border-box; padding: 8px 10px; font-size: 14px;
           margin-bottom: 12px; border: 1px solid #d4d4d8; border-radius: 6px; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); border-radius: 6px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #eee; vertical-align: top; }}
  th {{ background: #fafafa; font-size: 13px; color: #3f3f46; position: sticky; top: 0; }}
  td.num {{ color: #a1a1aa; width: 44px; text-align: right; }}
  td.chk {{ width: 34px; text-align: center; }}
  td.chk input {{ width: 16px; height: 16px; cursor: pointer; }}
  td.ano {{ width: 56px; color: #52525b; font-variant-numeric: tabular-nums; }}
  td.rel {{ width: 72px; text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }}
  #filtro-rel {{ margin-bottom: 12px; font-size: 14px; color: #3f3f46; }}
  #filtro-rel input {{ vertical-align: middle; }}
  #filtro-rel b {{ font-variant-numeric: tabular-nums; }}
  td.doi {{ font-family: ui-monospace, "Consolas", "DejaVu Sans Mono", monospace; font-size: 13px; white-space: nowrap; }}
  td.doi a {{ color: #2563eb; text-decoration: none; }}
  td.doi a:hover {{ text-decoration: underline; }}
  td.motivo {{ color: #71717a; font-size: 13px; white-space: nowrap; }}
  .sem {{ color: #a1a1aa; font-style: italic; }}
  th.sortable {{ cursor: pointer; user-select: none; white-space: nowrap; }}
  th.sortable:hover {{ color: #2563eb; }}
  th .arrow {{ margin-left: 4px; color: #2563eb; font-size: 11px; }}
  tr:hover td {{ background: #f9fafb; }}
  /* Linha já aberta: escurece e risca o título, DOI fica esmaecido */
  tr.aberto td {{ background: #f0fdf4; color: #a1a1aa; }}
  tr.aberto td:nth-child(3) {{ text-decoration: line-through; }}
  tr.aberto td.doi a {{ color: #86efac; }}
  #contador {{ font-weight: 600; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #18181b; color: #e4e4e7; }}
    p.sub {{ color: #a1a1aa; }}
    table {{ background: #27272a; box-shadow: none; }}
    th {{ background: #1f1f23; color: #d4d4d8; }}
    th.sortable:hover {{ color: #60a5fa; }}
    th .arrow {{ color: #60a5fa; }}
    th, td {{ border-bottom-color: #3f3f46; }}
    #busca {{ background: #27272a; color: #e4e4e7; border-color: #3f3f46; }}
    td.doi a {{ color: #60a5fa; }}
    tr:hover td {{ background: #2e2e33; }}
    tr.aberto td {{ background: #14261b; color: #6b7280; }}
    tr.aberto td.doi a {{ color: #3f6b4d; }}
    #filtro-rel {{ color: #d4d4d8; }}
  }}
</style>
</head>
<body>
<h1>Artigos não baixados</h1>
<p class="sub">{total} artigo(s) sem versão em acesso aberto. Clique no DOI para abrir
no navegador — se estiver logado no acesso institucional, o PDF abre direto.<br>
Ao clicar num DOI, a linha é marcada como aberta (✓) e o progresso fica salvo neste
navegador. Você também pode marcar/desmarcar manualmente.
Clique num cabeçalho (ex.: <b>Ano</b>) para ordenar. <span id="contador"></span></p>
<input id="busca" type="search" placeholder="Filtrar por título ou DOI…"
       oninput="filtrar(this.value)" autofocus>
{filtro_rel}
<table>
<thead>{cabecalho}</thead>
<tbody id="corpo">
{linhas}
</tbody>
</table>
<script>
// Persistência local por chave (DOI/título). Uma chave por artigo; o progresso
// sobrevive a fechar/reabrir o arquivo neste mesmo navegador.
const STORE = 'csvtopdf_abertos';
function carregar() {{ try {{ return JSON.parse(localStorage.getItem(STORE)) || {{}}; }} catch (e) {{ return {{}}; }} }}
function salvar(estado) {{ try {{ localStorage.setItem(STORE, JSON.stringify(estado)); }} catch (e) {{}} }}

function aplicar(tr, aberto, estado) {{
  tr.classList.toggle('aberto', aberto);
  tr.querySelector('.chk input').checked = aberto;
  if (aberto) estado[tr.dataset.key] = 1; else delete estado[tr.dataset.key];
}}
function contar() {{
  const total = document.querySelectorAll('#corpo tr').length;
  const abertos = document.querySelectorAll('#corpo tr.aberto').length;
  document.getElementById('contador').textContent = abertos + ' de ' + total + ' já abertos.';
}}
function toggle(chk) {{
  const estado = carregar();
  aplicar(chk.closest('tr'), chk.checked, estado);
  salvar(estado); contar();
}}
function marcar(a) {{   // chamado ao clicar no link do DOI (o link abre normalmente)
  const estado = carregar();
  aplicar(a.closest('tr'), true, estado);
  salvar(estado); contar();
}}
// Filtros combinados: texto (título/DOI) E relevância mínima. Uma linha só
// aparece se passar nos dois.
const REL_COL = {rel_col};
let filtroTexto = '', filtroRel = 0;
function aplicarFiltros() {{
  for (const tr of document.querySelectorAll('#corpo tr')) {{
    const okTexto = tr.textContent.toLowerCase().includes(filtroTexto);
    const rel = parseFloat(tr.dataset.rel);
    const okRel = isNaN(rel) ? (filtroRel <= 0) : (rel >= filtroRel);
    tr.style.display = (okTexto && okRel) ? '' : 'none';
  }}
}}
function filtrar(q) {{ filtroTexto = q.toLowerCase(); aplicarFiltros(); }}
function filtrarRel(v) {{
  filtroRel = parseFloat(v) || 0;
  document.getElementById('relval').textContent = filtroRel.toFixed(2);
  aplicarFiltros();
}}

// Ordenação por clique no cabeçalho. Guardamos coluna e direção atuais; clicar
// de novo na mesma coluna inverte. Ano e Relevância começam do maior (desc).
let ordem = {{col: null, dir: 1}};
function sortBy(col, tipo) {{
  const descPrimeiro = (tipo === 'year' || tipo === 'rel');
  ordem.dir = (ordem.col === col) ? -ordem.dir : (descPrimeiro ? -1 : 1);
  ordem.col = col;
  const corpo = document.getElementById('corpo');
  const linhas = [...corpo.querySelectorAll('tr')];
  const numerico = (tipo === 'num' || tipo === 'year' || tipo === 'rel');
  linhas.sort((a, b) => {{
    let va = a.children[col].textContent.trim();
    let vb = b.children[col].textContent.trim();
    if (numerico) {{
      const fa = parseFloat(va), fb = parseFloat(vb);
      const na = isNaN(fa), nb = isNaN(fb);
      if (na && nb) return 0;
      if (na) return 1;             // vazios sempre por último
      if (nb) return -1;
      return (fa - fb) * ordem.dir;
    }}
    return va.localeCompare(vb, 'pt', {{sensitivity: 'base'}}) * ordem.dir;
  }});
  for (const tr of linhas) corpo.appendChild(tr);
  // seta indicadora no cabeçalho
  document.querySelectorAll('th .arrow').forEach(s => s.textContent = '');
  const th = document.querySelectorAll('thead th')[col];
  if (th) th.querySelector('.arrow').textContent = ordem.dir === 1 ? '▲' : '▼';
}}

// Restaura o estado salvo ao abrir; ordena por relevância (desc) se houver.
(function () {{
  const estado = carregar();
  for (const tr of document.querySelectorAll('#corpo tr')) {{
    if (estado[tr.dataset.key]) aplicar(tr, true, estado);
  }}
  contar();
  if (REL_COL >= 0) sortBy(REL_COL, 'rel');
}})();
</script>
</body>
</html>
"""


def demo():
    """Self-check leve: sem framework de teste, só asserts (ver skill ponytail)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # Log de diagnóstico: liga, escreve, desliga sem deixar handler pendurado.
        debug_path = setup_logging(tmp, True)
        assert debug_path == tmp / "csvtopdf_debug.log"
        logger.warning("teste")
        assert "teste" in debug_path.read_text(encoding="utf-8")
        assert setup_logging(tmp, False) is None
        assert logger.handlers == []

        csv_path = tmp / "clean.csv"
        csv_path.write_text("DOI,Title\n10.1/abc,Um Titulo\n,Sem doi\n", encoding="utf-8")
        header, rows, delim, doi_col, title_col, year_col, rel_col = read_file(csv_path)
        assert delim == ","
        assert doi_col == 0 and title_col == 1 and year_col is None and rel_col is None
        articles = extract_articles(rows, doi_col, title_col, year_col, rel_col)
        assert articles == [
            {"doi": "10.1/abc", "title": "Um Titulo", "year": "", "relevancia": ""},
            {"doi": "", "title": "Sem doi", "year": "", "relevancia": ""},
        ]

        wos_path = tmp / "wos_raw.txt"
        wos_path.write_text("PY\tDI\tTI\n2020\thttps://doi.org/10.2/xyz\tOutro Titulo\n", encoding="utf-8")
        header, rows, delim, doi_col, title_col, year_col, rel_col = read_file(wos_path)
        assert delim == "\t"
        assert doi_col == 1 and title_col == 2 and year_col == 0  # PY detectado
        articles = extract_articles(rows, doi_col, title_col, year_col, rel_col)
        assert articles == [{"doi": "10.2/xyz", "title": "Outro Titulo", "year": "2020", "relevancia": ""}]
        assert _parse_year("May 2019") == "2019" and _parse_year("s/d") == ""

        # Modelo de entrada com relevância (aceita vírgula decimal e sinônimo "score")
        rel_path = tmp / "com_rel.csv"
        rel_path.write_text("DOI,Titulo,Ano,score\n10.5/x,Artigo,2019,\"0,90\"\n", encoding="utf-8")
        header, rows, delim, dc, tc, yc, rc = read_file(rel_path)
        assert rc == 3  # coluna "score" reconhecida como relevância
        arts = extract_articles(rows, dc, tc, yc, rc)
        assert arts[0]["relevancia"] == "0.90"
        assert _parse_relevancia("0,85") == "0.85" and _parse_relevancia("x") == ""

        unicos, removidos = dedupe_articles([
            {"doi": "10.1/AAA", "title": "X"},
            {"doi": "10.1/aaa", "title": "X copia"},
            {"doi": "", "title": "Sem DOI"},
            {"doi": "", "title": "sem doi"},
            {"doi": "10.2/bbb", "title": "Y"},
        ])
        assert removidos == 2 and len(unicos) == 3
        assert unicos[0]["doi"] == "10.1/AAA"

        assert sanitize_filename("Um Título Normal", "fallback") == "Um Título Normal"
        assert "/" not in sanitize_filename("A/B*C", "fallback")
        assert sanitize_filename("", "10.1/abc") == "10.1_abc"
        assert _clean_doi("https://doi.org/10.3/qwe") == "10.3/qwe"

        resultados = [
            {"doi": "10.1/a", "title": "Baixado", "year": "2021", "relevancia": "0.90",
             "status": "downloaded", "detail": "ok", "filename": "x.pdf", "fonte": "OpenAlex"},
            {"doi": "10.2/b", "title": "Sem OA", "year": "2010", "relevancia": "0.30",
             "status": "no_oa", "detail": "sem versão OA", "filename": "", "fonte": ""},
            {"doi": "", "title": "Sem DOI <script>", "year": "", "relevancia": "",
             "status": "error", "detail": "sem DOI", "filename": "", "fonte": ""},
        ]
        # download_log.csv é sempre gravado (mesmo sem os relatórios de não baixados)
        log, c, h = write_reports(resultados, tmp, salvar_nao_encontrados=False)
        assert log.exists() and c is None and h is None
        assert not (tmp / "nao_encontrados.csv").exists()
        log_txt = log.read_text(encoding="utf-8-sig").splitlines()
        assert log_txt[0] == "doi,title,year,status,fonte,relevancia"  # fonte + relevancia
        assert "OpenAlex" in log_txt[1] and log_txt[1].endswith("0.90")

        log, c, h = write_reports(resultados, tmp, salvar_nao_encontrados=True)
        assert log.exists() and c.exists() and h.exists()
        assert c.read_text(encoding="utf-8-sig").splitlines()[0] == "doi,title,year,relevancia"
        conteudo = h.read_text(encoding="utf-8")
        assert conteudo.count('<tr data-key=') == 2  # só os 2 não baixados
        assert 'href="https://doi.org/10.2/b"' in conteudo
        assert 'onclick="marcar(this)"' in conteudo   # DOI marca a linha ao abrir
        assert "localStorage" in conteudo              # progresso persistido
        assert ",'year')" in conteudo                  # cabeçalho Ano ordenável
        assert '<td class="ano">2010</td>' in conteudo
        assert ",'rel')" in conteudo and 'id="filtro-rel"' in conteudo  # coluna + slider de relevância
        assert 'data-rel="0.30"' in conteudo
        assert "&lt;script&gt;" in conteudo  # título escapado (sem XSS)

        # Sem ano nem relevância: essas colunas/filtros não aparecem
        simples = build_not_found_html([{"doi": "10.9/z", "title": "T", "status": "no_oa"}])
        assert ",'year')" not in simples and 'class="ano"' not in simples
        assert ",'rel')" not in simples and 'id="filtro-rel"' not in simples

        # Orquestrador buscar_pdf: para na 1ª fonte com PDF; colhe ano das anteriores.
        globals()["REQUEST_DELAY"] = 0  # sem pausa real no self-check
        class _Resp:
            def __init__(self, js, code=200): self._js, self.status_code = js, code
            def raise_for_status(self):
                if self.status_code >= 400: raise requests.exceptions.HTTPError()
            def json(self): return self._js
        class _Sess:
            def get(self, url, **kw):
                if "unpaywall" in url:  # responde, ano, mas sem PDF
                    return _Resp({"year": 2018, "best_oa_location": None})
                if "openalex" in url:   # acha o PDF
                    return _Resp({"publication_year": 2018,
                                  "best_oa_location": {"pdf_url": "http://x/a.pdf"}})
                return _Resp({}, 404)
        pdf, fonte, year, err = buscar_pdf("10.1/a", "e@x.com", _Sess())
        assert pdf == "http://x/a.pdf" and fonte == "OpenAlex" and year == "2018" and err is None

        # Chave do Semantic Scholar vai no header x-api-key só quando informada
        # (evita o 429 da cota anônima observado em execuções reais).
        chamadas = []
        class _SessCaptura:
            def get(self, url, headers=None, **kw):
                chamadas.append(headers)
                return _Resp({}, 404)
        buscar_semantic_scholar("10.1/a", "e@x.com", _SessCaptura())
        assert chamadas[-1] is None
        buscar_semantic_scholar("10.1/a", "e@x.com", _SessCaptura(), semantic_key="minha-chave")
        assert chamadas[-1] == {"x-api-key": "minha-chave"}

    print("downloader.py: todos os self-checks passaram.")


if __name__ == "__main__":
    demo()
