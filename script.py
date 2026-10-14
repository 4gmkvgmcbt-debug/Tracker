import json
import os
import asyncio
import re
import random
import string
import time
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from telebot.async_telebot import AsyncTeleBot

# --- CONFIGURAÇÕES VIA GITHUB SECRETS ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "SEU_TOKEN_AQUI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "484947413")
WISHLIST_URL = "https://www.amazon.com.br/hz/wishlist/ls/JEVF1U57APZQ"

# Proxy de Fallback (só é usado se a Amazon bloquear o IP do servidor do GitHub)
PROXY_USER = "flm10a5059e-package-standard"
PROXY_PASS = "0d2353b7"

ARQUIVO_HISTORICO = "historico_precos.json"
ARQUIVO_ESTADO_BOT = "estado_bot.json"
QUEDA_MINIMA_ALERTA = 1.0
TEMPO_EXECUCAO_ACTION = 260  # Roda por 4m20s dentro de cada janela de 5 min do GitHub

bot = AsyncTeleBot(TELEGRAM_TOKEN)

def carregar_json(caminho):
    if os.path.exists(caminho):
        try:
            with open(caminho, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def salvar_json(caminho, dados):
    caminho_temp = f"{caminho}.tmp"
    with open(caminho_temp, 'w', encoding='utf-8') as f:
        json.dump(dados, f, indent=4, ensure_ascii=False)
    os.replace(caminho_temp, caminho)

async def requisitar_wishlist():
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1"
    }
    url_fresca = f"{WISHLIST_URL}?_={int(time.time())}"

    # 1ª Tentativa: Direto pelo IP do GitHub (Custo Zero de Proxy)
    try:
        async with AsyncSession(impersonate="chrome120") as session:
            resp = await session.get(url_fresca, headers=headers, timeout=15)
            if resp.status_code == 200 and "captcha" not in resp.text.lower() and 'id="g-items"' in resp.text:
                return resp.text, "Direto (GitHub IP)"
    except Exception:
        pass

    # 2ª Tentativa (Fallback): Usa FlameProxies se o IP do GitHub tomar bloqueio da Amazon
    for tentativa in range(2):
        sessao_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        proxy_url = f"http://{PROXY_USER}_session-{sessao_id}:{PROXY_PASS}@proxy.flameproxies.com:8989"
        proxies = {"http": proxy_url, "https": proxy_url}
        try:
            async with AsyncSession(impersonate="chrome120", proxies=proxies) as session:
                resp = await session.get(url_fresca, headers=headers, timeout=20)
                if resp.status_code == 200 and "captcha" not in resp.text.lower() and 'id="g-items"' in resp.text:
                    return resp.text, f"Proxy ({sessao_id})"
        except Exception:
            await asyncio.sleep(2)

    return None, "Falha/WAF"

async def processar_comandos_telegram(dados_atuais):
    """Lê comandos /status ou /listar enviados enquanto o Action estava rodando"""
    estado = carregar_json(ARQUIVO_ESTADO_BOT)
    last_update_id = estado.get("last_update_id", 0)
    try:
        updates = await bot.get_updates(offset=last_update_id + 1, timeout=1)
        for u in updates:
            last_update_id = max(last_update_id, u.update_id)
            if u.message and u.message.text:
                txt = u.message.text.strip().lower()
                if txt in ["/status", "/listar", "/start"]:
                    await enviar_relatorio(dados_atuais, forcar_novo=True)
        estado["last_update_id"] = last_update_id
        salvar_json(ARQUIVO_ESTADO_BOT, estado)
    except Exception:
        pass

async def enviar_relatorio(dados_atuais, forcar_novo=False):
    if not dados_atuais:
        return
    estado = carregar_json(ARQUIVO_ESTADO_BOT)
    id_msg_antiga = estado.get("id_mensagem_relatorio")

    linhas = [f"☁️ **Radar GitHub Actions Ativo ({len(dados_atuais)} itens)**"]
    for idx, (asin, info) in enumerate(dados_atuais.items()):
        preco = info['preco']
        alvo = info['alvo']
        url = info['url']
        txt_preco = f"💰 *R$ {preco:.2f}*" if preco > 0 else "🔴 *ESGOTADO*"
        linhas.append(f"**{idx+1}.** [{info['titulo'][:28]}...]({url}) ➔ {txt_preco} _(Alvo: {alvo:.0f})_")

    texto = "\n".join(linhas)

    try:
        if id_msg_antiga and not forcar_novo:
            try:
                await bot.delete_message(TELEGRAM_CHAT_ID, id_msg_antiga)
            except Exception:
                pass
        msg = await bot.send_message(TELEGRAM_CHAT_ID, texto, parse_mode='Markdown', disable_web_page_preview=True)
        estado["id_mensagem_relatorio"] = msg.message_id
        salvar_json(ARQUIVO_ESTADO_BOT, estado)
    except Exception as e:
        print(f"Erro ao enviar relatório: {e}")

async def varrer_wishlist():
    historico = carregar_json(ARQUIVO_HISTORICO)
    html, origem = await requisitar_wishlist()

    if not html:
        print(f"[{time.strftime('%H:%M:%S')}] ⚠️ Não foi possível ler a Wishlist ({origem}).")
        return {}

    soup = BeautifulSoup(html, 'html.parser')
    itens_html = soup.select('li[data-itemid]')
    novos_dados = {}
    alterou_historico = False

    for item in itens_html:
        param_str = item.get('data-reposition-action-params', '')
        match_asin = re.search(r'ASIN:([A-Z0-9]{10})', param_str)
        item_id = item.get('data-itemid', '')
        nome_elem = item.select_one(f'#itemName_{item_id}') or item.select_one('a[id^="itemName_"]')

        if not match_asin and nome_elem and nome_elem.get('href'):
            match_asin = re.search(r'/dp/([A-Z0-9]{10})', nome_elem['href'])
        if not match_asin:
            continue

        asin = match_asin.group(1)
        url_produto = f"https://www.amazon.com.br/dp/{asin}"
        url_carrinho = f"https://www.amazon.com.br/gp/aws/cart/add.html?ASIN.1={asin}&Quantity.1=1"
        titulo = nome_elem.text.strip() if nome_elem else f"Produto {asin}"

        # Extração blindada de preço da Wishlist
        preco_atual = 0.0
        dp = item.get('data-price')
        if dp:
            try:
                val = float(dp)
                if val >= 30.0:  # Ignora centavos/erros
                    preco_atual = val
            except ValueError:
                pass

        if preco_atual == 0.0:
            preco_span = item.select_one('.a-price .a-offscreen')
            if preco_span:
                m_preco = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2})', preco_span.text)
                if m_preco:
                    try:
                        val = float(m_preco.group(1).replace('.', '').replace(',', '.'))
                        if val >= 30.0:
                            preco_atual = val
                    except ValueError:
                        pass

        # Preço Alvo lido do comentário da Wishlist
        alvo = 0.0
        comentario_elem = item.select_one(f'#itemComment_{item_id}') or item.select_one('span[id^="itemComment_"]')
        if comentario_elem and comentario_elem.text:
            m_alvo = re.search(r'(\d+(?:[.,]\d+)?)', comentario_elem.text)
            if m_alvo:
                try:
                    alvo = float(m_alvo.group(1).replace(',', '.'))
                except ValueError:
                    pass

        novos_dados[asin] = {"titulo": titulo, "preco": preco_atual, "alvo": alvo, "url": url_produto}
        ultimo_registro = historico.get(asin, {}).get("preco", 0.0)
        queda_pct = ((ultimo_registro - preco_atual) / ultimo_registro) * 100 if (ultimo_registro > 0 and preco_atual > 0) else 0.0

        # Lógica de Alertas Instantâneos
        msg_alerta = None
        rodape_links = f"⚡ [🛒 ADICIONAR DIRETO AO CARRINHO]({url_carrinho})\n🔗 [Abrir na Amazon]({url_produto})"

        if preco_atual > 0 and alvo > 0 and preco_atual <= alvo and (ultimo_registro > alvo or ultimo_registro == 0.0):
            msg_alerta = f"🎯 **ALVO ATINGIDO!**\n📦 {titulo[:65]}\n💰 Preço: *R$ {preco_atual:.2f}* (Alvo: R$ {alvo:.2f})\n\n{rodape_links}"
        elif ultimo_registro == 0.0 and preco_atual > 0:
            msg_alerta = f"🟢 **VOLTOU AO ESTOQUE (BUYBOX)!**\n📦 {titulo[:65]}\n💰 Preço: *R$ {preco_atual:.2f}* (Alvo: R$ {alvo:.2f})\n\n{rodape_links}"
        elif preco_atual > 0 and ultimo_registro > 0 and queda_pct >= QUEDA_MINIMA_ALERTA:
            msg_alerta = f"📉 **QUEDA DE PREÇO!**\n📦 {titulo[:65]}\n💰 Novo: *R$ {preco_atual:.2f}* (Antes: R$ {ultimo_registro:.2f})\n\n{rodape_links}"
        elif ultimo_registro > 0.0 and preco_atual == 0.0:
            msg_alerta = f"🔴 **ESGOTOU NA BUYBOX!**\n📦 {titulo[:65]}\n🔗 [Conferir Produto]({url_produto})"

        if msg_alerta:
            try:
                await bot.send_message(TELEGRAM_CHAT_ID, msg_alerta, parse_mode='Markdown', disable_web_page_preview=True)
            except Exception as e:
                print(f"Erro ao enviar alerta Telegram: {e}")
                continue

        if preco_atual != ultimo_registro or asin not in historico:
            historico[asin] = {"titulo": titulo, "preco": preco_atual, "alvo": alvo}
            alterou_historico = True

    if alterou_historico:
        salvar_json(ARQUIVO_HISTORICO, historico)

    ativos = sum(1 for x in novos_dados.values() if x['preco'] > 0)
    print(f"[{time.strftime('%H:%M:%S')}] Via {origem} | {len(novos_dados)} itens ({ativos} c/ BuyBox)")
    return novos_dados

async def main():
    inicio = time.time()
    ultimo_relatorio = 0
    dados_atuais = {}

    while (time.time() - inicio) < TEMPO_EXECUCAO_ACTION:
        dados = await varrer_wishlist()
        if dados:
            dados_atuais = dados

        # Responde a comandos manuais (/status ou /listar) enviados no Telegram
        await processar_comandos_telegram(dados_atuais)

        # Atualiza o painel silencioso a cada ~2 minutos
        if dados_atuais and (time.time() - ultimo_relatorio) >= 120:
            await enviar_relatorio(dados_atuais)
            ultimo_relatorio = time.time()

        await asyncio.sleep(random.uniform(22.0, 28.0))

    await bot.close_session()

if __name__ == "__main__":
    asyncio.run(main())
