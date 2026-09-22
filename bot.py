import discord
import aiohttp
import os
import re
import threading
import asyncio
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Modal, TextInput, Select
from flask import Flask

from config import *
from database import (
    load_data, save_data, init_db,
    has_linked_before, get_unlinked_tracking, get_all_unlinked_tracking,
    start_unlinked_tracking, stop_unlinked_tracking, mark_reminder_sent,
    create_group, get_group, get_all_groups, delete_group, mark_group_warned,
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

async def riot_get(url):
    headers = {"X-Riot-Token": RIOT_API_KEY}
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

def build_search_embed(creator: discord.abc.User, lane_display, rank_line, bottom_line, thumbnail_tier=None):
    embed = discord.Embed(title="🟢 Grupo Abierto", color=COLOR_GROUP_OPEN)
    embed.set_author(name=creator.display_name, icon_url=creator.display_avatar.url)

    parts = []
    if lane_display:
        parts.append(lane_display)
    if rank_line:
        parts.append(rank_line)
    parts.append(bottom_line)
    embed.description = "\n\n".join(parts)

    if thumbnail_tier:
        thumb = tier_thumbnail_url(thumbnail_tier)
        if thumb:
            embed.set_thumbnail(url=thumb)
    embed.timestamp = discord.utils.utcnow()  # Discord lo traduce solo a "hace X min" y se conserva al clonar
    return embed

async def update_group_embed(thread: discord.Thread):
    """Reconstruye el embed del grupo a partir del propio hilo (fuente de
    verdad): quién está dentro (thread.fetch_members()) y si está cerrado
    (thread.locked). No guardamos la lista de miembros en ningún sitio
    aparte — siempre se lee de Discord. El tier de SoloQ de cada jugador se
    deduce de sus roles actuales (igual que en get_effective_primary), así
    que funciona también con gente vinculada por Orianna Bot."""
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
    mode = GAME_MODES.get(group["mode"], GAME_MODES["DUOQ"])
    lines = []
    for tm in thread_members:
        member = guild.get_member(tm.id)
        if not member or member.bot:
            continue
        solo_tier = get_member_tier_from_roles(member, SOLO_ROLES) or "UNRANKED"
        solo_display = format_tier_display(solo_tier)
        marker = "🔸" if member.id == creator_id else "🔹"
        if mode["show_lane"]:
            lane_role = get_member_lane_role(member)
            lane_display = get_lane_display(lane_role) if lane_role else "—"
            lines.append(f"{marker} {member.display_name} · {lane_display} · SoloQ {solo_display}")
        else:
            lines.append(f"{marker} {member.display_name} · SoloQ {solo_display}")

    embed = discord.Embed.from_dict(message.embeds[0].to_dict()) if message.embeds else discord.Embed()
    contador = f"({len(lines)}/{mode['max_members']})"
    embed.title = f"🔴 Grupo Cerrado {contador}" if thread.locked else f"🟢 Grupo Abierto {contador}"
    embed.color = COLOR_GROUP_CLOSED if thread.locked else COLOR_GROUP_OPEN

    # Quitamos cualquier campo "JUGADORES" de una actualización anterior
    # para no duplicarlo (el resto del embed vive en la descripción, no en
    # campos, así que no hay nada más que conservar aparte de este campo).
    kept_fields = [f for f in embed.fields if f.name != "👥 JUGADORES"]
    embed.clear_fields()
    for f in kept_fields:
        embed.add_field(name=f.name, value=f.value, inline=f.inline)
    if lines:
        embed.add_field(name="👥 JUGADORES", value="\n".join(lines), inline=False)

    try:
        await message.edit(embed=embed)
    except discord.HTTPException:
        pass

class GroupView(View):
    """Vista dinámica del grupo: Unirse / Salir del grupo / Cerrar grupo.
    Los custom_id llevan el thread_id (y el creator_id en 'cerrar') dentro,
    así que no hace falta guardar nada aparte para que los botones sigan
    funcionando tras un reinicio — solo hay que volver a registrar esta
    vista al arrancar (ver reregister_group_views)."""

    def __init__(self, thread_id: int, creator_id: int):
        super().__init__(timeout=None)
        self.thread_id = thread_id
        self.creator_id = creator_id

        join_btn = discord.ui.Button(
            label="Unirse", emoji="✅", style=discord.ButtonStyle.success,
            custom_id=f"group_join:{thread_id}"
        )
        join_btn.callback = self.join
        self.add_item(join_btn)

        leave_btn = discord.ui.Button(
            label="Salir del grupo", emoji="🚪", style=discord.ButtonStyle.secondary,
            custom_id=f"group_leave:{thread_id}"
        )
        leave_btn.callback = self.leave
        self.add_item(leave_btn)

        close_btn = discord.ui.Button(
            label="Cerrar grupo", emoji="🔒", style=discord.ButtonStyle.danger,
            custom_id=f"group_close:{thread_id}:{creator_id}"
        )
        close_btn.callback = self.close_group
        self.add_item(close_btn)

        cancel_btn = discord.ui.Button(
            label="Cancelar búsqueda", emoji="🗑️", style=discord.ButtonStyle.secondary,
            custom_id=f"group_cancel:{thread_id}:{creator_id}"
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
                return await interaction.followup.send(
                    "❌ Necesitas tener una cuenta vinculada para unirte a un grupo.", ephemeral=True
                )

            thread = await self._get_thread(interaction)
            if not thread:
                return await interaction.followup.send("❌ Este grupo ya no existe.", ephemeral=True)
            if thread.locked:
                return await interaction.followup.send(
                    "🔒 Este grupo está cerrado, no admite más gente.", ephemeral=True
                )

            try:
                members = await thread.fetch_members()
            except discord.HTTPException:
                members = []
            if interaction.user.id in [m.id for m in members]:
                return await interaction.followup.send("Ya estás en este grupo.", ephemeral=True)

            if thread.archived:
                thread = await thread.edit(archived=False)  # capturamos el objeto actualizado, por si acaso
            await thread.add_user(interaction.user)

            try:
                members_after = await thread.fetch_members()
            except discord.HTTPException:
                members_after = []

            group_record = get_group(thread.id)
            mode = GAME_MODES.get(group_record["mode"], GAME_MODES["DUOQ"]) if group_record else GAME_MODES["DUOQ"]

            if len(members_after) >= mode["max_members"]:
                # Equipo completo: se cierra solo, misma lógica que "Cerrar grupo".
                if await self._lock_group(thread, interaction):
                    try:
                        full_embed = apply_footer(discord.Embed(
                            title="🔒 Grupo cerrado",
                            description="Este grupo ha llegado a su tamaño completo y se ha cerrado "
                                        "automáticamente. Ya no se admiten nuevos jugadores.",
                            color=COLOR_GROUP_CLOSED
                        ), thread.guild)
                        await thread.send(embed=full_embed, allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        pass
                await interaction.followup.send(
                    f"✅ Te has unido al grupo — ¡equipo completo! Habla con ellos en {thread.mention}.",
                    ephemeral=True
                )
            else:
                await update_group_embed(thread)
                await interaction.followup.send(
                    f"✅ Te has unido al grupo. Habla con ellos en {thread.mention}.", ephemeral=True
                )
        except Exception as e:
            print(f"[GRUPO] Error en 'Unirse': {type(e).__name__}: {e}")
            await send_error(interaction, f"Ha ocurrido un error inesperado ({type(e).__name__}).")

    async def leave(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            if interaction.user.id == self.creator_id:
                return await interaction.followup.send(
                    "Eres el creador del grupo — usa **Cerrar grupo** si quieres dejar de admitir gente.",
                    ephemeral=True
                )

            thread = await self._get_thread(interaction)
            if not thread:
                return await interaction.followup.send("❌ Este grupo ya no existe.", ephemeral=True)

            try:
                members = await thread.fetch_members()
            except discord.HTTPException:
                members = []
            if interaction.user.id not in [m.id for m in members]:
                return await interaction.followup.send("No estás en este grupo.", ephemeral=True)

            await thread.remove_user(interaction.user)
            await update_group_embed(thread)
            await interaction.followup.send("🚪 Has salido del grupo.", ephemeral=True)
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
                return await interaction.followup.send("Solo el creador del grupo puede cerrarlo.", ephemeral=True)

            thread = await self._get_thread(interaction)
            if not thread:
                return await interaction.followup.send("❌ Este grupo ya no existe.", ephemeral=True)
            if thread.locked:
                return await interaction.followup.send("Este grupo ya estaba cerrado.", ephemeral=True)

            if not await self._lock_group(thread, interaction):
                return  # _lock_group ya avisó del motivo del fallo
            await interaction.followup.send("🔒 Grupo cerrado. Ya no se admite gente nueva.", ephemeral=True)
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
                return await interaction.followup.send(
                    "Solo el creador de la búsqueda puede cancelarla.", ephemeral=True
                )

            thread = await self._get_thread(interaction)
            if thread:
                try:
                    await thread.delete()
                except discord.HTTPException:
                    pass

            if interaction.message:
                try:
                    await interaction.message.delete()
                except discord.HTTPException:
                    pass

            delete_group(str(self.thread_id))
            await interaction.followup.send(
                "🗑️ Búsqueda cancelada. Se ha eliminado el hilo y el aviso del canal.", ephemeral=True
            )
        except Exception as e:
            print(f"[GRUPO] Error en 'Cancelar búsqueda': {type(e).__name__}: {e}")
            await send_error(interaction, f"Ha ocurrido un error inesperado ({type(e).__name__}).")

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
    rank_line = None
    thumbnail_tier = None

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

        # Texto "Platino / Oro": el propio tier primero, luego el/los vecinos
        # más bajos (window_tiers está guardado de menor a mayor en config.py).
        window_text = " / ".join(TIER_DISPLAY_ES.get(t, t) for t in reversed(window_tiers))
        rank_line = f"{format_tier_display(solo_tier)} · {format_tier_display(flex_tier)} Flex"
        bottom_line = f"👥 Buscando jugadores de {window_text}"
        thumbnail_tier = solo_tier
    else:
        channel_id = mode.get("channel_id")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if not channel:
            return await send_error(
                interaction, f"El canal de **{mode['label']}** todavía no está configurado."
            )

        if mode["rank_source"] == "flex":
            rank_line = f"{format_tier_display(flex_tier)} FlexQ"
            thumbnail_tier = flex_tier
        elif mode["rank_source"] == "solo_ref":
            rank_line = f"{format_tier_display(solo_tier)} SoloQ (referencia)"
            thumbnail_tier = solo_tier
        # rank_source None: no se muestra ningún rango

        bottom_line = f"👥 Buscando jugadores para {mode['label']}"

    lane_display = get_lane_display(lane_role) if lane_role else None
    embed = build_search_embed(interaction.user, lane_display, rank_line, bottom_line, thumbnail_tier)
    apply_footer(embed, interaction.guild)

    mention_prefix = f"{' '.join(role_mentions)} " if role_mentions else ""
    try:
        sent_message = await channel.send(
            content=f"{mention_prefix}{interaction.user.mention} está buscando partida",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True, users=True)
        )
    except discord.Forbidden:
        return await send_error(interaction, f"No tengo permisos para escribir en {channel.mention}.")

    SEARCH_COOLDOWNS[cooldown_key] = now

    # Hilo privado para que el grupo se comunique sin MD, con sus botones de
    # Unirse / Salir / Cerrar grupo. Si por lo que sea falla la creación
    # (permisos, etc.), seguimos adelante sin grupo — el aviso ya se mandó.
    try:
        thread = await channel.create_thread(
            name=f"{mode['label']} de {interaction.user.display_name}",
            type=discord.ChannelType.private_thread,
            auto_archive_duration=GROUP_THREAD_ARCHIVE_MINUTES,
            invitable=False
        )
        await thread.add_user(interaction.user)

        create_group(thread.id, sent_message.id, channel.id, interaction.user.id,
                      discord.utils.utcnow().isoformat(), mode=mode_key)

        view = GroupView(thread.id, interaction.user.id)
        bot.add_view(view)
        await sent_message.edit(view=view)
        await update_group_embed(thread)  # título → "Grupo Abierto" + te añade a ti a la lista

        await thread.send(
            f"🔒 Este es tu hilo privado, {interaction.user.mention}. Habla aquí con quien se una "
            f"al grupo. Se eliminará automáticamente pasadas {GROUP_LIFETIME_HOURS}h."
        )
    except discord.HTTPException as e:
        print(f"[GRUPO] No se pudo crear el hilo para {interaction.user}: {e}")

    await interaction.followup.send(
        f"✅ Aviso de **{mode['label']}** publicado en {channel.mention}. ¡Suerte encontrando partida!",
        ephemeral=True
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
            primary["solo"] = solo
            primary["flex"] = flex
            save_data(data)
            await apply_roles(interaction.user, primary["region"], solo, flex)

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
        primary_acc["solo"] = solo
        primary_acc["flex"] = flex
        changed = True

        for guild in bot.guilds:
            member = guild.get_member(int(uid))
            if member:
                await apply_roles(member, primary_acc["region"], solo, flex)

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
        color=0x5865F2
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
    embed = discord.Embed(title="🟢 JUGADORES ONLINE", color=0x57F287)
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

            view = GroupView(thread_id, int(group["creator_id"]))
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
                    # histórico. Solo se borra el HILO, nunca el mensaje.
                    await close_group_silently(thread, group)
                    try:
                        await thread.delete()
                    except discord.HTTPException:
                        pass

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
                            color=COLOR_GROUP_CLOSED
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
async def grupos(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    open_groups = []
    for group in get_all_groups():
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

    embed = discord.Embed(title="🔎 Grupos abiertos ahora mismo", color=COLOR_GROUP_OPEN)
    apply_footer(embed, guild)

    if not open_groups:
        embed.description = (
            f"No hay ningún grupo abierto ahora mismo. ¡Publica el primero en "
            f"<#{SEARCH_PANEL_CHANNEL_ID}>!"
        )
        return await interaction.followup.send(embed=embed, ephemeral=True)

    open_groups.sort(key=lambda g: g["created_at"], reverse=True)
    embed.description = "\n".join(
        f"🎮 **{g['mode_label']}** · {g['creator_name']} · {g['count']}/{g['max']} · [Ver mensaje]({g['url']})"
        for g in open_groups
    )

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
        tiempo = humanize_delta(now - member.joined_at)
        # Vinculado si NO tiene el rol "Sin vincular" — coherente con cómo
        # el resto del bot determina el estado de vinculación.
        vinculado = not any(r.id == UNLINKED_ROLE_ID for r in member.roles)
        estado = "🟢" if vinculado else "🔴"
        lines.append(f"{estado} {member.mention} — {tiempo}")

    embed = discord.Embed(
        title="🕐 ÚLTIMOS EN LLEGAR",
        description=f"Últimos {len(ultimos_miembros)} miembros\n\n" + "\n".join(lines),
        color=0x5865F2
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
            color=0xF1C40F
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
