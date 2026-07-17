#!/usr/bin/env python3
"""CSVtoPDF — launcher universal (Linux e Windows). Não requer administrador.

Adaptado do launcher do Excerpta: instala as dependências que faltarem
(com janela de progresso) e abre o programa.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import traceback

# Única dependência externa; tkinter vem com o Python (no Linux, via python3-tk).
DEPS_OBRIGATORIAS = [
    ("requests", "requests"),
]

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

# (binário, comando de instalação, pacote do Tk, pacote do pip)
PKG_MANAGERS = [
    ("apt-get", ["apt-get", "install", "-y"],              "python3-tk",      "python3-pip"),
    ("dnf",     ["dnf", "install", "-y"],                   "python3-tkinter", "python3-pip"),
    ("yum",     ["yum", "install", "-y"],                   "python3-tkinter", "python3-pip"),
    ("zypper",  ["zypper", "--non-interactive", "install"], "python3-tk",      "python3-pip"),
    ("pacman",  ["pacman", "-S", "--noconfirm"],            "tk",              "python-pip"),
    ("apk",     ["apk", "add"],                             "python3-tkinter", "py3-pip"),
]
_ENV_JA_TENTOU = "_CSVTOPDF_TENTOU_INSTALAR_SO"


def _pip_flags():
    """Flags para pip que evitam precisar de permissão de administrador."""
    if IS_WIN:
        return ["--user"]
    # Ubuntu 23+/Debian 12+ exigem --break-system-packages para pip fora de venv
    return ["--break-system-packages"]


def _tk_disponivel():
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


def _pip_disponivel():
    return importlib.util.find_spec("pip") is not None


def _detectar_gerenciador():
    for binario, cmd, pkg_tk, pkg_pip in PKG_MANAGERS:
        if shutil.which(binario):
            return binario, cmd, pkg_tk, pkg_pip
    return None


def _instalar_pacotes_sistema(cmd, pacotes):
    try:
        res = subprocess.run(["sudo"] + cmd + pacotes)
        return res.returncode == 0
    except FileNotFoundError:
        res = subprocess.run(cmd + pacotes)
        return res.returncode == 0


def _garantir_tk_e_pip():
    """No Linux, garante tkinter e pip no sistema, instalando via apt/dnf/pacman/…"""
    faltando = []
    if not _tk_disponivel():
        faltando.append("tk")
    if not _pip_disponivel():
        faltando.append("pip")

    if not faltando:
        return True

    info = _detectar_gerenciador()

    if os.environ.get(_ENV_JA_TENTOU) == "1":
        print(f"\n⚠ Ainda faltam pacotes do sistema: {', '.join(faltando)}")
        if info:
            _, cmd, pkg_tk, pkg_pip = info
            pacotes = [p for p, nome in ((pkg_tk, "tk"), (pkg_pip, "pip")) if nome in faltando]
            print(f"Instale manualmente: sudo {' '.join(cmd)} {' '.join(pacotes)}")
        input("Pressione Enter para sair.")
        return False

    if not info:
        print("Não consegui detectar o gerenciador de pacotes da sua distribuição.")
        print(f"Instale manualmente os pacotes de sistema para: {', '.join(faltando)} "
              "(ex. Debian/Ubuntu: python3-tk, python3-pip)")
        input("Pressione Enter para sair.")
        return False

    binario, cmd, pkg_tk, pkg_pip = info
    pacotes = []
    if "tk" in faltando:
        pacotes.append(pkg_tk)
    if "pip" in faltando:
        pacotes.append(pkg_pip)

    print(f"Faltam pacotes do sistema ({binario} detectado): {', '.join(pacotes)}")
    print("Vou instalar agora — pode pedir sua senha de administrador.\n")
    ok = _instalar_pacotes_sistema(cmd, pacotes)
    if not ok:
        print("\n⚠ Falha ao instalar automaticamente.")
        print(f"Rode manualmente: sudo {' '.join(cmd)} {' '.join(pacotes)}")
        input("Pressione Enter para sair.")
        return False

    print("\n✓ Pacotes do sistema instalados. Reiniciando o CSVtoPDF...\n")
    os.environ[_ENV_JA_TENTOU] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _configurar_atalho():
    """Deixa o programa clicável sem configuração manual: garante o bit de
    execução deste launcher e cria/atualiza o atalho no menu de aplicativos
    (Linux). Idempotente — roda em toda inicialização e nunca bloqueia o app."""
    try:
        os.chmod(__file__, os.stat(__file__).st_mode | 0o111)
    except OSError:
        pass

    if not IS_LINUX:
        return

    try:
        pasta_apps = os.path.expanduser("~/.local/share/applications")
        os.makedirs(pasta_apps, exist_ok=True)
        atalho = os.path.join(pasta_apps, "csvtopdf.desktop")
        conteudo = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=CSVtoPDF\n"
            "Comment=Baixa PDFs de artigos em acesso aberto (Unpaywall)\n"
            f'Exec={sys.executable} "{os.path.abspath(__file__)}"\n'
            "Icon=document-save\n"
            "Terminal=false\n"
            "Categories=Office;\n"
        )
        # Reescreve só se mudou (ex.: pasta do programa foi movida)
        atual = ""
        if os.path.exists(atalho):
            with open(atalho, "r", encoding="utf-8") as f:
                atual = f.read()
        if atual != conteudo:
            with open(atalho, "w", encoding="utf-8") as f:
                f.write(conteudo)
            os.chmod(atalho, 0o755)
    except OSError:
        pass


def checar_faltando(deps):
    faltando = []
    for modulo, pacote in deps:
        if importlib.util.find_spec(modulo) is None:
            faltando.append((modulo, pacote))
    return faltando


def _instalar_pacotes(pacotes, callback_log, callback_fim):
    """Instala a lista de pacotes. Roda em thread separada."""
    erros = []
    flags = _pip_flags()
    for _mod, pacote in pacotes:
        callback_log(f"> pip install {pacote}")
        res = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", pacote] + flags,
            capture_output=True, text=True,
        )
        if res.returncode == 0:
            callback_log(f"  ✓ {pacote} instalado com sucesso")
        else:
            # Última tentativa: sem --user / sem --break-system-packages
            res2 = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", pacote],
                capture_output=True, text=True,
            )
            if res2.returncode == 0:
                callback_log(f"  ✓ {pacote} instalado")
            else:
                erros.append(pacote)
                callback_log(f"  ✗ Falha: {(res.stderr or res2.stderr).strip()[:200]}")

    callback_fim(erros)


def _abrir_app():
    """Abre a ferramenta no MESMO processo, em vez de spawnar-e-sair.

    Spawnar e encerrar escondia qualquer erro de inicialização: se o app
    quebrava ao abrir, a janela simplesmente não aparecia e ninguém via a
    causa. Rodando aqui, qualquer exceção é capturada e mostrada (diálogo +
    console + arquivo de log), e o código de saída != 0 faz o .bat pausar.
    """
    pasta = os.path.dirname(SCRIPT)
    if pasta not in sys.path:
        sys.path.insert(0, pasta)
    try:
        import gui
    except ImportError:
        # Dependência recém-instalada pode não importar neste processo já
        # iniciado; reabre num processo limpo (aí sim como subprocess).
        res = subprocess.run([sys.executable, SCRIPT])
        if res.returncode != 0:
            _erro_ao_abrir(f"O programa saiu com código {res.returncode}. "
                           "Veja a janela do programa para detalhes.")
        return
    try:
        gui.App().mainloop()
    except Exception:
        _erro_ao_abrir(traceback.format_exc())


def _erro_ao_abrir(detalhe):
    """Mostra a falha de inicialização de todas as formas possíveis, para que
    o usuário consiga ver e reportar (em vez de a janela só não abrir)."""
    log = os.path.join(os.path.dirname(SCRIPT), "csvtopdf_erro.txt")
    try:
        with open(log, "w", encoding="utf-8") as f:
            f.write(detalhe)
    except OSError:
        log = "(não foi possível salvar o log)"

    print("\n===== Erro ao abrir o CSVtoPDF =====")
    print(detalhe)
    print(f"\nDetalhes salvos em: {log}")

    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        ultima = detalhe.strip().splitlines()[-1] if detalhe.strip() else "erro desconhecido"
        messagebox.showerror(
            "CSVtoPDF — erro ao abrir",
            "O programa encontrou um erro ao abrir:\n\n"
            f"{ultima}\n\nDetalhes completos salvos em:\n{log}")
        r.destroy()
    except Exception:
        pass

    try:
        input("\nPressione Enter para sair.")
    except EOFError:
        pass
    sys.exit(1)


def _instalar_gui(pacotes, root, depois_de_instalar):
    import tkinter as tk
    from tkinter import messagebox, ttk

    dlg = tk.Toplevel(root)
    dlg.title("Instalando dependências…")
    dlg.resizable(False, False)
    dlg.grab_set()
    dlg.transient(root)

    ttk.Label(dlg, text="Instalando pacotes, aguarde…",
              font=("TkDefaultFont", 10)).pack(padx=24, pady=(16, 6))

    barra = ttk.Progressbar(dlg, mode="indeterminate", length=340)
    barra.pack(padx=24, pady=(0, 6))
    barra.start(10)

    log = tk.Text(dlg, height=8, width=62, state="disabled",
                  font=("TkFixedFont", 8), bg="#1e1e1e", fg="#d4d4d4")
    log.pack(padx=24, pady=(0, 16))

    def _log(txt):
        log.config(state="normal")
        log.insert("end", txt + "\n")
        log.see("end")
        log.config(state="disabled")
        dlg.update()

    def _fim(erros):
        barra.stop()
        if erros:
            _log(f"\n⚠  Falha ao instalar: {', '.join(erros)}")
            cmd = (f"pip install {' '.join(erros)}" +
                   ("" if IS_WIN else " --break-system-packages"))
            root.after(0, lambda: messagebox.showwarning(
                "Atenção",
                f"Não foi possível instalar: {', '.join(erros)}\n\n"
                f"Tente manualmente no terminal:\n  {cmd}"))
        else:
            _log("\n✅ Tudo instalado! Abrindo o CSVtoPDF…")
            root.after(800, lambda: [dlg.destroy(), depois_de_instalar()])

    threading.Thread(target=_instalar_pacotes,
                     args=(pacotes, _log, _fim),
                     daemon=True).start()
    dlg.wait_window()


def main_gui():
    """Mostra a tela de configuração inicial (se faltar dependência) e devolve
    True quando o app deve ser aberto em seguida. O app é aberto pelo chamador,
    depois deste mainloop terminar — nunca aninhado num callback."""
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.withdraw()

    faltando = checar_faltando(DEPS_OBRIGATORIAS)

    if not faltando:
        # Tudo instalado — abrir direto, sem mostrar nenhuma janela
        root.destroy()
        return True

    root.deiconify()
    root.title("CSVtoPDF — Configuração inicial")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=28)
    frame.pack()

    ttk.Label(frame, text="CSVtoPDF",
              font=("TkDefaultFont", 14, "bold")).pack(pady=(0, 2))
    ttk.Label(frame,
              text="Baixa PDFs de artigos em acesso aberto (Unpaywall)",
              foreground="gray").pack(pady=(0, 16))

    ttk.Label(frame,
              text="Dependências a instalar (nenhuma requer administrador):",
              font=("TkDefaultFont", 9, "bold")).pack(anchor="w")
    for _, p in faltando:
        ttk.Label(frame, text=f"  • {p}", foreground="#333").pack(anchor="w")
    ttk.Label(frame, text="").pack()

    estado = {"abrir": False}

    def _iniciar():
        _instalar_gui(faltando, root,
                      lambda: [estado.__setitem__("abrir", True), root.destroy()])

    frame_btn = ttk.Frame(frame)
    frame_btn.pack()
    ttk.Button(frame_btn, text="Instalar e abrir",
               command=_iniciar).pack(side="left", padx=6)
    ttk.Button(frame_btn, text="Cancelar",
               command=root.destroy).pack(side="left", padx=6)

    root.mainloop()
    return estado["abrir"]


def main_console():
    """Fallback sem tkinter (Linux sem python3-tk instalado)."""
    print("=" * 52)
    print("  CSVtoPDF — Configuração inicial")
    print("=" * 52)
    print()

    faltando = checar_faltando(DEPS_OBRIGATORIAS)

    if not faltando:
        print("Tudo instalado. Abrindo CSVtoPDF...")
        return True

    print("Dependências a instalar:")
    for _, p in faltando:
        print(f"  • {p}")
    print()
    resp = input("Instalar agora? [S/n]: ").strip().lower()
    if resp in ("n", "no", "nao", "não"):
        print("Cancelado.")
        return False

    print()
    erros = []
    t = threading.Thread(target=_instalar_pacotes,
                         args=(faltando, print, erros.extend))
    t.start()
    t.join()

    if erros:
        print(f"\n⚠  Falha: {', '.join(erros)}")
        print("Tente manualmente:")
        for e in erros:
            print(f"  pip install {e}" + ("" if IS_WIN else " --break-system-packages"))
        return False
    print("\n✓ Instalação concluída! Abrindo CSVtoPDF…")
    return True


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        print(f"CSVtoPDF requer Python 3.9+. Versão atual: {sys.version}")
        print("Baixe a versão mais recente em: https://python.org/downloads")
        input("Pressione Enter para sair.")
        sys.exit(1)

    if IS_LINUX and not _garantir_tk_e_pip():
        sys.exit(1)

    _configurar_atalho()

    try:
        import tkinter  # noqa: F401
        abrir = main_gui()
    except ImportError:
        if IS_WIN:
            # No Windows o tkinter vem no instalador oficial do Python (opção
            # "tcl/tk and IDLE"); se faltar, não dá pra instalar via pip.
            print("tkinter não está disponível nesta instalação do Python.")
            print()
            print('Reinstale o Python marcando a opção "tcl/tk and IDLE":')
            print("  https://python.org/downloads")
            input("Pressione Enter para sair.")
            sys.exit(1)
        print("tkinter não disponível — usando modo console.")
        abrir = main_console()

    # Abre o app só aqui, fora de qualquer mainloop/callback, para que erros
    # de inicialização apareçam (ver _abrir_app / _erro_ao_abrir).
    if abrir:
        _abrir_app()
