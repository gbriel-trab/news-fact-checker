"""Painel local para inspecionar o sistema e disparar o radar.

    python -m src.painel            # sobe em http://127.0.0.1:8765

Ferramenta de operação, não produto: mostra a saúde da coleta, o funil da
extração com os custos gravados, o digest, as últimas verificações que o
boletim gravou, e permite disparar o radar — sempre com o preço estimado
na tela e confirmação antes de qualquer chamada paga.

Stdlib pura de propósito (http.server + um HTML): o projeto não tem
framework web e um painel de desenvolvimento não justifica adicionar um.
Escuta só em 127.0.0.1 — não há autenticação porque não há rede.

A única ação paga daqui é radar.busca, que reaproveita o módulo real. A
verificação em si acontece no boletim (radar → premissas → check), e o
resultado dela aparece aqui pela tabela `consultas`, que o boletim
alimenta.
"""

import json
import sqlite3
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config

PORTA = 8765
_HTML = Path(__file__).with_name("painel.html")

# grafo carrega o modelo de embedding (~10 s na primeira vez); o lock
# impede duas requisições simultâneas de pagarem essa carga juntas.
_trava = threading.Lock()


def _ro() -> sqlite3.Connection:
    con = sqlite3.connect(
        f"file:{config.BANCO}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


# ------------------------------------------------------------------ leituras

def resumo() -> dict:
    con = _ro()
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    um = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731

    materias = um("SELECT COUNT(DISTINCT url_norm) FROM artigos")
    ultima = um("SELECT MAX(coletado_em) FROM artigos")
    por_dia = [dict(r) for r in q(
        "SELECT SUBSTR(coletado_em,1,10) dia, COUNT(*) registros "
        "FROM artigos GROUP BY dia ORDER BY dia DESC LIMIT 10")]
    por_veiculo = [dict(r) for r in q(
        "SELECT veiculo, COUNT(DISTINCT url_norm) materias FROM artigos "
        "GROUP BY veiculo ORDER BY materias DESC")]

    custo = um("SELECT COALESCE(SUM(custo_usd),0) FROM extracoes")
    # O recorte que o grafo lê — LITERALMENTE: os contadores vêm do
    # próprio grafo.carrega, não de SQL paralelo. A revisão de 01/09/2026
    # pegou a versão SQL somando v2 E v3 do MESMO artigo (o grafo escolhe
    # uma extração por matéria); duplicar a regra aqui era pedir para as
    # duas divergirem de novo.
    import collections

    from . import grafo, vocabulario
    compat = ",".join(str(v) for v in sorted(vocabulario.COMPATIVEIS))
    vocab = um("SELECT COALESCE(MAX(vocab_versao),0) FROM extracoes")
    extraidas = um("SELECT COUNT(DISTINCT artigo_id) FROM extracoes "
                   f"WHERE vocab_versao IN ({compat})")
    acervo = grafo.carrega(con)
    triplas_v = len(acervo)
    outro_v = sum(1 for a in acervo if a.relacao == "outro")
    contagem = collections.Counter(a.relacao for a in acervo)
    relacoes = [{"relacao": r, "n": n} for r, n in contagem.most_common(12)]
    consultas = [dict(r) for r in q(
        "SELECT afirmacao, veredito, veiculos, custo_usd, consultado_em "
        "FROM consultas ORDER BY id DESC LIMIT 12")]
    con.close()
    return {
        "acervo": {"materias": materias, "ultima_coleta": ultima,
                   "por_dia": por_dia, "por_veiculo": por_veiculo},
        "extracao": {"materias_extraidas": extraidas,
                     "custo_usd": round(custo, 4),
                     "vocab_versao": vocab, "triplas": triplas_v,
                     "outro": outro_v, "relacoes": relacoes},
        "consultas": consultas,
    }


def digest_json(horas: int, topicos: list[str]) -> dict:
    from . import digest as dg
    from . import grafo
    from .storage import conecta
    from datetime import datetime, timedelta, timezone

    desde = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
    con = conecta(config.BANCO)
    with _trava:
        afirmacoes = grafo.carrega(con, desde=desde)
        recorte = dg.recorta(dg.filtra_topicos(afirmacoes, topicos))
    con.close()

    def item(c):
        return {
            "sujeito": c.sujeito, "relacao": c.chave[1],
            "objeto": c.objeto, "contexto": c.contexto,
            "veiculos": sorted(c.veiculos),
            "itens": [{"veiculo": a.veiculo, "titulo": a.titulo,
                       "url": a.url, "valor": a.valor, "unidade": a.unidade,
                       "data_fato": a.data_fato}
                      for a in c.afirmacoes],
        }

    def divergencia(c):
        base = item(c)
        base["disputas"] = [
            {"unidade": unidade,
             "valores": [{"veiculo": a.veiculo, "valor": a.valor,
                          "data_fato": a.data_fato, "titulo": a.titulo}
                         for a in sorted(grupo, key=lambda x: x.valor)]}
            for unidade, grupo in c.divergencias]
        return base

    return {
        "horas": horas, "topicos": topicos,
        "materias": len(recorte.materias),
        "veiculos": len(recorte.veiculos),
        "afirmacoes": len(recorte.afirmacoes),
        "taxa_confirmacao": round(recorte.taxa_confirmacao, 3),
        "confirmados": [item(c) for c in recorte.confirmados[:25]],
        "divergentes": [divergencia(c) for c in recorte.divergentes],
        "unicos_total": len(recorte.unicos),
        "unicos": [item(c) for c in recorte.unicos[:15]],
    }


# ------------------------------------------------------------ ações (pagas)

def rodar_radar(dias: int) -> dict:
    from . import radar
    rodada = radar.busca(config.HANDLES_RADAR, dias)

    def item(i: int, c) -> dict:
        ref = c.referenciado
        return {"numero": i, "autor": c.post.autor,
                "quando": radar.quando(c.post.criado_em),
                "tipo": c.post.tipo, "url": c.post.url,
                "texto": c.post.texto,
                "contexto": ({"de": ref.autor, "proprio": c.contexto_proprio,
                              "texto": " ".join(ref.texto.split())}
                             if ref is not None and ref.texto.strip()
                             else None)}

    return {"handles": list(config.HANDLES_RADAR),
            "posts": [item(i, c) for i, c in enumerate(rodada.capturas, 1)],
            "descartados": [{"autor": p.autor, "url": p.url, "motivo": m}
                            for p, m in rodada.descartados],
            "notas": list(rodada.notas), "lidos": rodada.lidos,
            "custo_estimado_usd": round(rodada.custo_estimado_usd, 4)}


# ---------------------------------------------------------------- servidor

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # silencioso; o terminal é do usuário
        pass

    def _json(self, corpo: dict, status: int = 200) -> None:
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self) -> None:
        url = urllib.parse.urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                html = _HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            elif url.path == "/api/resumo":
                self._json(resumo())
            elif url.path == "/api/digest":
                q = urllib.parse.parse_qs(url.query)
                horas = int(q.get("horas", ["48"])[0])
                topicos = [t.strip() for t in
                           q.get("topicos", [""])[0].split(",") if t.strip()]
                self._json(digest_json(horas, topicos))
            else:
                self._json({"erro": "rota desconhecida"}, 404)
        except Exception as erro:  # painel de dev: o erro vai para a tela
            self._json({"erro": f"{type(erro).__name__}: {erro}"}, 500)

    def do_POST(self) -> None:
        try:
            tamanho = int(self.headers.get("Content-Length", 0))
            corpo = json.loads(self.rfile.read(tamanho) or b"{}")
            if self.path == "/api/radar":
                self._json(rodar_radar(int(corpo.get("dias", 2))))
            else:
                self._json({"erro": "rota desconhecida"}, 404)
        except Exception as erro:
            self._json({"erro": f"{type(erro).__name__}: {erro}"}, 500)


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")
    servidor = ThreadingHTTPServer(("127.0.0.1", PORTA), _Handler)
    print(f"Painel em http://127.0.0.1:{PORTA}  (Ctrl+C para parar)")
    print("Leituras são grátis; ações pagas pedem confirmação na tela.")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
