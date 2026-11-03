"""Kanti - estoque e vendas para cantinas escolares. Streamlit: streamlit run streamlit_app.py."""
import base64
import logging
import re
from html import escape
from pathlib import Path
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import sqlite3

import streamlit as st
import streamlit.components.v1 as components

LOGO_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".svg": "image/svg+xml"}


def find_logo():
    """Procura logo.png/jpg/svg/webp ao lado do app (ou em assets/)."""
    base = Path(__file__).parent
    for folder in (base, base / "assets"):
        for ext in LOGO_MIME:
            path = folder / ("logo" + ext)
            if path.exists():
                return path
    return None


LOGO = find_logo()
st.set_page_config(page_title="Kanti", page_icon=str(LOGO) if LOGO and LOGO.suffix != ".svg" else "📦", layout="wide")

import database as db


@st.cache_resource(show_spinner=False)
def initialize_database():
    db.criar_banco()
    return True


def money(value):
    return f"R$ {Decimal(str(value or 0)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def brdate(value):
    return value.strftime("%d/%m/%Y") if value else "Sem validade"


def situation(product):
    if product["validade"] and product["quantidade"] > 0:
        days = (product["validade"] - today()).days
        if days < 0:
            return "🔴 Vencido"
        if days <= db.DIAS_ALERTA_VALIDADE:
            return f"🟡 Vence em {days} dia(s)"
    if product["quantidade"] <= product["estoque_minimo"]:
        return "🟡 Estoque baixo"
    return "🟢 Em estoque"


def notify(message, kind="success"):
    st.session_state["notice"] = (kind, message)


def reset_editor():
    st.session_state.pop("editor", None)


def open_editor(kind, pid=None):
    st.session_state["editor"] = {"kind": kind, "pid": pid}
    st.rerun()


def logout():
    st.session_state.clear()
    st.rerun()


def initials(name):
    words = name.split()
    return (words[0][0] + (words[-1][0] if len(words) > 1 else words[0][1:2])).upper() if words else "C"


def html(markup):
    st.markdown(markup, unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def logo_html(path):
    if not path:
        return '<span class="logo-square">K</span>'
    data = base64.b64encode(Path(path).read_bytes()).decode()
    return f'<img class="logo-img" src="data:{LOGO_MIME[Path(path).suffix]};base64,{data}" alt="Kanti">'


def today():
    return datetime.now(ZoneInfo("America/Sao_Paulo")).date()


def apply_style():
    css = Path(__file__).with_name("streamlit_style.css").read_text(encoding="utf-8")
    html("<style>" + css + "</style>")


def authentication(notice=None):
    register = st.session_state.get("auth_mode") == "register"
    with st.container(key="auth_card"):
        html('<div class="auth-brand"><div class="auth-logo">' + logo_html(LOGO) + '<span>Kanti</span></div>'
             '<p class="auth-tagline">Sua cantina, seu controle.</p></div>')
        if notice:
            getattr(st, notice[0])(notice[1])
        st.subheader("Criar sua conta" if register else "Bem-vindo de volta")
        st.caption("Cada conta tem o próprio estoque e histórico" if register else "Entre para acessar seu estoque")
        if not register:
            with st.form("login_form", clear_on_submit=True, border=False):
                username = st.text_input("Usuário", max_chars=50, key="login_user", placeholder="ex.: maria")
                password = st.text_input("Senha", type="password", max_chars=128, key="login_password", placeholder="••••••")
                submitted = st.form_submit_button("Entrar", type="primary", width="stretch")
            if submitted:
                user = db.autenticar(username, password) if username.strip() and password else None
                if user:
                    st.session_state["user"] = user
                    st.rerun()
                st.error("Usuário ou senha incorretos.")
        else:
            with st.form("register_form", clear_on_submit=True, border=False):
                name = st.text_input("Nome completo", max_chars=100, key="register_name", placeholder="Como você quer ser chamado")
                username = st.text_input("Usuário", max_chars=50, key="register_user", placeholder="ex.: maria")
                password = st.text_input("Nova senha", type="password", max_chars=128, help="Use pelo menos 8 caracteres.", key="register_password")
                confirmation = st.text_input("Confirmar senha", type="password", max_chars=128, key="register_confirmation")
                submitted = st.form_submit_button("Criar conta", type="primary", width="stretch")
            if submitted:
                if not name.strip() or not username.strip():
                    st.error("Preencha seu nome e nome de usuário.")
                elif len(password) < 8:
                    st.error("Use uma senha com pelo menos 8 caracteres.")
                elif password != confirmation:
                    st.error("As senhas não coincidem.")
                else:
                    ok, _ = db.registrar_usuario(name, username, password)
                    if ok:
                        st.session_state["auth_mode"] = "login"
                        notify("Conta criada. Entre para acessar seu estoque.")
                        st.rerun()
                    st.error("Não foi possível criar a conta. Tente outro nome de usuário.")
        with st.container(key="auth_footer"):
            texto = "Já tem conta? **Entrar**" if register else "Não tem conta? **Criar registro**"
            if st.button(texto, key="toggle_auth", use_container_width=True):
                st.session_state["auth_mode"] = "login" if register else "register"
                st.rerun()


def sidebar(user):
    summary = db.resumo(user["id"])
    with st.sidebar, st.container(key="sidebar_shell"):
        html(f'<div class="profile"><span class="profile-avatar">{escape(initials(user["nome"]))}</span>'
             f'<div><strong>{escape(user["nome"].split()[0].upper())}</strong>'
             f'<small>@{escape(user["usuario"])}</small></div></div>')
        cards = ""
        for symbol, label, value, color in [("▦", "PRODUTOS", str(summary["produtos"]), "purple"),
                                           ("↓", "ESTOQUE BAIXO", str(summary["baixos"]), "teal"),
                                           ("$", "VENDIDO", money(summary["vendido"]), "rose")]:
            cards += f'<div class="sidebar-stat"><span class="stat-symbol {color}">{symbol}</span><div><small>{label}</small><strong>{escape(value)}</strong></div></div>'
        html('<div class="sidebar-stats">' + cards + '</div>')
        labels = {"Visão geral": "⌂     INÍCIO", "Produtos": "▢     PRODUTOS",
                  "Vendas": "$     VENDAS", "Movimentações": "⇅     MOVIMENTAÇÕES",
                  "Minha conta": "⚙     CONFIGURAÇÕES"}
        page = st.radio("Navegação", ["Visão geral", "Produtos", "Vendas", "Movimentações", "Minha conta"],
                        key="page", on_change=reset_editor, label_visibility="collapsed", format_func=labels.get)
        with st.container(key="sidebar_logout"):
            if st.button("Sair da conta", width="stretch", key="logout"):
                logout()
    return page


def go_to(page):
    st.session_state["page"] = page
    reset_editor()


def home(user):
    uid = user["id"]
    data, summary = db.painel(uid), db.resumo(uid)
    hour = datetime.now(ZoneInfo("America/Sao_Paulo")).hour
    greeting = "Bom dia" if hour < 12 else "Boa tarde" if hour < 18 else "Boa noite"
    head, action = st.columns([6, 1.6], vertical_alignment="center")
    with head:
        html(f'<div class="page-heading"><h1>{greeting}, {escape(user["nome"].split()[0])}</h1>'
             '<p>Resumo da sua cantina</p></div>')
    action.button("+  Nova venda", type="primary", key="home_new_sale", width="stretch", on_click=go_to, args=("Vendas",))

    kpis = [("$", "teal", "VENDAS HOJE", money(data["hoje_valor"]), f'{data["hoje_qtd"]} venda(s)'),
            ("▦", "purple", "VALOR EM ESTOQUE", money(data["estoque_valor"]), f'{data["unidades"]} unidade(s)'),
            ("!", "amber", "PERDAS (30 DIAS)", money(data["perda_valor"]), f'{data["perda_unidades"]} unidade(s) vencida(s)'),
            ("↓", "rose", "PEDEM ATENÇÃO", str(summary["baixos"] + summary["vencendo"]),
             f'{summary["baixos"]} em baixa · {summary["vencendo"]} vencendo')]
    for column, (symbol, color, label, value, sub) in zip(st.columns(4, gap="small"), kpis):
        with column:
            html(f'<div class="kpi"><span class="stat-symbol {color}">{symbol}</span><div><small>{label}</small>'
                 f'<strong>{escape(value)}</strong><p>{escape(sub)}</p></div></div>')

    left, right = st.columns([1.5, 1], gap="large")
    with left:
        days = [today() - timedelta(days=i) for i in range(6, -1, -1)]
        values = [data["por_dia"].get(day.isoformat(), (0, 0))[0] for day in days]
        peak = max(values) or 1
        weekdays = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
        bars = ""
        for day, value in zip(days, values):
            height = max(value / peak * 100, 2)
            now = " today" if day == today() else ""
            label = money(value).replace("R$ ", "") if value else "–"
            bars += (f'<div class="bar-col"><span class="bar-value">{label}</span><div class="bar-track">'
                     f'<div class="bar{now}" style="height:{height:.0f}%"></div></div>'
                     f'<small>{weekdays[day.weekday()]} {day.strftime("%d")}</small></div>')
        html(f'<div class="account-card"><div class="sale-head"><h2>Vendas dos últimos 7 dias</h2>'
             f'<strong>{money(sum(values))}</strong></div><div class="bars">{bars}</div></div>')

        recent = db.listar_vendas(uid)[:5]
        rows = "".join(f'<div class="alert-item"><span>Venda #{v["id"]} · {v["data"].strftime("%d/%m %H:%M")} · '
                       f'{escape(", ".join(i["nome"] for i in v["itens"][:2]))}{"…" if len(v["itens"]) > 2 else ""}</span>'
                       f'<strong>{money(v["total"])}</strong></div>' for v in recent)
        html('<div class="account-card"><h2>Últimas vendas</h2>' + (rows or '<p>Nenhuma venda ainda. Use “Nova venda” para começar.</p>') + '</div>')
    with right:
        low = db.itens_baixos(uid)[:5]
        rows = "".join(f'<div class="alert-item"><span>{escape(p["nome"])}</span>'
                       f'<strong>{p["quantidade"]} de {p["estoque_minimo"]} un.</strong></div>' for p in low)
        html(f'<div class="account-card"><h2 class="text-rose">Estoque baixo ({summary["baixos"]})</h2>' +
             (rows or '<p>Nenhum item abaixo do mínimo.</p>') + '</div>')
        expiring = db.proximos_vencimento(uid)[:5]
        rows = "".join(f'<div class="alert-item"><span>{escape(p["nome"])}</span><strong>' +
                       ("vence hoje" if p["dias"] <= 0 else f'em {p["dias"]} dia(s)') + f' · {brdate(p["validade"])}</strong></div>'
                       for p in expiring)
        html(f'<div class="account-card"><h2 class="text-amber">Vencendo em {db.DIAS_ALERTA_VALIDADE} dias ({summary["vencendo"]})</h2>' +
             (rows or '<p>Nada perto de vencer.</p>') + '</div>')
        rows = "".join(f'<div class="alert-item"><span>{escape(p["nome"])}</span><strong>{p["unidades"]} un.</strong></div>'
                       for p in data["mais_vendidos"])
        html('<div class="account-card"><h2>Mais vendidos (30 dias)</h2>' +
             (rows or '<p>Sem vendas nos últimos 30 dias.</p>') + '</div>')


def catalog(user):
    uid = user["id"]
    search, add = st.columns([8, 1.5], vertical_alignment="center")
    query = search.text_input("Buscar por nome ou categoria", placeholder="⌕   Buscar por nome ou categoria...",
                              max_chars=100, key="product_search", label_visibility="collapsed")
    if add.button("+  Novo", type="primary", key="new_product", width="stretch"):
        open_editor("new")
    html('<div class="page-heading"><h1>Produtos</h1><p>Gerencie cadastro e entradas de estoque</p></div>')
    products = db.listar_produtos(uid, query.strip())
    if not products:
        html('<div class="empty-state"><h3>Nenhum produto encontrado</h3><p>' +
             ('Tente outro termo.' if query else 'Clique em “+ Novo” para cadastrar o primeiro item.') + '</p></div>')
        if not query and st.button("Cadastrar primeiro produto", key="first_product"):
            open_editor("new")
        return
    for product in products:
        status = situation(product)
        normal = status.startswith("🟢")
        color = "danger" if status.startswith("🔴") or "Estoque baixo" in status else "warning"
        badge = '' if normal else f'<span class="status-pill {color}">{escape(status.split(" ", 1)[1])}</span>'
        with st.container(key=f'product_row_{product["id"]}'):
            info, entry, edit, expiry, quantity = st.columns([4.4, 1.4, 1.4, 1.6, .8], gap="small", vertical_alignment="center")
            with info:
                html(f'<div class="product-info {"" if normal else "alert-" + color}"><span class="product-avatar">{escape(initials(product["nome"]))}</span>'
                     f'<div><h2>{escape(product["nome"].upper())}</h2><p>{escape(product["categoria"])} · {money(product["preco"])} {badge}</p></div></div>')
            if entry.button("Entrada", key=f'entry_{product["id"]}', width="stretch"):
                open_editor("entry", product["id"])
            if edit.button("Editar", key=f'edit_{product["id"]}', width="stretch"):
                open_editor("edit", product["id"])
            with expiry:
                date_label = product["validade"].strftime("%d/%m/%y") if product["validade"] else "--/--/--"
                html(f'<div class="date-chip {color if "Venc" in status else ""}"><span>▣</span> {date_label}</div>')
            with quantity:
                html(f'<div class="quantity-chip {"danger" if product["quantidade"] <= product["estoque_minimo"] else ""}">{product["quantidade"]}</div>')


PRICE_MASK_JS = r"""
<script>
const doc = window.parent.document;
const setter = Object.getOwnPropertyDescriptor(window.parent.HTMLInputElement.prototype, 'value').set;
const fmt = v => { let d = v.replace(/\D/g, '').replace(/^0+/, '').slice(0, 10); while (d.length < 3) d = '0' + d; return d.slice(0, -2) + ',' + d.slice(-2); };
function bind() {
  doc.querySelectorAll('input[aria-label="Preço de venda (R$)"]').forEach(el => {
    if (el.dataset.mask) return;
    el.dataset.mask = '1';
    el.setAttribute('inputmode', 'numeric');
    el.addEventListener('input', () => {
      const f = fmt(el.value);
      if (el.value !== f) { setter.call(el, f); el.dispatchEvent(new Event('input', {bubbles: true})); }
    });
  });
}
bind(); setInterval(bind, 300);
</script>
"""


def product_editor(uid, product):
    st.title("Editar produto" if product else "Novo produto")
    if not product:
        st.caption("O estoque começa em zero. Registre uma entrada depois do cadastro; a validade é informada em cada entrada.")
    suffix = str(product["id"]) if product else "new"
    with st.container(key="price_mask"):
        components.html(PRICE_MASK_JS, height=0)
    with st.form("product_form_" + suffix):
        name = st.text_input("Nome do produto", value=product["nome"] if product else "", max_chars=100, key="product_name_" + suffix)
        category = st.text_input("Categoria", value=product["categoria"] if product else "", max_chars=50, key="product_category_" + suffix)
        left, right = st.columns(2)
        price_text = left.text_input("Preço de venda (R$)", value=f'{product["preco"]:.2f}'.replace(".", ",") if product else "0,00",
                                     max_chars=12, key="product_price_" + suffix, placeholder="00,00",
                                     help="Digite só os números: 5000 vira 50,00.")
        minimum = right.number_input("Estoque mínimo", min_value=0, max_value=2147483647,
                                     value=product["estoque_minimo"] if product else 0, step=1, key="product_minimum_" + suffix)
        submitted = st.form_submit_button("Salvar produto", type="primary")
    if submitted:
        try:
            if not re.fullmatch(r"\d{1,8},\d{2}", price_text.strip()):
                raise ValueError
            price = Decimal(price_text.strip().replace(",", "."))
            if not price.is_finite() or price < 0 or price > Decimal("99999999.99") or price != price.quantize(Decimal("0.01")):
                raise ValueError
            if not name.strip() or not category.strip():
                raise ValueError
        except (ValueError, InvalidOperation):
            st.error("Preencha nome e categoria. O preço deve estar no formato 00,00 (ex.: 50,00).")
            return
        if product:
            db.editar_produto(uid, product["id"], name.strip(), category.strip(), minimum, price)
        else:
            db.cadastrar_produto(uid, name.strip(), category.strip(), minimum, price)
        reset_editor()
        notify("Produto salvo.")
        st.rerun()
    if product:
        st.divider()
        st.caption("Produtos com movimentações são preservados no histórico.")
        if st.button("Excluir produto", key="delete_product"):
            open_editor("delete", product["id"])


def entry_editor(uid, product):
    st.title("Registrar entrada")
    st.subheader(product["nome"])
    st.caption(f'{product["quantidade"]} unidade(s) em estoque · Preço: {money(product["preco"])}')
    with st.form(f'movement_entry_{product["id"]}'):
        quantity = st.number_input("Quantidade", min_value=1, max_value=2147483647, value=1, step=1, key="movement_quantity")
        expiry = st.date_input("Validade do lote (opcional)", value=None, min_value=date(1900, 1, 1), max_value=date(2200, 12, 31),
                               format="DD/MM/YYYY", key="movement_expiry",
                               help="Com a validade informada, o lote sai do estoque sozinho quando vencer.")
        note = st.text_area("Observação (opcional)", max_chars=200, key="movement_note")
        submitted = st.form_submit_button("Confirmar entrada", type="primary")
    if submitted:
        if expiry and expiry < today():
            st.error("Esse lote já está vencido. Confira a data de validade.")
            return
        # Releia a propriedade e o saldo antes de qualquer gravação.
        current = db.obter_produto(uid, product["id"])
        if not current:
            st.error("Produto não encontrado nesta conta.")
            return
        if current["quantidade"] + quantity > 2147483647:
            st.error("Quantidade acima do limite permitido.")
            return
        ok, _ = db.registrar_entrada(uid, product["id"], quantity, expiry, note)
        if not ok:
            st.error("Não foi possível registrar. Confira os dados e tente novamente.")
            return
        reset_editor()
        notify("Entrada registrada.")
        st.rerun()


def editor(uid, state):
    if st.button("← Voltar ao estoque", key="cancel_editor"):
        reset_editor()
        st.rerun()
    kind = state["kind"]
    product = db.obter_produto(uid, state["pid"]) if kind != "new" else None
    if kind != "new" and not product:
        st.error("Produto não encontrado nesta conta.")
        return
    if kind in {"new", "edit"}:
        product_editor(uid, product)
    elif kind == "entry":
        entry_editor(uid, product)
    elif kind == "delete":
        st.title("Excluir produto")
        st.write(product["nome"])
        with st.form("delete_form"):
            confirm = st.checkbox("Confirmo a exclusão deste produto", key="delete_confirm")
            submitted = st.form_submit_button("Confirmar exclusão", type="primary")
        if submitted:
            if not confirm:
                st.error("Marque a confirmação para excluir.")
                return
            ok, _ = db.excluir_produto(uid, product["id"])
            if ok:
                reset_editor()
                notify("Produto excluído.")
                st.rerun()
            st.error("Produto com movimentações não pode ser excluído.")


def sales(uid):
    html('<div class="page-heading"><h1>Vendas</h1><p>Registre o que foi vendido. O estoque baixa sozinho, começando pelo lote que vence primeiro.</p></div>')
    new_tab, history_tab = st.tabs(["Nova venda", "Histórico de vendas"])
    with new_tab:
        new_sale(uid)
    with history_tab:
        sales_history(uid)


def new_sale(uid):
    cart = st.session_state.setdefault("cart", {})
    products = {p["id"]: p for p in db.listar_produtos(uid)}
    available = [pid for pid, p in products.items() if p["quantidade"] > 0]
    if not available:
        html('<div class="empty-state"><h3>Nenhum produto em estoque</h3><p>Registre uma entrada na página Produtos para poder vender.</p></div>')
        return
    total, count = Decimal("0"), 0
    with st.container(key="sale_wrap"):
        with st.container(key="sale_add"):
            with st.form("sale_add_form", clear_on_submit=True, border=False):
                pick, qty, add = st.columns([4, 1.4, 1.6], vertical_alignment="bottom")
                pid = pick.selectbox("Produto", available, index=None, placeholder="Escolha o produto...",
                                     format_func=lambda i: f'{products[i]["nome"]} · {money(products[i]["preco"])} · {products[i]["quantidade"]} em estoque')
                quantity = qty.number_input("Quantidade", min_value=1, max_value=2147483647, value=1, step=1)
                added = add.form_submit_button("+  Adicionar", type="primary", width="stretch")
        if added:
            if pid is None:
                st.error("Escolha um produto para adicionar.")
            elif cart.get(pid, 0) + quantity > products[pid]["quantidade"]:
                st.error(f'Estoque insuficiente: há {products[pid]["quantidade"]} unidade(s) de {products[pid]["nome"]}.')
            else:
                cart[pid] = cart.get(pid, 0) + quantity
                st.rerun()
        for item_id, item_qty in list(cart.items()):
            product = products.get(item_id)
            if not product:
                cart.pop(item_id)
                continue
            subtotal = product["preco"] * item_qty
            total += subtotal
            count += item_qty
            with st.container(key=f"cart_row_{item_id}"):
                info, amount, remove = st.columns([3.2, 1.4, 1.2], gap="small", vertical_alignment="center")
                with info:
                    html(f'<div class="cart-info"><h2>{escape(product["nome"].upper())}</h2><p>{item_qty} × {money(product["preco"])}</p></div>')
                with amount:
                    html(f'<div class="cart-subtotal">{money(subtotal)}</div>')
                if remove.button("Remover", key=f"cart_remove_{item_id}", width="stretch"):
                    cart.pop(item_id)
                    st.rerun()
        if not cart:
            html('<div class="empty-state compact"><h3>Nenhum item na venda</h3><p>Escolha um produto acima e clique em Adicionar.</p></div>')
    with st.container(key="sale_summary"):
        html(f'<div class="sale-total"><span>Total da venda<br><small>{count} item(ns)</small></span><strong>{money(total)}</strong></div>')
        note = st.text_input("Observação (opcional)", max_chars=200, key=f'sale_note_{st.session_state.get("sale_round", 0)}',
                             placeholder="ex.: cliente, forma de pagamento")
        finish, clear = st.columns([3, 1], gap="small")
        if finish.button("Finalizar venda", type="primary", key="finish_sale", width="stretch", disabled=not cart):
            ok, message = db.registrar_venda(uid, list(cart.items()), note.strip())
            if not ok:
                st.error(message)
                return
            st.session_state["cart"] = {}
            st.session_state["sale_round"] = st.session_state.get("sale_round", 0) + 1
            notify(f"Venda registrada: {money(total)}.")
            st.rerun()
        if clear.button("Limpar", key="clear_sale", width="stretch", disabled=not cart):
            st.session_state["cart"] = {}
            st.rerun()


def sales_history(uid):
    query = st.text_input("Buscar venda", placeholder="⌕   Buscar por produto ou observação...", max_chars=100,
                          key="sales_search", label_visibility="collapsed")
    records = db.listar_vendas(uid, query.strip())
    if not records:
        html('<div class="empty-state"><h3>Nenhuma venda encontrada</h3><p>' +
             ('Tente outro termo.' if query else 'As vendas registradas aparecem aqui.') + '</p></div>')
        return
    for sale in records:
        items = "".join(f'<div class="alert-item"><span>{escape(item["nome"])} × {item["quantidade"]}</span>'
                        f'<strong>{money(item["quantidade"] * item["valor_unit"])}</strong></div>' for item in sale["itens"])
        note = f'<p class="sale-note">{escape(sale["observacao"])}</p>' if sale["observacao"] else ''
        html(f'<div class="account-card"><div class="sale-head"><h2>Venda #{sale["id"]}</h2>'
             f'<span>{sale["data"].strftime("%d/%m/%Y  %H:%M")}</span><strong>{money(sale["total"])}</strong></div>{items}{note}</div>')


MOVEMENT_KINDS = {"entrada": ("teal", "↓", "+"), "saida": ("rose", "↑", "−"), "vencimento": ("amber", "!", "−")}


def movements(uid):
    query = st.text_input("Buscar produto ou tipo", placeholder="⌕   Buscar movimentação...", max_chars=100,
                          key="movement_search", label_visibility="collapsed")
    html('<div class="page-heading"><h1>Movimentações</h1><p>Entradas, vendas e baixas automáticas de lotes vencidos</p></div>')
    records = db.listar_movimentacoes(uid, query.strip())
    if not records:
        html('<div class="empty-state"><h3>Sem movimentações</h3><p>Entradas, vendas e vencimentos aparecem aqui.</p></div>')
        return
    for row in records:
        kind = row["tipo"]
        color, icon, sign = MOVEMENT_KINDS[kind]
        label = {"entrada": "Entrada", "saida": f'Venda #{row["venda_id"]}' if row["venda_id"] else "Saída",
                 "vencimento": "Vencido · baixa automática"}[kind]
        details = [label, row["data"].strftime("%d/%m/%Y  %H:%M")]
        if row["validade_lote"]:
            details.append(("venceu em " if kind == "vencimento" else "lote vence ") + brdate(row["validade_lote"]))
        if row["observacao"] and kind != "vencimento":
            details.append(row["observacao"])
        value = money(row["quantidade"] * row["valor_unit"])
        total = value if kind == "saida" else ("Perda " + value if kind == "vencimento" else "")
        html(f'<div class="movement-row"><span class="movement-icon {color}">{icon}</span>'
             f'<div class="movement-info"><h2>{escape(row["nome"].upper())}</h2><p>{escape(" · ".join(details))}</p></div>'
             f'<span class="movement-total">{total}</span><strong class="movement-quantity {color}">{sign}{row["quantidade"]}</strong></div>')


def account(user):
    html('<div class="page-heading"><h1>Configurações</h1><p>Conta, alertas e conexão</p></div>')
    html(f'<div class="account-card"><h2>Minha conta</h2><p>{escape(user["nome"])} · @{escape(user["usuario"])}</p></div>')
    with st.expander("Alterar senha"), st.form("password_form", clear_on_submit=True, border=False):
        st.subheader("Alterar senha")
        current = st.text_input("Senha atual", type="password", max_chars=128, key="current_password")
        password = st.text_input("Nova senha", type="password", max_chars=128, key="new_password")
        confirmation = st.text_input("Confirmar nova senha", type="password", max_chars=128, key="new_password_confirmation")
        submitted = st.form_submit_button("Salvar nova senha", type="primary")
    if submitted:
        if len(password) < 8:
            st.error("Use uma senha com pelo menos 8 caracteres.")
        elif password != confirmation:
            st.error("As senhas não coincidem.")
        else:
            ok, _ = db.alterar_senha(user["id"], current, password)
            if ok:
                st.session_state.clear()
                notify("Senha alterada. Entre novamente.")
                st.rerun()
            st.error("Não foi possível alterar. Confira a senha atual.")
    for title, items, color in [("Abaixo do estoque mínimo", db.itens_baixos(user["id"]), "rose"),
                                 (f"Vencendo em até {db.DIAS_ALERTA_VALIDADE} dias", db.proximos_vencimento(user["id"]), "amber")]:
        rows = ''
        for product in items:
            detail = f'{product["quantidade"]} de {product["estoque_minimo"]} un.' if color == "rose" else brdate(product["validade"])
            rows += f'<div class="alert-item"><span>{escape(product["nome"])}</span><strong>{escape(detail)}</strong></div>'
        html(f'<div class="account-card"><h2 class="text-{color}">{title} ({len(items)})</h2>' +
             (rows or '<p>Nenhum item — tudo certo.</p>') + '</div>')
    html('<div class="account-card"><h2>Banco de dados</h2><p>SQLite · arquivo local · conectado</p></div>')


def main():
    apply_style()
    with st.spinner("Conectando ao banco do Kanti…"):
        initialize_database()
    notice = st.session_state.pop("notice", None)
    user = st.session_state.get("user")
    if not user:
        authentication(notice)
        return
    if notice:
        getattr(st, notice[0])(notice[1])
    expired = db.baixar_vencidos(user["id"])
    if expired:
        st.toast(f"{expired} lote(s) vencido(s) saíram do estoque. Veja em Movimentações.", icon="⚠️")
    page = sidebar(user)
    state = st.session_state.get("editor")
    if state:
        editor(user["id"], state)
    elif page == "Visão geral":
        home(user)
    elif page == "Produtos":
        catalog(user)
    elif page == "Vendas":
        sales(user["id"])
    elif page == "Movimentações":
        movements(user["id"])
    else:
        account(user)


def show_connection_error(error):
    # Não exiba a exceção original: mensagens do driver podem conter detalhes internos.
    logging.getLogger(__name__).error("Falha ao abrir o Kanti: tipo=%s", type(error).__name__)
    if isinstance(error, FileNotFoundError) and Path(error.filename or "").name == "streamlit_style.css":
        message = "Falta streamlit_style.css. Envie esse arquivo ao GitHub, ao lado de streamlit_app.py."
    else:
        message = "Não foi possível abrir o banco de dados SQLite (cantina.db). Reinicie o app e tente novamente."
    st.error(message)


if __name__ == "__main__":
    try:
        main()
    except (sqlite3.Error, OSError) as error:
        show_connection_error(error)
        if st.button("Tentar novamente", key="retry_database"):
            st.rerun()
