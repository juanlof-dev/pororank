import discord
import aiohttp
import os
import re
import random
import threading
import asyncio
import time
import collections
import io
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
import math
from discord.ext import commands, tasks
from discord.ui import View, Modal, TextInput, Select
from flask import Flask

from config import *
from database import (
    load_data, save_data, init_db,
    has_linked_before, get_unlinked_tracking, get_all_unlinked_tracking,
    start_unlinked_tracking, stop_unlinked_tracking, mark_reminder_sent,
    create_group, get_group, get_all_groups, delete_group, mark_group_warned,
    clear_group_voice_channel,
)

# ------------------ BOT ------------------

intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # necesario si en el futuro usas comandos con prefijo "!"
intents.presences = True  # necesario para /online (saber quién está conectado)
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------ ESTILO ------------------
# Paleta de colores compartida por todos los embeds del bot.
COLOR_PANEL = 0x9B59B6        # 🟣 Morado — acciones principales / paneles
COLOR_GROUP_OPEN = 0x2ECC71   # 🟢 Verde — búsqueda/grupo activo
COLOR_GROUP_CLOSED = 0xE74C3C # 🔴 Rojo — cerrado/error
COLOR_VERIFICATION = 0xF1C40F # 🟡 Dorado — verificación
COLOR_ACCOUNT_INFO = 0x2C2F33 # ⚫ Oscuro — información de cuenta

def apply_footer(embed: discord.Embed, guild: discord.Guild) -> discord.Embed:
    """Firma de marca consistente en todos los embeds del bot: icono del
    servidor + nombre + lema. Se usa el nombre/icono reales del servidor
    (no un texto fijo), así funciona igual si cambian en el futuro."""
    icon = guild.icon.url if guild and guild.icon else None
    embed.set_footer(text=f"{guild.name} · Encuentra gente. Juega. Repite." if guild else "Encuentra gente. Juega. Repite.",
                     icon_url=icon)
    return embed

def tier_thumbnail_url(tier_code: str):
    """URL de la imagen del emoji personalizado de un tier (para usarla
    como thumbnail), extrayendo el ID de <:nombre:ID> tal como está
    guardado en TIER_EMOJIS. None si ese tier no tiene emoji (Sin rango)."""
    raw = TIER_EMOJIS.get(tier_code, "")
    match = re.search(r":(\d+)>$", raw)
    if not match:
        return None
    return f"https://cdn.discordapp.com/emojis/{match.group(1)}.png"

# ------------------ VERIFICACIÓN ------------------

PENDING_VERIFICATIONS = {}
VERIFICATION_ICON_ID = 25  # icono que debe ponerse el usuario

# ------------------ BUSCAR PARTIDA ------------------

# Último uso de "Buscar partida" por usuario (en memoria; se reinicia si el
# bot se reinicia, aceptable para un cooldown de minutos).
SEARCH_COOLDOWNS = {}

# Usuarios que pulsaron "Buscar partida" sin tener cuenta vinculada: al
# terminar de vincularla, se les lanza la búsqueda automáticamente para
# ahorrarles el segundo clic. user_id -> mode_key (qué modalidad querían).
SEARCH_INTENT = {}

# Modalidad que cada usuario eligió en el desplegable del panel de Buscar
# partida, hasta que pulse el botón (o cambie de opinión). user_id -> mode_key.
PENDING_MODE_SELECTION = {}

# ------------------ RIOT API ------------------

http_session: aiohttp.ClientSession | None = None

RIOT_CALL_TIMES = collections.deque()  # instantes de las últimas llamadas a Riot (ventana de 2 min)

def riot_calls_last_2min() -> int:
    """Cuántas llamadas a la API de Riot se han hecho en los últimos 2
    minutos (la clave de desarrollo permite 100 por ventana de 2 min)."""
    cutoff = time.time() - 120
    while RIOT_CALL_TIMES and RIOT_CALL_TIMES[0] < cutoff:
        RIOT_CALL_TIMES.popleft()
    return len(RIOT_CALL_TIMES)

async def riot_get(url):
    headers = {"X-Riot-Token": RIOT_API_KEY}
    RIOT_CALL_TIMES.append(time.time())
    async with http_session.get(url, headers=headers) as r:
        if r.status != 200:
            print("Riot API error:", r.status, url)
            return None
        return await r.json()

async def validate_riot_id(name, tag, region):
    _, routing, _ = REGIONS[region]
    return await riot_get(
        f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
        f"{quote(name)}/{quote(tag)}"
    )

async def get_summoner_by_puuid(puuid, region):
    platform, _, _ = REGIONS[region]
    return await riot_get(
        f"https://{platform}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
    )

async def get_ranks(puuid, region):
    platform, _, _ = REGIONS[region]
    data = await riot_get(
        f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
    )

    solo = flex = "UNRANKED"
    if data:
        for q in data:
            if q["queueType"] == "RANKED_SOLO_5x5":
                solo = q["tier"]
            elif q["queueType"] == "RANKED_FLEX_SR":
                flex = q["tier"]
    return solo, flex

# ------------------ ROLES (IDEMPOTENTES) ------------------

def get_desired_roles(member, region, solo, flex):
    roles = []
    region_role = member.guild.get_role(REGIONS[region][2])
    solo_role = member.guild.get_role(SOLO_ROLES[solo])
    flex_role = member.guild.get_role(FLEX_ROLES[flex])
    for r in (region_role, solo_role, flex_role):
        if r:
            roles.append(r)
    return set(roles)

async def apply_roles(member, region, solo, flex):
    """Aplica solo la DIFERENCIA entre los roles actuales y los deseados.
    Idempotente: a quien ya tiene los roles correctos no se le toca nada.
    Como esta función solo se llama cuando el usuario SÍ tiene cuenta
    vinculada, el rol "Sin vincular" nunca es un rol deseado — si lo tiene,
    se incluye en managed_ids para que el diff lo elimine solo.
    Devuelve True si hizo algún cambio, False si no había nada que tocar."""
    desired = get_desired_roles(member, region, solo, flex)
    managed_ids = (list(SOLO_ROLES.values()) + list(FLEX_ROLES.values())
                   + [r[2] for r in REGIONS.values()] + [UNLINKED_ROLE_ID])
    current = {r for r in member.roles if r.id in managed_ids}

    to_add = desired - current
    to_remove = current - desired

    if not to_add and not to_remove:
        print(f"[ROLES] {member} sin cambios, no se tocarán roles")
        return False

    print(f"[ROLES] {member} +{[r.name for r in to_add]} -{[r.name for r in to_remove]}")

    if to_remove:
        await member.remove_roles(*to_remove)
    if to_add:
        await member.add_roles(*to_add)
    return True

async def announce_rank_up(member, old_solo, new_solo):
    """Anuncia en el canal de charla una SUBIDA de rango en SoloQ. No avisa
    si no había rango previo (UNRANKED o desconocido) ni si el cambio es una
    bajada o un cambio de FlexQ. Nunca debe romper el flujo que lo llama."""
    try:
        if old_solo not in TIER_ORDER or new_solo not in TIER_ORDER or old_solo == "UNRANKED":
            return
        if TIER_ORDER.index(new_solo) <= TIER_ORDER.index(old_solo):
            return
        channel = member.guild.get_channel(RANKUP_CHANNEL_ID)
        if not channel:
            return
        embed = discord.Embed(
            description=f"🎉 {member.mention} ha subido a {format_tier_display(new_solo)} en SoloQ",
            color=COLOR_GROUP_OPEN
        )
        thumb = tier_thumbnail_url(new_solo)
        if thumb:
            embed.set_thumbnail(url=thumb)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        print(f"[RANKUP] No se pudo anunciar la subida de {member}: {type(e).__name__}: {e}")

async def clear_roles(member):
    """Se llama cuando el usuario se queda sin ninguna cuenta vinculada:
    quita región/SoloQ/FlexQ y le devuelve el rol "Sin vincular"."""
    managed_ids = list(SOLO_ROLES.values()) + list(FLEX_ROLES.values()) + [r[2] for r in REGIONS.values()]
    roles = [r for r in member.roles if r.id in managed_ids]
    if roles:
        await member.remove_roles(*roles)

    unlinked_role = member.guild.get_role(UNLINKED_ROLE_ID)
    if unlinked_role and unlinked_role not in member.roles:
        await member.add_roles(unlinked_role)

# ------------------ EMBEDS ------------------

def verification_embed(name, tag):
    embed = discord.Embed(
        title="🔐 Verificación de propiedad",
        description=(f"Para verificar que eres el dueño de **{name}#{tag}**:\n\n"
                     "1️⃣ Abre el cliente de **League of Legends**\n"
                     "2️⃣ Cambia tu **icono de invocador** por el siguiente\n\n"
                     "Cuando lo hayas hecho, pulsa **He cambiado el icono**"),
        color=COLOR_VERIFICATION
    )
    embed.set_thumbnail(
        url=f"https://raw.communitydragon.org/latest/plugins/"
            f"rcp-be-lol-game-data/global/default/v1/profile-icons/{VERIFICATION_ICON_ID}.jpg"
    )
    return embed

def build_account_embed(acc, summoner):
    icon_url = (
        "https://raw.communitydragon.org/latest/plugins/"
        "rcp-be-lol-game-data/global/default/v1/profile-icons/"
        f"{summoner['profileIconId']}.jpg"
    )
    title = f"{'⭐ ' if acc['primary'] else ''}{acc['riot_id']} ({acc['region']})"
    embed = discord.Embed(title=title, color=COLOR_ACCOUNT_INFO)
    embed.set_thumbnail(url=icon_url)
    solo_display = format_tier_display(acc["solo"])
    flex_display = format_tier_display(acc["flex"])
    embed.add_field(name="Nivel", value=str(summoner["summonerLevel"]), inline=True)
    embed.add_field(name="SoloQ", value=solo_display, inline=True)
    embed.add_field(name="FlexQ", value=flex_display, inline=True)
    return embed

def get_member_lane_role(member):
    """Devuelve el rol de posición (Top/Jungla/Mid/ADC/Support) que el
    onboarding de Discord ya le asignó al miembro, o None si no tiene
    ninguno (no debería pasar, la pregunta es obligatoria)."""
    lane_ids = set(LANE_ROLES.values())
    for role in member.roles:
        if role.id in lane_ids:
            return role
    return None

def get_member_tier_from_roles(member, role_map):
    """Deduce un tier (SoloQ o FlexQ) a partir de qué rol de ese mapa tiene
    el miembro ahora mismo — funciona igual si el rol se lo asignó nuestro
    bot o cualquier otro (p.ej. Orianna Bot, que usa los mismos roles)."""
    member_role_ids = {r.id for r in member.roles}
    for tier, role_id in role_map.items():
        if role_id in member_role_ids:
            return tier
    return None

def get_effective_primary(user_id: str, member: discord.Member):
    """Cuenta 'principal' para Buscar partida / unirse a un grupo. Preferimos
    los datos reales de nuestra DB (con puuid/riot_id, necesarios para Ver
    cuentas y Actualizar datos). Si no hay cuenta en nuestra DB pero la
    persona ya tiene puesto un rol de rango (p.ej. vinculada por Orianna Bot,
    que usa los mismos roles que nosotros), deducimos el tier directamente
    de sus roles — así puede usar Buscar partida sin haber pasado por
    nuestro flujo. Esta cuenta 'derivada' NO sirve para Ver cuentas ni
    Actualizar datos, que sí necesitan el puuid real."""
    data = load_data()
    accounts = data.get(user_id)
    primary = next((a for a in accounts if a["primary"]), None) if accounts else None
    if primary:
        return primary

    solo_tier = get_member_tier_from_roles(member, SOLO_ROLES)
    if not solo_tier:
        return None

    flex_tier = get_member_tier_from_roles(member, FLEX_ROLES) or "UNRANKED"
    return {"solo": solo_tier, "flex": flex_tier, "riot_id": None, "puuid": None, "region": None}

def format_tier_display(tier_code):
    """Emoji + nombre en español de un tier, p.ej. '<:Oro:...> Oro'."""
    emoji = TIER_EMOJIS.get(tier_code, "")
    name = TIER_DISPLAY_ES.get(tier_code, tier_code)
    return f"{emoji} {name}".strip()

def get_lane_display(role):
    """Emoji + nombre real del rol de lane que ya tiene el miembro."""
    key = next((k for k, v in LANE_ROLES.items() if v == role.id), None)
    emoji = LANE_EMOJIS.get(key, "") if key else ""
    return f"{emoji} {role.name}".strip()

def humanize_delta(delta):
    """'hace 12 min' / 'hace 2 h' / 'ayer' / 'hace 3 días', a partir de un
    timedelta transcurrido."""
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "hace unos segundos"
    minutes = seconds // 60
    if minutes < 60:
        return f"hace {minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"hace {hours} h"
    days = hours // 24
    if days == 1:
        return "ayer"
    return f"hace {days} días"

# ------------------ TARJETA DEL AVISO DE BÚSQUEDA ------------------

def mode_short_name(mode: dict) -> str:
    """'DuoQ (SoloQ)' -> 'DuoQ'; el resto de modos se queda igual."""
    return mode["label"].split(" (")[0]

def tier_emblem_url(tier_code):
    raw = TIER_EMOJIS.get(tier_code, "")
    m = re.search(r":(\d+)>$", raw)
    return f"https://cdn.discordapp.com/emojis/{m.group(1)}.png?size=256" if m else None

def _fit_font(draw, text, base_size, max_width, min_size=18):
    """Fuente más pequeña (hasta min_size) si el texto no cabe en max_width."""
    size = base_size
    font = _load_font(size)
    while size > min_size and draw.textlength(text, font=font) > max_width:
        size -= 2
        font = _load_font(size)
    return font

def _dim(img):
    """Apaga una imagen RGBA (menos color y brillo) conservando la transparencia."""
    alpha = img.getchannel("A")
    rgb = ImageEnhance.Color(img.convert("RGB")).enhance(0.35)
    rgb = ImageEnhance.Brightness(rgb).enhance(0.8)
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out

def render_group_card(card):
    """Tarjeta del aviso: emblema(s) de rango, nombre, posición (con icono),
    rangos, a quién busca y plazas con el avatar de cada jugador. NO lleva
    el modo ni el estado (van en el título del embed) ni la hora de cierre
    (va en texto, para que Discord la mantenga al día). Aros de las plazas:
    verde si el grupo está abierto, rojo si está cerrado."""
    W, H = 800, 250
    GREEN, RED = (46, 204, 113), (231, 76, 60)
    BG, MUTED, TEXT = (30, 31, 34), (170, 175, 185), (240, 240, 243)
    f_name, f_rank, f_rank_big = _load_font(44), _load_font(30), _load_font(36)
    f_busca, f_count, f_chip, f_lbl = _load_font(28), _load_font(22), _load_font(20), _load_font(16)
    f_wm = _load_font(150)

    im = Image.new("RGBA", (W, H), BG + (255,))
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for x in range(W):
        sd.line([(x, 0), (x, H)], fill=(85, 90, 115, int(70 * (1 - x / W))))
    im.alpha_composite(shade)

    # Marca de agua con el nombre del modo (gris muy tenue, sin colores de estado)
    wm_text = card.get("mode_name", "").upper()
    if wm_text:
        wm = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        wd = ImageDraw.Draw(wm)
        size = 150
        font = f_wm
        while wd.textlength(wm_text, font=font) > W - 40 and size > 60:
            size -= 10
            font = _load_font(size)
        wd.text((W - wd.textlength(wm_text, font=font) - 12, H - size - 38), wm_text, font=font, fill=(255, 255, 255, 9))
        im.alpha_composite(wm)
    d = ImageDraw.Draw(im)

    def paste_emblem(raw, size, pos, tier):
        col = TIER_COLORS.get(tier, TIER_COLORS["UNRANKED"])
        if raw:
            try:
                e = Image.open(io.BytesIO(raw)).convert("RGBA").resize((size, size), Image.LANCZOS)
                im.alpha_composite(e, pos)
                return
            except Exception:
                pass
        d.ellipse([pos[0], pos[1], pos[0] + size, pos[1] + size], fill=(43, 45, 49, 255), outline=col + (255,), width=4)
        d.text((pos[0] + size / 2 - d.textlength("?", font=f_rank) / 2, pos[1] + size / 2 - 20), "?", font=f_rank, fill=col + (255,))

    paste_emblem(card.get("emblem"), 140, (26, 40), card.get("emblem_tier", "UNRANKED"))
    if card.get("emblem2_tier"):
        paste_emblem(card.get("emblem2"), 74, (122, 140), card["emblem2_tier"])

    name_txt = card.get("name", "Jugador")
    f_name = _fit_font(d, name_txt, 44, 235, 26)
    d.text((220, 28), _fit_text(d, name_txt, f_name, 235), font=f_name, fill=(255, 255, 255, 255))

    # Posición (chip con icono)
    has_lane = bool(card.get("lane"))
    if has_lane:
        label = card["lane"].upper()
        icon = None
        if card.get("lane_icon"):
            try:
                icon = Image.open(io.BytesIO(card["lane_icon"])).convert("RGBA").resize((28, 28), Image.LANCZOS)
            except Exception:
                icon = None
        w = d.textlength(label, font=f_chip) + 28 + (36 if icon else 0)
        d.rounded_rectangle([220, 92, 220 + w, 130], radius=19, fill=(60, 64, 72, 255))
        tx = 220 + 14
        if icon:
            im.alpha_composite(icon, (tx, 97))
            tx += 36
        d.text((tx, 99), label, font=f_chip, fill=(255, 255, 255, 255))

    # Rangos: si no hay posición suben y se hacen más grandes (ARAM, Arena)
    ranks = card.get("ranks", [])
    ly, vy, base = (146, 166, 30) if has_lane else (104, 126, 36)
    texts = [TIER_DISPLAY_ES.get(t, t) for _, t in ranks]
    # Un único tamaño para todos los rangos, el mayor al que quepan en 240 px.
    rsize = base
    while rsize > 18:
        f = _load_font(rsize)
        if sum(d.textlength(t, font=f) for t in texts) + 28 * max(len(texts) - 1, 0) <= 240:
            break
        rsize -= 2
    fv = _load_font(rsize)
    rx = 220
    for (label, tier), tier_txt in zip(ranks, texts):
        col = TIER_COLORS.get(tier, TIER_COLORS["UNRANKED"])
        d.text((rx, ly), label.upper(), font=f_lbl, fill=MUTED + (255,))
        d.text((rx, vy + (base - rsize) // 2), tier_txt, font=fv, fill=col + (255,))
        rx += max(d.textlength(tier_txt, font=fv), d.textlength(label.upper(), font=f_lbl)) + 28

    # Separador, a quién busca y plazas con avatares
    d.line([(470, 34), (470, H - 34)], fill=(70, 72, 78, 255), width=2)
    d.text((500, 32), "BUSCA", font=f_lbl, fill=MUTED + (255,))
    busca_txt = card.get("busca", "")
    f_busca = _fit_font(d, busca_txt, 28, W - 500 - 20, 20)
    d.text((500, 54), _fit_text(d, busca_txt, f_busca, W - 500 - 20), font=f_busca, fill=TEXT + (255,))

    closed = bool(card.get("closed"))
    if closed:  # grupo cerrado: todo lo anterior se ve apagado (sin añadir colores nuevos)
        im = _dim(im)
        d = ImageDraw.Draw(im)
    ring = RED if closed else GREEN
    total = card.get("max", 2)
    joined = card.get("joined", [])
    step = min(58, (W - 500 - 20) // max(total, 1))
    size = min(52, step - 6)
    for i in range(total):
        cx = 500 + i * step
        box = [cx, 124, cx + size, 124 + size]
        if i < len(joined):
            raw, initial = joined[i]
            avatar = None
            if raw:
                try:
                    avatar = Image.open(io.BytesIO(raw)).convert("RGBA").resize((size, size), Image.LANCZOS)
                except Exception:
                    avatar = None
            if avatar is None:
                avatar = Image.new("RGBA", (size, size), (88, 91, 99, 255))
                ImageDraw.Draw(avatar).text((size / 2 - 8, size / 2 - 14), initial, font=f_chip, fill=(255, 255, 255, 255))
            if closed:
                avatar = _dim(avatar)
            circle = Image.new("L", (size, size), 0)
            ImageDraw.Draw(circle).ellipse([0, 0, size - 1, size - 1], fill=255)
            avatar.putalpha(circle)
            im.alpha_composite(avatar, (cx, 124))
            d.ellipse(box, outline=ring + (255,), width=4)
            if i == 0:  # el creador (siempre la primera plaza) lleva una estrella dorada
                bx, by, br = cx + size - 6, 124 - 2, 13
                d.ellipse([bx - br, by - br, bx + br, by + br], fill=(30, 31, 34, 255), outline=(241, 196, 15, 255), width=2)
                pts = []
                for k in range(10):
                    ang = -math.pi / 2 + k * math.pi / 5
                    rad = 9 if k % 2 == 0 else 4
                    pts.append((bx + rad * math.cos(ang), by + rad * math.sin(ang)))
                d.polygon(pts, fill=(241, 196, 15, 255))
        else:
            d.ellipse(box, outline=(110, 114, 122, 255), width=3)
            d.text((cx + size / 2 - d.textlength("+", font=f_rank) / 2, 124 + size / 2 - 19), "+", font=f_rank, fill=(110, 114, 122, 255))
    n_in = min(len(joined), total) if joined else card.get("count", 0)
    count_txt = f"{n_in}/{total} jugadores"
    d.text((500, 192), count_txt, font=f_count, fill=TEXT + (255,))
    if not closed and total - n_in == 1:  # a falta de una plaza, lo destacamos
        f_falta = _load_font(20)
        d.text((500 + d.textlength(count_txt, font=f_count) + 10, 194), "¡Falta 1!", font=f_falta, fill=GREEN + (255,))

    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=18, fill=255)
    im.putalpha(mask)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()

async def build_group_message(guild, creator, mode_key, solo_tier, flex_tier, count, locked,
                              created_at_iso, lines=None, voice_channel=None, members=None):
    """Embed + tarjeta del aviso de búsqueda. Se usa tanto al publicarlo como
    al actualizarlo (unirse/salir/cerrar), así que todo se calcula aquí a
    partir de datos actuales. Devuelve (embed, file | None)."""
    mode = GAME_MODES.get(mode_key, GAME_MODES["DUOQ"])
    max_m = mode["max_members"]
    state = "Cerrado" if locked else "Abierto"
    title_icon = "🔴" if locked else "🟢"

    embed = discord.Embed(
        title=f"{title_icon} {mode_short_name(mode)} · Grupo {state} ({count}/{max_m})",
        color=COLOR_GROUP_CLOSED if locked else COLOR_GROUP_OPEN
    )
    if creator:
        embed.set_author(name=creator.display_name, icon_url=creator.display_avatar.url)
    apply_footer(embed, guild)
    try:
        created = datetime.fromisoformat(created_at_iso)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        embed.timestamp = created
    except (ValueError, TypeError):
        created = None

    paragraphs = []
    if not locked and created:
        end_ts = int((created + timedelta(hours=GROUP_LIFETIME_HOURS)).timestamp())
        paragraphs.append(f"⏳ Se cierra <t:{end_ts}:R>")
    if voice_channel:
        paragraphs.append(f"🔊 {voice_channel.name} · *abierta a cuentas vinculadas*")
    embed.description = "\n\n".join(paragraphs) if paragraphs else None
    if lines:
        embed.add_field(name="👥 JUGADORES", value="\n".join(lines), inline=False)

    # ---- Datos de la tarjeta según el modo ----
    lane_role = get_member_lane_role(creator) if (creator and mode["show_lane"]) else None
    card = {"name": creator.display_name if creator else "Jugador", "lane": lane_role.name if lane_role else None,
            "count": count, "max": max_m, "closed": locked, "ranks": [], "mode_name": mode_short_name(mode)}
    lane_icon_url = None
    if lane_role:
        lane_key = next((k for k, v in LANE_ROLES.items() if v == lane_role.id), None)
        em = re.search(r":(\d+)>$", LANE_EMOJIS.get(lane_key, "")) if lane_key else None
        lane_icon_url = f"https://cdn.discordapp.com/emojis/{em.group(1)}.png?size=64" if em else None
    # Jugadores dentro (el creador primero) para las plazas con avatar.
    people = list(members) if members else ([creator] if creator else [])
    people = [m for m in people if m is not None][:max_m]
    if mode_key == "DUOQ":
        window = TIER_SEARCH_WINDOWS.get(solo_tier, [solo_tier])
        card["busca"] = " / ".join(TIER_DISPLAY_ES.get(t, t) for t in reversed(window))
        card.update(emblem_tier=solo_tier, emblem2_tier=flex_tier)
        card["ranks"] = [("SoloQ", solo_tier), ("FlexQ", flex_tier)]
    else:
        card["busca"] = f"Jugadores para {mode_short_name(mode)}"
        if mode["rank_source"] == "flex":
            card.update(emblem_tier=flex_tier)
            card["ranks"] = [("FlexQ", flex_tier)]
        elif mode["rank_source"] == "solo_ref":
            card.update(emblem_tier=solo_tier)
            card["ranks"] = [("SoloQ (ref.)", solo_tier)]
        else:
            card["emblem_tier"] = "UNRANKED"

    try:
        results = await asyncio.gather(
            fetch_asset(tier_emblem_url(card.get("emblem_tier"))),
            fetch_asset(tier_emblem_url(card.get("emblem2_tier"))) if card.get("emblem2_tier") else asyncio.sleep(0),
            fetch_asset(lane_icon_url),
            *[fetch_asset(str(p.display_avatar.replace(size=128, format="png").url)) for p in people],
        )
        card["emblem"], card["emblem2"], card["lane_icon"] = results[0], results[1], results[2]
        card["joined"] = [(raw, (p.display_name[:1] or "?").upper()) for p, raw in zip(people, results[3:])]
        png = await asyncio.to_thread(render_group_card, card)
    except Exception as e:
        print(f"[GRUPO] No se pudo dibujar la tarjeta del aviso: {type(e).__name__}: {e}")
        # Sin imagen, el embed lleva los datos básicos en texto para no quedarse vacío.
        if card.get("lane"):
            embed.add_field(name="Posición", value=card["lane"], inline=True)
        for label, tier in card["ranks"]:
            embed.add_field(name=label, value=format_tier_display(tier), inline=True)
        return embed, None

    embed.set_image(url="attachment://busqueda.png")
    return embed, discord.File(io.BytesIO(png), filename="busqueda.png")

async def update_group_embed(thread: discord.Thread):
    """Reconstruye el aviso del grupo (embed + tarjeta) a partir del propio
    hilo (fuente de verdad): quién está dentro (thread.fetch_members()) y si
    está cerrado (thread.locked). No guardamos la lista de miembros en
    ningún sitio aparte — siempre se lee de Discord. Los rangos del creador
    se deducen de sus roles actuales, así que funciona también con gente
    vinculada por Orianna Bot."""
    group = get_group(thread.id)
    if not group:
        return

    channel = bot.get_channel(int(group["channel_id"]))
    if not channel:
        try:
            channel = await bot.fetch_channel(int(group["channel_id"]))
        except discord.HTTPException:
            return

    try:
        message = await channel.fetch_message(int(group["message_id"]))
    except discord.HTTPException:
        return

    try:
        thread_members = await thread.fetch_members()
    except discord.HTTPException:
        thread_members = []

    guild = thread.guild
    creator_id = int(group["creator_id"])
    mode_key = group["mode"] if group["mode"] in GAME_MODES else "DUOQ"
    mode = GAME_MODES[mode_key]
    lines = []
    present = []
    for tm in thread_members:
        member = guild.get_member(tm.id)
        if not member or member.bot:
            continue
        present.append(member)
        solo_tier = get_member_tier_from_roles(member, SOLO_ROLES) or "UNRANKED"
        solo_display = format_tier_display(solo_tier)
        marker = "🔸" if member.id == creator_id else "🔹"
        if mode["show_lane"]:
            lane_role = get_member_lane_role(member)
            lane_display = get_lane_display(lane_role) if lane_role else "—"
            lines.append(f"{marker} {member.display_name} · {lane_display} · SoloQ {solo_display}")
        else:
            lines.append(f"{marker} {member.display_name} · SoloQ {solo_display}")

    creator = guild.get_member(creator_id)
    solo_tier = (get_member_tier_from_roles(creator, SOLO_ROLES) if creator else None) or "UNRANKED"
    flex_tier = (get_member_tier_from_roles(creator, FLEX_ROLES) if creator else None) or "UNRANKED"

    voice_channel = None
    if group.get("voice_channel_id"):
        voice_channel = guild.get_channel(int(group["voice_channel_id"]))

    embed, file = await build_group_message(
        guild, creator, mode_key, solo_tier, flex_tier, len(lines), thread.locked,
        group["created_at"], lines=lines, voice_channel=voice_channel,
        members=sorted(present, key=lambda m: m.id != creator_id)  # el creador primero
    )
    try:
        if file:
            await message.edit(embed=embed, attachments=[file])
        else:
            await message.edit(embed=embed, attachments=[])
    except discord.HTTPException:
        pass

def voice_join_view(voice_channel, guild_id: int):
    """Vista de un solo botón de enlace a la sala de voz del grupo, o None
    si el grupo no tiene canal de voz (para no adjuntar una vista vacía)."""
    if not voice_channel:
        return None
    view = View()
    view.add_item(discord.ui.Button(
        label=f"Ir a {voice_channel.name}", emoji="🔊", style=discord.ButtonStyle.link,
        url=f"https://discord.com/channels/{guild_id}/{voice_channel.id}"
    ))
    return view

async def refresh_group_buttons(thread: discord.Thread, group: dict):
    """Reconstruye los botones del mensaje del grupo leyendo el estado
    actual de la DB (por ejemplo, tras borrarse la sala de voz, para quitar
    el botón 'Voz' que ya no llevaría a ningún sitio). No hace nada si el
    grupo ya está cerrado — ese mensaje ya se quedó sin botones."""
    if thread.locked:
        return
    channel = bot.get_channel(int(group["channel_id"]))
    if not channel:
        try:
            channel = await bot.fetch_channel(int(group["channel_id"]))
        except discord.HTTPException:
            return
    try:
        message = await channel.fetch_message(int(group["message_id"]))
    except discord.HTTPException:
        return

    fresh = get_group(group["thread_id"])
    voice_id = int(fresh["voice_channel_id"]) if fresh and fresh.get("voice_channel_id") else None
    view = GroupView(int(group["thread_id"]), int(group["creator_id"]), voice_id, thread.guild.id)
    bot.add_view(view)
    try:
        await message.edit(view=view)
    except discord.HTTPException:
        pass

class GroupView(View):
    """Vista dinámica del grupo: Unirse / Salir del grupo / Cerrar grupo.
    Los custom_id llevan el thread_id (y el creator_id en 'cerrar') dentro,
    así que no hace falta guardar nada aparte para que los botones sigan
    funcionando tras un reinicio — solo hay que volver a registrar esta
    vista al arrancar (ver reregister_group_views)."""

    def __init__(self, thread_id: int, creator_id: int, voice_channel_id: int = None, guild_id: int = None):
        super().__init__(timeout=None)
        self.thread_id = thread_id
        self.creator_id = creator_id

        # Fila 0: acciones de cualquier miembro del grupo.
        join_btn = discord.ui.Button(
            label="Unirse", emoji="✅", style=discord.ButtonStyle.success,
            custom_id=f"group_join:{thread_id}", row=0
        )
        join_btn.callback = self.join
        self.add_item(join_btn)

        leave_btn = discord.ui.Button(
            label="Salir del grupo", emoji="🚪", style=discord.ButtonStyle.secondary,
            custom_id=f"group_leave:{thread_id}", row=0
        )
        leave_btn.callback = self.leave
        self.add_item(leave_btn)

        # Botón de enlace a la sala de voz — solo si el grupo tiene una. Es
        # de tipo "link" (no custom_id, no pasa por nuestro callback), así
        # que no necesita que nadie tenga ya acceso: Discord gestiona el
        # permiso al pulsar. Se quita solo si la sala se borra (ver
        # refresh_group_buttons).
        if voice_channel_id and guild_id:
            self.add_item(discord.ui.Button(
                label="Voz", emoji="🔊", style=discord.ButtonStyle.link, row=0,
                url=f"https://discord.com/channels/{guild_id}/{voice_channel_id}"
            ))

        # Fila 1: solo el creador, y son destructivas — separadas a propósito.
        close_btn = discord.ui.Button(
            label="Cerrar grupo", emoji="🔒", style=discord.ButtonStyle.danger,
            custom_id=f"group_close:{thread_id}:{creator_id}", row=1
        )
        close_btn.callback = self.close_group
        self.add_item(close_btn)

        cancel_btn = discord.ui.Button(
            label="Cancelar búsqueda", emoji="🗑️", style=discord.ButtonStyle.secondary,
            custom_id=f"group_cancel:{thread_id}:{creator_id}", row=1
        )
        cancel_btn.callback = self.cancel_search
        self.add_item(cancel_btn)

    async def _get_thread(self, interaction: discord.Interaction):
        thread = interaction.guild.get_channel_or_thread(self.thread_id)
        if thread:
            return thread
        try:
            return await bot.fetch_channel(self.thread_id)
        except discord.HTTPException:
            return None

    async def join(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            uid = str(interaction.user.id)
            primary = get_effective_primary(uid, interaction.user)
            if not primary:
                return await send_notice(interaction, "warn", "❌ Necesitas tener una cuenta vinculada para unirte a un grupo.")

            thread = await self._get_thread(interaction)
            if not thread:
                return await send_notice(interaction, "warn", "❌ Este grupo ya no existe.")
            if thread.locked:
                return await send_notice(interaction, "info", "🔒 Este grupo está cerrado, no admite más gente.")

            try:
                members = await thread.fetch_members()
            except discord.HTTPException:
                members = []
            if interaction.user.id in [m.id for m in members]:
                return await send_notice(interaction, "info", "ℹ️ Ya estás en este grupo.")

            if thread.archived:
                thread = await thread.edit(archived=False)  # capturamos el objeto actualizado, por si acaso
            await thread.add_user(interaction.user)

            group_record = get_group(thread.id)
            mode = GAME_MODES.get(group_record["mode"], GAME_MODES["DUOQ"]) if group_record else GAME_MODES["DUOQ"]

            # Si el grupo tiene canal de voz vinculado, le damos acceso también.
            voice_channel = None
            if group_record and group_record.get("voice_channel_id"):
                voice_channel = interaction.guild.get_channel(int(group_record["voice_channel_id"]))
            voice_view = voice_join_view(voice_channel, interaction.guild.id)

            try:
                members_after = await thread.fetch_members()
            except discord.HTTPException:
                members_after = []

            if len(members_after) >= mode["max_members"]:
                # Equipo completo: se cierra solo, misma lógica que "Cerrar grupo".
                if await self._lock_group(thread, interaction):
                    try:
                        full_embed = apply_footer(discord.Embed(
                            title="🔒 Grupo completo",
                            description="¡Equipo al completo! El grupo se ha cerrado automáticamente "
                                        "y ya no admite más jugadores. ¡A jugar!",
                            color=COLOR_GROUP_CLOSED
                        ), thread.guild)
                        await thread.send(embed=full_embed, allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        pass
                await send_notice(interaction, "ok",
                    f"✅ Te has unido al grupo — ¡equipo completo! Habla con ellos en {thread.mention}.",
                    view=voice_view)
            else:
                await update_group_embed(thread)
                await send_notice(interaction, "ok",
                    f"✅ Te has unido al grupo. Habla con ellos en {thread.mention}.",
                    view=voice_view)
        except Exception as e:
            print(f"[GRUPO] Error en 'Unirse': {type(e).__name__}: {e}")
            await send_error(interaction, f"Ha ocurrido un error inesperado ({type(e).__name__}).")

    async def leave(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            if interaction.user.id == self.creator_id:
                return await send_notice(interaction, "info",
                    "ℹ️ Eres el creador del grupo — usa **Cerrar grupo** si quieres dejar de admitir gente.")

            thread = await self._get_thread(interaction)
            if not thread:
                return await send_notice(interaction, "warn", "❌ Este grupo ya no existe.")

            try:
                members = await thread.fetch_members()
            except discord.HTTPException:
                members = []
            if interaction.user.id not in [m.id for m in members]:
                return await send_notice(interaction, "info", "ℹ️ No estás en este grupo.")

            await thread.remove_user(interaction.user)

            await update_group_embed(thread)
            await send_notice(interaction, "ok", "🚪 Has salido del grupo.")
        except Exception as e:
            print(f"[GRUPO] Error en 'Salir': {type(e).__name__}: {e}")
            await send_error(interaction, f"Ha ocurrido un error inesperado ({type(e).__name__}).")

    async def _lock_group(self, thread: discord.Thread, interaction: discord.Interaction) -> bool:
        """Bloquea el hilo, refresca el embed y QUITA los botones del
        mensaje del canal por completo (no solo deshabilitarlos). Quien ya
        esté dentro del hilo siempre puede abandonarlo de forma nativa desde
        Discord, así que no hace falta mantener un botón de Salir aquí.
        Compartido entre el cierre manual y el cierre automático al
        llenarse el grupo. Devuelve True si se pudo bloquear, False si
        falló (y ya se avisó al usuario del motivo)."""
        try:
            thread = await thread.edit(locked=True)  # capturamos el objeto actualizado, no el viejo
        except discord.Forbidden:
            await send_error(interaction, "No tengo permiso de **Gestionar hilos** en este canal, así que no puedo cerrar el grupo.")
            return False
        except discord.HTTPException as e:
            await send_error(interaction, f"No se pudo cerrar el grupo (error de Discord: {e.status}).")
            return False

        await update_group_embed(thread)
        if interaction.message:
            try:
                await interaction.message.edit(view=None)
            except discord.HTTPException:
                pass
        return True

    async def close_group(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            if interaction.user.id != self.creator_id:
                return await send_notice(interaction, "warn", "⚠️ Solo el creador del grupo puede cerrarlo.")

            thread = await self._get_thread(interaction)
            if not thread:
                return await send_notice(interaction, "warn", "❌ Este grupo ya no existe.")
            if thread.locked:
                return await send_notice(interaction, "info", "ℹ️ Este grupo ya estaba cerrado.")

            if not await self._lock_group(thread, interaction):
                return  # _lock_group ya avisó del motivo del fallo
            await send_notice(interaction, "ok", "🔒 Grupo cerrado. Ya no se admite gente nueva.")
        except Exception as e:
            print(f"[GRUPO] Error en 'Cerrar grupo': {type(e).__name__}: {e}")
            await send_error(interaction, f"Ha ocurrido un error inesperado ({type(e).__name__}).")

    async def cancel_search(self, interaction: discord.Interaction):
        """A diferencia de 'Cerrar grupo' (que bloquea pero deja el mensaje
        como histórico), esto borra el hilo Y el mensaje al momento — para
        cuando el creador ya no quiere ni rastro de esta búsqueda (encontró
        gente por otro lado, se equivocó de modalidad, etc.)."""
        await interaction.response.defer(ephemeral=True)
        try:
            if interaction.user.id != self.creator_id:
                return await send_notice(interaction, "warn", "⚠️ Solo el creador de la búsqueda puede cancelarla.")

            thread = await self._get_thread(interaction)
            if thread:
                try:
                    await thread.delete()
                except discord.HTTPException:
                    pass

            group_record = get_group(self.thread_id)
            if group_record:
                await delete_group_voice_channel(group_record)

            if interaction.message:
                try:
                    await interaction.message.delete()
                except discord.HTTPException:
                    pass

            delete_group(str(self.thread_id))
            await send_notice(interaction, "ok", "🗑️ Búsqueda cancelada. Se ha eliminado el hilo y el aviso del canal.")
        except Exception as e:
            print(f"[GRUPO] Error en 'Cancelar búsqueda': {type(e).__name__}: {e}")
            await send_error(interaction, f"Ha ocurrido un error inesperado ({type(e).__name__}).")

async def send_notice(interaction: discord.Interaction, kind: str, text: str, view=None):
    """Respuesta efímera uniforme para avisos normales (no errores de
    infraestructura): kind = 'ok' (verde), 'info' (morado) o 'warn' (amarillo).
    El icono va en el propio texto que se pasa."""
    color = {"ok": COLOR_GROUP_OPEN, "info": COLOR_PANEL, "warn": COLOR_VERIFICATION}.get(kind, COLOR_PANEL)
    embed = discord.Embed(description=text, color=color)
    kwargs = {"view": view} if view is not None else {}
    await interaction.followup.send(embed=embed, ephemeral=True, **kwargs)

async def send_error(interaction: discord.Interaction, detail: str):
    """Mensaje de error estandarizado para fallos de INFRAESTRUCTURA (algo
    está mal configurado o roto — un canal que falta, permisos, una
    excepción inesperada). NO se usa para mensajes de validación normales
    (cooldown, formato, 'ya estás en el grupo'...), que se quedan con su
    propio texto específico porque ahí el usuario puede actuar por su
    cuenta sin necesitar a un administrador."""
    embed = discord.Embed(
        title="❌ No se ha podido completar la acción",
        description=f"{detail}\n\nContacta con un administrador a través de <#{ADMIN_CONTACT_CHANNEL_ID}>.",
        color=COLOR_GROUP_CLOSED
    )
    apply_footer(embed, interaction.guild)
    await interaction.followup.send(embed=embed, ephemeral=True)

async def perform_search(interaction: discord.Interaction, mode_key: str, primary: dict):
    """Lógica de 'Buscar partida' para cualquier modalidad. DUOQ usa el
    sistema de 11 canales por tier de SoloQ (con ventana de tiers cercanos,
    mencionando a los roles de esa ventana); el resto de modos usa un único
    canal fijo, sin ventana ni menciones de rol. Asume que la interacción ya
    se difirió (interaction.response.defer) y usa followups para responder.
    Se reutiliza tanto desde el botón del panel como desde el flujo de
    vinculación automática (cuando el usuario no tenía cuenta y se le abrió
    el modal de vinculación desde 'Buscar partida')."""
    mode = GAME_MODES.get(mode_key, GAME_MODES["DUOQ"])
    uid = str(interaction.user.id)
    now = time.time()
    cooldown_key = f"{uid}:{mode_key}"

    last_use = SEARCH_COOLDOWNS.get(cooldown_key)
    if last_use and (now - last_use) < SEARCH_COOLDOWN_SECONDS:
        remaining = int(SEARCH_COOLDOWN_SECONDS - (now - last_use))
        minutos, segundos = divmod(remaining, 60)
        return await interaction.followup.send(
            f"⏳ Ya has publicado una búsqueda de **{mode['label']}** hace poco. "
            f"Espera **{minutos}m {segundos}s** para volver a usarlo.",
            ephemeral=True
        )

    lane_role = None
    if mode["show_lane"]:
        lane_role = get_member_lane_role(interaction.user)
        if not lane_role:
            return await interaction.followup.send(
                "❌ No se ha detectado tu rol de posición (lane). Revisa que completaste las "
                "preguntas de incorporación del servidor.",
                ephemeral=True
            )

    solo_tier = primary["solo"]
    flex_tier = primary["flex"]

    # ---------- Canal, menciones, texto de rango y miniatura según el modo ----------
    role_mentions = []

    if mode_key == "DUOQ":
        channel_id = TIER_CHANNELS.get(solo_tier)
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if not channel:
            return await send_error(interaction, "No se ha encontrado tu canal de rango.")

        # Mencionamos a los roles de toda la ventana (propio + vecinos), no
        # solo al del canal, para que también se enteren los de tiers cercanos.
        window_tiers = TIER_SEARCH_WINDOWS.get(solo_tier, [solo_tier])
        for tier in window_tiers:
            tier_role = interaction.guild.get_role(SOLO_ROLES.get(tier))
            if tier_role:
                role_mentions.append(tier_role.mention)

    else:
        channel_id = mode.get("channel_id")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if not channel:
            return await send_error(
                interaction, f"El canal de **{mode['label']}** todavía no está configurado."
            )

    embed, card_file = await build_group_message(
        interaction.guild, interaction.user, mode_key, solo_tier, flex_tier, 1, False,
        discord.utils.utcnow().isoformat()
    )

    mention_prefix = f"{' '.join(role_mentions)} " if role_mentions else ""
    try:
        sent_message = await channel.send(
            content=f"{mention_prefix}{interaction.user.mention} está buscando partida",
            embed=embed,
            **({"file": card_file} if card_file else {}),
            allowed_mentions=discord.AllowedMentions(roles=True, users=True)
        )
    except discord.Forbidden:
        return await send_error(interaction, f"No tengo permisos para escribir en {channel.mention}.")

    SEARCH_COOLDOWNS[cooldown_key] = now

    # Hilo privado para que el grupo se comunique sin MD, con sus botones de
    # Unirse / Salir / Cerrar grupo. Si falla (permisos, etc.), el aviso ya
    # se mandó pero NO decimos "todo ok" — avisamos claramente de que el
    # grupo no quedó operativo, para que no pase desapercibido.
    group_ready = False
    try:
        thread = await channel.create_thread(
            name=f"{mode['label']} de {interaction.user.display_name}",
            type=discord.ChannelType.private_thread,
            auto_archive_duration=GROUP_THREAD_ARCHIVE_MINUTES,
            invitable=False
        )
        await thread.add_user(interaction.user)

        # Canal de voz temporal vinculado al grupo — abierto a cuentas vinculadas (lo ven
        # hasta unirse al hilo), con nombre aleatorio de la lista temática,
        # y con el mismo límite de gente que la modalidad. Se borra solo:
        # si queda vacío (on_voice_state_update, con margen de gracia) o si
        # nadie se une nunca (barrido periódico). Si falla, el grupo sigue
        # funcionando igual sin voz.
        voice_channel = None
        try:
            reference = interaction.guild.get_channel(VOICE_CHANNEL_REFERENCE_ID)
            place_after = None
            if isinstance(reference, discord.CategoryChannel):
                voice_category = reference
            elif reference is not None:
                # Es un canal normal: usamos su categoría y, si es de voz,
                # luego lo colocamos justo debajo de él.
                voice_category = reference.category
                if isinstance(reference, discord.VoiceChannel):
                    place_after = reference
            else:
                print(f"[GRUPO] VOICE_CHANNEL_REFERENCE_ID ({VOICE_CHANNEL_REFERENCE_ID}) no se "
                      "encuentra en el servidor; el canal de voz se creará sin categoría.")
                voice_category = None

            voice_overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, connect=True),
            }
            # Visible y accesible para cualquiera con cuenta vinculada (roles de región).
            for _r in REGIONS.values():
                _role = interaction.guild.get_role(_r[2])
                if _role:
                    voice_overwrites[_role] = discord.PermissionOverwrite(view_channel=True, connect=True)
            voice_overwrites.update({
                interaction.guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True),
            })
            voice_channel = await interaction.guild.create_voice_channel(
                name=random.choice(VOICE_CHANNEL_NAMES),
                category=voice_category,
                user_limit=mode["max_members"],
                overwrites=voice_overwrites
            )
            if place_after:
                try:
                    await voice_channel.move(after=place_after, category=voice_category)
                except (discord.HTTPException, ValueError) as e:
                    print(f"[GRUPO] No se pudo colocar el canal de voz bajo el de referencia: {e}")
        except discord.HTTPException as e:
            print(f"[GRUPO] No se pudo crear el canal de voz para {interaction.user}: {e}")

        create_group(thread.id, sent_message.id, channel.id, interaction.user.id,
                      discord.utils.utcnow().isoformat(), mode=mode_key,
                      voice_channel_id=voice_channel.id if voice_channel else None)

        view = GroupView(thread.id, interaction.user.id,
                         voice_channel.id if voice_channel else None, interaction.guild.id)
        bot.add_view(view)
        await sent_message.edit(view=view)
        await update_group_embed(thread)  # título → "Grupo Abierto" + te añade a ti a la lista

        welcome = discord.Embed(
            title=f"🎮 {mode['label']} · hilo del grupo",
            description=(
                f"Hola {interaction.user.mention}, este es vuestro hilo privado: habla aquí con quien se una.\n\n"
                f"👥 Plazas: **{mode['max_members']}** · "
                f"⏳ Se eliminará solo pasadas **{GROUP_LIFETIME_HOURS} h**."
            ),
            color=COLOR_GROUP_OPEN
        )
        if voice_channel:
            welcome.add_field(
                name="🔊 Sala de voz",
                value=f"{voice_channel.mention}\nAbierta a todos los que tengan cuenta vinculada.",
                inline=False
            )
        else:
            welcome.add_field(
                name="⚠️ Sin sala de voz",
                value="No se pudo crear (revisa el permiso **Gestionar canales** del bot).",
                inline=False
            )
        apply_footer(welcome, interaction.guild)
        await thread.send(embed=welcome, allowed_mentions=discord.AllowedMentions.none())
        group_ready = True
    except discord.HTTPException as e:
        print(f"[GRUPO] No se pudo crear el hilo para {interaction.user} en {mode['label']}: "
              f"{type(e).__name__}: {e}")

    if group_ready:
        await interaction.followup.send(
            f"✅ Aviso de **{mode['label']}** publicado en {channel.mention}. ¡Suerte encontrando partida!",
            view=voice_join_view(voice_channel, interaction.guild.id),
            ephemeral=True
        )
    else:
        await send_error(
            interaction,
            f"El aviso de **{mode['label']}** se publicó en {channel.mention}, pero no se pudo crear "
            "el hilo de grupo (probablemente falta un permiso del bot en ese canal). Ese mensaje se "
            "quedará sin botones ni cierre automático hasta que se corrija."
        )

# ------------------ VIEWS ------------------

class VerifyIconView(View):
    def __init__(self, user_id):
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(label="He cambiado el icono", style=discord.ButtonStyle.success, custom_id="verify_icon")
    async def verify(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        if str(interaction.user.id) != self.user_id:
            return await interaction.followup.send("❌ Esta verificación no es tuya.", ephemeral=True)

        pending = PENDING_VERIFICATIONS.get(self.user_id)
        if not pending:
            return await interaction.followup.send("⏰ Verificación expirada.", ephemeral=True)

        summoner = await get_summoner_by_puuid(pending["puuid"], pending["region"])
        if not summoner:
            return await interaction.followup.send("❌ No se pudieron obtener datos de Riot.", ephemeral=True)

        profile_icon = int(summoner.get("profileIconId", 0))
        if profile_icon != VERIFICATION_ICON_ID:
            return await interaction.followup.send(
                f"❌ El icono no coincide. Debe ser **{VERIFICATION_ICON_ID}**, "
                f"pero tu cuenta tiene **{profile_icon}**.",
                ephemeral=True
            )

        solo, flex = await get_ranks(pending["puuid"], pending["region"])
        data = load_data()
        data.setdefault(self.user_id, [])
        for a in data[self.user_id]:
            a["primary"] = False

        acc = {"riot_id": pending["riot_id"], "puuid": pending["puuid"],
               "region": pending["region"], "solo": solo, "flex": flex, "primary": True}
        data[self.user_id].append(acc)
        save_data(data)

        await apply_roles(interaction.user, acc["region"], solo, flex)
        del PENDING_VERIFICATIONS[self.user_id]
        stop_unlinked_tracking(self.user_id)  # ya vinculó, para el cronómetro de recordatorios

        await interaction.followup.send("✅ **Cuenta vinculada correctamente**",
                                        embed=apply_footer(build_account_embed(acc, summoner), interaction.guild),
                                        ephemeral=True)

        # Si llegó aquí desde "Buscar partida" (no tenía cuenta y le abrimos
        # el modal de vinculación), le lanzamos la búsqueda automáticamente
        # para ahorrarle el segundo clic.
        if self.user_id in SEARCH_INTENT:
            mode_key = SEARCH_INTENT.pop(self.user_id)
            await perform_search(interaction, mode_key, acc)

class AccountSelect(discord.ui.Select):
    """Desplegable para elegir sobre qué cuenta actuar, cuando 'Ver cuentas'
    muestra todas combinadas en un solo embed."""
    def __init__(self, owner_id: str, accounts: list):
        self.owner_id = owner_id
        options = [
            discord.SelectOption(
                label=acc["riot_id"],
                value=acc["puuid"],
                description=f"{TIER_DISPLAY_ES.get(acc['solo'], acc['solo'])} SoloQ" + (" · Principal" if acc["primary"] else ""),
                emoji="⭐" if acc["primary"] else None
            )
            for acc in accounts
        ]
        super().__init__(placeholder="Elige una cuenta para gestionar…", options=options)

    async def callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.owner_id:
            return await interaction.response.send_message("❌ No puedes usar esto.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        puuid = self.values[0]
        data = load_data()
        accs = data.get(self.owner_id, [])
        acc = next((a for a in accs if a["puuid"] == puuid), None)
        if not acc:
            return await interaction.followup.send("❌ Esta cuenta ya no existe.", ephemeral=True)

        summoner = await get_summoner_by_puuid(acc["puuid"], acc["region"])
        if not summoner:
            return await interaction.followup.send(
                "⚠️ No se pudieron obtener los datos de esa cuenta ahora mismo. Inténtalo en unos minutos.",
                ephemeral=True
            )

        embed = apply_footer(build_account_embed(acc, summoner), interaction.guild)
        view = AccountActionsView(owner_id=self.owner_id, puuid=acc["puuid"], is_primary=acc["primary"])
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

class AccountSelectView(View):
    def __init__(self, owner_id: str, accounts: list):
        super().__init__(timeout=300)
        self.add_item(AccountSelect(owner_id, accounts))

class AccountActionsView(View):
    def __init__(self, owner_id, puuid, is_primary: bool):
        # timeout finito: esta vista es efímera (nace y muere con el mensaje
        # "Ver cuentas"), no la volvemos a registrar como persistente tras un
        # reinicio para evitar que quede enganchada a datos de otra instancia.
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.puuid = puuid
        if is_primary:
            self.primary.disabled = True
            self.primary.label = "Cuenta principal"
            self.primary.style = discord.ButtonStyle.secondary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("❌ No puedes usar estos botones.", ephemeral=True)
            return False
        return True

    def _find_account(self, accs):
        """Localiza la cuenta por puuid (no por posición), para no
        desincronizarse si otra cuenta fue eliminada mientras tanto."""
        return next((i for i, a in enumerate(accs) if a["puuid"] == self.puuid), None)

    @discord.ui.button(label="Marcar principal", style=discord.ButtonStyle.success, custom_id="account_primary")
    async def primary(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        data = load_data()
        accs = data.get(self.owner_id, [])
        idx = self._find_account(accs)
        if idx is None:
            return await interaction.followup.send(
                "❌ Esta cuenta ya no existe (puede que la hayas eliminado desde otro mensaje).",
                ephemeral=True
            )

        for a in accs:
            a["primary"] = False
        accs[idx]["primary"] = True
        save_data(data)
        acc = accs[idx]
        await apply_roles(interaction.user, acc["region"], acc["solo"], acc["flex"])

        summoner = await get_summoner_by_puuid(acc["puuid"], acc["region"])
        if not summoner:
            return await interaction.followup.send(
                f"✅ Has marcado **{acc['riot_id']}** como principal, pero no se pudieron "
                "obtener sus datos actuales de Riot (inténtalo de nuevo más tarde).",
                ephemeral=True
            )
        embed = apply_footer(build_account_embed(acc, summoner), interaction.guild)
        await interaction.followup.send(f"✅ Has marcado **{acc['riot_id']}** como tu cuenta principal",
                                        embed=embed, ephemeral=True)

    @discord.ui.button(label="Eliminar", style=discord.ButtonStyle.danger, custom_id="account_delete")
    async def delete(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        data = load_data()
        accs = data.get(self.owner_id, [])
        idx = self._find_account(accs)
        if idx is None:
            return await interaction.followup.send(
                "❌ Esta cuenta ya no existe (puede que ya la hubieras eliminado).",
                ephemeral=True
            )

        removed = accs.pop(idx)
        if accs:
            accs[0]["primary"] = True
            await apply_roles(interaction.user, accs[0]["region"], accs[0]["solo"], accs[0]["flex"])
        else:
            await clear_roles(interaction.user)
        save_data(data)
        await interaction.followup.send(f"🗑️ Cuenta **{removed['riot_id']}** eliminada.", ephemeral=True)

    @discord.ui.button(emoji="🗑️", style=discord.ButtonStyle.secondary, custom_id="account_close_msg")
    async def close_message(self, interaction, _):
        """Cierra este mensaje — el footer ya no promete un botón que no
        existe, ahora sí que hay uno de verdad."""
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass

# ------------------ LINK FLOW ------------------

class RegionDropdown(Select):
    def __init__(self, name, tag):
        self.name = name
        self.tag = tag
        options = [discord.SelectOption(label=r, value=r) for r in REGIONS.keys()]
        super().__init__(placeholder="Selecciona región", options=options)

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        region = self.values[0]
        acc = await validate_riot_id(self.name, self.tag, region)
        if not acc:
            return await interaction.followup.send("❌ Riot ID no válido.", ephemeral=True)

        PENDING_VERIFICATIONS[str(interaction.user.id)] = {"riot_id": f"{self.name}#{self.tag}", "puuid": acc["puuid"], "region": region}
        v_embed = apply_footer(verification_embed(self.name, self.tag), interaction.guild)
        await interaction.followup.send(embed=v_embed,
                                        view=VerifyIconView(str(interaction.user.id)), ephemeral=True)

class RegionView(View):
    def __init__(self, name, tag):
        super().__init__()
        self.add_item(RegionDropdown(name, tag))

class LinkModal(Modal):
    def __init__(self):
        super().__init__(title="Vincular cuenta LoL")

        self.name = TextInput(
            label="Nombre de invocador",
            placeholder="Ej: XOKAS THE KING",
            max_length=16
        )

        self.tag = TextInput(
            label="TAG",
            placeholder="KEKY",
            max_length=5
        )

        self.add_item(self.name)
        self.add_item(self.tag)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)

        name = self.name.value.strip()
        tag = self.tag.value.strip().upper()

        if "#" in name or "#" in tag:
            return await interaction.followup.send(
                "❌ No incluyas el carácter **#**.\n"
                "👉 Escríbelo separado: **Nombre** y **TAG**.",
                ephemeral=True
            )

        if not name or not tag:
            return await interaction.followup.send(
                "❌ Debes rellenar ambos campos.",
                ephemeral=True
            )

        await interaction.followup.send(
            "Selecciona la región:",
            view=RegionView(name, tag),
            ephemeral=True
        )

def unlinked_account_embed(guild: discord.Guild) -> discord.Embed:
    """Embed estándar de 'cuenta no vinculada', con botón para arreglarlo
    en el sitio. Reutilizado en todos los sitios donde una acción requiere
    cuenta y el usuario no tiene ninguna."""
    embed = discord.Embed(
        title="🔒 Cuenta no vinculada",
        description="Necesitas vincular una cuenta de League of Legends antes de utilizar esta función.",
        color=COLOR_GROUP_CLOSED
    )
    return apply_footer(embed, guild)

class VincularPromptView(View):
    """Botón que abre el modal de vinculación directamente, para acompañar
    a unlinked_account_embed()."""
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Vincular cuenta", emoji="🔗", style=discord.ButtonStyle.primary)
    async def link(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(LinkModal())

# ------------------ PANEL ------------------

class Panel(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Vincular cuenta", style=discord.ButtonStyle.primary, custom_id="panel_link")
    async def link(self, interaction, _):
        await interaction.response.send_modal(LinkModal())

    @discord.ui.button(label="Ver cuentas", style=discord.ButtonStyle.secondary, custom_id="panel_view_accounts")
    async def view_accounts(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        accounts = load_data().get(str(interaction.user.id), [])
        if not accounts:
            return await interaction.followup.send(
                embed=unlinked_account_embed(interaction.guild), view=VincularPromptView(), ephemeral=True
            )

        embed = discord.Embed(title="🎮 Mis cuentas", color=COLOR_ACCOUNT_INFO)
        for acc in accounts:
            marker = "⭐ Principal" if acc["primary"] else "Secundaria"
            embed.add_field(
                name=f"{acc['riot_id']} · {marker}",
                value=(
                    f"SoloQ {format_tier_display(acc['solo'])}\n"
                    f"FlexQ {format_tier_display(acc['flex'])}\n"
                    f"Región {acc['region']}"
                ),
                inline=True
            )
        apply_footer(embed, interaction.guild)

        view = AccountSelectView(str(interaction.user.id), accounts)
        await interaction.followup.send(
            "Elige una cuenta en el desplegable para marcarla como principal o eliminarla:",
            embed=embed, view=view, ephemeral=True
        )

    @discord.ui.button(label="Actualizar datos", style=discord.ButtonStyle.success, custom_id="panel_refresh")
    async def refresh(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        data = load_data()
        uid = str(interaction.user.id)
        if uid not in data:
            return await interaction.followup.send(
                embed=unlinked_account_embed(interaction.guild), view=VincularPromptView(), ephemeral=True
            )

        primary = next(a for a in data[uid] if a["primary"])
        solo, flex = await get_ranks(primary["puuid"], primary["region"])

        if solo != primary["solo"] or flex != primary["flex"]:
            old_solo = primary["solo"]
            primary["solo"] = solo
            primary["flex"] = flex
            save_data(data)
            await apply_roles(interaction.user, primary["region"], solo, flex)
            await announce_rank_up(interaction.user, old_solo, solo)

        embed = discord.Embed(
            title="✅ Datos actualizados",
            description="Tu cuenta ha sido actualizada correctamente.\nTus roles del servidor están sincronizados.",
            color=COLOR_GROUP_OPEN
        )
        embed.add_field(name="SoloQ", value=format_tier_display(solo), inline=True)
        embed.add_field(name="FlexQ", value=format_tier_display(flex), inline=True)
        embed.add_field(name="Región", value=primary["region"], inline=True)
        apply_footer(embed, interaction.guild)
        await interaction.followup.send(embed=embed, ephemeral=True)

# ------------------ PANEL DE BUSCAR PARTIDA (independiente) ------------------

class ModeSelect(discord.ui.Select):
    """Desplegable para elegir la modalidad antes de pulsar 'Buscar
    partida'. La elección se guarda en PENDING_MODE_SELECTION hasta que se
    pulse el botón (o se cambie de opinión eligiendo otra)."""
    def __init__(self):
        options = [
            discord.SelectOption(label=mode["label"], value=key)
            for key, mode in GAME_MODES.items()
        ]
        super().__init__(placeholder="Elige una modalidad…", options=options,
                         custom_id="panel_mode_select", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        mode_key = self.values[0]
        PENDING_MODE_SELECTION[uid] = mode_key
        mode = GAME_MODES.get(mode_key, GAME_MODES["DUOQ"])
        await interaction.response.send_message(
            f"✅ Modalidad seleccionada: **{mode['label']}**. Pulsa **Buscar partida** para publicarla.",
            ephemeral=True
        )

class SearchPanel(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ModeSelect())

    @discord.ui.button(label="Buscar partida", emoji="🔎", style=discord.ButtonStyle.primary, custom_id="panel_search_game")
    async def search_game(self, interaction, _):
        uid = str(interaction.user.id)
        mode_key = PENDING_MODE_SELECTION.get(uid, "DUOQ")
        primary = get_effective_primary(uid, interaction.user)

        if not primary:
            # Ni cuenta en nuestra DB ni rol de rango puesto por nadie: le
            # abrimos el modal de "Vincular cuenta" (send_modal debe ser la
            # PRIMERA respuesta a la interacción, así que esto va antes del
            # defer()). Marcamos la intención (con la modalidad elegida) para
            # que, al terminar de vincular, se le lance la búsqueda automática.
            SEARCH_INTENT[uid] = mode_key
            return await interaction.response.send_modal(LinkModal())

        await interaction.response.defer(ephemeral=True)
        await perform_search(interaction, mode_key, primary)

# ------------------ REFRESCO AUTOMÁTICO DE RANGOS ------------------

@tasks.loop(hours=12)
async def update_ranks_loop():
    print("🔄 Actualizando ranks de todos los usuarios...")
    data = load_data()
    changed = False

    for uid, accounts in data.items():
        primary_acc = next((a for a in accounts if a["primary"]), None)
        if not primary_acc:
            continue

        solo, flex = await get_ranks(primary_acc["puuid"], primary_acc["region"])
        if solo == primary_acc["solo"] and flex == primary_acc["flex"]:
            print(f"[RANKS] {uid} sin cambios")
            continue

        print(f"[RANKS] {uid}: {primary_acc['solo']}/{primary_acc['flex']} → {solo}/{flex}")
        old_solo = primary_acc["solo"]
        primary_acc["solo"] = solo
        primary_acc["flex"] = flex
        changed = True

        for guild in bot.guilds:
            member = guild.get_member(int(uid))
            if member:
                await apply_roles(member, primary_acc["region"], solo, flex)
                await announce_rank_up(member, old_solo, solo)

        await asyncio.sleep(0.5)

    if changed:
        save_data(data)
    print("✅ Ranks actualizados correctamente.")

# ------------------ FUNCION DEPLOY PANEL ------------------

async def deploy_panel():
    channel = bot.get_channel(PANEL_CHANNEL_ID)
    if not channel:
        print(f"❌ No se encontró el canal con ID {PANEL_CHANNEL_ID}")
        return
    await channel.purge(limit=5)
    embed = discord.Embed(
        title="🎮 Tu cuenta de League of Legends, conectada con la comunidad.",
        description=(
            "Vincula tu cuenta para obtener automáticamente tus rangos de SoloQ y FlexQ.\n\n"
            "**¿Qué puedes hacer?**\n"
            "🔗 Vincular tu cuenta\n"
            "👤 Gestionar tus cuentas\n"
            "🔄 Mantener tus rangos actualizados\n\n"
            "───────────────\n\n"
            "🔎 **¿Buscas gente para jugar?**\n"
            f"Dirígete a <#{SEARCH_PANEL_CHANNEL_ID}> para publicar tu búsqueda."
        ),
        color=COLOR_PANEL
    )
    embed.set_thumbnail(url="https://upload.wikimedia.org/wikipedia/en/7/77/League_of_Legends_Logo.png")
    apply_footer(embed, channel.guild)
    await channel.send(embed=embed, view=Panel())

async def deploy_search_panel():
    channel = bot.get_channel(SEARCH_PANEL_CHANNEL_ID)
    if not channel:
        print(f"❌ No se encontró el canal con ID {SEARCH_PANEL_CHANNEL_ID}")
        return
    await channel.purge(limit=5)
    embed = discord.Embed(
        title="🔎 Encuentra jugadores de tu nivel",
        description=(
            "Elige la modalidad en el desplegable y pulsa el botón — publicaremos "
            "automáticamente tu búsqueda en el canal correspondiente.\n\n"
            "🎮 **DuoQ, FlexQ, Normales, Clash, ARAM, Arena o Classic**\n"
            "🏆 **Rango:** según corresponda a la modalidad\n"
            "👥 **DuoQ:** también avisa a rangos cercanos\n\n"
            "Cuando encuentres grupo, tendrás un hilo privado para organizaros.\n\n"
            "⏱️ **Cooldown:** 10 minutos, independiente por cada modalidad"
        ),
        color=COLOR_PANEL
    )
    apply_footer(embed, channel.guild)
    await channel.send(embed=embed, view=SearchPanel())

# ------------------ NUEVO MIEMBRO ------------------

@bot.event
async def on_member_join(member: discord.Member):
    """Si el usuario que entra ya tiene cuenta(s) vinculada(s) en la DB
    (p.ej. porque venía de otro servidor), le reaplicamos los roles de su
    cuenta principal usando los datos ya guardados. No se recalcula el rango
    contra Riot ni se toca a nadie más: solo actúa sobre quien acaba de entrar.

    Si nunca ha vinculado nada, arrancamos el cronómetro de recordatorios de
    vinculación (24h/72h) usando su fecha real de entrada."""
    data = load_data()
    accounts = data.get(str(member.id))
    primary = next((a for a in accounts if a["primary"]), None) if accounts else None

    if primary:
        await apply_roles(member, primary["region"], primary["solo"], primary["flex"])
        print(f"[JOIN] {member} ya tenía cuenta vinculada ({primary['riot_id']}), roles reaplicados")
        return

    if not has_linked_before(str(member.id)):
        start_unlinked_tracking(str(member.id), member.joined_at.isoformat())

@bot.event
async def on_member_remove(member: discord.Member):
    """Limpieza: si se va del servidor, no tiene sentido seguir su
    seguimiento de recordatorios (y evita mandarle un MD a quien ya no
    compartimos servidor)."""
    stop_unlinked_tracking(str(member.id))

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Detecta cuando alguien recibe un rol de rango de SoloQ sin haber
    pasado por nuestro flujo — típicamente porque lo vinculó con Orianna
    Bot, que usa los mismos roles que nosotros. En ese caso: le quitamos
    'Sin vincular' (no puede tener ambos a la vez) y paramos sus
    recordatorios automáticos, porque claramente no es alguien que no sepa
    cómo funciona el sistema."""
    if before.roles == after.roles:
        return  # nada de roles cambió, no hay nada que comprobar

    after_ids = {r.id for r in after.roles}
    if UNLINKED_ROLE_ID not in after_ids:
        return  # ya no tiene "Sin vincular" (lo habrá gestionado nuestro propio flujo)

    if not any(role_id in after_ids for role_id in SOLO_ROLES.values()):
        return  # sigue sin ningún rol de rango, nada que arreglar

    unlinked_role = after.guild.get_role(UNLINKED_ROLE_ID)
    if unlinked_role:
        try:
            await after.remove_roles(unlinked_role)
        except discord.HTTPException:
            pass
    stop_unlinked_tracking(str(after.id))

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Si alguien sale de un canal de voz que resulta ser el de un grupo, y
    el canal se queda vacío, lo borramos — con un margen de gracia de 60s
    por si alguien se desconecta un momento y vuelve (corte de red, etc.),
    en vez de borrarlo al instante."""
    left_channel = before.channel
    if not left_channel or left_channel == after.channel:
        return  # no salió de ningún canal, o solo cambió de estado (mute, etc.)
    if len(left_channel.members) > 0:
        return  # todavía queda gente dentro, nada que hacer

    group = next((g for g in get_all_groups() if g.get("voice_channel_id") == str(left_channel.id)), None)
    if not group:
        return  # no es un canal de voz nuestro

    await asyncio.sleep(60)  # margen de gracia
    channel_again = bot.get_channel(left_channel.id)
    if not channel_again or len(channel_again.members) > 0:
        return  # alguien volvió a entrar durante el margen de gracia

    try:
        await channel_again.delete()
    except discord.HTTPException:
        pass
    clear_group_voice_channel(group["thread_id"])

    thread = bot.get_channel(int(group["thread_id"]))
    if not thread:
        try:
            thread = await bot.fetch_channel(int(group["thread_id"]))
        except discord.HTTPException:
            thread = None
    if thread:
        await update_group_embed(thread)  # quita el campo "Sala de voz" ya mismo, no en el próximo join/leave
        await refresh_group_buttons(thread, group)  # quita el botón "Voz" del mensaje

# ------------------ SINCRONIZACIÓN MANUAL ------------------

@bot.tree.command(
    name="sincronizar_roles",
    description="Aplica los roles guardados en la DB a quien le falten. No toca a quien ya los tenga correctos."
)
@app_commands.checks.has_permissions(administrator=True)
async def sincronizar_roles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    data = load_data()
    corregidos = 0
    ya_correctos = 0
    sin_cuenta = 0

    for member in interaction.guild.members:
        accounts = data.get(str(member.id))
        primary = next((a for a in accounts if a["primary"]), None) if accounts else None

        if not primary:
            sin_cuenta += 1
            continue

        cambio = await apply_roles(member, primary["region"], primary["solo"], primary["flex"])
        if cambio:
            corregidos += 1
        else:
            ya_correctos += 1

        await asyncio.sleep(0.3)  # evitar rate limit de Discord en servidores grandes

    await interaction.followup.send(
        "✅ **Sincronización completada**\n"
        f"🔧 Roles corregidos: **{corregidos}**\n"
        f"✔️ Ya estaban correctos (no tocados): **{ya_correctos}**\n"
        f"⏭️ Sin cuenta vinculada en la DB: **{sin_cuenta}**",
        ephemeral=True
    )

@sincronizar_roles.error
async def sincronizar_roles_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ Necesitas permisos de administrador para usar este comando.", ephemeral=True
        )
    else:
        raise error

# ------------------ INFO DE JUGADOR (/info, menú contextual) ------------------

PROFILE_CACHE_SECONDS = 600       # un perfil se reutiliza 10 min (cada consulta nueva cuesta ~8 llamadas)
INFO_COOLDOWN_SECONDS = 15        # entre consultas de la misma persona
INFO_API_BUDGET = 80              # si ya llevamos tantas llamadas en 2 min, pedimos esperar (límite real: 100)
PROFILE_CACHE = {}                # puuid -> (timestamp, datos)
INFO_COOLDOWNS = {}               # user_id -> timestamp de la última consulta
DDRAGON = {"version": None, "champs": {}, "loaded_at": 0}

QUEUE_NAMES = {
    420: "SoloQ", 440: "FlexQ", 400: "Normal", 430: "Normal", 490: "Normal",
    450: "ARAM", 1700: "Arena", 1710: "Arena", 900: "URF", 1900: "URF", 700: "Clash",
}

async def ensure_ddragon():
    """Carga (y refresca cada 12 h) la versión actual de Data Dragon y el
    mapa championId -> (nombre en español, id interno para el icono)."""
    if DDRAGON["champs"] and time.time() - DDRAGON["loaded_at"] < 43200:
        return
    try:
        async with http_session.get("https://ddragon.leagueoflegends.com/api/versions.json") as r:
            version = (await r.json())[0]
        async with http_session.get(
            f"https://ddragon.leagueoflegends.com/cdn/{version}/data/es_ES/champion.json"
        ) as r:
            champs = (await r.json())["data"]
        DDRAGON["version"] = version
        DDRAGON["champs"] = {int(c["key"]): (c["name"], c["id"]) for c in champs.values()}
        DDRAGON["loaded_at"] = time.time()
    except Exception as e:
        print(f"[INFO] No se pudo cargar Data Dragon: {type(e).__name__}: {e}")

def champ_name(champion_id: int) -> str:
    return DDRAGON["champs"].get(champion_id, (f"Campeón {champion_id}", None))[0]

def champ_icon_url(champion_id: int):
    entry = DDRAGON["champs"].get(champion_id)
    if not entry or not DDRAGON["version"]:
        return None
    return f"https://ddragon.leagueoflegends.com/cdn/{DDRAGON['version']}/img/champion/{entry[1]}.png"

async def fetch_profile(puuid: str, region: str):
    """Datos del perfil de una cuenta (ligas, top 3 maestría, últimas 5
    partidas), con caché. Cada campo que falle queda en None / lista vacía:
    el embed se muestra igualmente con lo que haya."""
    cached = PROFILE_CACHE.get(puuid)
    if cached and time.time() - cached[0] < PROFILE_CACHE_SECONDS:
        return cached[1]

    platform, routing, _ = REGIONS[region]
    await ensure_ddragon()

    leagues_task = riot_get(f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}")
    mastery_task = riot_get(
        f"https://{platform}.api.riotgames.com/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top?count=3"
    )
    ids_task = riot_get(
        f"https://{routing}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count=5"
    )
    leagues, mastery, match_ids = await asyncio.gather(leagues_task, mastery_task, ids_task)

    matches = []
    if match_ids:
        details = await asyncio.gather(*[
            riot_get(f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{mid}") for mid in match_ids
        ])
        for d in details:
            if not d:
                continue
            info = d.get("info", {})
            me = next((p for p in info.get("participants", []) if p.get("puuid") == puuid), None)
            if not me:
                continue
            duration = info.get("gameDuration", 0)
            if duration > 100000:  # algunas partidas antiguas lo dan en ms
                duration //= 1000
            matches.append({
                "champion": me.get("championName", "?"),
                "champion_id": me.get("championId"),
                "kills": me.get("kills", 0), "deaths": me.get("deaths", 0), "assists": me.get("assists", 0),
                "win": bool(me.get("win")),
                "remake": bool(me.get("gameEndedInEarlySurrender")) or duration < 300,
                "queue": QUEUE_NAMES.get(info.get("queueId"), "Partida"),
                "ended": (info.get("gameEndTimestamp") or 0) // 1000,
            })

    league_by_queue = {}
    for q in (leagues or []):
        league_by_queue[q.get("queueType")] = q

    data = {"leagues": league_by_queue, "mastery": mastery or [], "matches": matches,
            "league_ok": leagues is not None, "matches_ok": match_ids is not None, "assets": None}

    # Imágenes necesarias para dibujar la tarjeta (iconos, banner, emblema). Se
    # descargan una vez y se cachean; la tarjeta se dibuja al construir el embed.
    try:
        top = (mastery or [])[:3]
        champ_ids = [m["championId"] for m in top] + [g["champion_id"] for g in matches]
        solo_entry = league_by_queue.get("RANKED_SOLO_5x5")
        emblem_url = None
        if solo_entry:
            em = re.search(r":(\d+)>$", TIER_EMOJIS.get(solo_entry.get("tier"), ""))
            emblem_url = f"https://cdn.discordapp.com/emojis/{em.group(1)}.png?size=256" if em else None
        main_id = top[0]["championId"] if top else (matches[0]["champion_id"] if matches else None)
        results = await asyncio.gather(
            asyncio.gather(*[fetch_champ_icon(cid) for cid in champ_ids]),
            fetch_asset(banner_url(main_id)),
            fetch_asset(emblem_url),
        )
        icons, banner_bytes, emblem_bytes = results
        data["assets"] = {"icons": list(icons), "banner": banner_bytes, "emblem": emblem_bytes,
                          "n_mastery": len(top)}
    except Exception as e:
        print(f"[INFO] No se pudieron descargar las imágenes del perfil: {type(e).__name__}: {e}")
        data["assets"] = None
    # Solo cacheamos si lo esencial salió bien (para no "congelar" un fallo de 429 durante 10 min).
    if leagues is not None:
        PROFILE_CACHE[puuid] = (time.time(), data)
    return data

# ------------------ IMAGEN DEL PERFIL (iconos con filtro) ------------------

ICON_CACHE = {}  # (versión, championId) -> bytes del PNG

def _load_font(size: int):
    for path in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

async def fetch_champ_icon(champion_id):
    """Bytes del icono de un campeón (con caché en memoria), o None."""
    url = champ_icon_url(champion_id) if champion_id else None
    if not url:
        return None
    key = (DDRAGON["version"], champion_id)
    if key in ICON_CACHE:
        return ICON_CACHE[key]
    try:
        async with http_session.get(url) as r:
            if r.status != 200:
                return None
            data = await r.read()
        ICON_CACHE[key] = data
        return data
    except Exception as e:
        print(f"[INFO] No se pudo descargar el icono {champion_id}: {type(e).__name__}: {e}")
        return None

def _rounded(img, radius):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1], radius=radius, fill=255)
    out = img.copy()
    out.putalpha(mask)
    return out

def _icon(raw, size, tint=None, border=None):
    """Icono cuadrado con esquinas redondeadas; opcionalmente con un filtro
    de color encima (tint = (r, g, b)) y un borde del mismo color."""
    if raw:
        img = Image.open(io.BytesIO(raw)).convert("RGBA").resize((size, size), Image.LANCZOS)
    else:
        img = Image.new("RGBA", (size, size), (60, 63, 68, 255))
    if tint:
        overlay = Image.new("RGBA", (size, size), tint + (110,))
        img = Image.alpha_composite(img, overlay)
    img = _rounded(img, max(8, size // 8))
    if border:
        ImageDraw.Draw(img).rounded_rectangle(
            [0, 0, size - 1, size - 1], radius=max(8, size // 8), outline=border + (255,), width=4
        )
    return img

TIER_COLORS = {
    "UNRANKED": (160, 165, 175), "IRON": (130, 120, 115), "BRONZE": (176, 110, 70),
    "SILVER": (190, 200, 215), "GOLD": (240, 200, 80), "PLATINUM": (80, 200, 190),
    "EMERALD": (46, 204, 113), "DIAMOND": (110, 150, 255), "MASTER": (190, 100, 240),
    "GRANDMASTER": (235, 90, 90), "CHALLENGER": (110, 215, 255),
}

ASSET_CACHE = {}  # url -> bytes (banners y emblemas de rango)

async def fetch_asset(url):
    """Descarga (con caché en memoria) una imagen por URL; None si falla."""
    if not url:
        return None
    if url in ASSET_CACHE:
        return ASSET_CACHE[url]
    try:
        async with http_session.get(url) as r:
            if r.status != 200:
                return None
            data = await r.read()
        if len(ASSET_CACHE) > 400:  # tope para no crecer sin límite (avatares, banners…)
            ASSET_CACHE.clear()
        ASSET_CACHE[url] = data
        return data
    except Exception as e:
        print(f"[INFO] No se pudo descargar {url}: {type(e).__name__}: {e}")
        return None

def banner_url(champion_id):
    entry = DDRAGON["champs"].get(champion_id) if champion_id else None
    if not entry:
        return None
    return f"https://ddragon.leagueoflegends.com/cdn/img/champion/centered/{entry[1]}_0.jpg"

def _fit_text(draw, text, font, max_width):
    """Recorta con '…' si el texto no cabe en max_width."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"

def render_profile_card(card):
    """Tarjeta de perfil completa en un PNG: banner del campeón principal,
    emblema + Riot ID, bloques SoloQ/FlexQ con barra de winrate, top 3 de
    maestría (el 1.º más grande) y últimas partidas con filtro verde/rojo.
    `card` es un dict con todo lo necesario ya descargado (ver fetch_profile)."""
    W, H = 860, 560
    GREEN, RED, GREY = (46, 204, 113), (231, 76, 60), (149, 165, 166)
    BG, PANEL, MUTED, TEXT = (30, 31, 34), (43, 45, 49), (160, 165, 175), (235, 235, 238)
    f_title, f_big, f_mid = _load_font(32), _load_font(22), _load_font(18)
    f_lbl, f_sm, f_xs = _load_font(15), _load_font(14), _load_font(12)

    im = Image.new("RGBA", (W, H), BG + (255,))
    d = ImageDraw.Draw(im)

    # ---- Banner ----
    BANNER_H = 158
    banner = None
    if card.get("banner"):
        try:
            b = Image.open(io.BytesIO(card["banner"])).convert("RGBA")
            scale = W / b.width
            b = b.resize((W, max(BANNER_H, int(b.height * scale))), Image.LANCZOS)
            top = max(0, (b.height - BANNER_H) // 3)
            banner = b.crop((0, top, W, top + BANNER_H))
        except Exception:
            banner = None
    if banner is None:
        banner = Image.new("RGBA", (W, BANNER_H), (50, 52, 70, 255))
    # Degradado: muy oscuro a la izquierda (donde va el texto) y más suave a la derecha.
    shade = Image.new("RGBA", (W, BANNER_H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for x in range(W):
        a = int(235 - 150 * (x / W))
        sd.line([(x, 0), (x, BANNER_H)], fill=BG + (a,))
    banner = Image.alpha_composite(banner, shade)
    for y in range(BANNER_H - 28, BANNER_H):  # funde con el fondo por abajo
        a = int(255 * (y - (BANNER_H - 28)) / 28)
        ImageDraw.Draw(banner).line([(0, y), (W, y)], fill=BG + (a,))
    im.alpha_composite(banner, (0, 0))

    # ---- Emblema + nombre ----
    solo = card.get("solo")
    tier = (solo or {}).get("tier", "UNRANKED")
    tcol = TIER_COLORS.get(tier, TIER_COLORS["UNRANKED"])
    if card.get("emblem"):
        try:
            e = Image.open(io.BytesIO(card["emblem"])).convert("RGBA").resize((110, 110), Image.LANCZOS)
            im.alpha_composite(e, (30, 28))
        except Exception:
            card["emblem"] = None
    if not card.get("emblem"):
        d.ellipse([34, 36, 134, 136], fill=PANEL + (255,), outline=tcol + (255,), width=4)
        q = "?"
        d.text((84 - d.textlength(q, font=f_title) / 2, 66), q, font=f_title, fill=tcol + (255,))
    name = _fit_text(d, card.get("riot_id") or "Cuenta", f_title, W - 170 - 30)
    d.text((156, 40), name, font=f_title, fill=(255, 255, 255, 255))
    sub = f"{card.get('region', '?')}"
    if card.get("lane"):
        sub += f"  ·  {card['lane']}"
    d.text((158, 88), sub, font=f_mid, fill=(205, 205, 215, 255))

    # ---- Bloques SoloQ / FlexQ ----
    def queue_block(x, title, entry):
        d.rounded_rectangle([x, 168, x + 390, 285], radius=14, fill=PANEL + (255,))
        d.text((x + 18, 180), title, font=f_lbl, fill=MUTED + (255,))
        if not entry:
            d.text((x + 18, 208), "Sin clasificar", font=f_big, fill=(190, 192, 200, 255))
            return
        t = entry.get("tier", "UNRANKED")
        col = TIER_COLORS.get(t, TIER_COLORS["UNRANKED"])
        label = TIER_DISPLAY_ES.get(t, t)
        if t not in ("MASTER", "GRANDMASTER", "CHALLENGER"):
            label += f" {entry.get('rank', '')}"
        d.text((x + 18, 206), label, font=f_big, fill=col + (255,))
        lp = f"{entry.get('leaguePoints', 0)} LP"
        d.text((x + 372 - d.textlength(lp, font=f_mid), 208), lp, font=f_mid, fill=TEXT + (255,))
        wins, losses = entry.get("wins", 0), entry.get("losses", 0)
        total = wins + losses
        d.rounded_rectangle([x + 18, 246, x + 372, 260], radius=7, fill=(70, 72, 78, 255))
        if total:
            wr = wins / total
            d.rounded_rectangle([x + 18, 246, x + 18 + max(14, int(354 * wr)), 260], radius=7, fill=GREEN + (255,))
            txt = f"{round(wr * 100)}% WR  ·  {wins}V {losses}D"
        else:
            txt = "Sin partidas"
        d.text((x + 18, 265), txt, font=f_sm, fill=(200, 200, 210, 255))

    queue_block(30, "SOLOQ", solo)
    queue_block(440, "FLEXQ", card.get("flex"))

    # ---- Maestría ----
    d.text((30, 302), "CAMPEONES CON MÁS MAESTRÍA", font=f_lbl, fill=MUTED + (255,))
    sizes = [120, 84, 84]
    x = 30
    for i, (raw, name_txt, lvl_txt) in enumerate(card.get("mastery", [])[:3]):
        s = sizes[i]
        y = 326 + (sizes[0] - s)
        im.alpha_composite(_icon(raw, s, border=(241, 196, 15) if i == 0 else None), (x, y))
        ty = 326 + sizes[0] + 6
        d.text((x, ty), _fit_text(d, name_txt, f_sm, max(s, 100)), font=f_sm, fill=(255, 255, 255, 255))
        d.text((x, ty + 18), lvl_txt, font=f_xs, fill=MUTED + (255,))
        x += s + (22 if i == 0 else 18)
    if not card.get("mastery"):
        d.text((30, 340), "Sin datos de maestría", font=f_sm, fill=MUTED + (255,))

    # ---- Últimas partidas ----
    matches = card.get("matches", [])
    d.text((400, 302), f"ÚLTIMAS {len(matches) or 5} PARTIDAS", font=f_lbl, fill=MUTED + (255,))
    for i, m in enumerate(matches[:5]):
        x = 400 + i * 90
        col = GREEN if m["state"] == "win" else RED if m["state"] == "loss" else GREY
        im.alpha_composite(_icon(m["raw"], 78, tint=col, border=col), (x, 326))
        chip = m["queue"]
        cw = d.textlength(chip, font=f_xs) + 14
        cx = x + (78 - cw) / 2
        d.rounded_rectangle([cx, 380, cx + cw, 398], radius=8, fill=BG + (235,))
        d.text((cx + 7, 383), chip, font=f_xs, fill=(255, 255, 255, 255))
        d.text((x + 2, 408), m["kda"], font=f_sm, fill=TEXT + (255,))
        d.text((x + 2, 428), m["ago"], font=f_xs, fill=(150, 155, 165, 255))
    if not matches:
        d.text((400, 340), "Sin partidas recientes", font=f_sm, fill=MUTED + (255,))

    # ---- Resumen ----
    d.line([(30, 488), (W - 30, 488)], fill=(60, 62, 68, 255), width=2)
    if card.get("summary"):
        d.text((30, 504), _fit_text(d, card["summary"], f_sm, W - 60), font=f_sm, fill=(205, 205, 215, 255))

    # Esquinas redondeadas de toda la tarjeta
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=18, fill=255)
    im.putalpha(mask)

    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()

def format_league_entry(entry):
    """'<emoji> Oro II · 45 LP' + winrate, o 'Sin clasificar'."""
    if not entry:
        return "Sin clasificar"
    tier = entry.get("tier", "UNRANKED")
    tier_txt = format_tier_display(tier)
    if tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        head = f"{tier_txt} · {entry.get('leaguePoints', 0)} LP"
    else:
        head = f"{tier_txt} {entry.get('rank', '')} · {entry.get('leaguePoints', 0)} LP"
    wins, losses = entry.get("wins", 0), entry.get("losses", 0)
    total = wins + losses
    wr = f"{round(wins * 100 / total)}% WR · {wins}V {losses}D" if total else "Sin partidas"
    return f"{head}\n{wr}"

async def build_profile_embed(member, account, profile, guild):
    """Embed mínimo + tarjeta de perfil dibujada (imagen adjunta). Devuelve
    (embed, file). Si la imagen no se pudo generar, el embed lleva los datos
    básicos en texto para no quedarse vacío."""
    riot_id = account.get("riot_id") or "Cuenta sin nombre"
    solo = profile["leagues"].get("RANKED_SOLO_5x5")
    flex = profile["leagues"].get("RANKED_FLEX_SR")
    tier = solo["tier"] if solo else "UNRANKED"
    tcol = TIER_COLORS.get(tier, TIER_COLORS["UNRANKED"])

    embed = discord.Embed(title=f"🎮 {riot_id}", description=member.mention,
                          color=discord.Color.from_rgb(*tcol))
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    apply_footer(embed, guild)

    notes = []
    if not profile["league_ok"]:
        notes.append("⚠️ No se pudieron obtener los rangos ahora mismo.")
    if not profile["matches_ok"]:
        notes.append("⚠️ No se pudieron obtener las últimas partidas ahora mismo.")
    if notes:
        embed.description += "\n\n" + "\n".join(notes)

    assets = profile.get("assets")
    if not assets:
        embed.add_field(name="SoloQ", value=format_league_entry(solo), inline=True)
        embed.add_field(name="FlexQ", value=format_league_entry(flex), inline=True)
        return embed, None

    icons = assets["icons"]
    n_m = assets["n_mastery"]
    mastery = []
    for m, raw in zip(profile["mastery"][:3], icons[:n_m]):
        pts = m.get("championPoints", 0)
        pts_txt = f"{pts / 1000:.0f}k" if pts >= 10000 else f"{pts:,}".replace(",", ".")
        mastery.append((raw, champ_name(m["championId"]), f"M{m.get('championLevel', 0)} · {pts_txt} pts"))

    now = datetime.now(timezone.utc)
    matches, wins_streak, streak_open = [], 0, True
    kda_ratios, played = [], {}
    for g, raw in zip(profile["matches"], icons[n_m:]):
        state = "remake" if g["remake"] else "win" if g["win"] else "loss"
        ago = ""
        if g["ended"]:
            ago = humanize_delta(now - datetime.fromtimestamp(g["ended"], tz=timezone.utc))
        matches.append({"raw": raw, "state": state, "queue": g["queue"],
                        "kda": f"{g['kills']}/{g['deaths']}/{g['assists']}", "ago": ago})
        if state != "remake":
            if streak_open and state == "win":
                wins_streak += 1
            else:
                streak_open = False
            kda_ratios.append((g["kills"] + g["assists"]) / max(g["deaths"], 1))
        name = champ_name(g["champion_id"]) if g["champion_id"] else g["champion"]
        played[name] = played.get(name, 0) + 1

    parts = []
    if kda_ratios:
        parts.append(f"KDA medio: {sum(kda_ratios) / len(kda_ratios):.1f}".replace(".", ","))
    if played:
        top_name, top_n = max(played.items(), key=lambda kv: kv[1])
        if top_n > 1:
            parts.append(f"Más jugado: {top_name} ({top_n} de {len(profile['matches'])})")
    if wins_streak >= 2:
        parts.append(f"Racha: {wins_streak} victorias")

    lane_role = get_member_lane_role(member)
    card = {
        "riot_id": riot_id, "region": account.get("region", "?"),
        "lane": lane_role.name if lane_role else None,
        "solo": solo, "flex": flex, "emblem": assets["emblem"], "banner": assets["banner"],
        "mastery": mastery, "matches": matches, "summary": "   ·   ".join(parts),
    }
    try:
        png = await asyncio.to_thread(render_profile_card, card)
    except Exception as e:
        print(f"[INFO] No se pudo dibujar la tarjeta: {type(e).__name__}: {e}")
        embed.add_field(name="SoloQ", value=format_league_entry(solo), inline=True)
        embed.add_field(name="FlexQ", value=format_league_entry(flex), inline=True)
        return embed, None

    embed.set_image(url="attachment://perfil.png")
    return embed, discord.File(io.BytesIO(png), filename="perfil.png")

def build_roles_only_embed(member, derived, guild):
    """Para quien tiene roles de rango (p.ej. vinculado por Orianna Bot) pero
    no una cuenta nuestra: sin puuid no hay datos detallados."""
    embed = discord.Embed(
        title=f"🎮 {member.display_name}",
        description=f"{member.mention}\n\nNo tiene la cuenta vinculada con este bot, así que solo "
                    f"podemos mostrar sus rangos según sus roles.",
        color=COLOR_ACCOUNT_INFO
    )
    embed.add_field(name="SoloQ", value=format_tier_display(derived["solo"]), inline=True)
    embed.add_field(name="FlexQ", value=format_tier_display(derived["flex"]), inline=True)
    thumb = tier_thumbnail_url(derived["solo"])
    if thumb:
        embed.set_thumbnail(url=thumb)
    apply_footer(embed, guild)
    return embed

class InfoAccountSelect(Select):
    def __init__(self, member, accounts, selected_index, invoker_id):
        options = [
            discord.SelectOption(
                label=a["riot_id"][:100], value=str(i),
                description=f"{a['region']}" + (" · principal" if a.get("primary") else ""),
                default=(i == selected_index)
            ) for i, a in enumerate(accounts)
        ]
        super().__init__(placeholder="Cambiar de cuenta…", options=options, min_values=1, max_values=1)
        self.member, self.accounts, self.invoker_id = member, accounts, invoker_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.invoker_id:
            return await interaction.response.send_message(
                "Solo quien lanzó este comando puede cambiar de cuenta. Usa `/info` para ver otro perfil.",
                ephemeral=True
            )
        await interaction.response.defer()
        idx = int(self.values[0])
        acc = self.accounts[idx]
        if riot_calls_last_2min() >= INFO_API_BUDGET and acc["puuid"] not in PROFILE_CACHE:
            return await interaction.followup.send(
                "⏳ Hay muchas consultas ahora mismo. Prueba de nuevo en un minuto.", ephemeral=True
            )
        profile = await fetch_profile(acc["puuid"], acc["region"])
        embed, file = await build_profile_embed(self.member, acc, profile, interaction.guild)
        view = InfoView(self.member, self.accounts, idx, self.invoker_id)
        await interaction.edit_original_response(
            embed=embed, view=view, attachments=[file] if file else []
        )

class InfoView(View):
    def __init__(self, member, accounts, selected_index, invoker_id):
        super().__init__(timeout=300)
        self.message = None
        if len(accounts) > 1:
            self.add_item(InfoAccountSelect(member, accounts, selected_index, invoker_id))

    async def on_timeout(self):
        # El desplegable deja de funcionar a los 5 min: lo quitamos para no dejar un control muerto.
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

async def run_info(interaction: discord.Interaction, target):
    """Lógica común de /info y del menú contextual. Todas las comprobaciones
    que pueden fallar se responden en privado ANTES de responder en público."""
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Este comando solo funciona en el servidor.", ephemeral=True)
    if not isinstance(target, discord.Member):
        target = guild.get_member(target.id)
    if target is None or target.bot:
        return await interaction.response.send_message(
            "Elige a un miembro del servidor (no a un bot).", ephemeral=True
        )

    # Quien consulta debe tener su cuenta vinculada (igual que Buscar partida).
    if not get_effective_primary(str(interaction.user.id), interaction.user):
        return await interaction.response.send_message(
            embed=unlinked_account_embed(guild), view=VincularPromptView(), ephemeral=True
        )

    now = time.time()
    wait = INFO_COOLDOWN_SECONDS - (now - INFO_COOLDOWNS.get(interaction.user.id, 0))
    if wait > 0:
        return await interaction.response.send_message(
            f"⏳ Espera {int(wait) + 1} s antes de consultar otro perfil.", ephemeral=True
        )

    data = load_data()
    accounts = data.get(str(target.id)) or []
    if not accounts:
        derived = get_effective_primary(str(target.id), target)
        if derived:
            INFO_COOLDOWNS[interaction.user.id] = now
            return await interaction.response.send_message(embed=build_roles_only_embed(target, derived, guild))
        return await interaction.response.send_message(
            f"**{target.display_name}** todavía no ha vinculado su cuenta de League of Legends.", ephemeral=True
        )

    # Principal primero en el desplegable por defecto.
    primary_idx = next((i for i, a in enumerate(accounts) if a.get("primary")), 0)
    account = accounts[primary_idx]

    if account["puuid"] not in PROFILE_CACHE and riot_calls_last_2min() >= INFO_API_BUDGET:
        return await interaction.response.send_message(
            "⏳ Hay muchas consultas a Riot ahora mismo. Prueba de nuevo en un minuto.", ephemeral=True
        )

    INFO_COOLDOWNS[interaction.user.id] = now
    await interaction.response.defer()  # público
    try:
        profile = await fetch_profile(account["puuid"], account["region"])
        embed, file = await build_profile_embed(target, account, profile, guild)
    except Exception as e:
        print(f"[INFO] Error construyendo el perfil de {target}: {type(e).__name__}: {e}")
        INFO_COOLDOWNS.pop(interaction.user.id, None)
        return await send_error(interaction, f"No se pudo cargar el perfil ({type(e).__name__}).")

    view = InfoView(target, accounts, primary_idx, interaction.user.id)
    kwargs = {"embed": embed}
    if file:
        kwargs["file"] = file
    if len(accounts) > 1:
        kwargs["view"] = view
    message = await interaction.followup.send(**kwargs)
    view.message = message

@bot.tree.command(name="info", description="Perfil de LoL de un jugador: rangos, winrate, campeones y últimas partidas.")
@app_commands.describe(usuario="El jugador del que quieres ver el perfil")
@app_commands.guild_only()
async def info_command(interaction: discord.Interaction, usuario: discord.Member):
    await run_info(interaction, usuario)

@bot.tree.context_menu(name="Ver perfil LoL")
@app_commands.guild_only()
async def info_context_menu(interaction: discord.Interaction, member: discord.Member):
    await run_info(interaction, member)

# ------------------ COMPOSICIÓN DEL SERVIDOR ------------------

@bot.tree.command(
    name="servidor",
    description="Cuántos miembros hay vinculados/sin vincular, por rango de SoloQ y por rol principal."
)
async def servidor(interaction: discord.Interaction):
    await interaction.response.defer()

    guild = interaction.guild

    # ---------- MIEMBROS HUMANOS ----------
    miembros_humanos = [
        member for member in guild.members
        if not member.bot
    ]

    total_miembros = len(miembros_humanos)

    # ---------- VINCULACIÓN ----------
    unlinked_role = guild.get_role(UNLINKED_ROLE_ID)

    if unlinked_role:
        sin_vincular = sum(
            1 for member in unlinked_role.members
            if not member.bot
        )
    else:
        sin_vincular = 0

    # Los miembros con cuenta vinculada son los que tienen un rol de SoloQ.
    # Usamos IDs únicos para evitar contar dos veces al mismo usuario.
    vinculados_ids = set()

    for tier, role_id in SOLO_ROLES.items():
        role = guild.get_role(role_id)

        if role:
            vinculados_ids.update(
                member.id
                for member in role.members
                if not member.bot
            )

    vinculados = len(vinculados_ids)

    # ---------- PORCENTAJES ----------
    if total_miembros > 0:
        porcentaje_vinculados = round(
            (vinculados / total_miembros) * 100
        )
        porcentaje_sin_vincular = round(
            (sin_vincular / total_miembros) * 100
        )
    else:
        porcentaje_vinculados = 0
        porcentaje_sin_vincular = 0

    # ---------- SOLOQ ----------
    # De mayor a menor:
    # Aspirante → Gran Maestro → Maestro → Diamante → ...
    tier_order = list(SOLO_ROLES.keys())

    solo_counts = []

    for tier in tier_order:
        role = guild.get_role(SOLO_ROLES[tier])

        if not role:
            continue

        count = sum(
            1 for member in role.members
            if not member.bot
        )

        if count > 0:
            solo_counts.append((tier, count))

    solo_counts.sort(
        key=lambda item: tier_order.index(item[0]),
        reverse=True
    )

    # ---------- ROL PRINCIPAL ----------
    lane_counts = []

    for lane, role_id in LANE_ROLES.items():
        role = guild.get_role(role_id)

        if not role:
            continue

        count = sum(
            1 for member in role.members
            if not member.bot
        )

        if count > 0:
            emoji = LANE_EMOJIS.get(lane, "")
            label = f"{emoji} {role.name}".strip()
            lane_counts.append((label, count))

    # ---------- EMBED ----------
    embed = discord.Embed(
        title="📊 COMPOSICIÓN DEL SERVIDOR",
        description=f"👥 {total_miembros} miembros",
        color=COLOR_PANEL
    )

    # 🔗 Vinculación
    embed.add_field(
        name="🔗 Vinculación",
        value=(
            f"🟢 {vinculados} vinculados · {porcentaje_vinculados}%\n"
            f"⚪ {sin_vincular} pendientes · {porcentaje_sin_vincular}%"
        ),
        inline=False
    )

    # 🔸 SoloQ
    if solo_counts:
        solo_lines = "\n".join(
            f"{format_tier_display(tier)} · {count}"
            for tier, count in solo_counts
        )

        embed.add_field(
            name="🔸 SoloQ",
            value=solo_lines,
            inline=False
        )

    # 🔹 Posiciones
    if lane_counts:
        lane_lines = "\n".join(
            f"{label} · {count}"
            for label, count in lane_counts
        )

        embed.add_field(
            name="🔹 Posiciones",
            value=lane_lines,
            inline=False
        )

    apply_footer(embed, guild)
    await interaction.followup.send(
    embed=embed
)

# ------------------ JUGADORES ONLINE ------------------

@bot.tree.command(
    name="online",
    description="Quién está conectado ahora mismo, por rango de SoloQ y por rol principal."
)
async def online(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild

    # "Jugador online" = miembro humano, conectado (no offline/invisible) Y
    # con cuenta vinculada (tiene un rol de tier de SoloQ). Así el total y
    # los desgloses por rango/lane siempre cuadran entre sí.
    online_ids = set()
    tier_order = list(SOLO_ROLES.keys())
    solo_counts = []

    for tier in tier_order:
        role = guild.get_role(SOLO_ROLES[tier])
        if not role:
            continue
        count = 0
        for member in role.members:
            if member.bot or member.status == discord.Status.offline:
                continue
            online_ids.add(member.id)
            count += 1
        if count > 0:
            solo_counts.append((tier, count))
    solo_counts.sort(key=lambda item: tier_order.index(item[0]), reverse=True)

    lane_counts = []
    for lane, role_id in LANE_ROLES.items():
        role = guild.get_role(role_id)
        if not role:
            continue
        count = sum(1 for member in role.members if member.id in online_ids)
        if count > 0:
            emoji = LANE_EMOJIS.get(lane, "")
            label = f"{emoji} {role.name}".strip()
            lane_counts.append((label, count))

    total = len(online_ids)
    embed = discord.Embed(title="🟢 JUGADORES ONLINE", color=COLOR_GROUP_OPEN)
    apply_footer(embed, guild)

    if total == 0:
        embed.description = "No hay ningún jugador con cuenta vinculada conectado ahora mismo."
        return await interaction.followup.send(embed=embed)

    embed.description = f"👥 {total} jugadores conectados"

    solo_lines = "\n".join(
        f"{format_tier_display(tier)} · {count}" for tier, count in solo_counts
    )
    embed.add_field(name="🔸 SoloQ", value=solo_lines, inline=False)

    if lane_counts:
        lane_lines = "\n".join(f"{label} · {count}" for label, count in lane_counts)
        embed.add_field(name="🔹 Posiciones", value=lane_lines, inline=False)

    await interaction.followup.send(embed=embed)

# ------------------ LIMPIEZA DE GRUPOS CADUCADOS ------------------

async def delete_group_voice_channel(group: dict):
    """Borra el canal de voz vinculado a un grupo, si tiene uno. Se llama
    solo cuando el HILO desaparece de verdad (caducidad o cancelación) —
    nunca al 'Cerrar grupo' sin más, que deja el hilo vivo para quien ya
    está dentro."""
    voice_channel_id = group.get("voice_channel_id")
    if not voice_channel_id:
        return
    voice_channel = bot.get_channel(int(voice_channel_id))
    if not voice_channel:
        try:
            voice_channel = await bot.fetch_channel(int(voice_channel_id))
        except discord.HTTPException:
            return
    try:
        await voice_channel.delete()
    except discord.HTTPException:
        pass

async def close_group_silently(thread: discord.Thread, group: dict):
    """Igual que GroupView._lock_group pero sin necesitar una interacción —
    para el cierre automático por caducidad, antes de borrarlo. Si el
    borrado falla luego (p.ej. por permisos), al menos el grupo se queda
    visualmente cerrado en vez de "abierto para siempre"."""
    if not thread.locked:
        try:
            thread = await thread.edit(locked=True)
        except discord.HTTPException as e:
            print(f"[GRUPO] No se pudo bloquear el hilo {thread.id} al caducar: {e}")

    await update_group_embed(thread)

    channel = bot.get_channel(int(group["channel_id"]))
    if not channel:
        try:
            channel = await bot.fetch_channel(int(group["channel_id"]))
        except discord.HTTPException:
            channel = None
    if channel:
        try:
            message = await channel.fetch_message(int(group["message_id"]))
            await message.edit(view=None)
        except discord.HTTPException:
            pass

async def reregister_group_views():
    """Al arrancar: vuelve a registrar los botones de todos los grupos que
    seguían activos en la DB, para que sigan funcionando tras el reinicio.
    Si el grupo ya estaba cerrado antes de reiniciar, el mensaje ya se
    quedó sin botones al cerrarse — no hay nada que volver a registrar.
    Un fallo con UN grupo no debe impedir registrar el resto."""
    for group in get_all_groups():
        try:
            thread_id = int(group["thread_id"])

            thread = bot.get_channel(thread_id)
            if not thread:
                try:
                    thread = await bot.fetch_channel(thread_id)
                except discord.HTTPException:
                    thread = None

            if thread and thread.locked:
                continue

            voice_id = int(group["voice_channel_id"]) if group.get("voice_channel_id") else None
            guild_id = thread.guild.id if thread else None
            view = GroupView(thread_id, int(group["creator_id"]), voice_id, guild_id)
            bot.add_view(view)
        except Exception as e:
            print(f"[GRUPO] Error re-registrando el grupo {group.get('thread_id')}: {type(e).__name__}: {e}")

@tasks.loop(minutes=10)
async def cleanup_groups_loop():
    """Cada 10 min (no cada 30: con una ventana de aviso de 15 min, un
    intervalo de 30 podría saltársela por completo y borrar sin avisar).
    Cada grupo se procesa en su propio try/except: si uno falla (datos
    corruptos, permisos, lo que sea), no debe tirar abajo el resto de la
    pasada NI parar la tarea completa para siempre."""
    print("🧹 Comprobando grupos caducados...")
    now = discord.utils.utcnow()
    warn_threshold = timedelta(hours=GROUP_LIFETIME_HOURS) - timedelta(minutes=GROUP_WARNING_MINUTES)

    for group in get_all_groups():
        try:
            created_at = datetime.fromisoformat(group["created_at"])
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            elapsed = now - created_at

            # Canal de voz que nadie ha usado nunca (0 miembros) pasado el
            # tiempo de gracia: se borra aparte, independientemente de si el
            # grupo en sí ya toca cerrarse o no.
            if group.get("voice_channel_id") and elapsed >= timedelta(minutes=VOICE_EMPTY_TIMEOUT_MINUTES):
                voice_channel = bot.get_channel(int(group["voice_channel_id"]))
                if not voice_channel:
                    try:
                        voice_channel = await bot.fetch_channel(int(group["voice_channel_id"]))
                    except discord.HTTPException:
                        voice_channel = None
                if voice_channel and len(voice_channel.members) == 0:
                    try:
                        await voice_channel.delete()
                    except discord.HTTPException:
                        pass
                    clear_group_voice_channel(group["thread_id"])

                    sweep_thread = bot.get_channel(int(group["thread_id"]))
                    if not sweep_thread:
                        try:
                            sweep_thread = await bot.fetch_channel(int(group["thread_id"]))
                        except discord.HTTPException:
                            sweep_thread = None
                    if sweep_thread:
                        await update_group_embed(sweep_thread)  # quita el campo "Sala de voz" ya mismo
                        await refresh_group_buttons(sweep_thread, group)  # quita el botón "Voz" del mensaje

            if elapsed >= timedelta(hours=GROUP_LIFETIME_HOURS):
                thread = bot.get_channel(int(group["thread_id"]))
                if not thread:
                    try:
                        thread = await bot.fetch_channel(int(group["thread_id"]))
                    except discord.HTTPException:
                        thread = None

                if thread:
                    # close_group_silently ya deja el embed en "🔴 Grupo
                    # Cerrado" y sin botones — eso se queda en el canal como
                    # histórico. Solo se borran el HILO y el canal de voz
                    # vinculado, nunca el mensaje.
                    await close_group_silently(thread, group)
                    try:
                        await thread.delete()
                    except discord.HTTPException:
                        pass

                await delete_group_voice_channel(group)
                delete_group(group["thread_id"])

            elif elapsed >= warn_threshold and not group["warned"]:
                thread = bot.get_channel(int(group["thread_id"]))
                if not thread:
                    try:
                        thread = await bot.fetch_channel(int(group["thread_id"]))
                    except discord.HTTPException:
                        thread = None
                if thread:
                    try:
                        warn_embed = apply_footer(discord.Embed(
                            title="⏳ El grupo está a punto de cerrarse",
                            description=(
                                f"Este hilo se eliminará automáticamente en aproximadamente "
                                f"{GROUP_WARNING_MINUTES} minutos para evitar saturar el servidor.\n\n"
                                "Si vais a jugar, ¡es un buen momento para organizaros!"
                            ),
                            color=COLOR_VERIFICATION
                        ), thread.guild)
                        await thread.send(embed=warn_embed, allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        pass
                mark_group_warned(group["thread_id"])
        except Exception as e:
            print(f"[GRUPO] Error procesando el grupo {group.get('thread_id')} en la limpieza: "
                  f"{type(e).__name__}: {e}")

        await asyncio.sleep(0.3)

    print("✅ Grupos comprobados.")

@cleanup_groups_loop.error
async def cleanup_groups_loop_error(error: BaseException):
    """Red de seguridad final: si algo se escapa de los try/except de
    arriba, lo dejamos en el log en vez de dejar que discord.py pare la
    tarea para siempre en silencio."""
    print(f"[GRUPO] cleanup_groups_loop se cayó con un error no controlado: {type(error).__name__}: {error}")

# ------------------ GRUPOS ABIERTOS ------------------

@bot.tree.command(
    name="grupos",
    description="Grupos de búsqueda abiertos ahora mismo, en cualquier modalidad."
)
@app_commands.describe(modo="Filtra por una modalidad concreta (opcional)")
@app_commands.choices(modo=[
    app_commands.Choice(name=info["label"], value=key) for key, info in GAME_MODES.items()
])
async def grupos(interaction: discord.Interaction, modo: app_commands.Choice[str] = None):
    await interaction.response.defer(ephemeral=True)
    filtro = modo.value if modo else None
    guild = interaction.guild

    open_groups = []
    for group in get_all_groups():
        if filtro and group["mode"] != filtro:
            continue

        thread_id = int(group["thread_id"])
        thread = bot.get_channel(thread_id)
        if not thread:
            try:
                thread = await bot.fetch_channel(thread_id)
            except discord.HTTPException:
                continue
        if not thread or thread.locked:
            continue

        try:
            members = await thread.fetch_members()
        except discord.HTTPException:
            members = []
        count = sum(1 for m in members if guild.get_member(m.id) and not guild.get_member(m.id).bot)

        mode = GAME_MODES.get(group["mode"], GAME_MODES["DUOQ"])
        creator = guild.get_member(int(group["creator_id"]))
        creator_name = creator.display_name if creator else "alguien"
        jump_url = f"https://discord.com/channels/{guild.id}/{group['channel_id']}/{group['message_id']}"

        open_groups.append({
            "mode_label": mode["label"],
            "creator_name": creator_name,
            "count": count,
            "max": mode["max_members"],
            "url": jump_url,
            "created_at": group["created_at"],
        })

    titulo_modo = f" — {GAME_MODES[filtro]['label']}" if filtro else ""
    embed = discord.Embed(title=f"🔎 Grupos abiertos ahora mismo{titulo_modo}", color=COLOR_GROUP_OPEN)
    apply_footer(embed, guild)

    if not open_groups:
        if filtro:
            embed.description = (
                f"No hay ningún grupo de **{GAME_MODES[filtro]['label']}** abierto ahora mismo. "
                f"¡Publica el primero en <#{SEARCH_PANEL_CHANNEL_ID}>!"
            )
        else:
            embed.description = (
                f"No hay ningún grupo abierto ahora mismo. ¡Publica el primero en "
                f"<#{SEARCH_PANEL_CHANNEL_ID}>!"
            )
        return await interaction.followup.send(embed=embed, ephemeral=True)

    open_groups.sort(key=lambda g: g["created_at"], reverse=True)
    def _group_line(g):
        free = max(g["max"] - g["count"], 0)
        dots = " ".join("●" * min(g["count"], g["max"]) + "○" * free)
        falta = " · **¡Falta 1!**" if free == 1 else ""
        try:
            created = datetime.fromisoformat(g["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            cierra = f" · cierra <t:{int((created + timedelta(hours=GROUP_LIFETIME_HOURS)).timestamp())}:R>"
        except (ValueError, TypeError):
            cierra = ""
        return (f"🎮 **{g['mode_label']}** · {g['creator_name']}\n"
                f"`{dots}` {g['count']}/{g['max']}{falta}{cierra} · [Ver aviso]({g['url']})")

    embed.description = "\n\n".join(_group_line(g) for g in open_groups)

    await interaction.followup.send(embed=embed, ephemeral=True)

# ------------------ ÚLTIMOS EN LLEGAR ------------------

@bot.tree.command(
    name="ultimos",
    description="Últimos 5 miembros en unirse al servidor y si han vinculado cuenta."
)
@app_commands.checks.has_permissions(administrator=True)
async def ultimos(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    humanos = [m for m in guild.members if not m.bot and m.joined_at]
    humanos.sort(key=lambda m: m.joined_at, reverse=True)
    ultimos_miembros = humanos[:5]

    if not ultimos_miembros:
        return await interaction.followup.send("No hay miembros que mostrar.", ephemeral=True)

    now = discord.utils.utcnow()
    lines = []
    for member in ultimos_miembros:
        tiempo = f"<t:{int(member.joined_at.timestamp())}:R>"
        # Vinculado si NO tiene el rol "Sin vincular" — coherente con cómo
        # el resto del bot determina el estado de vinculación.
        vinculado = not any(r.id == UNLINKED_ROLE_ID for r in member.roles)
        estado = "🟢" if vinculado else "🔴"
        lines.append(f"{estado} {member.mention} — {tiempo}")

    embed = discord.Embed(
        title="🕐 ÚLTIMOS EN LLEGAR",
        description=f"Últimos {len(ultimos_miembros)} miembros · 🟢 vinculado · 🔴 sin vincular\n\n" + "\n".join(lines),
        color=COLOR_PANEL
    )
    apply_footer(embed, guild)

    await interaction.followup.send(embed=embed, ephemeral=True)

@ultimos.error
async def ultimos_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ Necesitas permisos de administrador para usar este comando.", ephemeral=True
        )
    else:
        raise error

# ------------------ RECORDATORIOS DE VINCULACIÓN ------------------

async def send_unlinked_reminder(member: discord.Member, guild: discord.Guild, stage: str,
                                  elapsed: timedelta, log_channel):
    """Manda el MD de recordatorio (24h o 72h) y registra el resultado en el
    canal de log. stage: '24h' o '72h'."""
    hours = int(elapsed.total_seconds() // 3600)
    minutes = int((elapsed.total_seconds() % 3600) // 60)
    tiempo_str = f"{hours}h {minutes}min"

    jump_url = f"https://discord.com/channels/{guild.id}/{PANEL_CHANNEL_ID}"
    link_view = View()
    link_view.add_item(discord.ui.Button(
        label="Ir a vincular mi cuenta", emoji="🔗",
        style=discord.ButtonStyle.link, url=jump_url
    ))

    if stage == "24h":
        embed = discord.Embed(
            title=f"🔗 Te falta un paso para completar tu entrada en {guild.name}",
            description=(
                "Hemos visto que todavía no has vinculado tu cuenta de League of Legends.\n\n"
                "Vincularla te da tus roles de rango y te permite usar **Buscar partida** "
                "para encontrar gente con la que jugar ahora mismo.\n\n"
                "📍 Puedes hacerlo en cualquier momento desde el canal de vinculación del servidor.\n\n"
                "*Mensaje automático · se envía una sola vez*"
            ),
            color=COLOR_VERIFICATION
        )
    else:
        embed = discord.Embed(
            title="🔗 ¿Sigues buscando gente con la que jugar?",
            description=(
                f"Todavía tienes pendiente vincular tu cuenta en **{guild.name}**.\n\n"
                "En cuanto la vincules, desbloqueas tus canales de rango y **Buscar partida** "
                "para encontrar grupo al instante.\n\n"
                "📍 Te esperamos en el canal de vinculación.\n\n"
                "*Último recordatorio automático · no recibirás más MD sobre esto*"
            ),
            color=0xE74C3C
        )
    apply_footer(embed, guild)

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    dm_ok = True
    fail_reason = ""
    try:
        await member.send(embed=embed, view=link_view)
    except discord.Forbidden:
        dm_ok = False
        fail_reason = "tiene los MDs cerrados"
    except discord.HTTPException as e:
        dm_ok = False
        fail_reason = f"error de Discord ({e.status})"

    if not log_channel:
        return

    estado = "✅ MD enviado correctamente" if dm_ok else f"❌ MD no entregado — {fail_reason}"
    if stage == "24h":
        emoji_titulo, etiqueta, color_log = "⚠️", "Recordatorio de vinculación enviado (24h)", 0xF1C40F
    else:
        emoji_titulo, etiqueta, color_log = "🔴", "Segundo recordatorio enviado (72h)", 0xE74C3C

    log_embed = discord.Embed(
        title=f"{emoji_titulo} {etiqueta}",
        description=(
            f"👤 {member.mention}\n"
            f"🕐 Lleva **{tiempo_str}** sin vincular\n"
            f"🔗 {estado}\n"
            f"📅 Entrada: {member.joined_at.strftime('%d/%m/%Y %H:%M')}"
        ),
        color=color_log
    )
    apply_footer(log_embed, guild)
    try:
        await log_channel.send(embed=log_embed)
    except discord.Forbidden:
        print("[RECORDATORIO] Sin permisos para escribir en el canal de log")

async def process_unlinked_member(member: discord.Member, guild: discord.Guild, record: dict,
                                   now: datetime, log_channel):
    since = datetime.fromisoformat(record["unlinked_since"])
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    elapsed = now - since
    uid = str(member.id)

    if elapsed >= timedelta(hours=72) and not record["reminder_72h_sent"]:
        if not record["reminder_24h_sent"]:
            # Se pasó de las 24h sin que lo pilláramos (p.ej. bot caído):
            # no mandamos ya el de 24h caducado, solo lo damos por hecho
            # y mandamos directamente el de 72h.
            mark_reminder_sent(uid, "24h")
        await send_unlinked_reminder(member, guild, "72h", elapsed, log_channel)
        mark_reminder_sent(uid, "72h")

    elif elapsed >= timedelta(hours=24) and not record["reminder_24h_sent"]:
        await send_unlinked_reminder(member, guild, "24h", elapsed, log_channel)
        mark_reminder_sent(uid, "24h")

async def backfill_unlinked_tracking():
    """Al arrancar: crea seguimiento para quien ya estaba en el servidor,
    nunca ha vinculado nada, y aún no tenía seguimiento (porque se unió
    antes de que existiera esta función). No toca a quien ya tiene fila —
    eso reiniciaría sus recordatorios ya enviados."""
    for guild in bot.guilds:
        role = guild.get_role(UNLINKED_ROLE_ID)
        if not role:
            continue
        for member in role.members:
            if member.bot:
                continue
            uid = str(member.id)
            if has_linked_before(uid) or get_unlinked_tracking(uid):
                continue
            start_unlinked_tracking(uid, member.joined_at.isoformat())

@tasks.loop(hours=3)
async def check_unlinked_reminders_loop():
    print("🔔 Comprobando recordatorios de vinculación...")
    log_channel = bot.get_channel(REMINDER_LOG_CHANNEL_ID)
    now = discord.utils.utcnow()

    for uid, record in get_all_unlinked_tracking().items():
        member = None
        member_guild = None
        for guild in bot.guilds:
            m = guild.get_member(int(uid))
            if m:
                member, member_guild = m, guild
                break

        if not member:
            stop_unlinked_tracking(uid)  # ya no comparte servidor con nosotros
            continue

        await process_unlinked_member(member, member_guild, record, now, log_channel)
        await asyncio.sleep(0.3)

    print("✅ Recordatorios comprobados.")

# ------------------ READY ------------------
@bot.event
async def on_ready():
    init_db()

    # ---------- VISTAS PERSISTENTES ----------
    # Solo el Panel necesita sobrevivir a reinicios: es un mensaje fijo en un
    # canal, sin estado por-usuario en sus custom_id. AccountActionsView es
    # efímera (mensajes "Ver cuentas" de vida corta) y lleva estado propio
    # (owner_id, puuid), así que NO se registra aquí para evitar que un
    # reinicio reenganche interacciones reales a una instancia con datos falsos.
    bot.add_view(Panel())
    bot.add_view(SearchPanel())

    # ---------- REGISTRO DE COMANDOS SLASH ----------
    # Copiamos los comandos globales a cada guild para que estén disponibles al instante.
    for guild in bot.guilds:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"🟢 [SYNC] {guild.name}: {[cmd.name for cmd in synced]}")

    await deploy_panel()
    await deploy_search_panel()

    if not update_ranks_loop.is_running():
        update_ranks_loop.start()

    # Backfill antes de arrancar el loop: crea seguimiento para quien ya
    # estaba sin vincular desde antes de que existiera esta función, usando
    # su fecha real de entrada. La primera pasada del loop (que se ejecuta
    # de inmediato al arrancar) ya les mandará el recordatorio si les toca.
    await backfill_unlinked_tracking()
    if not check_unlinked_reminders_loop.is_running():
        check_unlinked_reminders_loop.start()

    # ---------- GRUPOS DE BUSCAR PARTIDA ----------
    try:
        await reregister_group_views()
    except Exception as e:
        # Que un fallo aquí no impida arrancar la limpieza automática de abajo.
        print(f"[GRUPO] Error en reregister_group_views: {type(e).__name__}: {e}")
    if not cleanup_groups_loop.is_running():
        cleanup_groups_loop.start()

    print("🤖 Bot listo")
    
# ------------------ WEB SERVER ------------------

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot activo", 200

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))

# daemon=True: que este hilo no impida cerrar el proceso si el bot termina
threading.Thread(target=run_flask, daemon=True).start()

# ------------------ START ------------------

async def main():
    global http_session
    async with aiohttp.ClientSession() as session:
        http_session = session
        async with bot:
            await bot.start(TOKEN)

asyncio.run(main())
