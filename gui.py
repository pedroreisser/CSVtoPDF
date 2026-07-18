"""Interface tkinter. Toda a lógica pesada roda em downloader.py, numa thread
separada; esta camada só desenha, dispara a thread e consome a fila (queue.Queue)."""
import os
import platform
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import downloader as dl

COLORS = {
    "bg": "#f4f4f5",
    "step_active": "#2563eb",
    "step_done": "#16a34a",
    "step_pending": "#9ca3af",
    "downloaded": "#166534",
    "downloaded_bg": "#e8f5e9",
    "no_oa": "#52525b",
    "no_oa_bg": "#f0f0f1",
    "error": "#b91c1c",
    "error_bg": "#fdecea",
    "current_bg": "#fef3c7",
}

MONO_FONT = {"Windows": "Consolas", "Darwin": "Menlo"}.get(platform.system(), "DejaVu Sans Mono")


def _pasta_area_trabalho():
    """Área de trabalho do usuário; cai para a pasta home se não existir."""
    desktop = Path.home() / "Desktop"
    if platform.system() == "Linux":
        # O nome varia com o idioma ("Área de trabalho", "Desktop", …)
        try:
            saida = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                   capture_output=True, text=True).stdout.strip()
            if saida:
                desktop = Path(saida)
        except OSError:
            pass
    return desktop if desktop.is_dir() else Path.home()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CSVtoPDF")
        self.minsize(760, 620)
        self.geometry("820x840")
        self.configure(bg=COLORS["bg"])

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        # ponytail: ttk.Treeview só aceita uma fonte para a tabela inteira (não por
        # coluna), então usamos monoespaçada em toda a linha para manter os DOIs
        # alinhados; se precisar de fonte normal por célula, trocar por um grid de
        # Labels rolável.
        style.configure("Treeview", rowheight=26, font=(MONO_FONT, 10))
        style.configure("Green.Horizontal.TProgressbar", background=COLORS["step_active"])

        self.articles = []
        self.header = []
        self.doi_col = None
        self.title_col = None
        self._pending_manual = []
        self._last_dir = str(_pasta_area_trabalho())
        self.dest_folder = None
        self.msg_queue = queue.Queue()
        self.cancel_event = None
        self.worker = None
        self.row_ids = {}
        self.start_time = None
        self.finished = False

        self._build_layout()
        self._load_saved_config()
        self._update_steps()
        # Salva o e-mail também ao fechar: sem isso, quem digita o e-mail e sai
        # sem iniciar um download perderia a informação na próxima sessão.
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        if "@" in self.email_var.get():
            self._save_config()
        self.destroy()

    # ---------- layout ----------

    def _build_layout(self):
        self.steps_frame = tk.Frame(self, bg=COLORS["bg"])
        self.steps_frame.pack(fill="x", padx=16, pady=(12, 4))
        self.step_labels = []
        for i, text in enumerate(["1. Arquivo", "2. Configuração", "3. Download", "4. Resultado"]):
            lbl = tk.Label(self.steps_frame, text=text, bg=COLORS["bg"], fg=COLORS["step_pending"],
                            font=("TkDefaultFont", 10, "bold"))
            lbl.pack(side="left", padx=(0 if i == 0 else 16, 0))
            self.step_labels.append(lbl)

        container = tk.Frame(self, bg=COLORS["bg"])
        container.pack(fill="both", expand=True, padx=16, pady=8)

        self._build_file_section(container)
        self._build_config_section(container)
        self._build_download_section(container)
        self._build_result_section(container)

    def _section(self, parent, title):
        frame = tk.LabelFrame(parent, text=title, bg=COLORS["bg"], padx=10, pady=10, font=("TkDefaultFont", 10, "bold"))
        frame.pack(fill="x", pady=6)
        return frame

    def _build_file_section(self, parent):
        f = self._section(parent, "1. Arquivo")
        row = tk.Frame(f, bg=COLORS["bg"])
        row.pack(fill="x")
        self.btn_select_file = tk.Button(row, text="\U0001F4C1 Selecionar arquivo", command=self._on_select_file)
        self.btn_select_file.pack(side="left")
        self.lbl_file = tk.Label(row, text="Nenhum arquivo selecionado", bg=COLORS["bg"], anchor="w")
        self.lbl_file.pack(side="left", padx=10)

        self.lbl_file_info = tk.Label(f, text="", bg=COLORS["bg"], justify="left", anchor="w")
        self.lbl_file_info.pack(fill="x", pady=(6, 0))

        self.manual_cols_frame = tk.Frame(f, bg=COLORS["bg"])
        tk.Label(self.manual_cols_frame, text="Coluna DOI:", bg=COLORS["bg"]).grid(row=0, column=0, padx=(0, 4))
        self.doi_col_var = tk.StringVar()
        self.doi_col_combo = ttk.Combobox(self.manual_cols_frame, textvariable=self.doi_col_var, state="readonly", width=20)
        self.doi_col_combo.grid(row=0, column=1, padx=(0, 12))
        tk.Label(self.manual_cols_frame, text="Coluna Título:", bg=COLORS["bg"]).grid(row=0, column=2, padx=(0, 4))
        self.title_col_var = tk.StringVar()
        self.title_col_combo = ttk.Combobox(self.manual_cols_frame, textvariable=self.title_col_var, state="readonly", width=20)
        self.title_col_combo.grid(row=0, column=3, padx=(0, 12))
        tk.Button(self.manual_cols_frame, text="Confirmar colunas", command=self._on_confirm_manual_cols).grid(row=0, column=4)

    def _build_config_section(self, parent):
        f = self._section(parent, "2. Configuração")
        row1 = tk.Frame(f, bg=COLORS["bg"])
        row1.pack(fill="x", pady=2)
        tk.Label(row1, text="✉ E-mail (exigido pela Unpaywall API):", bg=COLORS["bg"], width=32, anchor="w").pack(side="left")
        self.email_var = tk.StringVar()
        self.email_var.trace_add("write", lambda *_: self._update_start_button())
        tk.Entry(row1, textvariable=self.email_var, width=35).pack(side="left", fill="x", expand=True)

        row2 = tk.Frame(f, bg=COLORS["bg"])
        row2.pack(fill="x", pady=2)
        tk.Label(row2, text="\U0001F4C2 Pasta de destino:", bg=COLORS["bg"], width=32, anchor="w").pack(side="left")
        self.dest_var = tk.StringVar()
        tk.Entry(row2, textvariable=self.dest_var, width=35).pack(side="left", fill="x", expand=True)
        tk.Button(row2, text="Escolher...", command=self._on_choose_dest).pack(side="left", padx=(6, 0))

        self.nao_encontrados_var = tk.BooleanVar(value=True)
        tk.Checkbutton(f, text="Salvar lista dos não baixados",
                       variable=self.nao_encontrados_var, bg=COLORS["bg"],
                       anchor="w").pack(fill="x", pady=(4, 0))

    def _build_download_section(self, parent):
        f = self._section(parent, "3. Download")
        row = tk.Frame(f, bg=COLORS["bg"])
        row.pack(fill="x")
        self.btn_start = tk.Button(row, text="⬇ Iniciar download", command=self._on_start, state="disabled")
        self.btn_start.pack(side="left")
        self.btn_cancel = tk.Button(row, text="✖ Cancelar", command=self._on_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        self.lbl_counters = tk.Label(row, text="", bg=COLORS["bg"], font=("TkDefaultFont", 10, "bold"))
        self.lbl_counters.pack(side="left", padx=16)

        self.progress = ttk.Progressbar(f, style="Green.Horizontal.TProgressbar", mode="determinate")
        self.progress.pack(fill="x", pady=(8, 2))
        self.lbl_progress = tk.Label(f, text="", bg=COLORS["bg"], anchor="w")
        self.lbl_progress.pack(fill="x")

        # O frame da tabela precisa existir antes da Treeview e ser o pai dela:
        # pack(in_=frame) num irmão criado depois esconde a árvore atrás do frame.
        table = tk.Frame(f, bg=COLORS["bg"])
        table.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("title", "doi", "status")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", height=12)
        self.tree.heading("title", text="Título")
        self.tree.heading("doi", text="DOI")
        self.tree.heading("status", text="Status")
        self.tree.column("title", width=340)
        self.tree.column("doi", width=180)
        self.tree.column("status", width=140)
        self.tree.tag_configure("downloaded", background=COLORS["downloaded_bg"], foreground=COLORS["downloaded"])
        self.tree.tag_configure("no_oa", background=COLORS["no_oa_bg"], foreground=COLORS["no_oa"])
        self.tree.tag_configure("error", background=COLORS["error_bg"], foreground=COLORS["error"])
        self.tree.tag_configure("current", background=COLORS["current_bg"])
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

    def _build_result_section(self, parent):
        self.result_frame = self._section(parent, "4. Resultado")
        self.result_frame.pack_forget()

        big = tk.Frame(self.result_frame, bg=COLORS["bg"])
        big.pack(fill="x")
        self.big_downloaded = self._big_stat(big, COLORS["downloaded"])
        self.big_no_oa = self._big_stat(big, COLORS["no_oa"])
        self.big_error = self._big_stat(big, COLORS["error"])

        self.lbl_not_found = tk.Label(self.result_frame, text="", bg=COLORS["bg"], anchor="w", justify="left")
        self.lbl_not_found.pack(fill="x", pady=(8, 4))

        self.btn_open_folder = tk.Button(self.result_frame, text="\U0001F4C2 Abrir pasta de destino", command=self._on_open_folder)
        self.btn_open_folder.pack(anchor="w")

        self.btn_open_html = tk.Button(self.result_frame, text="\U0001F517 Abrir lista de DOIs (HTML)", command=self._on_open_html)
        # (empacotado só quando o HTML existe — ver _show_results)

    def _big_stat(self, parent, color):
        frame = tk.Frame(parent, bg=COLORS["bg"])
        frame.pack(side="left", padx=20)
        num = tk.Label(frame, text="0", bg=COLORS["bg"], fg=color, font=("TkDefaultFont", 28, "bold"))
        num.pack()
        return num

    # ---------- steps ----------

    def _update_steps(self):
        step = 1
        if self.articles:
            step = 2
        if self.worker and self.worker.is_alive():
            step = 3
        if self.finished:
            step = 4
        for i, lbl in enumerate(self.step_labels, start=1):
            color = COLORS["step_done"] if i < step else (COLORS["step_active"] if i == step else COLORS["step_pending"])
            lbl.configure(fg=color)

    # ---------- file selection ----------

    def _on_select_file(self):
        # Seleção múltipla: exports do WoS/Scopus saem em blocos de até 1000
        # registros, então uma revisão costuma ter vários arquivos.
        paths = filedialog.askopenfilenames(initialdir=self._last_dir,
                                            filetypes=[("CSV/TXT", "*.csv *.txt"), ("Todos", "*.*")])
        if paths:
            self._load_paths(list(paths))

    def _reset_file_state(self):
        # Zera o estado dos arquivos anteriores: sem isso, o botão Iniciar
        # continuaria ativo com a lista antiga sob o nome dos arquivos novos.
        self.articles = []
        self.doi_col = self.title_col = None
        self._pending_manual = []
        self.lbl_file_info.configure(text="")
        self.manual_cols_frame.pack_forget()
        self.tree.delete(*self.tree.get_children())
        self.row_ids = {}

    def _load_paths(self, paths):
        self._reset_file_state()
        # Próxima seleção abre onde o usuário estava, não mais na área de trabalho
        self._last_dir = str(Path(paths[0]).parent)
        if len(paths) == 1:
            nome = Path(paths[0]).name
        else:
            nome = f"{len(paths)} arquivos"
        self.title(f"CSVtoPDF — {nome}")
        self.lbl_file.configure(text=nome)

        carregados, erros_leitura = [], []
        for p in paths:
            try:
                header, rows, delim, doi_col, title_col, year_col = dl.read_file(p)
                carregados.append((Path(p).name, header, rows, doi_col, title_col, year_col))
            except Exception as e:
                erros_leitura.append(f"{Path(p).name}: {e}")

        if not carregados:
            self._update_start_button()
            self._update_steps()
            messagebox.showerror("Erro ao ler arquivo(s)", "\n".join(erros_leitura))
            return

        detectados = [c for c in carregados if c[3] is not None and c[4] is not None]
        sem_colunas = [c for c in carregados if c[3] is None or c[4] is None]

        avisos = []
        if erros_leitura:
            avisos.append(f"⚠ Não foi possível ler: {'; '.join(erros_leitura)}")

        if detectados:
            # ponytail: se alguns arquivos detectam e outros não, carrega os que
            # detectaram e apenas avisa — mapeamento manual por arquivo só se
            # essa mistura aparecer na prática.
            if sem_colunas:
                nomes = ", ".join(c[0] for c in sem_colunas)
                avisos.append(f"⚠ Ignorado(s) por colunas não identificadas: {nomes}")
            self._load_articles([(h, r, dc, tc, yc) for _, h, r, dc, tc, yc in detectados],
                                 len(detectados), avisos)
        elif len({tuple(c[1]) for c in sem_colunas}) == 1:
            # Nenhum detectado, mas todos têm o mesmo cabeçalho: fallback manual
            # único, aplicado a todos.
            header = sem_colunas[0][1]
            self._pending_manual = sem_colunas
            self.header = header
            self.doi_col_combo["values"] = header
            self.title_col_combo["values"] = header
            self.manual_cols_frame.pack(fill="x", pady=(8, 0))
            self.lbl_file_info.configure(
                text="Não foi possível identificar automaticamente as colunas de DOI e/ou Título. "
                     "Selecione manualmente abaixo.")
        else:
            self.lbl_file_info.configure(
                text="Os arquivos têm cabeçalhos diferentes e as colunas não foram identificadas.\n"
                     "Carregue-os um por vez para indicar as colunas manualmente.")

        default_dest = Path(paths[0]).parent / "pdfs"
        self.dest_var.set(str(default_dest))
        self._update_start_button()
        self._update_steps()

    def _on_confirm_manual_cols(self):
        doi_name, title_name = self.doi_col_var.get(), self.title_col_var.get()
        if not doi_name or not title_name:
            messagebox.showwarning("Colunas incompletas", "Selecione a coluna de DOI e a de Título.")
            return
        doi_col, title_col = self.header.index(doi_name), self.header.index(title_name)
        # year_col vem do que foi autodetectado em cada arquivo (índice 5), mesmo
        # que DOI/título tenham exigido escolha manual.
        self._load_articles([(h, r, doi_col, title_col, yc) for _, h, r, _, _, yc in self._pending_manual],
                             len(self._pending_manual), [])
        self._update_start_button()

    def _load_articles(self, file_infos, n_arquivos, avisos):
        """file_infos: lista de (header, rows, doi_col, title_col, year_col) resolvidos."""
        artigos = []
        for header, rows, doi_col, title_col, year_col in file_infos:
            artigos.extend(dl.extract_articles(rows, doi_col, title_col, year_col))
        self.articles, duplicados = dl.dedupe_articles(artigos)
        # Marca as colunas como resolvidas (habilita o botão Iniciar); os índices
        # por arquivo já foram aplicados em extract_articles.
        self.doi_col, self.title_col = file_infos[0][2], file_infos[0][3]

        total = len(self.articles)
        valid_dois = sum(1 for a in self.articles if a["doi"])
        partes = [f"{total} artigo(s)"]
        if n_arquivos > 1:
            partes[0] += f" de {n_arquivos} arquivo(s)"
        partes.append(f"{valid_dois} com DOI válido")
        if duplicados:
            partes.append(f"{duplicados} duplicado(s) removido(s)")
        info = "  ·  ".join(partes)
        if total and valid_dois / total < 0.5:
            info += "\n⚠ Poucos DOIs válidos foram encontrados nesta lista — confira o(s) arquivo(s) antes de continuar."
        for aviso in avisos:
            info += "\n" + aviso
        self.lbl_file_info.configure(text=info)
        # Mostra a lista carregada de imediato — confirmação visual de que o
        # arquivo foi entendido, antes de o usuário decidir baixar.
        self._populate_tree()
        self._update_steps()

    def _populate_tree(self):
        self.tree.configure(height=12)
        self.tree.delete(*self.tree.get_children())
        self.row_ids = {}
        for a in self.articles:
            row_id = self.tree.insert("", "end", values=(a["title"][:80], a["doi"], "aguardando"))
            self.row_ids[len(self.row_ids)] = row_id

    def _on_choose_dest(self):
        inicio = Path(self.dest_var.get().strip() or self._last_dir)
        if not inicio.is_dir():
            inicio = inicio.parent  # pasta "pdfs" padrão ainda não criada
        path = filedialog.askdirectory(initialdir=inicio)
        if path:
            escolhida = Path(path)
            # Os PDFs sempre vão para uma subpasta "pdfs" da pasta escolhida
            # (evita jogar arquivos soltos na pasta do usuário). Não duplica se
            # a própria pasta escolhida já for "pdfs".
            if escolhida.name.lower() != "pdfs":
                escolhida = escolhida / "pdfs"
            self.dest_var.set(str(escolhida))

    # ---------- config persistence ----------

    def _load_saved_config(self):
        cfg = dl.load_config()
        if cfg.get("email"):
            self.email_var.set(cfg["email"])
        self.nao_encontrados_var.set(cfg.get("salvar_nao_encontrados", True))

    def _save_config(self):
        dl.save_config({"email": self.email_var.get().strip(),
                        "salvar_nao_encontrados": self.nao_encontrados_var.get()})

    # ---------- download flow ----------

    def _update_start_button(self, *_):
        ok = bool(self.articles) and "@" in self.email_var.get() and self.doi_col is not None
        running = self.worker and self.worker.is_alive()
        self.btn_start.configure(state="normal" if ok and not running else "disabled")

    def _on_start(self):
        email = self.email_var.get().strip()
        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showwarning("Pasta de destino", "Escolha uma pasta de destino.")
            return

        self._save_config()

        self.dest_folder = dest
        self._populate_tree()
        self.progress.configure(maximum=len(self.articles), value=0)
        self.lbl_counters.configure(text="✅ 0 baixados · ⚪ 0 sem OA · ❌ 0 erros")
        self.lbl_progress.configure(text="")
        self.result_frame.pack_forget()

        self.finished = False
        self.cancel_event = threading.Event()
        self.start_time = time.time()
        self.worker = threading.Thread(
            target=dl.process_articles,
            args=(self.articles, email, self.dest_folder, self.msg_queue, self.cancel_event,
                  self.nao_encontrados_var.get()),
            daemon=True,
        )
        self.worker.start()

        self.btn_start.configure(state="disabled")
        self.btn_select_file.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self._update_steps()
        self.after(100, self._poll_queue)

    def _on_cancel(self):
        if self.cancel_event:
            self.cancel_event.set()
        self.btn_cancel.configure(state="disabled")

    def _poll_queue(self):
        # Segue agendando até consumir o "done" — checar worker.is_alive() aqui
        # perderia um "done" postado entre o esvaziamento da fila e a checagem.
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                self._handle_message(msg)
                if msg[0] == "done":
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_message(self, msg):
        kind = msg[0]
        if kind == "processing":
            _, i, total, title = msg
            for rid in self.tree.get_children():
                self.tree.item(rid, tags=[t for t in self.tree.item(rid, "tags") if t != "current"])
            row_id = self.row_ids.get(i - 1)
            if row_id:
                self.tree.item(row_id, tags=("current",))
                self.tree.see(row_id)
            self._update_progress_label(i, total)

        elif kind == "result":
            _, i, total, result = msg
            row_id = self.row_ids.get(i - 1)
            if row_id:
                self.tree.item(row_id, values=(result["title"][:80], result["doi"], self._status_label(result)),
                                tags=(result["status"],))
            self.progress.configure(value=i)
            self._refresh_counters()
            self._update_progress_label(i, total)

        elif kind == "done":
            _, summary, csv_path, html_path, cancelled = msg
            self._show_results(summary, csv_path, html_path, cancelled)

    def _status_label(self, result):
        if result["status"] == "downloaded":
            return "✅ baixado"
        if result["status"] == "no_oa":
            return f"⚪ {result['detail']}"
        return f"❌ {result['detail']}"

    def _refresh_counters(self):
        counts = {"downloaded": 0, "no_oa": 0, "error": 0}
        for rid in self.tree.get_children():
            tags = self.tree.item(rid, "tags")
            for t in tags:
                if t in counts:
                    counts[t] += 1
        self.lbl_counters.configure(
            text=f"✅ {counts['downloaded']} baixados · ⚪ {counts['no_oa']} sem OA · ❌ {counts['error']} erros")

    def _update_progress_label(self, i, total):
        elapsed = time.time() - self.start_time
        rate = elapsed / i if i else 0
        remaining = max(total - i, 0) * rate
        self.lbl_progress.configure(text=f"{i} de {total} processados · tempo restante estimado: {int(remaining)}s")

    def _show_results(self, summary, csv_path, html_path, cancelled):
        self.finished = True
        self.html_path = html_path
        self.btn_select_file.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self._update_start_button()

        self.big_downloaded.configure(text=str(summary["downloaded"]))
        self.big_no_oa.configure(text=str(summary["no_oa"]))
        self.big_error.configure(text=str(summary["error"]))

        not_found_count = summary["no_oa"] + summary["error"]
        note = " (processo cancelado antes do fim)" if cancelled else ""
        texto = f"{not_found_count} artigo(s) não baixado(s){note}"
        if csv_path:
            texto += (f" → salvos em 'nao_encontrados.csv' e 'nao_encontrados.html' "
                      f"(abra o HTML para clicar nos DOIs).")
        else:
            texto += " (lista não salva)."
        self.lbl_not_found.configure(text=texto)

        # Botão de abrir o HTML só faz sentido quando ele foi gerado
        self.btn_open_html.pack(anchor="w", pady=(6, 0)) if html_path else self.btn_open_html.pack_forget()

        # Encolhe a tabela antes de mostrar o resultado: com ela na altura cheia,
        # o pack corta o painel 4 para fora da janela e o dashboard fica invisível.
        self.tree.configure(height=4)
        self.result_frame.pack(fill="both", pady=6)
        self._update_steps()

    def _on_open_folder(self):
        if self.dest_folder:
            self._abrir(os.path.abspath(self.dest_folder))

    def _on_open_html(self):
        if getattr(self, "html_path", ""):
            self._abrir(self.html_path)

    def _abrir(self, path):
        system = platform.system()
        try:
            if system == "Windows":
                os.startfile(path)
            elif system == "Darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
        except OSError as e:
            messagebox.showerror("Erro", f"Não foi possível abrir:\n{e}")
