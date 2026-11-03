"""Camada de dados - SQLite (arquivo cantina.db, sem servidor externo).
Usa ? como parâmetro, separando SQL dos dados.
Cada usuário tem seu próprio estoque (coluna usuario_id).

Estoque por lotes: cada entrada cria (ou soma em) um lote com sua validade.
- Vendas consomem primeiro o lote que vence antes (FEFO).
- Lotes vencidos saem do estoque sozinhos (baixar_vencidos) e ficam no histórico.
"""
import hashlib
import os
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("CANTINA_DB", BASE_DIR / "cantina.db"))
DIAS_ALERTA_VALIDADE = 7
FUSO = ZoneInfo("America/Sao_Paulo")

_COLUNAS_DATA = {"validade", "validade_lote"}
_COLUNAS_DATAHORA = {"data", "criado_em"}
_COLUNAS_DINHEIRO = {"preco", "valor_unit", "total"}


def _agora():
    return datetime.now(FUSO).replace(tzinfo=None, microsecond=0)


def _hoje():
    return _agora().date()


def _converter(coluna, valor):
    """Converte o que o SQLite guarda (texto/número) para os tipos que o app espera."""
    if valor is None:
        return None
    if coluna in _COLUNAS_DATA:
        return date.fromisoformat(str(valor)[:10])
    if coluna in _COLUNAS_DATAHORA:
        return datetime.fromisoformat(str(valor))
    if coluna in _COLUNAS_DINHEIRO:
        return Decimal(str(valor)).quantize(Decimal("0.01"))
    return valor


def _linha_para_dict(cursor, linha):
    return {col[0]: _converter(col[0], valor) for col, valor in zip(cursor.description, linha)}


def conectar():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = _linha_para_dict
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _dinheiro(valor):
    """SQLite não aceita Decimal: guarda como número com 2 casas."""
    return float(Decimal(str(valor or 0)).quantize(Decimal("0.01")))


def _data(valor):
    return valor.isoformat() if valor else None


_SQL_BASE = """
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT NOT NULL,
        usuario TEXT NOT NULL UNIQUE,
        senha_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS produtos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER REFERENCES usuarios(id),
        nome TEXT NOT NULL,
        categoria TEXT NOT NULL,
        quantidade INTEGER NOT NULL DEFAULT 0 CHECK (quantidade >= 0),
        estoque_minimo INTEGER NOT NULL DEFAULT 0,
        validade TEXT NULL,
        preco REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS vendas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER REFERENCES usuarios(id),
        data TEXT NOT NULL,
        total REAL NOT NULL DEFAULT 0,
        observacao TEXT NULL
    );
    CREATE TABLE IF NOT EXISTS venda_itens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        venda_id INTEGER NOT NULL REFERENCES vendas(id) ON DELETE CASCADE,
        produto_id INTEGER NOT NULL REFERENCES produtos(id) ON DELETE RESTRICT,
        quantidade INTEGER NOT NULL CHECK (quantidade > 0),
        valor_unit REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS lotes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        produto_id INTEGER NOT NULL REFERENCES produtos(id) ON DELETE CASCADE,
        quantidade INTEGER NOT NULL CHECK (quantidade > 0),
        validade TEXT NULL
    );
"""

_SQL_MOVIMENTACOES = """
    CREATE TABLE IF NOT EXISTS {nome} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER REFERENCES usuarios(id),
        produto_id INTEGER NOT NULL REFERENCES produtos(id) ON DELETE RESTRICT,
        tipo TEXT NOT NULL CHECK (tipo IN ('entrada','saida','vencimento')),
        quantidade INTEGER NOT NULL CHECK (quantidade > 0),
        validade_lote TEXT NULL,
        observacao TEXT NULL,
        data TEXT NOT NULL,
        valor_unit REAL NOT NULL DEFAULT 0,
        venda_id INTEGER NULL REFERENCES vendas(id)
    );
"""


def criar_banco():
    con = conectar()
    con.executescript(_SQL_BASE + _SQL_MOVIMENTACOES.format(nome="movimentacoes"))
    _migrar(con)
    con.commit()
    con.close()


def _migrar(con):
    """Atualiza bancos criados na versão antiga (idempotente: pode rodar sempre)."""
    sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='movimentacoes'").fetchone()["sql"]
    if "vencimento" not in sql:
        # versão antiga: o CHECK só aceitava entrada/saida e não existia venda_id
        con.executescript(
            _SQL_MOVIMENTACOES.format(nome="movimentacoes_nova") +
            "INSERT INTO movimentacoes_nova(id,usuario_id,produto_id,tipo,quantidade,validade_lote,observacao,data,valor_unit) "
            "SELECT id,usuario_id,produto_id,tipo,quantidade,validade_lote,observacao,data,valor_unit FROM movimentacoes;"
            "DROP TABLE movimentacoes;"
            "ALTER TABLE movimentacoes_nova RENAME TO movimentacoes;")

    # estoque antigo vira um lote por produto, com a validade que o produto já tinha
    con.execute("INSERT INTO lotes(produto_id,quantidade,validade) "
                "SELECT id,quantidade,validade FROM produtos p "
                "WHERE quantidade>0 AND NOT EXISTS (SELECT 1 FROM lotes l WHERE l.produto_id=p.id)")

    # saídas antigas viram vendas de 1 item, para aparecerem no histórico de vendas
    antigas = con.execute("SELECT id,usuario_id,produto_id,quantidade,valor_unit,observacao,data "
                          "FROM movimentacoes WHERE tipo='saida' AND venda_id IS NULL").fetchall()
    for m in antigas:
        total = _dinheiro(m["quantidade"] * m["valor_unit"])
        venda_id = con.execute("INSERT INTO vendas(usuario_id,data,total,observacao) VALUES(?,?,?,?)",
                               (m["usuario_id"], m["data"].isoformat(sep=" "), total,
                                m["observacao"] or "Saída registrada antes do módulo de vendas")).lastrowid
        con.execute("INSERT INTO venda_itens(venda_id,produto_id,quantidade,valor_unit) VALUES(?,?,?,?)",
                    (venda_id, m["produto_id"], m["quantidade"], _dinheiro(m["valor_unit"])))
        con.execute("UPDATE movimentacoes SET venda_id=? WHERE id=?", (venda_id, m["id"]))


def _sincronizar(con, produto_id):
    """Mantém produtos.quantidade e produtos.validade iguais ao que há nos lotes."""
    con.execute("UPDATE produtos SET "
                "quantidade=COALESCE((SELECT SUM(quantidade) FROM lotes WHERE produto_id=produtos.id),0), "
                "validade=(SELECT MIN(validade) FROM lotes WHERE produto_id=produtos.id AND validade IS NOT NULL) "
                "WHERE id=?", (produto_id,))


# ================= USUÁRIOS / LOGIN =================
def _hash(senha, salt):
    return hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(salt), 120_000).hex()


def registrar_usuario(nome, usuario, senha):
    nome, usuario = nome.strip(), usuario.strip().lower()
    if not nome or not usuario or not senha:
        return False, "Preencha todos os campos"
    if len(senha) < 4:
        return False, "A senha deve ter pelo menos 4 caracteres"
    con = conectar()
    try:
        cur = con.cursor()
        cur.execute("SELECT id FROM usuarios WHERE usuario=?", (usuario,))
        if cur.fetchone():
            return False, "Este usuário já existe"
        salt = os.urandom(16).hex()
        cur.execute("INSERT INTO usuarios(nome,usuario,senha_hash,salt,criado_em) VALUES(?,?,?,?,?)",
                    (nome, usuario, _hash(senha, salt), salt, _agora().isoformat(sep=" ")))
        novo_id = cur.lastrowid
        # primeira conta assume produtos antigos (sem dono)
        cur.execute("SELECT COUNT(*) AS n FROM usuarios")
        if cur.fetchone()["n"] == 1:
            for tabela in ("produtos", "movimentacoes", "vendas"):
                cur.execute(f"UPDATE {tabela} SET usuario_id=? WHERE usuario_id IS NULL", (novo_id,))
        con.commit()
        return True, "Conta criada com sucesso"
    except Exception as e:
        con.rollback()
        return False, f"Erro ao criar conta: {e}"
    finally:
        con.close()


def autenticar(usuario, senha):
    con = conectar()
    u = con.execute("SELECT * FROM usuarios WHERE usuario=?", (usuario.strip().lower(),)).fetchone()
    con.close()
    if not u or _hash(senha, u["salt"]) != u["senha_hash"]:
        return None
    return {"id": u["id"], "nome": u["nome"], "usuario": u["usuario"]}


def alterar_senha(uid, atual, nova):
    con = conectar()
    u = con.execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone()
    if not u or _hash(atual, u["salt"]) != u["senha_hash"]:
        con.close()
        return False, "Senha atual incorreta"
    if len(nova) < 4:
        con.close()
        return False, "A nova senha deve ter pelo menos 4 caracteres"
    salt = os.urandom(16).hex()
    con.execute("UPDATE usuarios SET senha_hash=?, salt=? WHERE id=?", (_hash(nova, salt), salt, uid))
    con.commit()
    con.close()
    return True, "Senha alterada"


# ================= PRODUTOS =================
def cadastrar_produto(uid, nome, categoria, estoque_minimo, preco=0):
    """O produto nasce com estoque zero; a validade vem de cada entrada (lote)."""
    con = conectar()
    con.execute("INSERT INTO produtos(usuario_id,nome,categoria,quantidade,estoque_minimo,preco) "
                "VALUES(?,?,?,0,?,?)",
                (uid, nome, categoria, estoque_minimo, _dinheiro(preco)))
    con.commit()
    con.close()
    return True, "Produto cadastrado"


def editar_produto(uid, produto_id, nome, categoria, estoque_minimo, preco=0):
    con = conectar()
    con.execute("UPDATE produtos SET nome=?,categoria=?,estoque_minimo=?,preco=? "
                "WHERE id=? AND usuario_id=?",
                (nome, categoria, estoque_minimo, _dinheiro(preco), produto_id, uid))
    con.commit()
    con.close()
    return True, "Produto atualizado"


def excluir_produto(uid, produto_id):
    con = conectar()
    n = con.execute("SELECT COUNT(*) AS n FROM movimentacoes WHERE produto_id=?", (produto_id,)).fetchone()["n"]
    if n > 0:
        con.close()
        return False, "Produto possui histórico de movimentações e não pode ser excluído"
    con.execute("DELETE FROM produtos WHERE id=? AND usuario_id=?", (produto_id, uid))
    con.commit()
    con.close()
    return True, "Produto excluído"


# ================= ENTRADA DE ESTOQUE =================
def registrar_entrada(uid, produto_id, quantidade, validade_lote=None, obs=""):
    if quantidade <= 0:
        return False, "Quantidade deve ser maior que zero"
    if validade_lote and validade_lote < _hoje():
        return False, "Esse lote já está vencido"
    con = conectar()
    try:
        con.execute("BEGIN IMMEDIATE")  # trava escrita: evita duas movimentações ao mesmo tempo
        if not con.execute("SELECT 1 AS ok FROM produtos WHERE id=? AND usuario_id=?",
                           (produto_id, uid)).fetchone():
            con.rollback()
            return False, "Produto não encontrado"
        lote = con.execute("SELECT id FROM lotes WHERE produto_id=? AND validade IS ?",
                           (produto_id, _data(validade_lote))).fetchone()
        if lote:
            con.execute("UPDATE lotes SET quantidade=quantidade+? WHERE id=?", (quantidade, lote["id"]))
        else:
            con.execute("INSERT INTO lotes(produto_id,quantidade,validade) VALUES(?,?,?)",
                        (produto_id, quantidade, _data(validade_lote)))
        con.execute("INSERT INTO movimentacoes(usuario_id,produto_id,tipo,quantidade,validade_lote,observacao,data) "
                    "VALUES(?,?,'entrada',?,?,?,?)",
                    (uid, produto_id, quantidade, _data(validade_lote), obs, _agora().isoformat(sep=" ")))
        _sincronizar(con, produto_id)
        con.commit()
        return True, "Entrada registrada"
    except Exception as e:
        con.rollback()
        return False, str(e)
    finally:
        con.close()


# ================= VENCIDOS (baixa automática) =================
def baixar_vencidos(uid):
    """Tira do estoque todo lote com validade anterior a hoje e registra a perda no histórico.
    Devolve quantos lotes foram baixados. É chamada a cada carregamento de página."""
    hoje = _hoje().isoformat()
    sql = ("SELECT l.id, l.produto_id, l.quantidade, l.validade, p.preco FROM lotes l "
           "JOIN produtos p ON p.id=l.produto_id "
           "WHERE p.usuario_id=? AND l.validade IS NOT NULL AND l.validade < ?")
    con = conectar()
    try:
        if not con.execute(sql + " LIMIT 1", (uid, hoje)).fetchone():
            return 0  # caminho comum: nada vencido, não trava o banco
        con.execute("BEGIN IMMEDIATE")
        vencidos = con.execute(sql, (uid, hoje)).fetchall()
        agora = _agora().isoformat(sep=" ")
        for lote in vencidos:
            con.execute("INSERT INTO movimentacoes(usuario_id,produto_id,tipo,quantidade,validade_lote,observacao,"
                        "valor_unit,data) VALUES(?,?,'vencimento',?,?,?,?,?)",
                        (uid, lote["produto_id"], lote["quantidade"], _data(lote["validade"]),
                         "Baixa automática: lote vencido", _dinheiro(lote["preco"]), agora))
            con.execute("DELETE FROM lotes WHERE id=?", (lote["id"],))
        for produto_id in {lote["produto_id"] for lote in vencidos}:
            _sincronizar(con, produto_id)
        con.commit()
        return len(vencidos)
    except Exception:
        con.rollback()
        return 0
    finally:
        con.close()


# ================= VENDAS =================
def registrar_venda(uid, itens, obs=""):
    """itens: lista de (produto_id, quantidade). Baixa o estoque pelos lotes que vencem primeiro."""
    juntos = {}
    for produto_id, quantidade in itens:
        juntos[int(produto_id)] = juntos.get(int(produto_id), 0) + int(quantidade)
    if not juntos:
        return False, "Adicione pelo menos um produto à venda"
    if any(q <= 0 for q in juntos.values()):
        return False, "Quantidade deve ser maior que zero"
    baixar_vencidos(uid)  # garante que lote vencido não seja vendido
    con = conectar()
    try:
        con.execute("BEGIN IMMEDIATE")
        produtos = {}
        for produto_id, quantidade in juntos.items():
            p = con.execute("SELECT id, nome, quantidade, preco FROM produtos WHERE id=? AND usuario_id=?",
                            (produto_id, uid)).fetchone()
            if not p:
                con.rollback()
                return False, "Produto não encontrado"
            if p["quantidade"] < quantidade:
                con.rollback()
                return False, f"Estoque insuficiente de {p['nome']} (disponível: {p['quantidade']})"
            produtos[produto_id] = p

        agora = _agora().isoformat(sep=" ")
        total = sum((produtos[pid]["preco"] * q for pid, q in juntos.items()), Decimal("0"))
        venda_id = con.execute("INSERT INTO vendas(usuario_id,data,total,observacao) VALUES(?,?,?,?)",
                               (uid, agora, _dinheiro(total), obs or None)).lastrowid
        for produto_id, quantidade in juntos.items():
            preco = _dinheiro(produtos[produto_id]["preco"])
            falta = quantidade
            lotes = con.execute("SELECT id, quantidade FROM lotes WHERE produto_id=? "
                                "ORDER BY validade IS NULL, validade, id", (produto_id,)).fetchall()
            for lote in lotes:
                if not falta:
                    break
                tira = min(falta, lote["quantidade"])
                if tira == lote["quantidade"]:
                    con.execute("DELETE FROM lotes WHERE id=?", (lote["id"],))
                else:
                    con.execute("UPDATE lotes SET quantidade=quantidade-? WHERE id=?", (tira, lote["id"]))
                falta -= tira
            if falta:
                raise ValueError("lotes inconsistentes com o saldo do produto")
            con.execute("INSERT INTO venda_itens(venda_id,produto_id,quantidade,valor_unit) VALUES(?,?,?,?)",
                        (venda_id, produto_id, quantidade, preco))
            con.execute("INSERT INTO movimentacoes(usuario_id,produto_id,tipo,quantidade,observacao,valor_unit,"
                        "data,venda_id) VALUES(?,?,'saida',?,?,?,?,?)",
                        (uid, produto_id, quantidade, obs or None, preco, agora, venda_id))
            _sincronizar(con, produto_id)
        con.commit()
        return True, "Venda registrada"
    except Exception:
        con.rollback()
        return False, "Não foi possível registrar a venda"
    finally:
        con.close()


def listar_vendas(uid, busca=""):
    like = f"%{busca}%"
    vendas = _consulta(
        "SELECT v.id, v.data, v.total, v.observacao FROM vendas v WHERE v.usuario_id=? AND "
        "(v.observacao LIKE ? OR EXISTS (SELECT 1 FROM venda_itens i JOIN produtos p ON p.id=i.produto_id "
        "WHERE i.venda_id=v.id AND p.nome LIKE ?)) ORDER BY v.data DESC, v.id DESC LIMIT 200",
        (uid, like, like))
    if not vendas:
        return []
    ids = [v["id"] for v in vendas]
    itens = _consulta("SELECT i.venda_id, p.nome, i.quantidade, i.valor_unit FROM venda_itens i "
                      "JOIN produtos p ON p.id=i.produto_id "
                      f"WHERE i.venda_id IN ({','.join('?' * len(ids))}) ORDER BY i.id", ids)
    por_venda = {}
    for item in itens:
        por_venda.setdefault(item["venda_id"], []).append(item)
    for v in vendas:
        v["itens"] = por_venda.get(v["id"], [])
    return vendas


# ================= READ =================
def _consulta(sql, params):
    con = conectar()
    d = con.execute(sql, params).fetchall()
    con.close()
    return d


def listar_produtos(uid, busca=""):
    like = f"%{busca}%"
    return _consulta("SELECT * FROM produtos WHERE usuario_id=? AND (nome LIKE ? OR categoria LIKE ?) "
                     "ORDER BY nome", (uid, like, like))


def obter_produto(uid, produto_id):
    r = _consulta("SELECT * FROM produtos WHERE id=? AND usuario_id=?", (produto_id, uid))
    return r[0] if r else None


def listar_movimentacoes(uid, busca=""):
    like = f"%{busca}%"
    return _consulta("""
        SELECT m.id, p.nome, m.tipo, m.quantidade, m.validade_lote, m.observacao, m.data, m.valor_unit, m.venda_id
        FROM movimentacoes m JOIN produtos p ON p.id = m.produto_id
        WHERE p.usuario_id=? AND (p.nome LIKE ? OR m.tipo LIKE ?)
        ORDER BY m.data DESC, m.id DESC""", (uid, like, like))


def itens_baixos(uid):
    return _consulta("SELECT * FROM produtos WHERE usuario_id=? AND quantidade <= estoque_minimo "
                     "ORDER BY quantidade", (uid,))


def proximos_vencimento(uid, dias=DIAS_ALERTA_VALIDADE):
    hoje = _hoje().isoformat()
    return _consulta("SELECT *, CAST(julianday(validade) - julianday(?) AS INTEGER) AS dias FROM produtos "
                     "WHERE usuario_id=? AND validade IS NOT NULL AND quantidade > 0 "
                     "AND validade <= date(?, '+' || ? || ' days') ORDER BY validade",
                     (hoje, uid, hoje, int(dias)))


def resumo(uid):
    con = conectar()
    r = con.execute("SELECT COUNT(*) AS n_produtos, COALESCE(SUM(quantidade),0) AS unidades "
                    "FROM produtos WHERE usuario_id=?", (uid,)).fetchone()
    vendido = con.execute("SELECT COALESCE(SUM(total),0) AS v FROM vendas WHERE usuario_id=?",
                          (uid,)).fetchone()["v"]
    con.close()
    return {"produtos": r["n_produtos"], "unidades": int(r["unidades"]), "vendido": float(vendido),
            "baixos": len(itens_baixos(uid)), "vencendo": len(proximos_vencimento(uid))}
