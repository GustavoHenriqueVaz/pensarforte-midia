#!/usr/bin/env python3
"""Publicador CONTÍNUO para o GitHub Actions.

O cron do GitHub é irregular (execuções a cada ~3 h). Em vez de depender de 1 execução por horário,
cada execução fica VIVA por ~5h30 conferindo a fila a cada ~75 s. Como uma nova execução entra na fila
sempre que a anterior ainda roda (concurrency), a cobertura é praticamente contínua e o atraso cai de
horas para ~1–2 minutos. A cada rodada: git pull (pega fila nova e mídia nova), publica o que venceu e
envia a fila atualizada de volta.
"""
import os, subprocess, sys, time

os.environ["MODO_NUVEM"] = "1"
os.environ.setdefault("QUIETO", "1")
import publicar  # noqa: E402

MINUTOS = float(os.environ.get("LOOP_MINUTOS", "330"))
PAUSA = int(os.environ.get("LOOP_PAUSA", "75"))
fim = time.time() + MINUTOS * 60


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def enviar_fila():
    sh("git", "config", "user.name", "pensarforte-bot")
    sh("git", "config", "user.email", "pensarforte-bot@users.noreply.github.com")
    sh("git", "add", "fila.json", "segredos.enc")
    if sh("git", "diff", "--cached", "--quiet").returncode == 0:
        return
    sh("git", "commit", "-m", "automatico: atualiza fila")
    for _ in range(5):
        if sh("git", "push").returncode == 0:
            return
        sh("git", "pull", "--rebase", "--autostash")
        time.sleep(4)
    publicar.log("AVISO: não consegui enviar a fila ao repositório (será reenviada na próxima rodada)")


def main():
    rodadas = 0
    publicar.log(f"publicador contínuo iniciado: {MINUTOS:.0f} min, a cada {PAUSA}s")
    while time.time() < fim:
        sh("git", "pull", "--rebase", "--autostash")
        antes = open(publicar.FILA, "rb").read()
        try:
            publicar.main()
        except SystemExit as e:
            publicar.log(f"publicar.main saiu: {e}")
            sys.exit(1)
        except Exception as e:  # erro inesperado não derruba o loop
            publicar.log(f"AVISO: erro na rodada: {str(e)[:200]}")
        if open(publicar.FILA, "rb").read() != antes:
            enviar_fila()
        rodadas += 1
        time.sleep(PAUSA)
    publicar.log(f"publicador contínuo terminou após {rodadas} rodadas")
    enviar_fila()


if __name__ == "__main__":
    main()
