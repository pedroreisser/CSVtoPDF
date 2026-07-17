"""Lógica pura de leitura de listas de artigos e download via Unpaywall.

Sem dependência de interface — testável isoladamente (ver demo() no final).
"""
import csv
import html
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

import requests

UNPAYWALL_URL = "https://api.unpaywall.org/v2/{doi}"
REQUEST_DELAY = 1.0
API_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 30

DOI_SYNONYMS = {"doi", "di"}
TITLE_SYNONYMS = {"title", "titulo", "título", "ti"}
YEAR_SYNONYMS = {"year", "ano", "py", "publication year", "publicationyear", "pubyear"}

CONFIG_PATH = Path.home() / ".csvtopdf_config.json"

DOI_PREFIX_RE = re.compile(r"^(https?://)?(dx\.)?doi\.org/", re.IGNORECASE)
ILLEGAL_CHARS_RE = re.compile(r'[\\/*?:"<>|]')
YEAR_RE = re.compile(r"(19|20)\d\d")


def _parse_year(value):
    """Extrai um ano de 4 dígitos (1900–2099) de um texto; "" se não achar."""
    m = YEAR_RE.search(value or "")
    return m.group(0) if m else ""


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
    """Retorna (header, data_rows, delimiter, doi_col, title_col, year_col).

    doi_col / title_col ficam None quando não é possível detectar
    automaticamente — a GUI decide o que fazer nesse caso (fallback manual).
    year_col é opcional (None quando não há coluna de ano reconhecível).
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
    return header, data_rows, delimiter, doi_col, title_col, year_col


def _clean_doi(doi):
    return DOI_PREFIX_RE.sub("", doi.strip()).strip()


def extract_articles(data_rows, doi_col, title_col, year_col=None):
    articles = []
    for row in data_rows:
        doi = row[doi_col].strip() if doi_col is not None and doi_col < len(row) else ""
        title = row[title_col].strip() if title_col is not None and title_col < len(row) else ""
        year = _parse_year(row[year_col]) if year_col is not None and year_col < len(row) else ""
        if not doi and not title:
            continue
        articles.append({"doi": _clean_doi(doi), "title": title, "year": year})
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


def query_unpaywall(doi, email, session):
    """Retorna (pdf_url, erro, year). year vem da própria resposta da Unpaywall
    (serve de fallback quando o arquivo de origem não tem coluna de ano)."""
    url = UNPAYWALL_URL.format(doi=quote(doi, safe=""))
    resp = session.get(url, params={"email": email}, timeout=API_TIMEOUT)
    if resp.status_code == 404:
        return None, "DOI não encontrado", ""
    resp.raise_for_status()
    data = resp.json()
    year = str(data.get("year") or "").strip()
    best = data.get("best_oa_location")
    if not best or not best.get("url_for_pdf"):
        return None, "sem versão OA", year
    return best["url_for_pdf"], None, year


def download_pdf(url, dest_path, session):
    resp = session.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


def process_articles(articles, email, dest_folder, msg_queue, cancel_event,
                     salvar_nao_encontrados=True):
    """Roda em thread separada. Publica mensagens em msg_queue como tuplas:
        ("processing", i, total, title)
        ("result", i, total, result_dict)
        ("done", summary_dict, csv_path, html_path, was_cancelled)
    csv_path/html_path são "" quando o usuário optou por não salvar a lista.
    """
    dest_folder = Path(dest_folder)
    dest_folder.mkdir(parents=True, exist_ok=True)

    results = []
    session = requests.Session()
    total = len(articles)

    for i, article in enumerate(articles, start=1):
        if cancel_event.is_set():
            break

        doi, title = article["doi"], article["title"]
        msg_queue.put(("processing", i, total, title))
        status, detail, filename, year_api = _process_one(doi, title, email, dest_folder, session)

        # Ano: prefere o do arquivo de origem; cai para o que a Unpaywall devolveu.
        year = article.get("year") or year_api
        result = {"doi": doi, "title": title, "year": year,
                  "status": status, "detail": detail, "filename": filename}
        results.append(result)
        msg_queue.put(("result", i, total, result))

        if cancel_event.is_set():
            break
        time.sleep(REQUEST_DELAY)

    csv_path, html_path = write_reports(results, dest_folder, salvar_nao_encontrados)
    msg_queue.put(("done", _summarize(results),
                   str(csv_path) if csv_path else "",
                   str(html_path) if html_path else "", cancel_event.is_set()))


def _process_one(doi, title, email, dest_folder, session):
    """Retorna (status, detail, filename, year)."""
    if not doi:
        return "error", "sem DOI", "", ""
    try:
        pdf_url, err, year = query_unpaywall(doi, email, session)
        if err:
            return ("no_oa" if err == "sem versão OA" else "error"), err, "", year

        dest_path = _unique_path(dest_folder / (sanitize_filename(title, doi) + ".pdf"))
        try:
            download_pdf(pdf_url, dest_path, session)
            return "downloaded", "ok", dest_path.name, year
        except Exception:
            return "error", "falha no download", "", year
    except requests.exceptions.Timeout:
        return "error", "timeout", "", ""
    except requests.exceptions.RequestException:
        return "error", "erro de conexão", "", ""
    except Exception:
        return "error", "erro inesperado", "", ""


def _summarize(results):
    return {
        "downloaded": sum(1 for r in results if r["status"] == "downloaded"),
        "no_oa": sum(1 for r in results if r["status"] == "no_oa"),
        "error": sum(1 for r in results if r["status"] == "error"),
        "total": len(results),
    }


def write_reports(results, dest_folder, salvar_nao_encontrados=True):
    """Gera os relatórios dos artigos NÃO baixados: CSV + HTML com DOIs clicáveis.

    Retorna (csv_path, html_path); ambos None se o usuário desativou a opção.
    """
    if not salvar_nao_encontrados:
        return None, None

    not_found = [r for r in results if r["status"] != "downloaded"]

    csv_path = dest_folder / "nao_encontrados.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["doi", "title", "year"])
        for r in not_found:
            writer.writerow([r["doi"], r["title"], r.get("year", "")])

    html_path = dest_folder / "nao_encontrados.html"
    html_path.write_text(build_not_found_html(not_found), encoding="utf-8")

    return csv_path, html_path


def build_not_found_html(rows):
    """Monta um HTML autocontido com tabela dos não baixados e DOIs clicáveis.

    rows: lista de dicts com 'doi', 'title' e (opcional) 'year'/'detail'.
    O DOI vira link para https://doi.org/<doi> — abra este arquivo no navegador
    já logado no acesso institucional (CAFe/UFPel) e baixe manualmente.
    Clicar num cabeçalho ordena a tabela por aquela coluna (ex.: Ano).
    """
    tem_ano = any(r.get("year") for r in rows)
    tem_motivo = any(r.get("detail") for r in rows)
    linhas = []
    for i, r in enumerate(rows, start=1):
        doi = (r.get("doi") or "").strip()
        titulo = html.escape(r.get("title") or "")
        ano = html.escape(str(r.get("year") or ""))
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
        cols.append(f'<td class="doi">{doi_cell}</td>')
        if tem_motivo:
            cols.append(f'<td class="motivo">{html.escape(r.get("detail") or "")}</td>')
        linhas.append(f'<tr data-key="{chave}">' + "".join(cols) + "</tr>")

    # Cabeçalho com índices de coluna corretos (a coluna Ano desloca DOI/Motivo).
    ths = ['<th>✓</th>', _th("#", 1, "num"), _th("Título", 2, "text")]
    col = 3
    if tem_ano:
        ths.append(_th("Ano", col, "year")); col += 1
    ths.append(_th("DOI", col, "text")); col += 1
    if tem_motivo:
        ths.append(_th("Motivo", col, "text"))
    cabecalho = "<tr>" + "".join(ths) + "</tr>"

    return _HTML_TEMPLATE.format(
        total=len(rows),
        cabecalho=cabecalho,
        linhas="\n".join(linhas),
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
  }}
</style>
</head>
<body>
<h1>Artigos não baixados</h1>
<p class="sub">{total} artigo(s) sem versão em acesso aberto. Clique no DOI para abrir
no navegador — se estiver logado no acesso institucional (CAFe/UFPel), o PDF abre direto.<br>
Ao clicar num DOI, a linha é marcada como aberta (✓) e o progresso fica salvo neste
navegador. Você também pode marcar/desmarcar manualmente.
Clique num cabeçalho (ex.: <b>Ano</b>) para ordenar. <span id="contador"></span></p>
<input id="busca" type="search" placeholder="Filtrar por título ou DOI…"
       oninput="filtrar(this.value)" autofocus>
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
function filtrar(q) {{
  q = q.toLowerCase();
  for (const tr of document.querySelectorAll('#corpo tr')) {{
    tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none';
  }}
}}

// Ordenação por clique no cabeçalho. Guardamos coluna e direção atuais; clicar
// de novo na mesma coluna inverte. Colunas de ano começam do mais novo (desc).
let ordem = {{col: null, dir: 1}};
function sortBy(col, tipo) {{
  ordem.dir = (ordem.col === col) ? -ordem.dir : (tipo === 'year' ? -1 : 1);
  ordem.col = col;
  const corpo = document.getElementById('corpo');
  const linhas = [...corpo.querySelectorAll('tr')];
  const numerico = (tipo === 'num' || tipo === 'year');
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

// Restaura o estado salvo ao abrir
(function () {{
  const estado = carregar();
  for (const tr of document.querySelectorAll('#corpo tr')) {{
    if (estado[tr.dataset.key]) aplicar(tr, true, estado);
  }}
  contar();
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

        csv_path = tmp / "clean.csv"
        csv_path.write_text("DOI,Title\n10.1/abc,Um Titulo\n,Sem doi\n", encoding="utf-8")
        header, rows, delim, doi_col, title_col, year_col = read_file(csv_path)
        assert delim == ","
        assert doi_col == 0 and title_col == 1 and year_col is None
        articles = extract_articles(rows, doi_col, title_col, year_col)
        assert articles == [
            {"doi": "10.1/abc", "title": "Um Titulo", "year": ""},
            {"doi": "", "title": "Sem doi", "year": ""},
        ]

        wos_path = tmp / "wos_raw.txt"
        wos_path.write_text("PY\tDI\tTI\n2020\thttps://doi.org/10.2/xyz\tOutro Titulo\n", encoding="utf-8")
        header, rows, delim, doi_col, title_col, year_col = read_file(wos_path)
        assert delim == "\t"
        assert doi_col == 1 and title_col == 2 and year_col == 0  # PY detectado
        articles = extract_articles(rows, doi_col, title_col, year_col)
        assert articles == [{"doi": "10.2/xyz", "title": "Outro Titulo", "year": "2020"}]
        assert _parse_year("May 2019") == "2019" and _parse_year("s/d") == ""

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
            {"doi": "10.1/a", "title": "Baixado", "year": "2021", "status": "downloaded", "detail": "ok", "filename": "x.pdf"},
            {"doi": "10.2/b", "title": "Sem OA", "year": "2010", "status": "no_oa", "detail": "sem versão OA", "filename": ""},
            {"doi": "", "title": "Sem DOI <script>", "year": "", "status": "error", "detail": "sem DOI", "filename": ""},
        ]
        c, h = write_reports(resultados, tmp, salvar_nao_encontrados=False)
        assert c is None and h is None
        assert not (tmp / "nao_encontrados.csv").exists()
        assert not (tmp / "download_log.csv").exists()  # não deve mais existir

        c, h = write_reports(resultados, tmp, salvar_nao_encontrados=True)
        assert c.exists() and h.exists()
        assert c.read_text(encoding="utf-8-sig").splitlines()[0] == "doi,title,year"  # ano no CSV
        conteudo = h.read_text(encoding="utf-8")
        assert conteudo.count('<tr data-key=') == 2  # só os 2 não baixados
        assert 'href="https://doi.org/10.2/b"' in conteudo
        assert 'onclick="marcar(this)"' in conteudo   # DOI marca a linha ao abrir
        assert 'onchange="toggle(this)"' in conteudo   # checkbox manual
        assert "localStorage" in conteudo              # progresso persistido
        assert 'onclick="sortBy(' in conteudo          # cabeçalhos ordenáveis
        assert ",'year')" in conteudo                  # cabeçalho Ano ordenável
        assert '<td class="ano">2010</td>' in conteudo
        assert "&lt;script&gt;" in conteudo  # título escapado (sem XSS)
        assert "<script>Sem DOI" not in conteudo

        # HTML sem nenhum ano: coluna Ano não aparece (nem th nem td)
        sem_ano = build_not_found_html([{"doi": "10.9/z", "title": "T", "status": "no_oa"}])
        assert ",'year')" not in sem_ano and 'class="ano"' not in sem_ano

    print("downloader.py: todos os self-checks passaram.")


if __name__ == "__main__":
    demo()
